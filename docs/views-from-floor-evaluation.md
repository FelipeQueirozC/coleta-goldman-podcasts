# Views From the Floor — Feasibility Evaluation

Status: Approved and implemented on `feature/views-from-floor`  
Date: 2026-09-19

## 1. Verdict

Integration is feasible. Use the existing `coleta-goldman-podcasts` pipeline.

Add `views_from_floor` as a third source. Download YouTube audio with
`yt-dlp`. Transcribe the audio with Groq. Reuse Stage 1, Stage 2, email,
Telegram, state, and error handling without changes to their interfaces.

## 2. Source Findings

The page `https://www.goldmansachs.com/what-we-do/ficc-and-equities`
embeds video cards as page JSON. The fetch used plain `curl`. No
bot protection blocked the request.

Observed card data on 2026-09-19:

- 9 unique YouTube video IDs appear on the page.
- 6 cards carry titles, eyebrows, descriptions, and video links.
- 2 eyebrows exist: `The Macro Call` and `The Breaks of the Game`.
- 1 YouTube playlist exists: `PLIyiGQywEp65E-tanAHdVfVeEgMiY1jT2`.
- Example titles: `Copper: AI Hype or Supply Squeeze?`,
  `Will Hyperscalers Justify AI Spend?`,
  `Why the Fed's Hawkish Turn Could Support the AI Boom`,
  `Can Markets Withstand AI Risks, Fed Hikes, and Oil Shocks?`,
  `Has the AI Rally Gone Too Far?`, `Markets Brace for Rate Cuts`.

Discovery method: parse the embedded card JSON for `cardTitle`,
`cardEyeBrow`, `cardDescription`, and `linkDestination`. Use the
YouTube video ID as the episode slug. Use the playlist as fallback
discovery through `yt-dlp --flat-playlist`.

## 3. Audio and Caption Findings

Tests ran on the OptiPlex against 2 videos.

With system `yt-dlp` 2024.04.09:

- Video `30ir9C1Im1M` reports no automatic captions and no subtitles.
- Video `vH16LrVAoBc` reports no automatic captions and no subtitles.
- The old version lists only storyboard formats. Audio extraction fails.

With current `yt-dlp` 2026.08.19 in a disposable venv:

- `30ir9C1Im1M` (`Why the Fed's Hawkish Turn Could Support the AI Boom`):
  duration 1396 seconds.
- `vH16LrVAoBc` (`Can Markets Withstand AI Risks, Fed Hikes, and Oil Shocks?`):
  duration 1211 seconds.
- Audio-only `m4a` at 49k measures 7.04 MiB for the 20-minute video.
- Both videos have no captions. Audio transcription is mandatory.

Consequences:

- Upgrade `yt-dlp` inside the project venv. Pin the version in
  `requirements.txt`. The system package stays untouched.
- Each episode needs about 20 minutes of Groq transcription.
- Each audio file fits in 1 Groq request. The 20 MB request limit
  from `kinea-podcast` covers 7 MB files without splitting.
- Transcription cost stays at cents per episode. Confirm the current
  Groq price before production use.

## 4. Reuse Path

`kinea-podcast` already solves the hard part. Its venv on the OptiPlex
contains `groq` 1.6.0 and `imageio-ffmpeg` 0.6.0. Its modules provide:

- `audio.py`: download, transcode below Groq limits, split large files.
- `transcribe.py`: `whisper-large-v3` transcription with language hint.

`profg-transcript` uses the same constants and flow. The design is
proven in production for Portuguese RSS audio.

New code needed in `coleta-goldman-podcasts`:

1. `youtube.py`: download best audio with `yt-dlp` to a temp dir.
   Command: `yt-dlp -f bestaudio[ext=m4a]/bestaudio --no-playlist`.
2. Copy of the Kinea audio/transcribe path with `language="en"`.
3. Third entry in `SOURCES` for the FICC page.
4. Card-JSON discovery function with a saved HTML fixture.
5. New `transcript_source` value: `youtube_audio`.
6. Stage 1 episode type: `trading_desk_brief`. Existing types
   (`market_brief`, `macro_outlook`) cover the rest.
7. New sender: `gs.viewsfromfloor@bot.qecapital.com.br`.
8. New dependencies: `yt-dlp`, `groq`, `imageio-ffmpeg`.

Unchanged modules: Stage 2 system prompt, English memo structure,
Kinea-style formatting, email adapter, Telegram adapters, staged
delivery state, error notifications, systemd shape.

## 5. Transcript Source Order

New order:

1. Valid transcript PDF.
2. Inline transcript on the episode page.
3. YouTube audio transcribed with Groq.
4. Visible failure.

YouTube sources skip steps 1 and 2. The state records
`transcript_source=youtube_audio` for audit.

## 6. Runtime Impact

Current weekday 11:00 BRT service handles 2 podcast listings.
The FICC page adds 1 listing fetch plus at most new episodes.

Per new video episode, expected added time:

- Audio download: about 1 minute for 7 MB.
- Groq transcription: about 2 to 3 minutes for 20 minutes of audio.
- Stage 1 plus Stage 2: same as current podcast episodes.

Memory impact stays low. Audio processing uses temp files.
No new daemon, container, or database is needed.

## 7. Backlog Policy

The page shows only current cards (about 6). Older videos leave the
page. First production run: send the newest video per eyebrow
(`The Macro Call`, `The Breaks of the Game`). Mark older visible
videos as `skipped-migration`. Maximum 2 first-run sends.

## 8. Risks and Mitigations

- YouTube throttling or bot checks: download audio only, keep the
  weekday cadence, send failures to the error channel, retry next run.
- `yt-dlp` version drift: pin the version, add a monthly update check.
- Page JSON drift: keep a saved card fixture, fail loudly on change.
- No captions: accepted, Groq covers the gap at low cost.
- YouTube terms restrict automated downloading: content is public
  Goldman marketing material, risk stays low but nonzero.

## 9. Test Plan

Mirror the existing suite:

- Card discovery from saved HTML fixture.
- Playlist fallback parsing.
- `yt-dlp` call shape with mocked subprocess.
- Transcript source precedence including `youtube_audio`.
- Stage 1 routing fixture for `trading_desk_brief`.
- English heading validation (unchanged).
- Partial delivery retry (unchanged).
- Live no-delivery discovery on the OptiPlex.

## 10. Decision Needed

Approve or reject these 2 items:

1. Add `views_from_floor` as a third source in
   `coleta-goldman-podcasts` with sender
   `gs.viewsfromfloor@bot.qecapital.com.br`.
2. First-run backlog: newest video per eyebrow (max 2 sends),
   older visible videos marked `skipped-migration`.

## Sources

- FICC page: <https://www.goldmansachs.com/what-we-do/ficc-and-equities>
- Playlist: <https://www.youtube.com/playlist?list=PLIyiGQywEp65E-tanAHdVfVeEgMiY1jT2>
- `profg-transcript`: <https://github.com/FelipeQueirozC/profg-transcript>
- Kinea audio path: `kinea-podcast/src/kinea_podcast/audio.py`
- Kinea Groq path: `kinea-podcast/src/kinea_podcast/transcribe.py`
- Pipeline target: `coleta-goldman-podcasts/main.py` (1079 lines, `SOURCES`,
  `extract_transcript`, `prepare_episode`, `deliver_prepared_episode`)
