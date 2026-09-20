from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import delivery as delivery_adapter
import formatting
import opencode
import routing as routing_module
import summarizer as summarizer_module
import youtube as youtube_adapter

DEFAULT_STATE_PATH = Path(__file__).with_name("sent_documents.json")
OUTPUT_DIR = Path(__file__).parent / "output"
BASE_URL = "https://www.goldmansachs.com"

DISCLAIMER_PATTERN = re.compile(r"The opinions and views expressed.*?All rights reserved\.", flags=re.DOTALL)
WHITESPACE_PATTERN = re.compile(r"[ \t]+")
PAGE_NUMBER_PATTERN = re.compile(r"(?m)^\s*\d+\s*$")
RECORDING_DATE_PATTERN = re.compile(r"Date of recording:\s*(.+)", flags=re.IGNORECASE)
SPEAKER_LINE_PATTERN = re.compile(r"^([A-Z][A-Za-z .'-]{1,80}):\s*(.*)$")

SOURCES = [
    {
        "id": "the_markets",
        "name": "GS The Markets",
        "sender_prefix": "gs.themarkets",
        "listing_url": "https://www.goldmansachs.com/insights/the-markets",
        "path_prefix": "/insights/the-markets/",
        "pdf_prefix": "/pdfs/insights/the-markets/"
    },
    {
        "id": "exchanges",
        "name": "GS Exchanges",
        "sender_prefix": "gs.exchanges",
        "listing_url": "https://www.goldmansachs.com/insights/goldman-sachs-exchanges",
        "path_prefix": "/insights/goldman-sachs-exchanges/",
        "pdf_prefix": "/pdfs/insights/goldman-sachs-exchanges/"
    },
    {
        "id": "views_from_floor",
        "name": "GS Views From the Floor",
        "sender_prefix": "gs.viewsfromfloor",
        "listing_url": "https://www.goldmansachs.com/what-we-do/ficc-and-equities",
        "kind": "youtube",
        "playlists": [
            {
                "id": "PLIyiGQywEp65E-tanAHdVfVeEgMiY1jT2",
                "eyebrow": "The Breaks of the Game",
            },
            {"id": "PLNRnpoa435Jc", "eyebrow": "The Macro Call"},
        ],
    }
]

SOURCE_IDS = {source["id"] for source in SOURCES}

class EpisodeStageError(RuntimeError):
    def __init__(self, stage: str, error: object):
        self.stage = stage
        super().__init__(f"{stage} failed: {error}")


@dataclass
class Episode:
    source_id: str
    source_name: str
    slug: str
    url: str
    title: str = ""
    description: str = ""
    date_iso: str = ""
    youtube_url: str = ""
    eyebrow: str = ""
    pdf_url: str = ""
    transcript_series: str = ""
    transcript_title: str = ""
    transcript_people: list[str] | None = None
    recording_date: str = ""
    transcript_text: str = ""
    transcript_source: str = ""
    summary: str = ""

def empty_state() -> dict:
    """Create the sent-episode file shape used by this script."""
    return {"sent": {source_id: {} for source_id in SOURCE_IDS}}

def normalize_state(state: dict) -> dict:
    """Accept old and new state-file formats, then return the new format.

    Older versions stored sent episodes as one big list. The current version
    groups them by podcast so matching slugs from different shows do not collide.
    """
    normalized = empty_state()
    if "updated_at" in state:
        normalized["updated_at"] = state["updated_at"]
    if isinstance(state.get("error_notifications"), dict):
        normalized["error_notifications"] = state["error_notifications"]
    normalized["version"] = max(2, int(state.get("version", 2)))

    sent = state.get("sent", {})

    if isinstance(sent, list):
        for record in sent:
            if not isinstance(record, dict):
                continue
            source_id = record.get("source")
            slug = record.get("slug")
            if not source_id or not slug:
                continue
            normalized["sent"].setdefault(source_id, {})[slug] = {
                "title": record.get("title", ""),
                "date_iso": record.get("date_iso", ""),
                "email_id": record.get("email_id", ""),
                "sent_at": record.get("sent_at", ""),
            }
        return normalized

    if isinstance(sent, dict):
        for source_id, source_records in sent.items():
            if isinstance(source_records, dict):
                normalized["sent"].setdefault(source_id, {}).update(source_records)

    return normalized

def get_state_path() -> Path:
    return Path(os.environ.get("STATE_PATH", str(DEFAULT_STATE_PATH))).expanduser()


def load_state(path: Path | None = None) -> dict:
    path = path or get_state_path()
    if not path.exists():
        return empty_state()

    state_text = path.read_text(encoding="utf-8").strip()
    if not state_text:
        return empty_state()

    return normalize_state(json.loads(state_text))

def save_state(state: dict, path: Path | None = None) -> None:
    path = path or get_state_path()
    state = normalize_state(state)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def was_sent(state: dict, source_id: str, slug: str) -> bool:
    """Check this specific podcast and episode slug."""
    record = state.get("sent", {}).get(source_id, {}).get(slug)
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    return not status or status in {"sent", "skipped-migration", "init_skip"}

ERROR_SECRET_NAMES = (
    "OPENCODE_API_KEY",
    "RESEND_API_KEY",
    "TELEGRAM_BOT_TOKEN",
)


def send_error_text(bot_token: str, chat_id: str, text: str) -> str:
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True},
        timeout=60,
    )
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram error notification failed ({exc.__class__.__name__})") from exc
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram error notification failed: {body}")
    return str(body["result"]["message_id"])


