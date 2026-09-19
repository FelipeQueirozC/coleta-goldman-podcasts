from pathlib import Path

import main


FIXTURES = Path(__file__).parent / "fixtures"


def test_pdf_transcript_has_precedence(monkeypatch):
    monkeypatch.setattr(main, "extract_pdf_text", lambda _pdf: "PDF transcript")

    text, source = main.extract_transcript(b"%PDF-test", "<b>Transcript:</b>")

    assert text == "PDF transcript"
    assert source == "transcript_pdf"


def test_speaker_names_are_taken_from_dialogue_without_splitting_titles():
    transcript = (
        "Rich Friedman: First answer.\n\n"
        "Alison Mass: First question.\n\n"
        "Rich Friedman: Second answer.\n\n"
        "Sachs Exchanges: Follow the show."
    )

    assert main.extract_speaker_names(transcript) == ["Rich Friedman", "Alison Mass"]


def test_youtube_audio_is_last_resort_before_missing():
    text, source = main.extract_transcript(
        b"",
        "<html><body>No transcript here.</body></html>",
        youtube_url="https://www.youtube.com/watch?v=30ir9C1Im1M",
        youtube_fetcher=lambda _url: "spoken transcript text",
    )

    assert text == "spoken transcript text"
    assert source == "youtube_audio"


def test_pdf_and_inline_transcripts_beat_youtube_fallback(monkeypatch):
    monkeypatch.setattr(main, "extract_pdf_text", lambda _pdf: "PDF transcript")

    def fail_fetcher(_url):
        raise AssertionError("YouTube must not be called")

    text, source = main.extract_transcript(
        b"%PDF-test",
        "<html></html>",
        youtube_url="https://www.youtube.com/watch?v=30ir9C1Im1M",
        youtube_fetcher=fail_fetcher,
    )

    assert (text, source) == ("PDF transcript", "transcript_pdf")


def test_youtu_be_short_links_resolve_to_video_ids():
    seen = []

    def fetcher(url):
        seen.append(url)
        return "spoken text"

    _, source = main.extract_transcript(
        b"",
        "<html></html>",
        youtube_url="https://youtu.be/30ir9C1Im1M?si=abc",
        youtube_fetcher=fetcher,
    )

    assert source == "youtube_audio"


def test_missing_transcript_without_any_source():
    assert main.extract_transcript(b"", "<html></html>") == ("", "missing")


def test_podcast_sources_warn_when_transcript_comes_from_youtube(tmp_path):
    podcast = main.Episode(
        "the_markets", "GS The Markets", "slug", "u", transcript_source="youtube_audio"
    )
    assert main.needs_transcript_fallback_warning(podcast)

    views = main.Episode(
        "views_from_floor", "GS Views", "vid", "u", transcript_source="youtube_audio"
    )
    assert not main.needs_transcript_fallback_warning(views)

    pdf = main.Episode(
        "exchanges", "GS Exchanges", "slug", "u", transcript_source="transcript_pdf"
    )
    assert not main.needs_transcript_fallback_warning(pdf)

    sent = []
    state_path = tmp_path / "state.json"
    assert main.warn_transcript_fallback(
        state_path,
        podcast,
        env={"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ERROR_CHAT_ID": "chat"},
        sender=lambda _t, _c, message: sent.append(message) or "1",
    )
    assert len(sent) == 1
    assert "youtube_audio" in sent[0] or "YouTube audio" in sent[0]
    assert "the_markets" in sent[0] or "GS The Markets" in sent[0]


def test_inline_transcript_is_used_when_pdf_is_missing():
    page_html = (FIXTURES / "inline_transcript.html").read_text(encoding="utf-8")

    text, source = main.extract_transcript(b"", page_html)

    assert source == "inline_html"
    assert "Chris Hussey: This is The Markets." in text
    assert "Nitin Jindal: Higher prices reduce demand over time." in text
    assert "Transcript:" not in text
