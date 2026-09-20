import os
import sys
import types

import pytest

import youtube


class FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_fetch_upload_date_parses_yt_dlp_output():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(stdout="20260918\n")

    assert youtube.fetch_upload_date("30ir9C1Im1M", runner=runner) == "2026-09-18"
    assert "--skip-download" in calls[0]
    assert "30ir9C1Im1M" in calls[0][-1]


def test_fetch_upload_date_returns_empty_on_failure():
    def runner(cmd, **kwargs):
        raise RuntimeError("no network")

    assert youtube.fetch_upload_date("30ir9C1Im1M", runner=runner) == ""


def test_fetch_metadata_survives_null_output():
    def runner(cmd, **kwargs):
        return FakeCompleted(stdout="null\n")

    meta = youtube.fetch_metadata("30ir9C1Im1M", runner=runner)

    assert meta == {"video_id": "30ir9C1Im1M", "title": "", "description": "", "upload_date": ""}


def test_ytdlp_prefers_venv_binary_over_system_path(tmp_path, monkeypatch):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    binary = venv_bin / "yt-dlp"
    binary.write_bytes(b"x")
    monkeypatch.setattr(youtube.sys, "executable", str(venv_bin / "python"))
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(stdout="20260918\n")

    youtube.fetch_upload_date("30ir9C1Im1M", runner=runner)

    assert calls[0][0] == str(binary)


def test_ytdlp_falls_back_to_path_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube.sys, "executable", str(tmp_path / "python"))
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(stdout="20260918\n")

    youtube.fetch_upload_date("30ir9C1Im1M", runner=runner)

    assert calls[0][0] == "yt-dlp"


def test_list_playlist_videos_parses_id_and_title():
    def runner(cmd, **kwargs):
        assert "--flat-playlist" in cmd
        assert "PLIyiGQywEp65E-tanAHdVfVeEgMiY1jT2" in cmd[-1]
        return FakeCompleted(
            stdout="JCbDQh2GokQ | Can Stocks Rally With a Hawkish Fed?\n"
            "vH16LrVAoBc | Can Markets Withstand AI Risks?\n"
            "\n"
        )

    items = youtube.list_playlist_videos("PLIyiGQywEp65E-tanAHdVfVeEgMiY1jT2", runner=runner)

    assert items == [
        {"video_id": "JCbDQh2GokQ", "title": "Can Stocks Rally With a Hawkish Fed?"},
        {"video_id": "vH16LrVAoBc", "title": "Can Markets Withstand AI Risks?"},
    ]


def test_list_playlist_videos_returns_empty_on_failure():
    def runner(cmd, **kwargs):
        raise RuntimeError("no network")

    assert youtube.list_playlist_videos("BAD", runner=runner) == []


def test_download_failure_reports_yt_dlp_stderr():
    import subprocess

    def runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: Sign in to confirm\n")

    with pytest.raises(RuntimeError, match="Sign in to confirm"):
        youtube.download_audio("30ir9C1Im1M", __import__("pathlib").Path("/tmp"), runner=runner)


def test_download_retries_transient_403_then_succeeds(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    monkeypatch.setattr(youtube, "YOUTUBE_RETRY_DELAYS", (0, 0))
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(
                1, cmd, stderr="ERROR: unable to download video data: HTTP Error 403: Forbidden\n"
            )
        target = tmp_path / "audio.m4a"
        target.write_bytes(b"fake-audio")
        return FakeCompleted()

    path = youtube.download_audio("30ir9C1Im1M", tmp_path, runner=runner)

    assert path == tmp_path / "audio.m4a"
    assert len(calls) == 3


def test_download_does_not_retry_fatal_errors(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(youtube, "YOUTUBE_RETRY_DELAYS", (0, 0))
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: Private video\n")

    with pytest.raises(RuntimeError, match="Private video"):
        youtube.download_audio(
            "30ir9C1Im1M", __import__("pathlib").Path(str(tmp_path)), runner=runner
        )

    assert len(calls) == 1


def test_download_exhausted_retries_reports_attempts(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(youtube, "YOUTUBE_RETRY_DELAYS", (0, 0))

    def runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: HTTP Error 429\n")

    with pytest.raises(RuntimeError, match="(?i)3 attempt.*429|429.*3 attempt"):
        youtube.download_audio("30ir9C1Im1M", tmp_path, runner=runner)


def test_download_audio_uses_bestaudio_single_video(tmp_path):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        target = tmp_path / "audio.m4a"
        target.write_bytes(b"fake-audio")
        return FakeCompleted()

    path = youtube.download_audio("30ir9C1Im1M", tmp_path, runner=runner)

    assert path == tmp_path / "audio.m4a"
    assert "-f" in calls[0]
    assert "bestaudio[ext=m4a]/bestaudio" in calls[0]
    assert "--no-playlist" in calls[0]


def test_transcribe_youtube_audio_end_to_end_with_fakes(tmp_path, monkeypatch):
    def runner(cmd, **kwargs):
        if "--print" in cmd:
            return FakeCompleted(stdout="20260918\n")
        (tmp_path / "audio.m4a").write_bytes(b"fake-audio")
        return FakeCompleted()

    class FakeTranscriptions:
        def create(self, **kwargs):
            assert kwargs["language"] == "en"
            assert kwargs["model"] == "whisper-large-v3"
            return "spoken transcript text"

    fake_groq = types.ModuleType("groq")
    fake_groq.Groq = lambda api_key: types.SimpleNamespace(
        api=api_key, audio=types.SimpleNamespace(transcriptions=FakeTranscriptions())
    )
    monkeypatch.setitem(sys.modules, "groq", fake_groq)

    text = youtube.transcribe_youtube_audio(
        "30ir9C1Im1M", api_key="groq-key", work_dir=tmp_path, runner=runner
    )

    assert text == "spoken transcript text"


def test_transcribe_rejects_oversized_audio(tmp_path):
    big = tmp_path / "audio.m4a"
    big.write_bytes(b"x" * (youtube.GROQ_MAX_UPLOAD_BYTES + 1))

    with pytest.raises(RuntimeError, match="exceeds Groq"):
        youtube.transcribe_audio_file(big, "groq-key")


def test_missing_groq_key_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        youtube.transcribe_youtube_audio("30ir9C1Im1M", work_dir=tmp_path)
