"""Stage 1 routing for Goldman Sachs podcast episodes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable

from opencode import OpenCodeConfig, chat_completion


PROMPT_PATH = Path(__file__).parent / "prompts" / "build_summary_prompt.txt"
TOKEN_RE = re.compile(r"\{\{\$json\.(\w+)\}\}")
HEAD_CHARS = 24_000
MIDDLE_CHARS = 6_000
END_CHARS = 6_000
SNIPPET_SEPARATOR = "\n\n[...transcript continues...]\n\n"
MAX_LARGE_PROMPT_CHARS = 1_500
MAX_LIST_ITEMS = 5


class Stage1Error(RuntimeError):
    pass


REPAIR_MESSAGE = (
    "Your previous response was not a parseable JSON object. "
    "Return ONLY the JSON object now: no preamble, no explanation, no code fences."
)
JSON_STRUCTURE_MARKERS = ("invalid JSON", "no JSON object")


def save_failed_stage_one_response(raw_response: str) -> Path:
    raw_path = Path(tempfile.gettempdir()) / (
        "goldman-stage1-failed-" f"{os.getpid()}-{int(time.time())}.json"
    )
    raw_path.write_text(raw_response, encoding="utf-8")
    return raw_path


@dataclass(frozen=True)
class RoutingDecision:
    episode_type: str
    summary_lens: str
    recommended_depth: str
    guest_role: str
    confidence: float
    primary_topics: tuple[str, ...]
    asset_classes: tuple[str, ...]
    episode_specific_focus: tuple[str, ...]
    sections_to_deemphasize: tuple[str, ...]
    large_model_prompt: str
    raw_response: str


def sample_transcript(
    transcript: str,
    head_chars: int = HEAD_CHARS,
    middle_chars: int = MIDDLE_CHARS,
    end_chars: int = END_CHARS,
) -> str:
    total = len(transcript)
    if total <= head_chars + middle_chars + end_chars:
        return transcript
    middle_start = max(head_chars, (total - middle_chars) // 2)
    return (
        transcript[:head_chars]
        + SNIPPET_SEPARATOR
        + transcript[middle_start : middle_start + middle_chars]
        + SNIPPET_SEPARATOR
        + transcript[-end_chars:]
    )


def build_routing_prompt(episode, transcript: str, path: Path = PROMPT_PATH) -> str:
    template = path.read_text(encoding="utf-8")
    values = {
        "podcast_name": episode.source_name,
        "title": episode.title,
        "description": getattr(episode, "description", ""),
        "publication_date": episode.date_iso,
        "people": ", ".join(episode.transcript_people or []),
        "transcript_snippet": sample_transcript(transcript),
    }

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise Stage1Error(f"Missing Stage 1 template value: {key}")
        return str(values[key])

    return TOKEN_RE.sub(replace, template)


def route_episode(
    episode,
    transcript: str,
    config: OpenCodeConfig,
    *,
    caller: Callable = chat_completion,
) -> RoutingDecision:
    messages = [{"role": "user", "content": build_routing_prompt(episode, transcript)}]
    try:
        response = caller(
            config,
            model=config.prompt_builder_model,
            messages=messages,
            json_mode=True,
            temperature=0.0,
            max_tokens=5000,
            timeout=300,
        )
    except Exception as exc:
        raise Stage1Error(f"Stage 1 model call failed: {exc}") from exc
    try:
        return parse_routing_json(response)
    except Stage1Error as exc:
        if not any(marker in str(exc) for marker in JSON_STRUCTURE_MARKERS):
            raise
    try:
        retry = caller(
            config,
            model=config.prompt_builder_model,
            messages=messages + [{"role": "user", "content": REPAIR_MESSAGE}],
            json_mode=True,
            temperature=0.0,
            max_tokens=5000,
            timeout=300,
        )
    except Exception as exc:
        raise Stage1Error(f"Stage 1 model call failed: {exc}") from exc
    return parse_routing_json(retry)


def parse_routing_json(raw_response: str) -> RoutingDecision:
    text = raw_response.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raw_path = save_failed_stage_one_response(raw_response)
        raise Stage1Error(
            "Stage 1 response contains no JSON object. "
            f"Raw response saved to {raw_path}"
        )
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raw_path = save_failed_stage_one_response(raw_response)
        raise Stage1Error(
            f"Stage 1 returned invalid JSON at line {exc.lineno}, column {exc.colno}. "
            f"Raw response saved to {raw_path}"
        ) from exc
    if not isinstance(data, dict):
        raise Stage1Error("Stage 1 JSON must be an object")
    if isinstance(data.get("response"), dict):
        data = data["response"]
    prompt = str(data.get("large_model_prompt", "")).strip()
    if not prompt:
        raise Stage1Error("Stage 1 response is missing large_model_prompt")
    if len(prompt) > MAX_LARGE_PROMPT_CHARS:
        raise Stage1Error("Stage 1 large_model_prompt exceeds 1,500 characters")

    def items(name: str) -> tuple[str, ...]:
        value = data.get(name, [])
        if not isinstance(value, list):
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())[:MAX_LIST_ITEMS]

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return RoutingDecision(
        episode_type=str(data.get("episode_type", "other")).strip() or "other",
        summary_lens=str(data.get("summary_lens", "")).strip(),
        recommended_depth=str(data.get("recommended_depth", "normal")).strip() or "normal",
        guest_role=str(data.get("guest_role", "")).strip(),
        confidence=confidence,
        primary_topics=items("primary_topics"),
        asset_classes=items("asset_classes"),
        episode_specific_focus=items("episode_specific_focus"),
        sections_to_deemphasize=items("sections_to_deemphasize"),
        large_model_prompt=prompt,
        raw_response=raw_response,
    )
