from __future__ import annotations

import sys
from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "LiveSegmentation"))


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        max_upload_bytes=1024 * 1024,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def headers():
    return {"X-LiveSeg-User": "alice"}
