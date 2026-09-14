"""
Email delivery for the daily arXiv digest.

Reads SMTP credentials from a local .env file (never committed to git) and
sends the generated Markdown digest as both plain text and a simple HTML
rendering, using Python's standard smtplib. No third-party mail service or
API key is required beyond your own mail provider's SMTP + an app password.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _markdown_to_basic_html(markdown_text: str) -> str:
    """
    Very small, dependency-free Markdown -> HTML conversion covering just
    the subset write_digest() produces (#, ##, ###, **bold**, [text](url),
    blank-line paragraphs, "- " bullets). Good enough for an email body;
    not a general Markdown renderer.
    """
    import re

    html_lines: list[str] = []
    in_list = False

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()

        if line.startswith("- "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{line[2:]}</li>")
            continue
        elif in_list:
            html_lines.append("</ul>")
            in_list = False

        if line.startswith("### "):
            html_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            html_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            html_lines.append(f"<h1>{line[2:]}</h1>")
        elif line.strip() == "":
            html_lines.append("<br>")
        else:
            html_lines.append(f"<p>{line}</p>")

    if in_list:
        html_lines.append("</ul>")

    html = "\n".join(html_lines)

    # Bold: **text**
    html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html)
    # Links: [text](url)
    html = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', html)

    return f"<html><body style='font-family: sans-serif;'>{html}</body></html>"


def send_digest_email(digest_path: Path, config: dict) -> None:
    """
    Send the digest file at digest_path as an email, using credentials from
    .env. Silently skips sending (with a printed warning) if the .env file
    or required variables are missing, so a missing config never crashes
    the daily run.
    """
    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        print("Email sending disabled in config.json (email.enabled=false); skipping.")
        return

    if not ENV_PATH.exists():
        print(
            f"Warning: {ENV_PATH} not found. Copy .env.example to .env and fill "
            "in your SMTP details to enable email delivery. Skipping email."
        )
        return

    load_dotenv(ENV_PATH)

    required_vars = [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "EMAIL_FROM",
        "EMAIL_TO",
    ]
    values = {name: os.environ.get(name) for name in required_vars}
    missing = [name for name, value in values.items() if not value]
    if missing:
        print(f"Warning: missing env vars {missing}; skipping email.")
        return

    digest_text = digest_path.read_text(encoding="utf-8")
    subject_prefix = email_config.get("subject_prefix", "Astro-ph digest")
    subject = f"{subject_prefix} — {digest_path.stem}"

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = values["EMAIL_FROM"]
    message["To"] = values["EMAIL_TO"]

    message.attach(MIMEText(digest_text, "plain"))
    message.attach(MIMEText(_markdown_to_basic_html(digest_text), "html"))

    smtp_port = int(values["SMTP_PORT"])

    with smtplib.SMTP(values["SMTP_HOST"], smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(values["SMTP_USERNAME"], values["SMTP_PASSWORD"])
        server.sendmail(values["EMAIL_FROM"], [values["EMAIL_TO"]], message.as_string())

    print(f"Digest emailed to {values['EMAIL_TO']}.")