from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

STATE_PATH = Path(__file__).with_name("sent_documents.json")
OUTPUT_DIR = Path(__file__).parent / "output"
BASE_URL = "https://www.goldmansachs.com"

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
GOOGLE_MODEL = "gemini-3.5-flash"
GOOGLE_INIT_MIN_SECONDS_BETWEEN_REQUESTS = 2.2

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
    }
]

SOURCE_IDS = {source["id"] for source in SOURCES}

@dataclass
class Episode:
    source_id: str
    source_name: str
    slug: str
    url: str
    title: str = ""
    date_iso: str = ""
    youtube_url: str = ""
    pdf_url: str = ""
    transcript_series: str = ""
    transcript_title: str = ""
    transcript_people: list[str] | None = None
    recording_date: str = ""
    transcript_text: str = ""
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

def load_state() -> dict:
    if not STATE_PATH.exists():
        return empty_state()

    state_text = STATE_PATH.read_text(encoding="utf-8").strip()
    if not state_text:
        return empty_state()

    return normalize_state(json.loads(state_text))

def save_state(state: dict) -> None:
    state = normalize_state(state)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def was_sent(state: dict, source_id: str, slug: str) -> bool:
    """Check this specific podcast and episode slug."""
    return slug in state.get("sent", {}).get(source_id, {})

def mark_sent(state: dict, episode: Episode, email_id: str) -> None:
    """Remember that this episode was handled so future runs skip it."""
    source_records = state.setdefault("sent", {}).setdefault(episode.source_id, {})
    source_records[episode.slug] = {
        "title": episode.title,
        "date_iso": episode.date_iso,
        "email_id": email_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }

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

def source_and_slug_from_episode_url(episode_url: str) -> tuple[dict, str]:
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
                if date_iso:
                    break

    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if "youtube.com" in href or "youtu.be" in href:
            youtube_url = href
            break

    return {"title": title, "date_iso": date_iso, "youtube_url": youtube_url}

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

def build_summary_prompts(source_id: str, source_name: str, title: str, transcript: str) -> tuple[str, str]:
    if source_id == "exchanges":
        system_prompt = (
            "You are a Senior Global Macro Research Analyst. Summarize Goldman Sachs Exchanges "
            "transcripts for a busy Portfolio Manager. Use professional, concise, and technical "
            "language. Prioritize non-consensus views, specific market stressors, and structural "
            "shifts. Avoid generic introductory remarks or biographical fluff unless it directly "
            "informs an investment strategy."
        )
        user_prompt = (
            "Summarize the attached Goldman Sachs Exchanges transcript.\n\n"
            "Use exactly these standalone markdown-style bold headers and keep the response concise:\n"
            "**THE BOTTOM LINE**\n"
            "Provide a three-sentence maximum distillation of the absolute core takeaway of this episode.\n\n"
            "**MARKET AND MACRO REGIME**\n"
            "Identify the guest's current view on the macro environment, specifically focusing on "
            "central bank hawkishness versus dovishness, inflation paths, and the tension between "
            "growth and spot reality.\n\n"
            "**HIGH-CONVICTION THEMES**\n"
            "Extract the three or four specific investment themes, sectors, or geographic regions "
            "discussed, such as AI data center capex, Japanese corporate governance, or specific "
            "credit sub-sectors.\n\n"
            "**TAIL RISKS AND VULNERABILITIES**\n"
            "Define the specific downside risks the guest believes are currently underpriced or "
            "misunderstood by the broader market, including geopolitical blockades or credit cycle defaults.\n\n"
            "**TACTICAL PLAYBOOK**\n"
            "List the actionable takeaways for a portfolio, specifically noting where to add risk, "
            "where to hedge aggressively, and which assets to look through during short-term volatility.\n\n"
            "**INVESTMENT DNA**\n"
            "If this is a Great Investor episode, summarize the core operational philosophy and "
            "risk management framework used by the guest's firm. If it is not relevant, write "
            "'Not applicable.'\n\n"
            "- Include only facts supported by the transcript.\n"
            "- Use short bullets under each section after THE BOTTOM LINE.\n"
            f"Title: {title}\n\n"
            "Transcript:\n"
            f"{transcript[:24000]}"
        )
        return system_prompt, user_prompt

    system_prompt = (
        "You are an expert Investment Strategist. Distill complex market discussions into high-signal, "
        "actionable summaries for institutional investors. Be concise, professional, and factual. "
        "Return plain text using standalone markdown-style bold headers and flat bullet points. "
        "Prioritize signal over noise and do not invent facts."
    )
    user_prompt = (
        f"Analyze the provided transcript from the Goldman Sachs '{source_name}' podcast and generate a "
        "concise structured summary.\n\n"
        "Requirements:\n"
        "- Use exactly these standalone headers: **Executive Overview**, **Backdrop**, "
        "**Quantitative Evidence**, **Key Themes**, **Tactical Playbook**, **Contrarian Insights**.\n"
        "- Under each relevant header, use short bullet points.\n"
        "- Include only facts supported by the transcript.\n"
        f"Title: {title}\n\n"
        "Transcript:\n"
        f"{transcript[:24000]}"
    )
    return system_prompt, user_prompt

