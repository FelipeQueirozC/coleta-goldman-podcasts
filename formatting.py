"""Kinea-style safe Markdown and delivery formatting."""

from __future__ import annotations

from html import escape
import re
import unicodedata


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
HR_RE = re.compile(r"^\s*-{3,}\s*$")


def markdown_to_safe_html(text: str) -> str:
    out: list[str] = []
    paragraph: list[str] = []
    list_open = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            joined = " ".join(paragraph).strip()
            if joined:
                out.append(f"<p>{inline_markdown(joined)}</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        heading = HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            close_list()
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{inline_markdown(heading.group(2).strip())}</h{level}>")
            continue
        if HR_RE.match(line):
            flush_paragraph()
            close_list()
            out.append("<hr>")
            continue
        bullet = BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{inline_markdown(bullet.group(1).strip())}</li>")
            continue
        close_list()
        paragraph.append(line.strip())

    flush_paragraph()
    close_list()
    return "\n".join(out)


def inline_markdown(text: str) -> str:
    return BOLD_RE.sub(r"<strong>\1</strong>", escape(text))


def humanize(value: str) -> str:
    return value.replace("_", " ").strip()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "episode"


def attachment_basename(episode) -> str:
    return f"{episode.date_iso or 'unknown-date'}-{episode.source_id}-{slugify(episode.title)}"


def markdown_filename(episode) -> str:
    return attachment_basename(episode) + ".md"


def html_filename(episode) -> str:
    return attachment_basename(episode) + ".html"


def build_email_subject(episode) -> str:
    return f"{episode.date_iso or 'Unknown date'} {episode.source_name}: {episode.title}"


def metadata_html(episode, routing) -> str:
    parts = [
        f"<strong>Podcast:</strong> {escape(episode.source_name)}",
        f"<strong>Published:</strong> {escape(episode.date_iso or 'Unknown')}",
        f"<strong>Type:</strong> {escape(humanize(routing.episode_type))}",
        f"<strong>Lens:</strong> {escape(humanize(routing.summary_lens))}",
        f"<strong>Depth:</strong> {escape(routing.recommended_depth)}",
        f"<strong>Routing confidence:</strong> {routing.confidence:.0%}",
    ]
    if routing.guest_role:
        parts.append(f"<strong>Guest role:</strong> {escape(routing.guest_role)}")
    return " &middot; ".join(parts)


def build_email_text(episode, summary: str, routing) -> str:
    return (
        f"{episode.title}\n\n"
        f"Podcast: {episode.source_name}\n"
        f"Published: {episode.date_iso or 'Unknown'}\n"
        f"Type: {humanize(routing.episode_type)}\n"
        f"Lens: {humanize(routing.summary_lens)}\n\n"
        f"{summary.strip()}\n\n"
        f"Episode: {episode.url}\n"
    )


def build_email_html(episode, summary: str, routing) -> str:
    links = f'<p><a href="{escape(episode.url, quote=True)}">Open episode page</a>'
    if episode.youtube_url:
        links += f' &middot; <a href="{escape(episode.youtube_url, quote=True)}">Watch on YouTube</a>'
    links += "</p>"
    return (
        '<html><body style="font-family: -apple-system, sans-serif; line-height: 1.5; '
        'max-width: 720px; margin: auto; padding: 0 16px;">\n'
        f'<h1 style="font-size: 1.4em;">{escape(episode.title)}</h1>\n'
        f'<p style="color: #666;">{metadata_html(episode, routing)}</p>\n'
        f"{links}\n"
        f"{markdown_to_safe_html(summary)}\n"
        '<hr><p style="color: #888; font-size: 0.85em;">'
        "The full transcript is attached as a Markdown document.</p>\n"
        "</body></html>"
    )


def build_markdown_attachment(episode, summary: str, routing) -> str:
    people = ", ".join(episode.transcript_people or []) or "Unknown"
    return (
        f"# {episode.title}\n\n"
        f"**Podcast:** {episode.source_name}\n\n"
        f"**Published:** {episode.date_iso or 'Unknown'}\n\n"
        f"**Episode URL:** {episode.url}\n\n"
        f"**Transcript source:** {episode.transcript_source}\n\n"
        f"**People:** {people}\n\n"
        f"**Episode type:** {humanize(routing.episode_type)}\n\n"
        f"**Summary lens:** {humanize(routing.summary_lens)}\n\n"
        f"{summary.strip()}\n\n"
        "## Full Transcript\n\n"
        f"{episode.transcript_text.strip()}\n"
    )


def build_html_attachment(episode, summary: str, routing) -> str:
    links = f'<a href="{escape(episode.url, quote=True)}">Episode page</a>'
    if episode.youtube_url:
        links += f' &middot; <a href="{escape(episode.youtube_url, quote=True)}">YouTube</a>'
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(episode.title)}</title></head>"
        '<body style="font-family: -apple-system, sans-serif; font-size: 17px; line-height: 1.5; '
        'max-width: 720px; margin: 2em auto; padding: 0 1em; color: #222;">\n'
        f"<h1>{escape(episode.title)}</h1>\n"
        f'<p style="color: #666;">{metadata_html(episode, routing)}</p>\n'
        f"<p>{links}</p>\n"
        f"{markdown_to_safe_html(summary)}\n"
        "<hr><h2>Full Transcript</h2>\n"
        '<pre style="white-space: pre-wrap; font-family: ui-monospace, monospace; '
        'font-size: 0.85em; background: #f6f6f6; padding: 1em; border-radius: 6px;">'
        f"{escape(episode.transcript_text)}</pre>\n"
        "</body></html>"
    )


def first_substantive_line(summary: str) -> str:
    for raw in summary.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line.lstrip("-* ")
    return ""


def build_telegram_message(episode, summary: str, routing) -> str:
    takeaway = first_substantive_line(summary) or "Full summary and transcript are attached below."
    return (
        f"<b>{escape(episode.title)}</b>\n"
        f"<i>{escape(episode.source_name)} · {escape(episode.date_iso or 'Unknown')}</i>\n\n"
        f"{escape(takeaway)}\n\n"
        f"<i>Type: {escape(humanize(routing.episode_type))} · "
        f"confidence: {routing.confidence:.0%}</i>"
    )


def build_telegram_caption(episode, routing) -> str:
    return (
        f"Full summary and transcript: {episode.title} "
        f"({episode.source_name}, {episode.date_iso or 'Unknown'})"
    )
