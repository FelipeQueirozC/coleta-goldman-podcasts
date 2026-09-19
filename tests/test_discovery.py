from pathlib import Path

import main


FIXTURES = Path(__file__).parent / "fixtures"


def test_discovers_youtube_cards_with_titles_and_eyebrows():
    html = (FIXTURES / "views_from_floor.html").read_text(encoding="utf-8")

    cards = main.discover_youtube_cards(html)
    by_id = {card["video_id"]: card for card in cards}

    assert {"30ir9C1Im1M", "vH16LrVAoBc", "RR0BF-5wgWI", "mlPGrvruhfk"} <= set(by_id)
    assert by_id["30ir9C1Im1M"]["title"] == "Why the Fed’s Hawkish Turn Could Support the AI Boom"
    assert by_id["30ir9C1Im1M"]["eyebrow"] == "The Breaks of the Game"
    assert by_id["RR0BF-5wgWI"]["eyebrow"] == "The Macro Call"
    assert by_id["RR0BF-5wgWI"]["title"] == "Copper: AI Hype or Supply Squeeze?"
    for card in cards:
        assert set(card) == {"video_id", "title", "eyebrow", "description"}
        assert card["title"] == card["title"].strip()
        assert "<" not in card["description"] and ">" not in card["description"]
    titles = [card["title"] for card in cards]
    assert "Cash Equities and Execution Services" not in titles
    assert len(by_id) == len(cards)