def summarize_transcript(source_id: str, source_name: str, title: str, transcript: str) -> str:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("Warning: GOOGLE_API_KEY not found. Skipping summary.")
        return "Summary not available (Missing API Key)."
    
    if not transcript:
        return "No transcript text available to summarize."

    system_prompt, user_prompt = build_summary_prompts(source_id, source_name, title, transcript)

    try:
        response = requests.post(
            f"{GOOGLE_API_URL}?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": f"System: {system_prompt}\n\nUser: {user_prompt}"}]}
                ],
                "generationConfig": {
                    "temperature": 0.2,
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except requests.exceptions.RequestException as e:
        print(f"Warning: Summary generation failed ({e})")
        return f"Summary generation failed: {e}"

def wait_before_init_summary(last_request_at: float) -> float:
    """Slow down the first bulk run so Google API does not reject requests."""
    elapsed = time.monotonic() - last_request_at
    wait_seconds = GOOGLE_INIT_MIN_SECONDS_BETWEEN_REQUESTS - elapsed
    if wait_seconds > 0:
        print(f"     Init mode: waiting {wait_seconds:.1f}s before the next Google summary.")
        time.sleep(wait_seconds)
    return time.monotonic()

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

def recording_date_to_iso(recording_date: str) -> str:
    if not recording_date:
        return ""
    try:
        return datetime.strptime(recording_date, "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""

def generate_markdown(episode: Episode) -> str:
    metadata_lines = [
        "## Metadata",
        f"- Source: {episode.source_name}",
    ]
    if episode.transcript_title and episode.transcript_title != episode.title:
        metadata_lines.append(f"- Transcript Title: {episode.transcript_title}")
    if episode.transcript_series and episode.transcript_series != episode.source_name:
        metadata_lines.append(f"- Transcript Series: {episode.transcript_series}")
    if episode.recording_date:
        metadata_lines.append(f"- Recording Date: {episode.recording_date}")
    metadata_lines.extend([
        f"- Published Date: {episode.date_iso}",
        f"- URL: {episode.url}",
    ])
    if episode.pdf_url:
        metadata_lines.append(f"- Transcript PDF: {episode.pdf_url}")
    if episode.transcript_people:
        metadata_lines.append("- Transcript Guests / Speakers:")
        metadata_lines.extend(f"  - {person}" for person in episode.transcript_people)
    metadata = "\n".join(metadata_lines)

    return (
        f"# {episode.title}\n\n"
        f"{metadata}\n\n"
        f"## Summary\n"
        f"{episode.summary}\n\n"
        f"## Transcript\n"
        f"{episode.transcript_text}"
    )

def episode_date_prefix(episode: Episode) -> str:
    return episode.date_iso or "unknown-date"

def save_local_markdown(episode: Episode, md_content: str):
    source_dir = OUTPUT_DIR / episode.source_id
    source_dir.mkdir(parents=True, exist_ok=True)
    
    file_path = source_dir / f"{episode_date_prefix(episode)}_{episode.slug}.md"
    file_path.write_text(md_content, encoding="utf-8")
    print(f"  -> Saved local markdown: {file_path}")

# Not currently used
def email_source_label(source_name: str) -> str:
    """Use a compact podcast name in email subjects."""
    short_name = source_name.removeprefix("Goldman Sachs ").strip()
    return f"GS {short_name}"

def send_email(episode: Episode, sender_prefix: str, md_content: str) -> str:
    import resend
    resend.api_key = os.environ.get("RESEND_API_KEY")
    if not resend.api_key:
        raise RuntimeError("Missing RESEND_API_KEY")
    
    domain = os.environ.get("RESEND_FROM_DOMAIN", "example.com").strip("@")
    from_email = f"{sender_prefix}@{domain}"
    subject = f"{episode_date_prefix(episode)} {episode.source_name}: {episode.title}"
    
    # Build a simple email body and attach the markdown transcript for reference.
    html_lines = [
        f"<h2>{html.escape(episode.source_name)}: {html.escape(episode.title)}</h2>",
        f"<p><strong>Published:</strong> {episode.date_iso}</p>",
        f"<p><strong>Link:</strong> <a href='{episode.url}'>Listen Here</a></p>"
    ]
    if episode.youtube_url:
        html_lines.append(f"<p><strong>YouTube:</strong> <a href='{episode.youtube_url}'>Watch Here</a></p>")
    
    html_lines.append("<h3>Summary</h3>")
    # Convert the summary's simple markdown style into basic email HTML.
    for line in episode.summary.split("\n"):
        if line.startswith("**") and line.endswith("**"):
            html_lines.append(f"<h4>{html.escape(line.strip('*'))}</h4>")
        elif line.startswith("- "):
            html_lines.append(f"<li>{html.escape(line[2:])}</li>")
        elif line.strip():
            html_lines.append(f"<p>{html.escape(line)}</p>")
    
    attachments = [
        {
            "filename": f"{episode_date_prefix(episode)}_{episode.slug}_transcript.md",
            "content": base64.b64encode(md_content.encode('utf-8')).decode('ascii'),
        }
    ]

    params = {
        "from": from_email,
        "to": [t.strip() for t in os.environ.get("RESEND_TO_EMAIL", "").split(",") if t.strip()],
        "subject": subject,
        "html": "".join(html_lines),
        "attachments": attachments
    }

    resp = resend.Emails.send(params)
    return str(resp.get("id") or getattr(resp, "id", "unknown_id"))

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
    transcript_text = ""

    if pdf_bytes:
        transcript_text = extract_pdf_text(pdf_bytes)
    else:
        tried_list = ", ".join(tried_pdf_urls)
        print(f"     Warning: transcript PDF not found or invalid. Tried: {tried_list}")
    transcript_header = parse_transcript_header(transcript_text)
    transcript_text = reflow_transcript_text(strip_transcript_header(transcript_text))
    date_iso = meta["date_iso"] or recording_date_to_iso(transcript_header.get("recording_date", ""))

    episode = Episode(
        source_id=source["id"],
        source_name=source["name"],
        slug=slug,
        url=episode_url,
        title=meta["title"] or slug,
        date_iso=date_iso,
        youtube_url=meta["youtube_url"],
        pdf_url=pdf_url,
        transcript_series=transcript_header.get("series", ""),
        transcript_title=transcript_header.get("title", ""),
        transcript_people=transcript_header.get("people", []),
        recording_date=transcript_header.get("recording_date", ""),
        transcript_text=transcript_text,
    )
    return episode, pdf_bytes

def process_episode(
    episode: Episode,
    source: dict,
    pdf_bytes: bytes,
    *,
    init_only: bool,
    dry_run: bool,
) -> tuple[str, str]:
    if dry_run:
        print(f"     Dry run: would summarize, save, and send '{episode.title}'.")
        return "", ""

    episode.summary = summarize_transcript(
        episode.source_id,
        episode.source_name,
        episode.title,
        episode.transcript_text,
    )

    md_content = generate_markdown(episode)
    save_local_markdown(episode, md_content)

    if init_only:
        print(f"     Init mode: marked '{episode.title}' as sent without emailing it.")
        return "init_skip", md_content

    email_id = send_email(episode, source["sender_prefix"], md_content)
    print(f"     Sent email {email_id} for '{episode.title}'.")
    return email_id, md_content

def run_single_episode(episode_url: str, dry_run: bool) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    source, slug = source_and_slug_from_episode_url(episode_url)
    print(f"Testing one episode from {source['name']}: {slug}")

    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; GoldmanExtractor/1.0)"})
        episode, pdf_bytes = collect_episode(session, source, slug)
        process_episode(episode, source, pdf_bytes, init_only=False, dry_run=dry_run)

    if not dry_run:
        print("\nSingle-episode test complete. State file was not updated.")
    return 0

def run(init_only: bool, dry_run: bool) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    state = load_state()
    new_episodes_found = False
    had_errors = False
    last_groq_request_at = 0.0

    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; GoldmanExtractor/1.0)"})

        for source in SOURCES:
            source_id = source["id"]
            print(f"\nChecking podcast: {source['name']}")
            try:
                listing_html = fetch_dynamic_html(source["listing_url"])
                slugs = discover_slugs(listing_html, source["path_prefix"])
            except Exception as e:
                had_errors = True
                print(f"  -> ERROR reading listing for {source['name']}: {e}", file=sys.stderr)
                continue

            print(f"  -> Found {len(slugs)} episode links on the listing page.")
            
            for slug in slugs:
                if was_sent(state, source_id, slug):
                    print(f"  -> Already handled: {slug}")
                    continue
                
                new_episodes_found = True
                episode_url = urljoin(BASE_URL, f"{source['path_prefix']}{slug}")
                print(f"  -> New episode found: {slug}")
                
                try:
                    ep, pdf_bytes = collect_episode(session, source, slug)
                    if init_only and not dry_run and ep.transcript_text and os.environ.get("GOOGLE_API_KEY"):
                        last_groq_request_at = wait_before_init_summary(last_groq_request_at)
                    email_id, _ = process_episode(
                        ep,
                        source,
                        pdf_bytes,
                        init_only=init_only,
                        dry_run=dry_run,
                    )
                    if dry_run:
                        continue

                    # Save progress after each successful episode.
                    mark_sent(state, ep, email_id)
                    save_state(state)

                except Exception as e:
                    had_errors = True
                    print(f"  -> ERROR processing {source['name']} / {slug}: {e}", file=sys.stderr)

    if init_only:
        save_state(state)
        print("\nInitialization complete. State file and local markdown files populated.")
    elif not new_episodes_found and not had_errors:
        print("\nNo new episodes found.")

    if had_errors:
        print("\nFinished with errors. Check the messages above for details.", file=sys.stderr)
        return 1

    return 0

def main():
    parser = argparse.ArgumentParser(description="Goldman Sachs Podcasts Extractor")
    parser.add_argument("--init", action="store_true", help="Download, summarize, and save all current episodes locally without sending emails.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch metadata but do not call Groq, save files, send emails, or update state.")
    parser.add_argument("--episode-url", help="Process exactly one episode URL for testing. Sends email unless --dry-run is also used. Does not update sent_documents.json.")
    args = parser.parse_args()

    if args.episode_url:
        return run_single_episode(args.episode_url, dry_run=args.dry_run)

    return run(init_only=args.init, dry_run=args.dry_run)

if __name__ == "__main__":
    sys.exit(main())
