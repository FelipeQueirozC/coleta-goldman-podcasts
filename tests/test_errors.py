import main


def test_same_sanitized_error_is_sent_once(tmp_path):
    calls = []
    env = {
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "TELEGRAM_ERROR_CHAT_ID": "error-chat",
        "OPENCODE_API_KEY": "opencode-secret",
    }

    def sender(_token, _chat_id, message):
        calls.append(message)
        return "42"

    path = tmp_path / "state.json"
    error = "OpenCode rejected opencode-secret and telegram-secret"

    assert main.notify_error_once(path, error, "GS Exchanges / sample / Stage 1", env=env, sender=sender)
    assert not main.notify_error_once(path, error, "GS Exchanges / sample / Stage 1", env=env, sender=sender)
    assert len(calls) == 1
    assert "opencode-secret" not in calls[0]
    assert "telegram-secret" not in calls[0]
    assert "GS Exchanges / sample / Stage 1" in calls[0]
