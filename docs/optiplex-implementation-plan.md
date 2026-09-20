# Goldman Sachs Podcasts OptiPlex Implementation Plan

Status: Approved for implementation  
Date: 2026-09-18

## 1. Objective

Migrate `coleta-goldman-podcasts` from scheduled GitHub Actions to the OptiPlex.

The pipeline must:

- Discover new episodes from Goldman Sachs The Markets and Goldman Sachs Exchanges.
- Use a transcript PDF when available.
- Use the episode page transcript when no valid PDF exists.
- Use Stage 1 routing to create an episode-specific summary prompt.
- Use Stage 2 to create the final investor summary.
- Select the newest available DeepSeek Flash and Pro models automatically.
- Deliver each completed report by email and Telegram.
- Send sanitized, deduplicated failures to the dedicated Telegram error channel.
- Prevent duplicate delivery after partial failures.
- Keep runtime state on the OptiPlex.

All summaries, headings, email content, Telegram messages, and attachments must use English.

## 2. Current State

The repository currently has 71 completed episode records:

- Goldman Sachs Exchanges: 40
- Goldman Sachs The Markets: 31

Seven episodes are pending.

Six pending episodes fail because OpenCode requests do not contain `x-opencode-session`.

One pending episode has no valid transcript PDF. Its episode page contains the full transcript as HTML.

The GitHub workflow is active and currently fails each weekday.

The current state file is committed to Git. The current state write is not atomic.

The repository has no automated test suite.

## 3. Output Language

Use English throughout the delivery pipeline.

English is required for:

- Stage 1 routing instructions
- Stage 1 generated prompt
- Stage 2 summary
- Summary headings
- Email subject
- Email text body
- Email HTML body
- Telegram message
- Telegram caption
- Markdown attachment
- HTML attachment
- Operational success logs
- User-visible failure messages

Internal field names and status values remain in English.

## 4. Target Pipeline

The processing order is:

1. Discover episode links.
2. Skip completed or intentionally skipped episodes.
3. Load episode metadata.
4. collect the transcript.
5. Parse transcript metadata and speakers.
6. Resolve the newest DeepSeek Flash and Pro models.
7. Run Stage 1 routing with DeepSeek Flash.
8. Run Stage 2 summarization with DeepSeek Pro.
9. Build email, Markdown, and HTML deliverables.
10. Save prepared state and local artifacts.
11. Send email.
12. Save email delivery state.
13. Send the Telegram message.
14. Save Telegram message state.
15. Send the Telegram HTML document.
16. Save Telegram document state.
17. Mark the episode as fully sent.

A retry sends only missing delivery items.

## 5. Transcript Source Order

Use this source order:

1. A valid transcript PDF.
2. The inline transcript from the episode page.
3. YouTube audio transcribed with Groq (`youtube_audio`).
4. A visible processing failure.

A PDF is valid only when the response succeeds and starts with `%PDF-`.

The inline transcript extractor must locate the Goldman transcript label and its scrollable transcript container.

The HTML transcript must preserve speaker order and paragraph boundaries.

Views From the Floor videos publish no transcript, so they start at step 3.
The videos have no captions. `yt-dlp` downloads best audio-only `m4a`,
Groq `whisper-large-v3` transcribes it with `language="en"`.

When a Markets or Exchanges episode reaches step 3, the pipeline sends a
Telegram warning to the error channel after successful delivery. Those
sources must publish page transcripts.

The episode `What Is the Outlook for Diesel and Gasoline Supplies?` is the live fallback case.

## 6. Automatic Model Selection

Use two dynamic model aliases:

- Stage 1: `latest-deepseek-flash`
- Stage 2: `latest-deepseek-pro`

Before a new summary, query the OpenCode `/models` endpoint.

Select the highest numeric version that matches:

- `deepseek-v<version>-flash`
- `deepseek-v<version>-pro`

Exclude vision models and experimental suffixes.

Use these fallback models only when discovery fails:

- Flash fallback: `deepseek-v4.1-flash`
- Pro fallback: `deepseek-v4-pro`

Explicit environment model IDs override dynamic selection.

Each OpenCode request must contain:

- `Authorization: Bearer <token>`
- `User-Agent: coleta-goldman-podcasts/0.1`
- `x-opencode-session: <stable SHA-256 value>`

