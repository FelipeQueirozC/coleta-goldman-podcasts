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
