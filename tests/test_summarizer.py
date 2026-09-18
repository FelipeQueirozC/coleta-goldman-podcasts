from types import SimpleNamespace

import pytest

import opencode
import summarizer


def test_stage_two_uses_tailored_prompt_and_full_transcript():
    calls = []

    output = "\n\n".join(
        f"## {heading}\n\nContent"
        for heading in summarizer.REQUIRED_HEADERS
    )

    def caller(config, **kwargs):
        calls.append(kwargs)
        return output

    config = opencode.OpenCodeConfig("key", summarizer_model="deepseek-v4.2-pro")
    routing = SimpleNamespace(large_model_prompt="Focus on portfolio construction.")
    transcript = "x" * 30_000

    result = summarizer.summarize(routing, transcript, config, caller=caller)

    assert result.startswith("## Key Takeaway")
    assert calls[0]["model"] == "deepseek-v4.2-pro"
    assert calls[0]["messages"][1]["content"] == routing.large_model_prompt
    assert calls[0]["messages"][2]["content"].endswith(transcript)
    assert "## Investor Interpretation" in calls[0]["messages"][0]["content"]
    assert "max_tokens" not in calls[0]


def test_stage_two_rejects_missing_required_headings():
    config = opencode.OpenCodeConfig("key", summarizer_model="deepseek-v4-pro")
    routing = SimpleNamespace(large_model_prompt="Focus on markets.")

    with pytest.raises(RuntimeError, match="missing required heading"):
        summarizer.summarize(
            routing,
            "transcript",
            config,
            caller=lambda *_args, **_kwargs: "## Key Takeaway\n\nOnly one section",
        )
