import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import opencode
import routing


def episode():
    return SimpleNamespace(
        source_name="GS Exchanges",
        title="A Great Investor on Private Markets",
        description="A hedge fund manager discusses investing across cycles.",
        date_iso="2026-09-18",
        transcript_people=["Jane Investor, Chief Investment Officer"],
    )


def test_transcript_sampling_uses_head_middle_and_end():
    transcript = "A" * 100 + "B" * 100 + "C" * 100

    sample = routing.sample_transcript(transcript, head_chars=20, middle_chars=20, end_chars=20)

    assert sample.startswith("A" * 20)
    assert "B" * 20 in sample
    assert sample.endswith("C" * 20)
    assert sample.count(routing.SNIPPET_SEPARATOR) == 2


def test_wrapped_routing_json_is_parsed_and_bounded():
    raw = json.dumps(
        {
            "type": "json_object",
            "response": {
                "episode_type": "investor_interview",
                "summary_lens": "investment_process",
                "recommended_depth": "deep",
                "guest_role": "hedge fund manager",
                "primary_topics": ["a", "b", "c", "d", "e", "ignored"],
                "asset_classes": ["equities"],
                "episode_specific_focus": ["risk management"],
                "sections_to_deemphasize": ["biography"],
                "large_model_prompt": "Focus on investment philosophy and portfolio construction.",
            },
        }
    )

    decision = routing.parse_routing_json(raw)

    assert decision.episode_type == "investor_interview"
    assert decision.summary_lens == "investment_process"
    assert len(decision.primary_topics) == 5
    assert "portfolio construction" in decision.large_model_prompt


def test_routing_template_covers_trading_desk_briefs():
    template = (Path(__file__).parent.parent / "prompts" / "build_summary_prompt.txt").read_text(
        encoding="utf-8"
    )

    assert "trading_desk_brief" in template
    assert "desk" in template.lower()


def test_stage_one_rejects_malformed_or_oversized_prompt_json():
    with pytest.raises(routing.Stage1Error, match="invalid JSON"):
        routing.parse_routing_json('{"large_model_prompt": "prompt",}')

    oversized = json.dumps({"large_model_prompt": "x" * 1501})
    with pytest.raises(routing.Stage1Error, match="1,500"):
        routing.parse_routing_json(oversized)


def valid_routing_json():
    return json.dumps(
        {
            "episode_type": "investor_interview",
            "summary_lens": "investment_process",
            "recommended_depth": "deep",
            "guest_role": "portfolio manager",
            "primary_topics": [],
            "asset_classes": [],
            "episode_specific_focus": [],
            "sections_to_deemphasize": [],
            "large_model_prompt": "Focus on the manager's process.",
        }
    )


def test_stage_one_retries_once_with_repair_demand_on_invalid_json():
    responses = ["{\nnot json}\n" + valid_routing_json(), valid_routing_json()]
    calls = []

    def caller(config, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    config = opencode.OpenCodeConfig("key", prompt_builder_model="deepseek-v4.1-flash")
    decision = routing.route_episode(episode(), "transcript", config, caller=caller)

    assert decision.episode_type == "investor_interview"
    assert len(calls) == 2
    assert "ONLY the JSON object" in calls[1]["messages"][-1]["content"]


def test_stage_one_failure_saves_raw_response_for_inspection(tmp_path, monkeypatch):
    monkeypatch.setattr(routing.tempfile, "gettempdir", lambda: str(tmp_path))
    raw = "{\nnot json}\n"
    config = opencode.OpenCodeConfig("key", prompt_builder_model="deepseek-v4.1-flash")

    with pytest.raises(routing.Stage1Error, match=r"goldman-stage1-failed-.*\.json"):
        routing.route_episode(
            episode(), "transcript", config, caller=lambda *_a, **_k: raw
        )

    saved = list(tmp_path.glob("goldman-stage1-failed-*.json"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == raw


def test_stage_one_uses_flash_json_mode_and_compact_output_limit():
    calls = []

    def caller(config, **kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "episode_type": "investor_interview",
                "summary_lens": "investment_process",
                "recommended_depth": "deep",
                "guest_role": "portfolio manager",
                "primary_topics": [],
                "asset_classes": [],
                "episode_specific_focus": [],
                "sections_to_deemphasize": [],
                "large_model_prompt": "Focus on the manager's process.",
            }
        )

    config = opencode.OpenCodeConfig("key", prompt_builder_model="deepseek-v4.1-flash")
    decision = routing.route_episode(episode(), "transcript", config, caller=caller)

    assert decision.episode_type == "investor_interview"
    assert calls[0]["model"] == "deepseek-v4.1-flash"
    assert calls[0]["json_mode"] is True
    assert calls[0]["max_tokens"] == 5000
