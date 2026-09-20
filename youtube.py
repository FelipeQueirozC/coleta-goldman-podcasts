"""YouTube audio download and Groq transcription.

Used for Views From the Floor videos, which publish no transcript PDF
and no captions. Podcast sources use this only as a last resort.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


GROQ_MODEL = "whisper-large-v3"
WHISPER_LANGUAGE = "en"
GROQ_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
YOUTUBE_MAX_ATTEMPTS = 3
YOUTUBE_RETRY_DELAYS = (5.0, 20.0)
TRANSIENT_DOWNLOAD_RE = re.compile(
    r"403|429|50[0-3]|timed?\s?out|connection\s?(reset|refused|aborted)|"
    r"broken\s?pipe|unable to download video data|temporary failure",
    re.IGNORECASE,
)
FATAL_DOWNLOAD_RE = re.compile(
    r"private video|video unavailable|removed|deleted|login required|"
    r"log in|age[- ]?gated|copyright|unsupported url|invalid url|not a valid url",
    re.IGNORECASE,
)


def is_transient_download_error(stderr: str) -> bool:
    if FATAL_DOWNLOAD_RE.search(stderr):
        return False
    return bool(TRANSIENT_DOWNLOAD_RE.search(stderr))

Runner = Callable[..., object]


def ytdlp_binary() -> str:
    """Prefer the yt-dlp beside the running interpreter.

    systemd units run with a minimal PATH where `yt-dlp` resolves to the
    outdated system package. The venv install lands next to python.
    """
    candidate = Path(sys.executable).parent / "yt-dlp"
    if candidate.is_file():
        return str(candidate)
    return "yt-dlp"


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def fetch_upload_date(video_id: str, runner: Runner = subprocess.run) -> str:
    """Return the upload date as YYYY-MM-DD, or empty when unavailable."""
    try:
        completed = runner(
            [
                ytdlp_binary(),
                "--skip-download",
                "--no-warnings",
                "--print",
                "%(upload_date)s",
                watch_url(video_id),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = str(getattr(completed, "stdout", "") or "").strip()
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        return ""
    except Exception as exc:
        print(f"     Warning: could not read upload date for {video_id}: {exc}")
        return ""


def download_audio(
    video_id: str, work_dir: Path, runner: Runner = subprocess.run
) -> Path:
    """Download best audio-only stream. Returns the audio file path."""
    work_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(work_dir / "audio.%(ext)s")
    command = [
        ytdlp_binary(),
        "-f",
        "bestaudio[ext=m4a]/bestaudio",
        "--no-playlist",
        "--no-warnings",
        "-o",
        output_template,
        watch_url(video_id),
    ]
    attempts = 0
    while True:
        attempts += 1
        try:
            runner(
                command,
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
            break
        except subprocess.CalledProcessError as exc:
            stderr = str(getattr(exc, "stderr", "") or "").strip()
            detail = stderr.splitlines()
            last_line = detail[-1] if detail else str(exc)
            retryable = (
                attempts < YOUTUBE_MAX_ATTEMPTS
                and is_transient_download_error(stderr)
            )
            if not retryable:
                raise RuntimeError(
                    f"yt-dlp download failed for {video_id} "
                    f"after {attempts} attempt(s): {last_line}"
                ) from exc
            delay = YOUTUBE_RETRY_DELAYS[min(attempts - 1, len(YOUTUBE_RETRY_DELAYS) - 1)]
            print(f"     Warning: download attempt {attempts} failed ({last_line}); "
                  f"retrying in {delay}s")
            time.sleep(delay)
    candidates = sorted(work_dir.glob("audio.*"))
    if not candidates:
        raise RuntimeError(f"yt-dlp produced no audio file for {video_id}")
    return candidates[0]


def transcribe_audio_file(audio_path: Path, api_key: str) -> str:
    if audio_path.stat().st_size > GROQ_MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"Audio file {audio_path} exceeds Groq upload limit "
            f"({GROQ_MAX_UPLOAD_BYTES} bytes)"
        )
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError(
            "The groq package is required. Run: pip install -r requirements.txt"
        ) from exc
    client = Groq(api_key=api_key)
    with audio_path.open("rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=GROQ_MODEL,
            file=audio_file,
            language=WHISPER_LANGUAGE,
            response_format="text",
            temperature=0.0,
        )
    text = transcription if isinstance(transcription, str) else getattr(
        transcription, "text", ""
    )
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("Groq returned an empty transcript")
    return text


def fetch_metadata(video_id: str, runner: Runner = subprocess.run) -> dict:
    """Return title, description, and upload date without downloading."""
    meta = {"video_id": video_id, "title": "", "description": "", "upload_date": ""}
    try:
        completed = runner(
            [
                ytdlp_binary(),
                "--skip-download",
                "--no-warnings",
                "--dump-single-json",
                watch_url(video_id),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        import json

        data = json.loads(str(getattr(completed, "stdout", "") or "{}"))
        meta["title"] = str(data.get("title", "") or "").strip()
        meta["description"] = str(data.get("description", "") or "").strip()
        meta["upload_date"] = str(data.get("upload_date", "") or "")
    except Exception as exc:
        print(f"     Warning: could not read metadata for {video_id}: {exc}")
    return meta


def transcribe_youtube_audio(
    video_id: str,
    api_key: str | None = None,
    work_dir: Path | None = None,
    runner: Runner = subprocess.run,
) -> str:
    """Download YouTube audio and transcribe it with Groq. Returns text."""
    api_key = api_key or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "Missing required environment variable: GROQ_API_KEY"
        )
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="goldman-youtube-") as temp_dir:
            audio_path = download_audio(video_id, Path(temp_dir), runner=runner)
            return transcribe_audio_file(audio_path, api_key)
    audio_path = download_audio(video_id, Path(work_dir), runner=runner)
    return transcribe_audio_file(audio_path, api_key)
