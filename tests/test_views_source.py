import main
from delivery import SENDER_PREFIXES


def youtube_source():
    return next(s for s in main.SOURCES if s["id"] == "views_from_floor")


def test_views_from_floor_source_is_configured():
    source = youtube_source()

    assert source["listing_url"] == "https://www.goldmansachs.com/what-we-do/ficc-and-equities"
    assert source["sender_prefix"] == "gs.viewsfromfloor"
    assert SENDER_PREFIXES["views_from_floor"] == "gs.viewsfromfloor"


def test_collect_youtube_episode_uses_card_metadata_and_groq_transcript():
    card = {
        "video_id": "30ir9C1Im1M",
        "title": "Why the Fed’s Hawkish Turn Could Support the AI Boom",
        "eyebrow": "The Breaks of the Game",
        "description": "Could moderate hawkishness support risk assets?",
    }

    episode = main.collect_youtube_episode(
        youtube_source(),
        card,
        upload_date_fetcher=lambda _vid: "2026-09-18",
        transcriber=lambda _vid: "Chris Hussey: Markets rallied today.",
    )

    assert episode.source_id == "views_from_floor"
    assert episode.slug == "30ir9C1Im1M"
    assert episode.url == "https://www.youtube.com/watch?v=30ir9C1Im1M"
    assert episode.title.startswith("Why the Fed")
    assert episode.eyebrow == "The Breaks of the Game"
    assert episode.date_iso == "2026-09-18"
    assert episode.transcript_source == "youtube_audio"
    assert "Markets rallied" in episode.transcript_text


def test_dry_run_collection_skips_youtube_download_and_transcription():
    card = {
        "video_id": "30ir9C1Im1M",
        "title": "Sample",
        "eyebrow": "The Macro Call",
        "description": "Desc",
    }

    def fail_fetcher(_vid):
        raise AssertionError("must not fetch")

    def fail_transcriber(_vid):
        raise AssertionError("must not transcribe")

    episode = main.collect_youtube_episode(
        youtube_source(),
        card,
        upload_date_fetcher=fail_fetcher,
        transcriber=fail_transcriber,
        dry_run=True,
    )

    assert episode.slug == "30ir9C1Im1M"
    assert episode.transcript_text == ""


def test_views_email_uses_approved_sender(tmp_path):
    from delivery import DeliveryConfig, build_email_payload
    from types import SimpleNamespace

    attachment = tmp_path / "episode.md"
    attachment.write_text("# Summary", encoding="utf-8")
    episode = SimpleNamespace(
        source_id="views_from_floor",
        source_name="GS Views From the Floor",
        title="Sample",
        date_iso="2026-09-18",
        url="https://www.youtube.com/watch?v=vid",
        youtube_url="",
        transcript_source="youtube_audio",
        transcript_people=[],
        transcript_text="text",
    )
    routing = SimpleNamespace(
        episode_type="trading_desk_brief",
        summary_lens="tactical_markets",
        recommended_depth="brief",
        guest_role="strategist",
        confidence=0.8,
    )
    config = DeliveryConfig(
        "resend-key", "bot.qecapital.com.br", ["to@example.com"], "t", "c", "e"
    )

    payload = build_email_payload(episode, "## Key Takeaway\n\nText", routing, config, attachment)

    assert payload["from"] == "gs.viewsfromfloor@bot.qecapital.com.br"


def test_youtube_watch_url_routes_to_views_source():
    source, slug = main.source_and_slug_from_episode_url(
        "https://www.youtube.com/watch?v=30ir9C1Im1M"
    )

    assert source["id"] == "views_from_floor"
    assert slug == "30ir9C1Im1M"


def test_migration_selects_newest_video_per_eyebrow():
    def episode(slug, eyebrow, date):
        return main.Episode(
            "views_from_floor",
            "GS Views From the Floor",
            slug,
            "https://www.youtube.com/watch?v=" + slug,
            date_iso=date,
            eyebrow=eyebrow,
        )

    pending = [
        episode("old-breaks", "The Breaks of the Game", "2026-09-10"),
        episode("new-breaks", "The Breaks of the Game", "2026-09-18"),
        episode("old-macro", "The Macro Call", "2026-09-11"),
        episode("new-macro", "The Macro Call", "2026-09-17"),
        main.Episode(
            "the_markets", "GS The Markets", "old", "u", date_iso="2026-09-01"
        ),
        main.Episode(
            "the_markets", "GS The Markets", "new", "u", date_iso="2026-09-18"
        ),
    ]

    selected, skipped = main.select_migration_pending(pending)
    selected_slugs = {ep.slug for ep in selected}

    assert selected_slugs == {"new-breaks", "new-macro", "new"}
    assert {ep.slug for ep in skipped["views_from_floor"]} == {"old-breaks", "old-macro"}
    assert [ep.slug for ep in skipped["the_markets"]] == ["old"]
