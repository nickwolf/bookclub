"""Shared pytest fixtures.

The app modules live in app/ and use paths relative to that directory
(StaticFiles(directory="static"), Jinja2Templates(directory="templates")),
so we chdir into app/ before anything imports main.
"""
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent / "app"
sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)

import pytest


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    """The db module pointed at a throwaway SQLite file, schema initialised."""
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db


@pytest.fixture()
def client(test_db):
    """TestClient for the app.

    Deliberately NOT used as a context manager: the startup hook seeds
    ~30 recommendations and can spawn an auto-sync thread that calls the
    Hardcover API. A plain TestClient never runs startup events.
    """
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)