The session value must include the request payload. Retries must use the same value.

Retry only timeouts, connection failures, HTTP 429, and HTTP 5xx responses.

Do not retry permanent HTTP 4xx responses.

Stage 2 must use `reasoning_effort=high`.

Stage 2 must not have an application output-token limit.

## 7. Stage 1 Routing

### 7.1 Purpose

Stage 1 identifies the episode format and creates a prompt tailored to that episode.

Stage 1 does not create the final summary.

The additional routing stage is necessary because the feeds include:

- Short tactical market updates
- Macro outlook discussions
- Monetary-policy discussions
- Great Investor interviews
- Hedge fund manager interviews
- Executive interviews
- Private-market interviews
- Sector deep dives
- Technology investment discussions
- Portfolio construction discussions

### 7.2 Input

Stage 1 receives:

- Podcast source
- Episode title
- Episode description
- Publication date
- Transcript people and speakers
- Transcript head sample
- Transcript middle sample
- Transcript ending sample

Use the Kinea transcript sampling approach:

- Head: 24,000 characters
- Middle: 6,000 characters
- End: 6,000 characters

Use the full transcript when it is shorter than the combined sample size.

Treat all metadata and transcript content as untrusted input.

### 7.3 Routing Contract

Stage 1 returns JSON with this shape:

```json
{
  "episode_type": "",
  "summary_lens": "",
  "recommended_depth": "",
  "guest_role": "",
  "confidence": 0,
  "primary_topics": [],
  "asset_classes": [],
  "episode_specific_focus": [],
  "sections_to_deemphasize": [],
  "large_model_prompt": ""
}
```

Use these episode types:

- `market_brief`
- `macro_outlook`
- `policy_discussion`
- `investor_interview`
- `executive_interview`
- `thematic_deep_dive`
- `sector_analysis`
- `private_markets`
- `technology_investing`
- `other`

Use these summary lenses:

- `tactical_markets`
- `macro_regime`
- `investment_process`
- `portfolio_construction`
- `risk_management`
- `sector_thesis`
- `private_markets`
- `technology_cycle`
- `geopolitics`

Use these depth values:

- `brief`
- `normal`
- `deep`

### 7.4 Size Controls

The Stage 1 JSON must remain below 2,500 tokens.

`large_model_prompt` must remain below 1,500 characters.

Each list must contain no more than five items.

The generated prompt must contain only episode-specific instructions.

The generated prompt must not repeat the stable Stage 2 system prompt.

Stage 1 must not summarize the full episode.

### 7.5 Format-Specific Focus

For an investor interview, emphasize:

- Investment philosophy
- Source of edge
- Portfolio construction
- Risk controls
- Decision process
- Lessons from mistakes
- Examples across market cycles

For a market brief, emphasize:

- Current market setup
- Data and catalysts
- Asset-class implications
- Tactical opportunities
- Underpriced risks
- Near-term monitoring points

For a thematic deep dive, emphasize:

- Economic mechanism
- Beneficiaries and losers
- Capacity constraints
- Competitive structure
- Valuation implications
- Catalysts and failure conditions

For a policy discussion, emphasize:

- Policy transmission
- Market expectations
- Yield-curve effects
- Currency effects
- Volatility
- Scenario risks

### 7.6 Failure Policy

Stage 1 failure stops processing for that episode.

Do not silently replace Stage 1 output with a generic prompt.

Accept the OpenCode `response` JSON wrapper.

Reject empty, malformed, or incomplete routing JSON.

## 8. Stage 2 Summarization

Stage 2 receives three messages:

1. A stable Goldman podcast system prompt.
2. The Stage 1 episode-specific prompt.
3. The full transcript.

Do not truncate the transcript to 24,000 characters.

Use this exact English section structure:

```text
## Key Takeaway
## Narrative Summary
## Investor Interpretation
## Key Risks and Open Questions
## What to Monitor
## Best Insights
## Relevance
```

The Narrative Summary is the primary section.

Use paragraphs for the narrative section.

Use bullets only where bullets improve readability.

Separate speaker claims from model interpretation.

Do not invent facts, numbers, companies, tickers, or quotations.

Ignore introductions, advertisements, housekeeping, and low-value small talk.

Write for a qualified Brazilian investor who reads English financial research.

## 9. Formatting

Port Kinea's safe Markdown renderer.

