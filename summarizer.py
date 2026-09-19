"""Stage 2 English investor-summary generation."""

from __future__ import annotations

import os
import tempfile
import time
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
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": routing.large_model_prompt},
        {"role": "user", "content": f"# Full transcript\n\n{transcript}"},
    ]
    text = clean_stage_two_output(
        caller(
            config,
            model=config.summarizer_model,
            messages=messages,
            reasoning_effort="high",
            temperature=0.0,
            timeout=1800,
        )
    )
    missing = missing_stage_two_headings(text)
    if missing:
        text = clean_stage_two_output(
            caller(
                config,
                model=config.summarizer_model,
                messages=messages
                + [
                    {
                        "role": "user",
                        "content": "The previous response is missing these required "
                        f"Markdown sections: {', '.join(missing)}. Return the "
                        "complete memo again with every required section in order.",
                    }
                ],
                reasoning_effort="high",
                temperature=0.0,
                timeout=1800,
            )
        )
        missing = missing_stage_two_headings(text)
    if missing:
        raw_path = Path(tempfile.gettempdir()) / (
            "goldman-stage2-failed-"
            f"{os.getpid()}-{int(time.time())}.md"
        )
        raw_path.write_text(text, encoding="utf-8")
        raise RuntimeError(
            f"Stage 2 summary is missing required heading: {missing[0]}. "
            f"Raw output saved to {raw_path}"
        )
    return text


def clean_stage_two_output(result: str) -> str:
    text = result.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def missing_stage_two_headings(text: str) -> list[str]:
    return [heading for heading in REQUIRED_HEADERS if f"## {heading}" not in text]
