#!/usr/bin/env python3
"""
Local daily arXiv astrophysics paper recommender.

Fetches recent astro-ph papers from the arXiv API, stores their metadata in a
local SQLite database, ranks them against a personal research profile using
local embeddings and keywords, writes a Markdown digest, and (optionally)
emails that digest to you.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import feedparser
import numpy as np
import requests
from sentence_transformers import SentenceTransformer

import email_utils

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DIGEST_DIR = BASE_DIR / "digests"
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = DATA_DIR / "papers.sqlite"

ARXIV_API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "ai-paper-bot/0.1 (personal astrophysics literature digest)"


@dataclass
class Paper:
    arxiv_id: str
    version: int
    title: str
    authors: str
    abstract: str
    categories: str
    primary_category: str
    submitted_at: str
    updated_at: str
    arxiv_url: str
    pdf_url: str
    semantic_score: float = 0.0
    keyword_score: float = 0.0
    category_score: float = 0.0
    total_score: float = 0.0
    matched_terms: str = ""


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CONFIG_PATH}. Create it using the example configuration."
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def init_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            title TEXT NOT NULL,
            authors TEXT NOT NULL,
            abstract TEXT NOT NULL,
            categories TEXT NOT NULL,
            primary_category TEXT,
            submitted_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            arxiv_url TEXT NOT NULL,
            pdf_url TEXT NOT NULL,
            semantic_score REAL DEFAULT 0.0,
            keyword_score REAL DEFAULT 0.0,
            category_score REAL DEFAULT 0.0,
            total_score REAL DEFAULT 0.0,
            matched_terms TEXT DEFAULT '',
            fetched_at TEXT NOT NULL,
            status TEXT DEFAULT 'unread'
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            arxiv_id TEXT NOT NULL,
            label TEXT NOT NULL CHECK(label IN (
                'saved', 'relevant', 'not_relevant', 'read'
            )),
            created_at TEXT NOT NULL,
            PRIMARY KEY (arxiv_id, label),
            FOREIGN KEY (arxiv_id) REFERENCES papers(arxiv_id)
        )
        """
    )

    connection.commit()


def build_query(categories: list[str]) -> str:
    category_clause = " OR ".join(f"cat:{category}" for category in categories)
    return f"({category_clause})"


def extract_arxiv_id(url: str) -> tuple[str, int]:
    """
    Example:
        http://arxiv.org/abs/2401.01234v2
        -> ('2401.01234', 2)
    """
    match = re.search(r"/abs/([^v]+)(?:v(\d+))?$", url)
    if not match:
        raise ValueError(f"Could not parse arXiv ID from URL: {url}")

    arxiv_id = match.group(1)
    version = int(match.group(2) or 1)
    return arxiv_id, version


ARXIV_PAGE_SIZE = 100
ARXIV_MIN_DELAY = 3.5
ARXIV_MAX_RETRIES = 5


def arxiv_request(params: dict) -> requests.Response:
    """
    Make one rate-limited arXiv API request.

    arXiv requests should be spaced by at least three seconds. This function
    also handles temporary 429 and 5xx responses with exponential backoff.
    """
    for attempt in range(ARXIV_MAX_RETRIES):
        if attempt > 0:
            delay = min(60.0, ARXIV_MIN_DELAY * (2 ** attempt))
            print(f"Waiting {delay:.1f} seconds before retry...")
            time.sleep(delay)

        try:
            response = requests.get(
                ARXIV_API_URL,
                params=params,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/atom+xml",
                },
                timeout=90,
            )

            if response.status_code == 200:
                return response

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(float(retry_after), ARXIV_MIN_DELAY)
                    print(
                        f"arXiv rate limit reached. "
                        f"Retry-After: {delay:.0f} seconds."
                    )
                    time.sleep(delay)
                else:
                    print("arXiv rate limit reached; backing off.")
                continue

            if response.status_code in {500, 502, 503, 504}:
                print(f"Temporary arXiv server error: HTTP {response.status_code}")
                continue

            response.raise_for_status()

        except requests.RequestException as exc:
            if attempt == ARXIV_MAX_RETRIES - 1:
                raise RuntimeError(
                    f"arXiv request failed after {ARXIV_MAX_RETRIES} attempts"
                ) from exc

            print(f"Request failed: {exc}")

    raise RuntimeError("Could not retrieve data from arXiv after retries.")


