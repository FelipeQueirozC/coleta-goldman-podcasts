"""Stage 2 English investor-summary generation."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from opencode import OpenCodeConfig, chat_completion


SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "summarize_system.txt"
REQUIRED_HEADERS = (
    "Key Takeaway",
    "Narrative Summary",
    "Investor Interpretation",
    "Key Risks and Open Questions",
    "What to Monitor",
    "Best Insights",
    "Relevance",
)


def summarize(
    routing,
    transcript: str,
    config: OpenCodeConfig,
    *,
    caller: Callable = chat_completion,
) -> str:
    if not transcript.strip():
        raise RuntimeError("Transcript is empty; refusing to create a summary")
    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    result = caller(
        config,
        model=config.summarizer_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": routing.large_model_prompt},
            {"role": "user", "content": f"# Full transcript\n\n{transcript}"},
        ],
        reasoning_effort="high",
        temperature=0.0,
        timeout=1800,
    )
    text = result.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    for heading in REQUIRED_HEADERS:
        if f"## {heading}" not in text:
            raise RuntimeError(f"Stage 2 summary is missing required heading: {heading}")
    return text