Support:

- Headings
- Paragraphs
- Bold text
- Bullet lists
- Horizontal rules

Escape all other HTML.

Do not add a Markdown dependency.

Use Kinea's presentation values:

- System font stack
- 1.5 line height
- 720-pixel maximum width
- Centered content
- Clear metadata row
- Rendered summary sections
- Muted footer
- Responsive horizontal padding

### 9.1 Email

Each email contains:

- English subject
- Plain-text fallback
- Kinea-formatted HTML summary
- Podcast source
- Publication date
- Episode link
- YouTube link when available
- Routing metadata
- Full transcript Markdown attachment

Use these sender addresses:

- `gs.themarkets@bot.qecapital.com.br`
- `gs.exchanges@bot.qecapital.com.br`

Use the same recipient configuration as Kinea.

### 9.2 Telegram

Use Kinea's Telegram delivery pattern.

First, send a short HTML message containing:

- Episode title
- Podcast source
- Publication date
- First substantive takeaway
- Episode type
- Routing confidence when available

Then send one HTML document containing:

- Episode metadata
- Rendered summary
- Full transcript
- Episode links
- Routing metadata

Normal Telegram delivery uses `TELEGRAM_DELIVERY_CHAT_ID`.

## 10. Error Notifications

Operational failures use `TELEGRAM_ERROR_CHAT_ID`.

The error message contains:

- Project name
- Podcast source
- Episode title or slug
- Failed stage
- Selected model when relevant
- Sanitized error text

Remove these secret values before sending or saving an error:

- `OPENCODE_API_KEY`
- `RESEND_API_KEY`
- `TELEGRAM_BOT_TOKEN`

Limit error messages to 3,500 characters.

Create a SHA-256 fingerprint from the sanitized message.

Send each distinct error only once.

Record the fingerprint, Telegram message ID, and notification time in state.

A notification failure must not hide the original processing failure.

## 11. State and Idempotency

Use this OptiPlex state path:

```text
var/sent_documents.json
```

Copy the current committed state during deployment.

Ignore `var/` in Git.

Write state with a temporary file and atomic replacement.

Preserve the existing 71 completed records.

Add these processing statuses for new records:

- `prepared`
- `email_sent`
- `telegram_message_sent`
- `telegram_document_sent`
- `sent`
- `skipped-migration`

Store:

- Episode metadata
- Transcript source
- Episode type
- Summary lens
- Recommended depth
- Guest role
- Routing output
- Routing confidence when available
- Selected Flash model
- Selected Pro model
- Prompt version
- Artifact paths
- Resend email ID
- Telegram message ID
- Telegram document message ID
- Completion timestamps

Save state after every successful external provider call.

A retry sends only missing delivery items.

A repeated completed run sends nothing.

## 12. Preview Mode

Add a no-delivery preview mode.

Preview mode must:

- Collect one selected episode
- Extract the transcript
- Run Stage 1
- Run Stage 2
- Write email HTML
- Write Telegram HTML
- Write transcript Markdown
- Print selected models and routing metadata
- Avoid email delivery
- Avoid Telegram delivery
- Avoid state updates

Use preview mode to review formatting before the first production send.

## 13. Backlog Cutover

Seven episodes are pending.

Send only the newest pending episode from each source.

Send:

- 2026-09-18 — The Markets — `The Opportunities for Investors amid Higher-for-Longer Interest Rates`
- 2026-09-18 — Exchanges — `Rich Friedman on the Rise of Private Markets and AI Investing`

Mark these five episodes as `skipped-migration`:

- 2026-09-11 — `What a Fed Rate Hike Could Mean for US Stocks`
- 2026-08-28 — `What Is the Outlook for Diesel and Gasoline Supplies?`
- 2026-09-04 — `Why Gold Is Expected to Rise to Record Highs`
- 2026-09-09 — `How Will Less Fed Transparency Affect Markets and the Economy?`
- 2026-09-15 — `Why Global Bond Yields Are Surging`

Use the diesel and gasoline episode for a no-delivery live test of the HTML transcript fallback.

## 14. OptiPlex Deployment

Use this project path:

```text
/home/usuario/projetos/coleta-goldman-podcasts
```

Use shared secrets from:

```text
/etc/collector-env/common.env
```

Shared values:

