import json

import opencode


def test_selects_latest_flash_and_pro_without_vision_models():
    models = [
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-v4.1-flash",
        "deepseek-v4-pro",
        "deepseek-v4.2-pro",
    ]

    assert opencode.latest_deepseek_model(models, "flash") == "deepseek-v4.1-flash"
    assert opencode.latest_deepseek_model(models, "pro") == "deepseek-v4.2-pro"


def test_model_discovery_uses_current_fallbacks_when_variants_are_missing(monkeypatch):
    monkeypatch.setattr(opencode, "list_models", lambda _config: ("other-model",))
    config = opencode.OpenCodeConfig("key")

    resolved = opencode.resolve_models(config)

    assert resolved.prompt_builder_model == "deepseek-v4.1-flash"
    assert resolved.summarizer_model == "deepseek-v4-pro"


def test_chat_completion_sends_stable_session_header(monkeypatch):
    calls = []

    def post_json(url, headers, payload, **_kwargs):
        calls.append((url, headers, payload))
        return json.dumps({"choices": [{"message": {"content": "summary"}}]})

    monkeypatch.setattr(opencode, "post_json", post_json)
    config = opencode.OpenCodeConfig(
        api_key="secret",
        base_url="https://example.com/v1",
        prompt_builder_model="deepseek-v4.1-flash",
        summarizer_model="deepseek-v4-pro",
    )
    for _ in range(2):
        assert opencode.chat_completion(
            config,
            model=config.summarizer_model,
            messages=[{"role": "user", "content": "test"}],
            reasoning_effort="high",
        ) == "summary"

    assert calls[0][1]["User-Agent"] == "coleta-goldman-podcasts/0.1"
    assert len(calls[0][1]["x-opencode-session"]) == 64
    assert calls[0][1]["x-opencode-session"] == calls[1][1]["x-opencode-session"]
    assert "max_tokens" not in calls[0][2]