def notify_error_once(
    state_path: Path,
    error: object,
    context: str,
    *,
    env: Mapping[str, str] | None = None,
    sender: Callable[[str, str, str], str] = send_error_text,
) -> bool:
    values = os.environ if env is None else env
    bot_token = values.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = values.get("TELEGRAM_ERROR_CHAT_ID", "")
    if not bot_token or not chat_id:
        return False
    message = re.sub(r"\s+", " ", str(error)).strip() or "unknown error"
    for name in ERROR_SECRET_NAMES:
        secret = values.get(name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    message = f"Goldman podcasts — {context}: {message}"[:3500]
    fingerprint = hashlib.sha256(message.encode()).hexdigest()
    state = load_state(state_path)
    notifications = state.setdefault("error_notifications", {})
    if fingerprint in notifications:
        return False
    message_id = sender(bot_token, chat_id, message)
    notifications[fingerprint] = {
        "message": message,
        "message_id": message_id,
        "notified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    save_state(state, state_path)
    return True


PODCAST_SOURCE_IDS = ("the_markets", "exchanges")


def needs_transcript_fallback_warning(episode: Episode) -> bool:
    """Podcast episodes must publish page transcripts.

    A YouTube-audio transcript for these sources signals a missing page
    transcript, so the operator gets a Telegram warning.
    """
    return (
        episode.source_id in PODCAST_SOURCE_IDS
        and episode.transcript_source == "youtube_audio"
    )


def warn_transcript_fallback(
    state_path: Path,
    episode: Episode,
    run_mode: str = "",
    **notify_kwargs,
) -> bool:
    if not needs_transcript_fallback_warning(episode):
        return False
    context = f"{episode.source_name} / {episode.slug} / transcript-fallback"
    if run_mode:
        context = f"{context} / {run_mode}"
    try:
        return notify_error_once(
            state_path,
            "No transcript PDF or inline transcript on the episode page; "
            "summary built from YouTube audio transcription.",
            context,
            **notify_kwargs,
        )
    except Exception as exc:
        print(
            f"  -> ERROR sending fallback warning: {exc.__class__.__name__}",
            file=sys.stderr,
        )
        return False


def fetch_dynamic_html(url: str) -> str:
    print(f"Opening the podcast listing page: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; GoldmanExtractor/1.0)")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Goldman Sachs loads more items as the page scrolls.
        for _ in range(3):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1000)
        content = page.content()
        browser.close()
        return content

def discover_slugs(html: str, path_prefix: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    slugs = set()
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        parsed = urlparse(urljoin(BASE_URL, href))
        path = parsed.path.rstrip("/")
        if path.startswith(path_prefix):
            slug = path.removeprefix(path_prefix).strip("/")
            if slug and "/" not in slug:
                slugs.add(slug)
    return sorted(slugs)

CARD_VALUE_RES = {
    name: re.compile(r'"' + name + r'":"((?:\\.|[^"\\])*)"')
    for name in ("cardEyeBrow", "cardTitle", "cardDescription", "linkDestination")
}
YOUTUBE_VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})")


