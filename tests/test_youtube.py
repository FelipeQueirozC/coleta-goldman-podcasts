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
