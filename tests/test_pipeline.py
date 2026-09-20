from pathlib import Path
from types import SimpleNamespace

import pytest

import delivery
import main
import opencode
import routing


def test_prepare_episode_runs_both_stages_once_and_saves_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path / "output")
    episode = main.Episode(
        source_id="exchanges",
        source_name="GS Exchanges",
        slug="sample",
        url="https://example.com",
        title="Sample",
        description="Description",
        date_iso="2026-09-18",
        transcript_source="inline_html",
        transcript_text="Full transcript",
    )
    decision = routing.RoutingDecision(
        episode_type="investor_interview",
        summary_lens="investment_process",
        recommended_depth="deep",
        guest_role="portfolio manager",
        confidence=0.9,
        primary_topics=(),
        asset_classes=(),
        episode_specific_focus=(),
        sections_to_deemphasize=(),
        large_model_prompt="Focus on process.",
        raw_response="{}",
    )
    calls = []

    def router(*_args):
        calls.append("route")
        return decision

    def summarize(*_args):
        calls.append("summarize")
        return "## Key Takeaway\n\nSummary"

    state = main.empty_state()
    state_path = tmp_path / "state.json"
    config = opencode.OpenCodeConfig(
        "key", prompt_builder_model="deepseek-v4.1-flash", summarizer_model="deepseek-v4-pro"
    )

    result = main.prepare_episode(
        episode, state, state_path, config, router=router, summary_writer=summarize
    )
    main.prepare_episode(
        episode,
        state,
        state_path,
        config,
        router=lambda *_args: (_ for _ in ()).throw(AssertionError("rerouted")),
        summary_writer=lambda *_args: (_ for _ in ()).throw(AssertionError("resummarized")),
    )

    record = state["sent"]["exchanges"]["sample"]
    assert result.episode_type == "investor_interview"
    assert calls == ["route", "summarize"]
    assert Path(record["markdown_path"]).is_file()
    assert Path(record["html_path"]).is_file()
    assert record["flash_model"] == "deepseek-v4.1-flash"
    assert record["pro_model"] == "deepseek-v4-pro"


def test_migration_selects_newest_episode_per_source():
    episodes = [
        main.Episode("the_markets", "GS The Markets", "old", "u", date_iso="2026-09-01"),
        main.Episode("the_markets", "GS The Markets", "new", "u", date_iso="2026-09-18"),
        main.Episode("exchanges", "GS Exchanges", "x-old", "u", date_iso="2026-09-09"),
        main.Episode("exchanges", "GS Exchanges", "x-new", "u", date_iso="2026-09-18"),
    ]

    selected, skipped = main.select_migration_pending(episodes)

    assert {episode.slug for episode in selected} == {"new", "x-new"}
    assert {episode.slug for values in skipped.values() for episode in values} == {"old", "x-old"}


def run_with_fakes(monkeypatch, tmp_path, slugs=("old", "new"), collect_failures=(), prepare_failures=()):
    from types import SimpleNamespace as NS

    notifications = []
    monkeypatch.setattr(main, "load_environment", lambda: None)
    monkeypatch.setattr(main, "get_state_path", lambda: tmp_path / "state.json")

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(main.requests, "Session", FakeSession)
    monkeypatch.setattr(main, "fetch_dynamic_html", lambda _url: "<html></html>")
    monkeypatch.setattr(main, "discover_slugs", lambda _html, _prefix: list(slugs))
    monkeypatch.setattr(main, "discover_youtube_cards", lambda _html: [])

    def collect(_session, source, slug):
        if slug in collect_failures:
            raise RuntimeError("transcript missing")
        dates = {"old": "2026-09-01", "new": "2026-09-18"}
        return (
            main.Episode(
                source["id"],
                source["name"],
                slug,
                f"https://example.com/{slug}",
                title=slug,
                date_iso=dates.get(slug, "2026-09-18"),
                transcript_source="inline_html",
                transcript_text="text",
            ),
            b"",
        )

    monkeypatch.setattr(main, "collect_episode", collect)
    monkeypatch.setattr(
        main,
        "load_runtime_configs",
        lambda: (NS(prompt_builder_model="f", summarizer_model="p"), object()),
    )

    def prepare(episode, _state, _path, _config):
        if (episode.source_id, episode.slug) in prepare_failures:
            raise main.EpisodeStageError("Stage 1", "bad json")
        return NS(episode_type="market_brief")

    monkeypatch.setattr(main, "prepare_episode", prepare)

    def deliver(episode, _decision, state, _path, _config):
        state.setdefault("sent", {}).setdefault(episode.source_id, {})[episode.slug] = (
            {"status": "sent"}
        )

    monkeypatch.setattr(main, "deliver_prepared_episode", deliver)
    monkeypatch.setattr(
        main, "notify_error_once", lambda _p, e, c, **_k: notifications.append((c, str(e))) or True
    )
    return notifications


