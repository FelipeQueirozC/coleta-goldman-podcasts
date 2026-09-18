from pathlib import Path

import main


FIXTURES = Path(__file__).parent / "fixtures"


def test_pdf_transcript_has_precedence(monkeypatch):
    monkeypatch.setattr(main, "extract_pdf_text", lambda _pdf: "PDF transcript")

    text, source = main.extract_transcript(b"%PDF-test", "<b>Transcript:</b>")

    assert text == "PDF transcript"
    assert source == "transcript_pdf"


def test_inline_transcript_is_used_when_pdf_is_missing():
    page_html = (FIXTURES / "inline_transcript.html").read_text(encoding="utf-8")

    text, source = main.extract_transcript(b"", page_html)

    assert source == "inline_html"
    assert "Chris Hussey: This is The Markets." in text
    assert "Nitin Jindal: Higher prices reduce demand over time." in text
    assert "Transcript:" not in text