def _unquote_card_value(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def strip_card_html(value: str) -> str:
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def discover_youtube_cards(html: str) -> list[dict]:
    """Extract Goldman video cards (title, eyebrow, description, video id).

    Cards live in embedded page JSON. Splitting on the eyebrow marker keeps
    each card's fields paired. Business-unit cards have no YouTube link and
    are skipped. Cards are deduped by video id, preserving page order.
    """
    cards = []
    seen = set()
    for segment in html.split('"cardEyeBrow":"')[1:]:
        eyebrow_match = re.match(r'((?:\\.|[^"\\])*)"', segment)
        eyebrow = (
            _unquote_card_value(eyebrow_match.group(1)).strip().strip('"')
            if eyebrow_match
            else ""
        )
        if not eyebrow:
            continue
        fields = {}
        for name, pattern in CARD_VALUE_RES.items():
            match = pattern.search(segment)
            fields[name] = _unquote_card_value(match.group(1)) if match else ""
        link = fields["linkDestination"]
        video_match = YOUTUBE_VIDEO_ID_RE.search(link)
        title = WHITESPACE_PATTERN.sub(" ", fields["cardTitle"].strip().strip('"')).strip()
        if not video_match or not title:
            continue
        video_id = video_match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        cards.append(
            {
                "video_id": video_id,
                "title": title,
                "eyebrow": eyebrow,
                "description": strip_card_html(fields["cardDescription"]),
            }
        )
    return cards


def views_source() -> dict:
    return next(source for source in SOURCES if source["id"] == "views_from_floor")


def merge_playlist_cards(
    page_cards: list[dict], playlist_items: list[dict], eyebrow: str
) -> list[dict]:
    """Union page cards with fresher playlist items.

    The FICC page lags behind YouTube, so playlist-only videos are appended
    with the playlist series as eyebrow. Page cards win on conflict because
    they carry descriptions.
    """
    merged = list(page_cards)
    known = {card["video_id"] for card in merged}
    for item in playlist_items:
        if item["video_id"] in known:
            continue
        known.add(item["video_id"])
        merged.append(
            {
                "video_id": item["video_id"],
                "title": item["title"],
                "eyebrow": eyebrow,
                "description": "",
            }
        )
    return merged


def discover_views_cards(listing_html: str, source: dict) -> list[dict]:
    cards = discover_youtube_cards(listing_html)
    for playlist in source.get("playlists", []):
        items = youtube_adapter.list_playlist_videos(playlist["id"])
        if items:
            cards = merge_playlist_cards(cards, items, playlist["eyebrow"])
        else:
            print(f"     Warning: playlist {playlist['id']} returned no videos")
    return cards


def collect_youtube_episode(
    source: dict,
    card: dict,
    upload_date_fetcher=None,
    transcriber=None,
    dry_run: bool = False,
) -> Episode:
    video_id = card["video_id"]
    print(f"  -> Collecting YouTube video: {youtube_adapter.watch_url(video_id)}")
    if dry_run:
        return Episode(
            source_id=source["id"],
            source_name=source["name"],
            slug=video_id,
            url=youtube_adapter.watch_url(video_id),
            title=card.get("title") or video_id,
            description=card.get("description", ""),
            date_iso=card.get("date_iso", ""),
            youtube_url=youtube_adapter.watch_url(video_id),
            eyebrow=card.get("eyebrow", ""),
            transcript_text="",
            transcript_source="",
        )
    fetch_date = upload_date_fetcher or youtube_adapter.fetch_upload_date
    transcribe = transcriber or youtube_adapter.transcribe_youtube_audio
    date_iso = card.get("date_iso") or fetch_date(video_id)
    transcript_text = transcribe(video_id)
    if not transcript_text.strip():
        raise EpisodeStageError(
            "transcript collection", "YouTube transcription returned empty text"
        )
    transcript_text = reflow_transcript_text(strip_transcript_header(transcript_text))
    return Episode(
        source_id=source["id"],
        source_name=source["name"],
        slug=video_id,
        url=youtube_adapter.watch_url(video_id),
        title=card.get("title") or video_id,
        description=card.get("description", ""),
        date_iso=date_iso,
        youtube_url=youtube_adapter.watch_url(video_id),
        eyebrow=card.get("eyebrow", ""),
        transcript_people=extract_speaker_names(transcript_text),
        transcript_text=transcript_text,
        transcript_source="youtube_audio",
    )


def source_and_slug_from_episode_url(episode_url: str) -> tuple[dict, str]:
    youtube_match = YOUTUBE_VIDEO_ID_RE.search(episode_url or "")
    if youtube_match:
        return views_source(), youtube_match.group(1)
    parsed = urlparse(urljoin(BASE_URL, episode_url))
    path = parsed.path.rstrip("/")

    for source in SOURCES:
        source_path = source["path_prefix"].rstrip("/")
        if path.startswith(source_path + "/"):
            slug = path.removeprefix(source_path).strip("/")
            if slug and "/" not in slug:
                return source, slug

    raise ValueError(f"Episode URL does not match a configured Goldman podcast: {episode_url}")

def parse_episode_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
   
    # Read the title Goldman Sachs puts in the page metadata.
    title = ""
    title_meta = soup.select_one("meta[property='og:title']")
    if title_meta:
        title = title_meta.get("content")
    else:
        h1 = soup.select_one("h1")
        if h1: title = h1.get_text(strip=True)
   
    # Read the publication date when it is available.
    date_iso = ""
    date_meta = soup.select_one("meta[property='article:published_time']")
    if date_meta:
        date_raw = date_meta.get("content", "")
        if len(date_raw) >= 10:
            date_iso = date_raw[:10]

    # If standard meta tag didn't yield a date, try JSON-LD
    if not date_iso:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or script.get_text())
            except json.JSONDecodeError:
                continue
            # Handle both dict and list
            if isinstance(data, dict):
                date_published = data.get("datePublished")
                if date_published and isinstance(date_published, str) and len(date_published) >= 10:
                    date_iso = date_published[:10]
                    break
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        date_published = item.get("datePublished")
                        if date_published and isinstance(date_published, str) and len(date_published) >= 10:
                            date_iso = date_published[:10]
                            break
                if date_iso:
                    break

    description = ""
    for selector in ("meta[name='description']", "meta[property='og:description']"):
        description_meta = soup.select_one(selector)
        if description_meta and description_meta.get("content"):
            description = description_meta.get("content", "").strip()
            break

    youtube_url = ""
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if "youtube.com" in href or "youtu.be" in href:
            youtube_url = href
            break

    return {
        "title": title,
        "description": description,
        "date_iso": date_iso,
        "youtube_url": youtube_url,
    }

