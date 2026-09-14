# Astro-ph Paper Bot

A local, mostly-offline daily digest of new arXiv astro-ph papers, ranked
against your research profile, delivered to your inbox every morning.

- **Data source:** the [arXiv API](https://info.arxiv.org/help/api/index.html) — free, no key needed.
- **Ranking:** runs fully on your machine using `sentence-transformers`
  (`all-MiniLM-L6-v2`, ~80 MB). The model downloads once and is cached
  locally afterward — no cloud AI calls, no API key for ranking.
- **Storage:** a local SQLite file (`data/papers.sqlite`) so you never see
  the same paper version twice.
- **Delivery:** plain SMTP email (e.g. Gmail) using Python's built-in
  `smtplib` — no third-party email service.

Only two things need the internet each run: fetching new papers from arXiv,
and sending the email. Everything else — embeddings, scoring, digest
generation — runs locally.

---

## Quick start

```bash
git clone https://github.com/meemik-iisc/AI_Paper_Bot.git
cd AI_Paper_Bot

conda create -n ai_paper_bot python=3.11 -y
conda activate ai_paper_bot
pip install -r requirements.txt

cp .env.example .env
# edit .env with your real Gmail/SMTP details (see step 3 below)

python main.py
```

That's the whole thing end to end. The sections below go through each step
in more detail, plus how to schedule it to run automatically every day.

---

## 1. Clone the repo

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
```

## 2. Set up the environment and install dependencies

This project was built and tested with **conda**:

```bash
conda create -n ai_paper_bot python=3.11 -y
conda activate ai_paper_bot
pip install -r requirements.txt
```

(A plain `venv` works too if you'd rather not use conda — same
`pip install -r requirements.txt` step either way. Just don't mix the two
for one project.)

The first run downloads the embedding model (~80 MB); every run after that
uses the cached copy (`~/.cache/torch/sentence_transformers/` on Linux/Mac).

## 3. Configure email delivery

```bash
cp .env.example .env
```

Edit `.env` with your real values:

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_16_character_app_password
EMAIL_FROM=your_email@gmail.com
EMAIL_TO=your_email@gmail.com

**`.env` is in `.gitignore` and must never be committed** — it holds your
real password. `.env.example` (the template above) is the safe, committed
version.

For Gmail specifically:
1. Turn on 2-Step Verification on your Google account.
2. Create an [App Password](https://myaccount.google.com/apppasswords)
   (choose "Mail" as the app).
3. Use that 16-character password as `SMTP_PASSWORD` — not your normal
   Google password.

Any other SMTP provider (Outlook, iCloud, Fastmail, a work mail server)
works the same way — just change `SMTP_HOST`/`SMTP_PORT`.

To disable email entirely and just generate the Markdown file, set
`"enabled": false` under `"email"` in `config.json`, or pass `--no-email`.

## 4. Configure your research profile

Edit `config.json`:

- `profile` — free-text description of your research interests. This gets
  embedded and compared against every paper's title+abstract.
- `keywords.strong` / `weak` / `negative` — boost or penalize specific terms.
- `categories` — arXiv categories to pull from (e.g. `astro-ph.GA`).
- `minimum_score` / `top_n` — how selective the digest is.
- `lookback_days` — how many days back to search each run. Already-seen
  paper versions are skipped automatically, so some overlap here is safe.

## 5. Run it

```bash
python main.py
```

This fetches recent papers page by page (paced to respect arXiv's rate
limits), ranks them, writes `digests/YYYY-MM-DD.md`, stores everything in
SQLite, and emails the digest (unless disabled).

Useful flags:
- `--lookback-days 7` — override the lookback window for one run.
- `--dry-run` — fetch and rank without writing to the database (useful
  while tuning `config.json`).
- `--no-email` — skip sending mail this run.

**Note on arXiv rate limits:** the arXiv API rate-limits by IP and can hold
a block for several minutes once tripped. The script already retries with
backoff, but if you're testing manually, avoid running it repeatedly in
quick succession — space test runs a few minutes apart.

## 6. Schedule it to run daily (cron)

Find the absolute path to your conda environment's Python:

```bash
conda activate ai_paper_bot
which python
# e.g. /home/you/miniconda3/envs/ai_paper_bot/bin/python
```

Open your crontab:

```bash
crontab -e
```

Add a line to run every day at 11:00 (adjust paths and time as needed):

0 11 * * * cd /full/path/to/paper_bot && /full/path/to/miniconda3/envs/ai_paper_bot/bin/python main.py >> /full/path/to/paper_bot/logs.txt 2>&1


Save and verify it's installed:

```bash
crontab -l
```

Cron uses your system's local timezone by default (check with
`timedatectl`), so `0 11 * * *` means 11:00 in your local time zone, not
UTC.

To test without waiting a full day, temporarily set the time a couple
minutes ahead, wait, then check `logs.txt` for the same output you'd see
running it manually — then switch the time back to `0 11 * * *`.

### Windows (Task Scheduler)

1. Open Task Scheduler → Create Basic Task.
2. Trigger: Daily, pick 11:00 AM.
3. Action: Start a program →
   `C:\path\to\miniconda3\envs\ai_paper_bot\python.exe`
   with arguments `main.py`, "Start in" set to the project folder.

---

## Files
paper_bot/
main.py # fetch → rank → store → digest → email
email_utils.py # SMTP sending, reads .env
config.json # your research profile, keywords, thresholds
.env.example # template for SMTP credentials (copy to .env)
.gitignore
requirements.txt
README.md
data/papers.sqlite # created on first run — gitignored
digests/YYYY-MM-DD.md # created each run — gitignored

## Uploading your own changes to GitHub

```bash
git status                 # confirm .env, data/, digests/ are NOT listed
git add .
git commit -m "Your message here"
git push
```

If this is the very first push for a brand-new repo:

```bash
git init
git add .
git commit -m "Initial commit: astro-ph paper bot"
git remote add origin https://github.com/<your-username>/<your-repo>.git
git branch -M main
git push -u origin main
```

## Notes / possible next steps

- The `feedback` table in SQLite already has columns for `saved`,
  `relevant`, `not_relevant`, `read` — a natural next step is a tiny CLI or
  web UI to mark papers, then fold that signal into `total_score`.
- If arXiv rate-limits you often, lower `max_results` in `config.json` or
  space out manual test runs.
- The score weights (`0.68` semantic / `0.22` keyword / `0.10` category) are
  in `rank_papers()` in `main.py` if you want to retune them.