def test_failed_episodes_leave_no_state_and_stay_retryable(tmp_path, monkeypatch, capsys):
    notifications = run_with_fakes(
        monkeypatch,
        tmp_path,
        slugs=("old", "new", "bad"),
        collect_failures={"bad"},
        prepare_failures={("exchanges", "new")},
    )

    assert main.run(False, False, migration_catch_up=True) == 1

    state = main.load_state(tmp_path / "state.json")
    assert state["sent"]["the_markets"]["new"] == {"status": "sent"}
    assert state["sent"]["the_markets"]["old"]["status"] == "skipped-migration"
    assert "new" not in state["sent"].get("exchanges", {})
    assert "old" not in state["sent"].get("exchanges", {})
    assert "bad" not in state["sent"].get("exchanges", {})
    assert not main.was_sent(state, "exchanges", "new")
    assert not main.was_sent(state, "exchanges", "old")
    contexts = [context for context, _ in notifications]
    assert any("collection" in c and "bad" in c for c in contexts)
    assert any("Stage 1" in c and "GS Exchanges" in c for c in contexts)


def test_migration_summary_reports_sent_skipped_failed_counts(tmp_path, monkeypatch, capsys):
    run_with_fakes(
        monkeypatch,
        tmp_path,
        prepare_failures={("exchanges", "new")},
    )

    main.run(False, False, migration_catch_up=True)

    out = capsys.readouterr().out
    assert (
        "Migration catch-up complete: 1 sent, 1 skipped, 1 failed, "
        "0 collection errors." in out
    )


def test_error_notifications_name_migration_run_mode(tmp_path, monkeypatch):
    notifications = run_with_fakes(
        monkeypatch,
        tmp_path,
        slugs=("bad",),
        collect_failures={"bad"},
    )

    main.run(False, False, migration_catch_up=True)

    assert notifications
    assert all("migration catch-up" in c for c, _ in notifications)


def test_error_notifications_name_daily_run_mode(tmp_path, monkeypatch):
    notifications = run_with_fakes(
        monkeypatch,
        tmp_path,
        slugs=("bad",),
        collect_failures={"bad"},
    )

    main.run(False, False)

    assert notifications
    assert all("daily" in c for c, _ in notifications)


def test_fallback_warning_names_run_mode(tmp_path):
    sent = []
    episode = main.Episode(
        "the_markets", "GS The Markets", "slug", "u", transcript_source="youtube_audio"
    )

    main.warn_transcript_fallback(
        tmp_path / "state.json",
        episode,
        run_mode="migration catch-up",
        env={"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ERROR_CHAT_ID": "chat"},
        sender=lambda _t, _c, message: sent.append(message) or "1",
    )

    assert len(sent) == 1
    assert "migration catch-up" in sent[0]


def test_partial_delivery_retry_does_not_repeat_completed_channels(tmp_path):
    episode = SimpleNamespace(
        source_id="exchanges",
        source_name="GS Exchanges",
        slug="sample",
        title="Sample",
        date_iso="2026-09-18",
        url="https://example.com",
        youtube_url="",
    )
    routing = SimpleNamespace(
        episode_type="investor_interview",
        summary_lens="investment_process",
        recommended_depth="deep",
        guest_role="portfolio manager",
        confidence=0.9,
    )
    markdown = tmp_path / "sample.md"
    html = tmp_path / "sample.html"
    markdown.write_text("summary", encoding="utf-8")
    html.write_text("<html>summary</html>", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = main.empty_state()
    state["sent"]["exchanges"]["sample"] = {
        "status": "prepared",
        "summary": "## Key Takeaway\n\nSummary",
        "markdown_path": str(markdown),
        "html_path": str(html),
    }
    config = delivery.DeliveryConfig("r", "bot.qecapital.com.br", ["to"], "t", "chat", "error")
    email_calls = []
    message_calls = []
    document_calls = []

    def email_sender(*_args):
        email_calls.append(True)
        return "email-1"

    def message_sender(*_args):
        message_calls.append(True)
        return "message-1"

    def document_sender(*_args):
        document_calls.append(True)
        if len(document_calls) == 1:
            raise RuntimeError("Telegram unavailable")
        return "document-1"

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        main.deliver_prepared_episode(
            episode,
            routing,
            state,
            state_path,
            config,
            email_sender=email_sender,
            message_sender=message_sender,
            document_sender=document_sender,
        )

    main.deliver_prepared_episode(
        episode,
        routing,
        state,
        state_path,
        config,
        email_sender=email_sender,
        message_sender=message_sender,
        document_sender=document_sender,
    )

    record = state["sent"]["exchanges"]["sample"]
    assert len(email_calls) == 1
    assert len(message_calls) == 1
    assert len(document_calls) == 2
    assert record["status"] == "sent"
    assert record["email_id"] == "email-1"
    assert record["telegram_message_id"] == "message-1"
    assert record["telegram_document_message_id"] == "document-1"
