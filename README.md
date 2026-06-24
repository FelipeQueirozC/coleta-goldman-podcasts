# Goldman Sachs Podcasts Extractor

This project checks two Goldman Sachs podcast pages every weekday:

- The Markets
- Goldman Sachs Exchanges

When it finds a new episode, it downloads the transcript PDF, turns the transcript into text, asks the OpenCode Go API (DeepSeek v4 Pro) for a short summary, and emails the summary plus transcript using Resend.

Goldman Sachs does not always use the same transcript file name. The script first tries the usual `transcript.pdf` address, then falls back to transcript PDF links found on the episode page.

## How It Remembers Sent Episodes

The script writes a file called `sent_documents.json`.

That file is the memory of the project. It stores which episodes were already handled, grouped by podcast. This prevents the same episode from being emailed again on the next run.

Keep this file in the repository so GitHub Actions can update it after each successful run.

## Setup

Create a virtual environment and install the required packages:

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
.\.venv\Scripts\activate   # Windows PowerShell
pip install -r requirements.txt
playwright install chromium
```

Create your local environment file:

```bash
cp .env.example .env
```

Then fill `.env` with your real values.

## Required Environment Variables

`RESEND_API_KEY`

Your Resend API key. The script needs this to send emails.

`RESEND_FROM_DOMAIN`

The verified sending domain in Resend, for example `bot.example.com`.

The script builds the sender address automatically for each podcast:

- `gs.themarkets@your-domain`
- `gs.exchanges@your-domain`

`RESEND_TO_EMAIL`

One or more destination email addresses. Use commas for multiple recipients:

```text
person@example.com,another@example.com
```

`OPENCODE_API_KEY`

Your OpenCode Go API key. The script needs this to summarize transcripts.

`OPENCODE_BASE_URL` (optional)

The OpenCode Go endpoint base URL. Defaults to `https://opencode.ai/zen/go/v1`.

`OPENCODE_SUMMARIZER_MODEL` (optional)

The model used for summarization. Defaults to `deepseek-v4-pro`.

## First Run

Use initialization mode before the first normal run:

```bash
python main.py --init
```

This scans the current podcast pages and marks all existing episodes as already handled. It saves markdown files locally, but it does not send emails. This avoids flooding your inbox with old episodes.

During initialization, the script also slows down summary requests so it stays under the usual 30-requests-per-minute limit. The first run can therefore take a few minutes.

## Normal Run

Run the mailer normally with:

```bash
python main.py
```

This checks both podcast pages, skips anything already listed in `sent_documents.json`, and emails only new episodes.

## Dry Run

Use dry run mode when you want to test discovery without changing anything:

```bash
python main.py --dry-run
```

Dry run mode fetches the podcast pages and reports what it would do. It does not summarize transcripts, save markdown files, send emails, or update `sent_documents.json`.

## Single Episode Test

Use single-episode mode when you want to test collection, summarization, markdown saving, and email delivery for one episode without touching `sent_documents.json`:

```bash
python main.py --episode-url "https://www.goldmansachs.com/insights/goldman-sachs-exchanges/why-arent-investors-more-worried/"
```

To test only the collection path without calling the summarizer, saving files, or sending email:

```bash
python main.py --episode-url "https://www.goldmansachs.com/insights/goldman-sachs-exchanges/why-arent-investors-more-worried/" --dry-run
```

## GitHub Actions

The workflow in `.github/workflows/daily.yaml` runs the script every weekday at 14:00 UTC, which is 11:00 in Sao Paulo during BRT.

Set these GitHub secrets:

- `RESEND_API_KEY`
- `OPENCODE_API_KEY`

Set these GitHub repository variables:

- `RESEND_FROM_DOMAIN`
- `RESEND_TO_EMAIL`

After each run, GitHub Actions commits any change to `sent_documents.json` so the next run knows what was already sent.