def discover_transcript_pdf_urls(page_html: str, page_url: str) -> list[str]:
    """Find transcript PDF links that Goldman Sachs names differently.

    Most episodes use /transcript.pdf, but some use names like
    /exchanges-bruce-kirk-transcript.pdf. Both still end with transcript.pdf.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    urls = []
    seen = set()

    def add_url(raw_url: str) -> None:
        clean_url = raw_url.replace("\\\\", "/").strip()
        full_url = urljoin(page_url, clean_url)
        parsed = urlparse(full_url)

        if parsed.path.lower().startswith("/content/dam/gs/gscom/pdfs/"):
            full_url = urljoin(BASE_URL, parsed.path.replace("/content/dam/gs/gscom/", "", 1))

        path = urlparse(full_url).path.lower()
        if path.endswith("transcript.pdf") and full_url not in seen:
            urls.append(full_url)
            seen.add(full_url)

    # Check standard href attributes
    for a in soup.select("a[href]"):
        add_url(a.get("href", ""))

    # Check for custom attributes like data-pdf-url or data-transcript-url
    for tag in soup.select("[data-pdf-url], [data-transcript-url]"):
        for attr in ["data-pdf-url", "data-transcript-url"]:
            if tag.has_attr(attr):
                add_url(tag[attr])

    # Check JSON-LD blocks for PDF URLs
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue
        # Recursively search through JSON data for PDF URLs
        def find_pdf_urls(obj):
            if isinstance(obj, dict):
                for value in obj.values():
                    find_pdf_urls(value)
            elif isinstance(obj, list):
                for item in obj:
                    find_pdf_urls(item)
            elif isinstance(obj, str) and ".pdf" in obj.lower():
                add_url(obj)
        find_pdf_urls(data)

    # Some pages keep the transcript link inside page data instead of an HTML link.
    for match in re.findall(
        r"https?://[^\\\"'<>\\\\]+transcript\\.pdf|/(?:content/dam/gs/gscom/)?pdfs/[^\\\"'<>\\\\]+transcript\\.pdf",
        page_html,
        flags=re.IGNORECASE,
    ):
        add_url(match)

    return urls

def is_pdf_response(response: requests.Response) -> bool:
    return response.status_code == 200 and response.content.startswith(b"%PDF-")

def fetch_transcript_pdf(
    session: requests.Session,
    expected_pdf_url: str,
    page_pdf_urls: list[str],
    referer_url: str,
) -> tuple[str, bytes, list[str]]:
    """Try the usual transcript URL, then fall back to links from the page."""
    candidate_urls = [expected_pdf_url]
    for pdf_url in page_pdf_urls:
        if pdf_url not in candidate_urls:
            candidate_urls.append(pdf_url)

    for pdf_url in candidate_urls:
        pdf_resp = session.get(pdf_url, headers={"Referer": referer_url}, timeout=30)
        if is_pdf_response(pdf_resp):
            if pdf_url != expected_pdf_url:
                print(f"     Found transcript using page PDF link: {pdf_url}")
            return pdf_url, pdf_resp.content, candidate_urls

    return expected_pdf_url, b"", candidate_urls

def extract_pdf_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(pdf_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(page.strip() for page in pages if page.strip())
    
    # Remove the repeated legal disclaimer so the summary focuses on the episode.
    text, _ = DISCLAIMER_PATTERN.subn("", text, count=1)
    text = PAGE_NUMBER_PATTERN.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def extract_inline_transcript(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    label = soup.find(
        string=lambda value: value and value.strip().lower() == "transcript:"
    )
    if not label:
        return ""
    container = label.find_parent("p")
    container = container.find_next("div") if container else None
    if not container:
        return ""
    paragraphs = [
        WHITESPACE_PATTERN.sub(" ", paragraph.get_text(" ", strip=True)).strip()
        for paragraph in container.find_all("p")
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def default_youtube_fetcher(youtube_url: str) -> str:
    match = YOUTUBE_VIDEO_ID_RE.search(youtube_url or "")
    if not match:
        return ""
    return youtube_adapter.transcribe_youtube_audio(match.group(1))


def extract_transcript(
    pdf_bytes: bytes,
    page_html: str,
    youtube_url: str = "",
    youtube_fetcher=None,
) -> tuple[str, str]:
    if pdf_bytes:
        text = extract_pdf_text(pdf_bytes)
        if text:
            return text, "transcript_pdf"
    inline_text = extract_inline_transcript(page_html)
    if inline_text:
        return inline_text, "inline_html"
    if youtube_url:
        fetcher = youtube_fetcher or default_youtube_fetcher
        youtube_text = fetcher(youtube_url)
        if youtube_text:
            return youtube_text, "youtube_audio"
    return "", "missing"


def parse_transcript_header(transcript_text: str) -> dict:
    """Pull the cover-page details out of a Goldman transcript when present."""
    lines = [WHITESPACE_PATTERN.sub(" ", line).strip() for line in transcript_text.splitlines()]
    lines = [line for line in lines if line and not line.isdigit()]
    if not lines:
        return {}

    date_index = None
    recording_date = ""
    for index, line in enumerate(lines[:30]):
        match = RECORDING_DATE_PATTERN.match(line)
        if match:
            date_index = index
            recording_date = match.group(1).strip()
            break

    if date_index is not None:
        header_lines = lines[:date_index]
    else:
        header_lines = []
        for line in lines[:12]:
            if re.match(r"^[A-Z][A-Za-z .'-]{1,80}:", line):
                break
            header_lines.append(line)

    if len(header_lines) < 2:
        return {"recording_date": recording_date} if recording_date else {}

    series = header_lines[0]
    remaining = header_lines[1:]

    title_parts = []
    while remaining:
        line = remaining[0]
        if title_parts and "," in line:
            break
        title_parts.append(line)
        remaining = remaining[1:]

    people = []
    current_person = ""
    for line in remaining:
        if "," in line and current_person:
            people.append(current_person.strip())
            current_person = line
        else:
            current_person = f"{current_person} {line}".strip()
    if current_person:
        people.append(current_person.strip())

    return {
        "series": series,
        "title": " ".join(title_parts).strip(),
        "people": people,
        "recording_date": recording_date,
    }

def strip_transcript_header(transcript_text: str) -> str:
    """Remove cover-page metadata so the transcript section starts at dialogue."""
    lines = transcript_text.splitlines()
    for index, line in enumerate(lines[:40]):
        if RECORDING_DATE_PATTERN.search(line):
            body = "\n".join(lines[index + 1:]).strip()
            return re.sub(r"\n{3,}", "\n\n", body)

    for index, line in enumerate(lines[:40]):
        if re.match(r"^[A-Z][A-Za-z .'-]{1,80}:\s+", line.strip()):
            body = "\n".join(lines[index:]).strip()
            return re.sub(r"\n{3,}", "\n\n", body)

    return transcript_text.strip()

def reflow_transcript_text(transcript_text: str) -> str:
    """Join PDF-wrapped lines into readable transcript paragraphs."""
    paragraphs = []
    current_lines = []

    def flush_current() -> None:
        if not current_lines:
            return
        paragraph = " ".join(line.strip() for line in current_lines if line.strip())
        paragraph = WHITESPACE_PATTERN.sub(" ", paragraph).strip()
        if paragraph:
            paragraphs.append(paragraph)
        current_lines.clear()

    for raw_line in transcript_text.splitlines():
        line = WHITESPACE_PATTERN.sub(" ", raw_line).strip()
        if not line:
            flush_current()
            continue

        if SPEAKER_LINE_PATTERN.match(line):
            flush_current()
            current_lines.append(line)
            continue

        current_lines.append(line)

    flush_current()
    return "\n\n".join(paragraphs).strip()

def extract_speaker_names(transcript_text: str) -> list[str]:
    speakers = []
    for raw_line in transcript_text.splitlines():
        match = SPEAKER_LINE_PATTERN.match(raw_line.strip())
        if not match:
            continue
        name = match.group(1)
        normalized = name.lower()
        if normalized.endswith(" exchanges") or normalized in {"the markets", "goldman sachs"}:
            continue
        if name not in speakers:
            speakers.append(name)
    return speakers


def recording_date_to_iso(recording_date: str) -> str:
    if not recording_date:
        return ""
    try:
        return datetime.strptime(recording_date, "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""

def collect_episode(session: requests.Session, source: dict, slug: str) -> tuple[Episode, bytes]:
    episode_url = urljoin(BASE_URL, f"{source['path_prefix']}{slug}")
    print(f"  -> Collecting episode: {episode_url}")

    # Open the episode page to get title, date, and YouTube link using Playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (compatible; GoldmanExtractor/1.0)")
        page.goto(episode_url, wait_until="domcontentloaded", timeout=30000)
        ep_html = page.content()
        browser.close()

    meta = parse_episode_page(ep_html)

    # Try the standard transcript name first. If that fails, use
    # transcript PDF links found on the episode page.
    expected_pdf_url = urljoin(BASE_URL, f"{source['pdf_prefix']}{slug}/transcript.pdf")
    page_pdf_urls = discover_transcript_pdf_urls(ep_html, episode_url)
    pdf_url, pdf_bytes, tried_pdf_urls = fetch_transcript_pdf(
        session,
        expected_pdf_url,
        page_pdf_urls,
        episode_url,
    )
    transcript_text, transcript_source = extract_transcript(
        pdf_bytes, ep_html, meta["youtube_url"]
    )
    if transcript_source == "inline_html":
        print("     Using the inline episode transcript because no valid PDF was found.")
    elif transcript_source == "missing":
        tried_list = ", ".join(tried_pdf_urls)
        print(f"     Warning: transcript not found. Tried PDFs: {tried_list}")
    transcript_header = parse_transcript_header(transcript_text)
    transcript_text = reflow_transcript_text(strip_transcript_header(transcript_text))
    speakers = extract_speaker_names(transcript_text)
    date_iso = meta["date_iso"] or recording_date_to_iso(transcript_header.get("recording_date", ""))

    episode = Episode(
        source_id=source["id"],
        source_name=source["name"],
        slug=slug,
        url=episode_url,
        title=meta["title"] or slug,
        description=meta["description"],
        date_iso=date_iso,
        youtube_url=meta["youtube_url"],
        pdf_url=pdf_url,
        transcript_series=transcript_header.get("series", ""),
        transcript_title=transcript_header.get("title", ""),
        transcript_people=speakers or transcript_header.get("people", []),
        recording_date=transcript_header.get("recording_date", ""),
        transcript_text=transcript_text,
        transcript_source=transcript_source,
    )
    return episode, pdf_bytes

def routing_to_dict(decision: routing_module.RoutingDecision) -> dict:
    return {
        "episode_type": decision.episode_type,
        "summary_lens": decision.summary_lens,
        "recommended_depth": decision.recommended_depth,
        "guest_role": decision.guest_role,
        "confidence": decision.confidence,
        "primary_topics": list(decision.primary_topics),
        "asset_classes": list(decision.asset_classes),
        "episode_specific_focus": list(decision.episode_specific_focus),
        "sections_to_deemphasize": list(decision.sections_to_deemphasize),
        "large_model_prompt": decision.large_model_prompt,
    }


def routing_from_dict(value: dict) -> routing_module.RoutingDecision:
    return routing_module.RoutingDecision(
        episode_type=str(value.get("episode_type", "other")),
        summary_lens=str(value.get("summary_lens", "")),
        recommended_depth=str(value.get("recommended_depth", "normal")),
        guest_role=str(value.get("guest_role", "")),
        confidence=float(value.get("confidence", 0.5)),
        primary_topics=tuple(value.get("primary_topics", [])),
        asset_classes=tuple(value.get("asset_classes", [])),
        episode_specific_focus=tuple(value.get("episode_specific_focus", [])),
        sections_to_deemphasize=tuple(value.get("sections_to_deemphasize", [])),
        large_model_prompt=str(value.get("large_model_prompt", "")),
        raw_response="",
    )


def prepare_episode(
    episode: Episode,
    state: dict,
    state_path: Path,
    opencode_config: opencode.OpenCodeConfig,
    *,
    router=routing_module.route_episode,
    summary_writer=summarizer_module.summarize,
) -> routing_module.RoutingDecision:
    if not episode.transcript_text.strip():
        raise EpisodeStageError("transcript collection", "no transcript text was found")
    records = state.setdefault("sent", {}).setdefault(episode.source_id, {})
    record = records.get(episode.slug, {})
    stored_routing = record.get("routing")
    if record.get("summary") and isinstance(stored_routing, dict):
        decision = routing_from_dict(stored_routing)
        summary = str(record["summary"])
    else:
        try:
            decision = router(episode, episode.transcript_text, opencode_config)
        except Exception as exc:
            raise EpisodeStageError("Stage 1", exc) from exc
        try:
            summary = summary_writer(
                decision, episode.transcript_text, opencode_config
            )
        except Exception as exc:
            raise EpisodeStageError("Stage 2", exc) from exc

    source_dir = OUTPUT_DIR / episode.source_id
    source_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = source_dir / formatting.markdown_filename(episode)
    html_path = source_dir / formatting.html_filename(episode)
    markdown_path.write_text(
        formatting.build_markdown_attachment(episode, summary, decision),
        encoding="utf-8",
    )
    html_path.write_text(
        formatting.build_html_attachment(episode, summary, decision),
        encoding="utf-8",
    )

    record.update(
        {
            "status": record.get("status", "prepared"),
            "title": episode.title,
            "date_iso": episode.date_iso,
            "url": episode.url,
            "transcript_source": episode.transcript_source,
            "summary": summary,
            "routing": routing_to_dict(decision),
            "flash_model": opencode_config.prompt_builder_model,
            "pro_model": opencode_config.summarizer_model,
            "prompt_version": 1,
            "markdown_path": str(markdown_path),
            "html_path": str(html_path),
            "prepared_at": record.get(
                "prepared_at", datetime.now(timezone.utc).isoformat()
            ),
        }
    )
    records[episode.slug] = record
    save_state(state, state_path)
    return decision


def deliver_prepared_episode(
    episode,
    routing,
    state: dict,
    state_path: Path,
    config: delivery_adapter.DeliveryConfig,
    *,
    email_sender=delivery_adapter.send_email,
    message_sender=delivery_adapter.send_telegram_message,
    document_sender=delivery_adapter.send_telegram_document,
) -> None:
    record = state["sent"][episode.source_id][episode.slug]
    summary = str(record["summary"])
    markdown_path = Path(record["markdown_path"])
    html_path = Path(record["html_path"])

    if not record.get("email_id"):
        record["email_id"] = email_sender(
            episode, summary, routing, config, markdown_path
        )
        record["status"] = "email_sent"
        save_state(state, state_path)

    if not record.get("telegram_message_id"):
        record["telegram_message_id"] = message_sender(
            episode, summary, routing, config
        )
        record["status"] = "telegram_message_sent"
        save_state(state, state_path)

    if not record.get("telegram_document_message_id"):
        record["telegram_document_message_id"] = document_sender(
            episode, routing, config, html_path
        )
        record["status"] = "telegram_document_sent"
        save_state(state, state_path)

    record["status"] = "sent"
    record["sent_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state, state_path)


def load_opencode_config() -> opencode.OpenCodeConfig:
    if not os.environ.get("OPENCODE_API_KEY"):
        raise RuntimeError("Missing required environment variable: OPENCODE_API_KEY")
    return opencode.resolve_models(
        opencode.OpenCodeConfig(
            api_key=os.environ["OPENCODE_API_KEY"],
            base_url=os.environ.get("OPENCODE_BASE_URL", opencode.DEFAULT_BASE_URL),
            prompt_builder_model=os.environ.get(
                "OPENCODE_PROMPT_BUILDER_MODEL", opencode.AUTO_FLASH_MODEL
            ),
            summarizer_model=os.environ.get(
                "OPENCODE_SUMMARIZER_MODEL", opencode.AUTO_PRO_MODEL
            ),
        )
    )


def load_delivery_config() -> delivery_adapter.DeliveryConfig:
    required = (
        "RESEND_API_KEY",
        "RESEND_FROM_DOMAIN",
        "RESEND_TO_EMAIL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_DELIVERY_CHAT_ID",
        "TELEGRAM_ERROR_CHAT_ID",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    recipients = [
        value.strip()
        for value in os.environ["RESEND_TO_EMAIL"].split(",")
        if value.strip()
    ]
    if not recipients:
        raise RuntimeError("RESEND_TO_EMAIL must contain at least one recipient")
    return delivery_adapter.DeliveryConfig(
        resend_api_key=os.environ["RESEND_API_KEY"],
        resend_from_domain=os.environ["RESEND_FROM_DOMAIN"],
        resend_to=recipients,
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_delivery_chat_id=os.environ["TELEGRAM_DELIVERY_CHAT_ID"],
        telegram_error_chat_id=os.environ["TELEGRAM_ERROR_CHAT_ID"],
    )


def load_runtime_configs() -> tuple[opencode.OpenCodeConfig, delivery_adapter.DeliveryConfig]:
    return load_opencode_config(), load_delivery_config()


def load_environment() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def run_single_episode(episode_url: str, dry_run: bool) -> int:
    load_environment()
    source, slug = source_and_slug_from_episode_url(episode_url)
    print(f"Previewing one episode from {source['name']}: {slug}")
    if source.get("kind") == "youtube":
        meta = youtube_adapter.fetch_metadata(slug)
        episode = collect_youtube_episode(
            source,
            {
                "video_id": slug,
                "title": meta["title"] or slug,
                "eyebrow": "",
                "description": meta["description"],
            },
        )
    else:
        with requests.Session() as session:
            session.headers.update(
                {"User-Agent": "Mozilla/5.0 (compatible; GoldmanExtractor/1.0)"}
            )
            episode, _ = collect_episode(session, source, slug)
    if dry_run:
        print("Dry run complete. No models, files, deliveries, or state updates were used.")
        return 0

    opencode_config = load_opencode_config()
    decision = routing_module.route_episode(
        episode, episode.transcript_text, opencode_config
    )
    summary = summarizer_module.summarize(
        decision, episode.transcript_text, opencode_config
    )
    preview_dir = OUTPUT_DIR / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = preview_dir / formatting.markdown_filename(episode)
    html_path = preview_dir / formatting.html_filename(episode)
    email_path = preview_dir / (formatting.attachment_basename(episode) + "-email.html")
    routing_path = preview_dir / (formatting.attachment_basename(episode) + "-routing.json")
    markdown_path.write_text(
        formatting.build_markdown_attachment(episode, summary, decision),
        encoding="utf-8",
    )
    html_path.write_text(
        formatting.build_html_attachment(episode, summary, decision),
        encoding="utf-8",
    )
    email_path.write_text(
        formatting.build_email_html(episode, summary, decision), encoding="utf-8"
    )
    routing_path.write_text(
        json.dumps(routing_to_dict(decision), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Models: {opencode_config.prompt_builder_model} / "
        f"{opencode_config.summarizer_model}"
    )
    print(
        f"Routing: {decision.episode_type} / {decision.summary_lens} / "
        f"{decision.recommended_depth}"
    )
    print(f"Routing decision: {routing_path}")
    print(f"Email preview: {email_path}")
    print(f"Telegram HTML: {html_path}")
    print(f"Markdown transcript: {markdown_path}")
    print("Preview complete. No delivery or state update occurred.")
    return 0


def mark_without_delivery(state: dict, episode: Episode, status: str) -> None:
    state.setdefault("sent", {}).setdefault(episode.source_id, {})[episode.slug] = {
        "status": status,
        "title": episode.title,
        "date_iso": episode.date_iso,
        "url": episode.url,
        "transcript_source": episode.transcript_source,
        "skipped_at": datetime.now(timezone.utc).isoformat(),
    }


def migration_groups(
    pending: list[Episode],
) -> list[tuple[str, list[Episode]]]:
    """Group pending episodes for catch-up selection.

    Views From the Floor groups by video series (eyebrow) so each series
    keeps its newest video. Podcast sources form a single group each.
    """
    groups = []
    for source in SOURCES:
        source_pending = [ep for ep in pending if ep.source_id == source["id"]]
        if not source_pending:
            continue
        if source["id"] == "views_from_floor":
            eyebrows: dict[str, list[Episode]] = {}
            for episode in source_pending:
                eyebrows.setdefault(episode.eyebrow or "", []).append(episode)
            for eyebrow in sorted(eyebrows):
                groups.append((source["id"], eyebrows[eyebrow]))
        else:
            groups.append((source["id"], source_pending))
    return groups


def select_migration_pending(
    pending: list[Episode],
) -> tuple[list[Episode], dict[str, list[Episode]]]:
    selected = []
    skipped_by_source: dict[str, list[Episode]] = {}
    for source_id, group in migration_groups(pending):
        newest = max(group, key=lambda ep: (ep.date_iso, ep.slug))
        selected.append(newest)
        skipped_by_source.setdefault(source_id, []).extend(
            episode for episode in group if episode.slug != newest.slug
        )
    return selected, skipped_by_source


def run(init_only: bool, dry_run: bool, migration_catch_up: bool = False) -> int:
    load_environment()
    state_path = get_state_path()
    state = load_state(state_path)
    pending: list[Episode] = []
    had_errors = False
    run_mode = "migration catch-up" if migration_catch_up else "daily"
    collection_errors = 0

    with requests.Session() as session:
        session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; GoldmanExtractor/1.0)"}
        )
        for source in SOURCES:
            print(f"\nChecking podcast: {source['name']}")
            try:
                listing_html = fetch_dynamic_html(source["listing_url"])
                youtube_cards = None
                if source.get("kind") == "youtube":
                    youtube_cards = {
                        card["video_id"]: card
                        for card in discover_views_cards(listing_html, source)
                    }
                    slugs = sorted(youtube_cards)
                else:
                    slugs = discover_slugs(listing_html, source["path_prefix"])
            except Exception as exc:
                had_errors = True
                print(f"  -> ERROR reading listing for {source['name']}: {exc}", file=sys.stderr)
                if not dry_run:
                    try:
                        notify_error_once(
                            state_path, exc, f"{source['name']} / discovery / {run_mode}"
                        )
                    except Exception as notify_exc:
                        print(
                            f"  -> ERROR sending failure notification: {notify_exc.__class__.__name__}",
                            file=sys.stderr,
                        )
                continue

            print(f"  -> Found {len(slugs)} episode links on the listing page.")
            for slug in slugs:
                if was_sent(state, source["id"], slug):
                    print(f"  -> Already handled: {slug}")
                    continue
                print(f"  -> New episode found: {slug}")
                try:
                    if youtube_cards is not None:
                        episode = collect_youtube_episode(
                            source, youtube_cards[slug], dry_run=dry_run
                        )
                    else:
                        episode, _ = collect_episode(session, source, slug)
                    pending.append(episode)
                    if dry_run:
                        print(f"     Dry run: would process '{episode.title}'.")
                except Exception as exc:
                    had_errors = True
                    collection_errors += 1
                    print(
                        f"  -> ERROR collecting {source['name']} / {slug}: {exc}",
                        file=sys.stderr,
                    )
                    if not dry_run:
                        try:
                            notify_error_once(
                                state_path,
                                exc,
                                f"{source['name']} / {slug} / collection / {run_mode}",
                            )
                        except Exception as notify_exc:
                            print(
                                f"  -> ERROR sending failure notification: {notify_exc.__class__.__name__}",
                                file=sys.stderr,
                            )

    if dry_run:
        return 1 if had_errors else 0
    if not pending and not had_errors:
        print("\nNo new episodes found.")
        return 0

    if init_only:
        for episode in pending:
            mark_without_delivery(state, episode, "init_skip")
        save_state(state, state_path)
        print(f"\nInitialization marked {len(pending)} episodes without delivery.")
        return 1 if had_errors else 0

    skipped_by_source: dict[str, list[Episode]] = {}
    if migration_catch_up:
        pending, skipped_by_source = select_migration_pending(pending)
        print(f"\nMigration catch-up selected {len(pending)} newest episodes.")

    opencode_config, delivery_config = load_runtime_configs()
    print(
        f"OpenCode models: {opencode_config.prompt_builder_model} / "
        f"{opencode_config.summarizer_model}"
    )
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    marked_skipped: set[tuple[str, str]] = set()
    for episode in sorted(pending, key=lambda ep: (ep.date_iso, ep.source_id, ep.slug)):
        stage = "Stage 1"
        try:
            decision = prepare_episode(
                episode, state, state_path, opencode_config
            )
            stage = "delivery"
            deliver_prepared_episode(
                episode,
                decision,
                state,
                state_path,
                delivery_config,
            )
            print(f"  -> Sent: {episode.title}")
            sent_count += 1
            warn_transcript_fallback(state_path, episode, run_mode=run_mode)
            for skipped in skipped_by_source.get(episode.source_id, []):
                skip_key = (skipped.source_id, skipped.slug)
                if skip_key in marked_skipped:
                    continue
                marked_skipped.add(skip_key)
                mark_without_delivery(state, skipped, "skipped-migration")
                skipped_count += 1
            save_state(state, state_path)
        except Exception as exc:
            had_errors = True
            failed_count += 1
            if isinstance(exc, EpisodeStageError):
                stage = exc.stage
            print(
                f"  -> ERROR processing {episode.source_name} / {episode.slug}: {exc}",
                file=sys.stderr,
            )
            try:
                notify_error_once(
                    state_path,
                    exc,
                    f"{episode.source_name} / {episode.slug} / {stage} / {run_mode}",
                )
            except Exception as notify_exc:
                print(
                    f"  -> ERROR sending failure notification: {notify_exc.__class__.__name__}",
                    file=sys.stderr,
                )

    if migration_catch_up:
        print(
            f"\nMigration catch-up complete: {sent_count} sent, "
            f"{skipped_count} skipped, {failed_count} failed, "
            f"{collection_errors} collection errors."
        )
    if had_errors:
        print("\nFinished with errors. Check the messages above for details.", file=sys.stderr)
        return 1
    return 0

def main():
    parser = argparse.ArgumentParser(description="Goldman Sachs Podcasts Extractor")
    parser.add_argument(
        "--init",
        action="store_true",
        help="Mark all currently visible episodes without models or delivery.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect metadata and transcripts without models, delivery, or state changes.",
    )
    parser.add_argument(
        "--episode-url",
        help="Preview one episode through both model stages without delivery or state updates.",
    )
    parser.add_argument(
        "--migration-catch-up",
        action="store_true",
        help="Send only the newest pending episode from each podcast and skip older pending episodes.",
    )
    args = parser.parse_args()

    try:
        if args.episode_url:
            return run_single_episode(args.episode_url, dry_run=args.dry_run)

        return run(
            init_only=args.init,
            dry_run=args.dry_run,
            migration_catch_up=(
                args.migration_catch_up
                or os.environ.get("GOLDMAN_PODCASTS_MIGRATION_CATCH_UP") == "1"
            ),
        )
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        load_environment()
        try:
            notify_error_once(get_state_path(), exc, "startup")
        except Exception as notify_exc:
            print(
                f"Error notification failed: {notify_exc.__class__.__name__}",
                file=sys.stderr,
            )
        return 1

if __name__ == "__main__":
    sys.exit(main())
