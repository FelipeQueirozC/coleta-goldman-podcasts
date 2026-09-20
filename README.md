# Goldman Sachs Podcasts

Collects Goldman Sachs The Markets and Goldman Sachs Exchanges episodes. The pipeline reads Goldman transcript PDFs or inline page transcripts, creates a routed English investor summary, and delivers it by email and Telegram.

## Summary pipeline

Stage 1 uses the newest available DeepSeek Flash model. It classifies the episode format and creates an episode-specific prompt.

Stage 2 uses the newest available DeepSeek Pro model. It creates an English memo with these sections:

- Key Takeaway
- Narrative Summary
- Investor Interpretation
- Key Risks and Open Questions
- What to Monitor
- Best Insights
- Relevance

Model discovery queries OpenCode `/models`. Fixed model IDs can override dynamic selection.

## Transcript sources

The collector uses this order:

1. Valid transcript PDF
2. Inline transcript on the episode page
3. YouTube audio transcribed with Groq (`youtube_audio`)
4. Visible failure

Views From the Floor videos publish no transcript, so they start at step 3.
The videos have no captions. `yt-dlp` downloads best audio, Groq
`whisper-large-v3` transcribes it in English.

The FICC page lags behind YouTube, so discovery unions page cards with
two series playlists (`The Breaks of the Game`, `The Macro Call`).
Page cards win on conflict because they carry descriptions.

When a Markets or Exchanges episode falls back to YouTube audio, the
pipeline sends a Telegram warning to the error channel. Those sources
must publish page transcripts.

## Delivery

Email includes a plain-text fallback, Kinea-style HTML summary, and full transcript Markdown attachment.

Telegram receives a short HTML message and one HTML document containing the summary and full transcript.

Failures go to `TELEGRAM_ERROR_CHAT_ID`. Identical sanitized failures are sent once.

## OptiPlex setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env
```

Shared secrets and Telegram channel IDs come from `/etc/collector-env/common.env`.

Project values in `.env`:

```text
RESEND_FROM_DOMAIN=bot.qecapital.com.br
RESEND_TO_EMAIL=<recipient list>
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1
OPENCODE_PROMPT_BUILDER_MODEL=latest-deepseek-flash
OPENCODE_SUMMARIZER_MODEL=latest-deepseek-pro
STATE_PATH=var/sent_documents.json
```

Install the systemd timer:

```bash
sudo sh deploy/install.sh
```

The timer runs Monday through Friday at 11:00 BRT.

## Commands

Live discovery without models, delivery, or state changes:

```bash
python main.py --dry-run
```

Full preview for one episode without delivery or state changes:

```bash
python main.py --episode-url "https://www.goldmansachs.com/insights/the-markets/example"
```

Controlled OptiPlex catch-up:

```bash
python main.py --migration-catch-up
```

Controlled catch-up sends only the newest pending episode from each podcast. It marks older pending episodes as `skipped-migration` after the selected episode succeeds.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

See `docs/optiplex-implementation-plan.md` for the complete approved design and acceptance criteria.

GitHub Actions permits manual runs only after cutover. Do not run GitHub and OptiPlex schedules together.