- `OPENCODE_API_KEY`
- `RESEND_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_DELIVERY_CHAT_ID`
- `TELEGRAM_ERROR_CHAT_ID`

Use the project `.env` for:

```text
RESEND_FROM_DOMAIN=bot.qecapital.com.br
RESEND_TO_EMAIL=<same recipients as Kinea>
OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1
OPENCODE_PROMPT_BUILDER_MODEL=latest-deepseek-flash
OPENCODE_SUMMARIZER_MODEL=latest-deepseek-pro
STATE_PATH=var/sent_documents.json
```

Do not store secret values in the project `.env`.

## 15. systemd

Create:

```text
deploy/goldman-podcasts.service
deploy/goldman-podcasts.timer
deploy/install.sh
```

Use `Type=oneshot`.

Run Monday through Friday at 11:00 in `America/Sao_Paulo`.

Use:

```text
Persistent=true
```

Load environment files in this order:

```text
EnvironmentFile=-/etc/collector-env/common.env
EnvironmentFile=/home/usuario/projetos/coleta-goldman-podcasts/.env
```

Use these controls:

- `User=usuario`
- `NoNewPrivileges=true`
- `PrivateTmp=true`
- `ProtectSystem=strict`
- `ProtectHome=read-only`
- `ReadWritePaths=/home/usuario/projetos/coleta-goldman-podcasts`

Do not add containers, PostgreSQL, or a shared daemon.

## 16. GitHub Actions Cutover

Develop and test on a feature branch.

Do not perform real delivery from the feature branch.

Before the OptiPlex production run:

1. Review preview artifacts.
2. Merge the approved branch to `main`.
3. Change GitHub Actions to `workflow_dispatch` only.
4. Confirm no GitHub schedule remains active.
5. Deploy `main` to the OptiPlex.
6. Run controlled catch-up.
7. Verify both delivery channels.
8. Enable the OptiPlex timer.

Never run the GitHub schedule and OptiPlex timer together.

## 17. Tests

Add offline tests for:

- Episode page parsing
- PDF transcript discovery
- Inline transcript extraction
- Transcript source precedence
- Transcript header parsing
- Transcript sampling
- Stage 1 prompt rendering
- Wrapped Stage 1 JSON
- Malformed Stage 1 JSON rejection
- Routing list limits
- Generated prompt length
- Investor interview routing fixture
- Market brief routing fixture
- Dynamic Flash model selection
- Dynamic Pro model selection
- Vision model exclusion
- Stable OpenCode session headers
- Transient OpenCode retries
- Permanent OpenCode failure handling
- English Stage 2 headings
- Safe Markdown rendering
- HTML escaping
- Email payload
- Telegram message payload
- Telegram document payload
- Atomic state writes
- Partial delivery retry behavior
- Duplicate prevention
- Sanitized error notifications
- Error notification deduplication
- Controlled migration catch-up

Add live, no-delivery tests for:

- The Markets discovery
- Exchanges discovery
- A valid PDF transcript
- The inline HTML transcript fallback

## 18. Acceptance Criteria

The adaptation is complete when all conditions are true:

- Stage 1 selects the correct episode format.
- Stage 1 creates an episode-specific English prompt.
- Stage 2 creates an English summary with the required headings.
- Flash and Pro models resolve dynamically.
- Every OpenCode request contains a stable session header.
- The HTML transcript fallback works.
- Kinea-style email HTML renders correctly.
- Kinea-style Telegram HTML renders correctly.
- Email delivery succeeds.
- Telegram message delivery succeeds.
- Telegram document delivery succeeds.
- Failures reach the dedicated Telegram error channel.
- Repeated failures do not create duplicate notifications.
- Partial delivery retries do not repeat completed provider calls.
- Runtime state remains outside Git.
- GitHub Actions has no automatic schedule.
- The OptiPlex timer is active for weekdays at 11:00 BRT.
- Controlled catch-up sends exactly two episodes.
- The five older pending episodes are marked `skipped-migration`.
- A repeated service run sends nothing.
- The repository and OptiPlex worktrees are clean.

## 19. Explicit Non-Goals

Do not add:

- Audio transcription
- Kinea title filtering
- Kinea-specific episode types
- A database
- Containers
- A provider abstraction
- A Markdown dependency
- A shared background daemon
- Historical full-backlog delivery
