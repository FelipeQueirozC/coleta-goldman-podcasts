import json

import main


def test_state_preserves_error_notifications_and_partial_delivery(tmp_path):
    path = tmp_path / "state.json"
    state = main.empty_state()
    state["error_notifications"] = {"fingerprint": {"message_id": "42"}}
    state["sent"]["exchanges"]["episode"] = {"status": "prepared"}

    main.save_state(state, path)
    loaded = main.load_state(path)

    assert loaded["error_notifications"]["fingerprint"]["message_id"] == "42"
    assert not main.was_sent(loaded, "exchanges", "episode")
    loaded["sent"]["exchanges"]["episode"]["status"] = "sent"
    assert main.was_sent(loaded, "exchanges", "episode")
    assert not path.with_suffix(".tmp").exists()
    json.loads(path.read_text(encoding="utf-8"))