def fetch_arxiv_papers(
    categories: list[str],
    lookback_days: int,
    max_results: int,
) -> list[Paper]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    search_query = build_query(categories)

    papers: list[Paper] = []
    start = 0
    page_number = 0

    while start < max_results:
        page_number += 1
        page_size = min(ARXIV_PAGE_SIZE, max_results - start)

        params = {
            "search_query": search_query,
            "start": start,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }

        print(
            f"Requesting arXiv page {page_number}: "
            f"start={start}, max_results={page_size}"
        )

        response = arxiv_request(params)
        feed = feedparser.parse(response.content)

        if getattr(feed, "bozo", False):
            raise RuntimeError(
                f"Could not parse arXiv API response: {feed.bozo_exception}"
            )

        if not feed.entries:
            break

        reached_cutoff = False

        for entry in feed.entries:
            published = datetime.fromisoformat(
                entry.published.replace("Z", "+00:00")
            )

            if published < cutoff:
                reached_cutoff = True
                break

            arxiv_id, version = extract_arxiv_id(entry.id)

            links = {link.get("type"): link.href for link in entry.links}
            pdf_url = links.get(
                "application/pdf",
                f"https://arxiv.org/pdf/{arxiv_id}",
            )

            tags = [tag.term for tag in entry.tags]

            primary_category_info = getattr(
                entry,
                "arxiv_primary_category",
                {},
            )

            primary_category = getattr(
                primary_category_info,
                "term",
                tags[0] if tags else "",
            )

            papers.append(
                Paper(
                    arxiv_id=arxiv_id,
                    version=version,
                    title=clean_text(entry.title),
                    authors=", ".join(
                        author.name for author in entry.authors
                    ),
                    abstract=clean_text(entry.summary),
                    categories=", ".join(tags),
                    primary_category=primary_category,
                    submitted_at=entry.published,
                    updated_at=entry.updated,
                    arxiv_url=f"https://arxiv.org/abs/{arxiv_id}",
                    pdf_url=pdf_url,
                )
            )

        if reached_cutoff:
            break

        start += len(feed.entries)

        if len(feed.entries) < page_size:
            break

        # Required pacing between successful requests.
        time.sleep(ARXIV_MIN_DELAY)

    return papers


def paper_exists(connection: sqlite3.Connection, arxiv_id: str, version: int) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM papers
        WHERE arxiv_id = ? AND version >= ?
        """,
        (arxiv_id, version),
    ).fetchone()
    return row is not None


def keyword_features(paper: Paper, config: dict) -> tuple[float, str]:
    text = f"{paper.title} {paper.abstract}".lower()
    keywords = config["keywords"]

    strong = [term for term in keywords["strong"] if term.lower() in text]
    weak = [term for term in keywords["weak"] if term.lower() in text]
    negative = [term for term in keywords["negative"] if term.lower() in text]

    score = 0.12 * len(strong) + 0.04 * len(weak) - 0.10 * len(negative)
    score = float(np.clip(score, -0.30, 0.55))

    matched = []
    if strong:
        matched.extend(strong)
    if weak:
        matched.extend(weak)
    if negative:
        matched.extend(f"low-priority: {term}" for term in negative)

    return score, ", ".join(matched[:10])


def category_feature(paper: Paper, config: dict) -> float:
    preferred = set(config["categories"])
    paper_categories = set(item.strip() for item in paper.categories.split(","))

    if paper.primary_category in preferred:
        return 1.0
    if paper_categories & preferred:
        return 0.6
    return 0.0


def rank_papers(papers: list[Paper], config: dict) -> list[Paper]:
    if not papers:
        return []

    print("Loading local embedding model...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    profile = config["profile"]
    paper_texts = [
        (
            f"Title: {paper.title}\n"
            f"Abstract: {paper.abstract}\n"
            f"Categories: {paper.categories}"
        )
        for paper in papers
    ]

    embeddings = model.encode(
        [profile] + paper_texts,
        normalize_embeddings=True,
        show_progress_bar=True,
    )

    profile_embedding = embeddings[0]
    paper_embeddings = embeddings[1:]

    for paper, embedding in zip(papers, paper_embeddings):
        paper.semantic_score = float(np.dot(profile_embedding, embedding))
        paper.keyword_score, paper.matched_terms = keyword_features(paper, config)
        paper.category_score = category_feature(paper, config)

        paper.total_score = (
            0.68 * paper.semantic_score
            + 0.22 * paper.keyword_score
            + 0.10 * paper.category_score
        )

    return sorted(papers, key=lambda paper: paper.total_score, reverse=True)


def save_papers(connection: sqlite3.Connection, papers: Iterable[Paper]) -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    saved_count = 0

    for paper in papers:
        if paper_exists(connection, paper.arxiv_id, paper.version):
            continue

        connection.execute(
            """
            INSERT INTO papers (
                arxiv_id, version, title, authors, abstract, categories,
                primary_category, submitted_at, updated_at, arxiv_url, pdf_url,
                semantic_score, keyword_score, category_score, total_score,
                matched_terms, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper.arxiv_id,
                paper.version,
                paper.title,
                paper.authors,
                paper.abstract,
                paper.categories,
                paper.primary_category,
                paper.submitted_at,
                paper.updated_at,
                paper.arxiv_url,
                paper.pdf_url,
                paper.semantic_score,
                paper.keyword_score,
                paper.category_score,
                paper.total_score,
                paper.matched_terms,
                fetched_at,
            ),
        )
        saved_count += 1

    connection.commit()
    return saved_count


