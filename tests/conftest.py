"""Shared test fixtures."""

import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolate_failure_dumps(tmp_path, monkeypatch):
    """Keep Stage 1/2 raw-failure dumps out of the real temp directory."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
