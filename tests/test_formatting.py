from types import SimpleNamespace

import formatting


def objects():
    episode = SimpleNamespace(
        source_id="exchanges",
        source_name="GS Exchanges",
        slug="sample",
        title="Sample <Episode>",
        date_iso="2026-09-18",
        url="https://example.com/episode",
        youtube_url="",
        pdf_url="https://example.com/transcript.pdf",
        transcript_source="transcript_pdf",
        transcript_people=["Jane Investor"],
        transcript_text="Jane: Full transcript <unsafe>.",
    )
    routing = SimpleNamespace(
        episode_type="investor_interview",
        summary_lens="investment_process",
        recommended_depth="deep",
        guest_role="portfolio manager",
        confidence=0.9,
    )
    return episode, routing


def test_kinea_style_html_renders_markdown_and_escapes_untrusted_content():
    episode, routing = objects()
    summary = "## Key Takeaway\n\nSafe <script>alert(1)</script>.\n\n## Best Insights\n\n- First insight"

    email = formatting.build_email_html(episode, summary, routing)
    attachment = formatting.build_html_attachment(episode, summary, routing)

    assert "<h2>Key Takeaway</h2>" in email
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in email
    assert "<li>First insight</li>" in email
    assert "max-width: 720px" in email
    assert "padding: 0 16px" in email
    assert "## Key Takeaway" not in email
    assert "Full transcript &lt;unsafe&gt;." in attachment
    assert "Full Transcript" in attachment
    assert 'name="viewport"' in attachment
    assert "font-size: 17px" in attachment


def test_telegram_message_and_all_content_are_english():
    episode, routing = objects()
    summary = "## Key Takeaway\n\nThe central investment conclusion."

    message = formatting.build_telegram_message(episode, summary, routing)
    caption = formatting.build_telegram_caption(episode, routing)

    assert "The central investment conclusion." in message
    assert "Full summary and transcript" in caption
    assert "investor interview" in message