def short_abstract(text: str, max_length: int = 420) -> str:
    if len(text) <= max_length:
        return text

    shortened = text[:max_length].rsplit(" ", 1)[0].rstrip()
    return f"{shortened}…"


def write_digest(
    papers: list[Paper],
    config: dict,
    output_path: Path,
    fetched_count: int,
    saved_count: int,
) -> None:
    minimum_score = config["minimum_score"]
    top_n = config["top_n"]

    selected = [
        paper for paper in papers if paper.total_score >= minimum_score
    ][:top_n]

    lines = [
        f"# Astro-ph paper digest — {datetime.now().date().isoformat()}",
        "",
        (
            f"Fetched **{fetched_count}** recent astro-ph records; "
            f"stored **{saved_count}** previously unseen paper versions."
        ),
        "",
        (
            "Ranking combines local semantic similarity to your research profile, "
            "keyword matches, and category preference. Scores are personal ranking "
            "signals, not measures of paper quality."
        ),
        "",
    ]

    if not selected:
        lines.extend(
            [
                "## No high-confidence matches",
                "",
                "No paper exceeded the current relevance threshold. Review the "
                "database or lower `minimum_score` in `config.json` if this seems "
                "too restrictive.",
                "",
            ]
        )
    else:
        lines.extend(["## Recommended reading", ""])

        for rank, paper in enumerate(selected, start=1):
            lines.extend(
                [
                    f"### {rank}. {paper.title}",
                    "",
                    (
                        f"**Score:** {paper.total_score:.3f} "
                        f"— semantic {paper.semantic_score:.3f}, "
                        f"keyword {paper.keyword_score:.3f}, "
                        f"category {paper.category_score:.1f}"
                    ),
                    "",
                    f"**Category:** `{paper.primary_category}`  ",
                    f"**Authors:** {paper.authors}  ",
                    f"**Submitted:** {paper.submitted_at}  ",
                    f"**Matched concepts:** {paper.matched_terms or 'semantic match only'}",
                    "",
                    short_abstract(paper.abstract),
                    "",
                    f"[Abstract]({paper.arxiv_url}) · [PDF]({paper.pdf_url})",
                    "",
                ]
            )

    lines.extend(
        [
            "## Next actions",
            "",
            "- Open the abstracts/PDFs for the top-ranked papers.",
            "- Adjust `config.json` whenever a topic is over- or under-selected.",
            "- In the next version, add saved/not-relevant feedback to improve ranking.",
            "",
        ]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a local daily arXiv astrophysics paper digest."
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Override the lookback period from config.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and rank papers without writing to SQLite.",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip sending the digest by email even if enabled in config.json.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    lookback_days = args.lookback_days or config["lookback_days"]
    max_results = config["max_results"]

    DATA_DIR.mkdir(exist_ok=True)
    DIGEST_DIR.mkdir(exist_ok=True)

    with sqlite3.connect(DB_PATH) as connection:
        init_database(connection)

        print(
            f"Fetching astro-ph papers from the last {lookback_days} day(s) "
            f"across: {', '.join(config['categories'])}"
        )

        papers = fetch_arxiv_papers(
            categories=config["categories"],
            lookback_days=lookback_days,
            max_results=max_results,
        )

        print(f"Fetched {len(papers)} papers from arXiv.")
        ranked_papers = rank_papers(papers, config)

        if args.dry_run:
            saved_count = 0
            print("Dry run: not writing papers to SQLite.")
        else:
            saved_count = save_papers(connection, ranked_papers)

        digest_path = DIGEST_DIR / f"{datetime.now().date().isoformat()}.md"
        write_digest(
            papers=ranked_papers,
            config=config,
            output_path=digest_path,
            fetched_count=len(papers),
            saved_count=saved_count,
        )

    print(f"Digest written to: {digest_path}")
    print(f"SQLite database: {DB_PATH}")

    if not args.no_email:
        email_utils.send_digest_email(digest_path, config)


if __name__ == "__main__":
    main()