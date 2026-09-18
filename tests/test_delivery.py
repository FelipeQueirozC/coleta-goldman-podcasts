from pathlib import Path
from types import SimpleNamespace

import delivery


def objects(tmp_path):
    episode = SimpleNamespace(
        source_id="the_markets",
        source_name="GS The Markets",
        slug="sample",
        title="Sample Episode",
        date_iso="2026-09-18",
        url="https://example.com/episode",
        youtube_url="",
        transcript_source="inline_html",
        transcript_people=[],
        transcript_text="Full transcript",
    )
    routing = SimpleNamespace(
        episode_type="market_brief",
        summary_lens="tactical_markets",
        recommended_depth="brief",
        guest_role="strategist",
        confidence=0.8,
    )
    attachment = tmp_path / "summary.md"
    attachment.write_text("# Summary", encoding="utf-8")
    html_attachment = tmp_path / "summary.html"
    html_attachment.write_text("<!doctype html><html></html>", encoding="utf-8")
    return episode, routing, attachment, html_attachment


def test_email_payload_uses_verified_sender_and_kinea_html(tmp_path):
    episode, routing, attachment, _ = objects(tmp_path)
    config = delivery.DeliveryConfig(
        resend_api_key="resend",
        resend_from_domain="bot.qecapital.com.br",
        resend_to=["to@example.com"],
        telegram_bot_token="token",
        telegram_delivery_chat_id="chat",
        telegram_error_chat_id="errors",
    )

    payload = delivery.build_email_payload(
        episode, "## Key Takeaway\n\nSummary", routing, config, attachment
    )

    assert payload["from"] == "gs.themarkets@bot.qecapital.com.br"
    assert payload["to"] == ["to@example.com"]
    assert "<h2>Key Takeaway</h2>" in payload["html"]
    assert payload["text"].startswith("Sample Episode")
    assert payload["attachments"][0]["filename"].endswith(".md")


def test_telegram_sends_message_and_html_document(tmp_path, monkeypatch):
    episode, routing, _, html_attachment = objects(tmp_path)
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "result": {"message_id": len(calls)}}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(delivery.requests, "post", post)
    config = delivery.DeliveryConfig("r", "bot.qecapital.com.br", ["to"], "token", "chat", "errors")

    message_id = delivery.send_telegram_message(
        episode, "## Key Takeaway\n\nSummary", routing, config
    )
    document_id = delivery.send_telegram_document(episode, routing, config, html_attachment)

    assert message_id == "1"
    assert document_id == "2"
    assert calls[0][0].endswith("/bottoken/sendMessage")
    assert calls[1][0].endswith("/bottoken/sendDocument")
    assert calls[1][1]["files"]["document"][2] == "text/html"
