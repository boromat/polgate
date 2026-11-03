"""Shared fixtures. Every test runs against a temporary database and a fixed encryption key."""

import os
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="polgate-tests-"))
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_TMP / 'test.db'}",
        "UPLOAD_DIR": str(_TMP / "uploads"),
        "SECRET_KEY": "test-secret-key",
        "VAULT_KEY": Fernet.generate_key().decode(),
        "LLM_API_KEY": "test-key",
        "LLM_BASE_URL": "https://provider.test/v1",
        "LLM_MODEL": "test/model",
        "JUDGE_BACKEND": "mock",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "admin-password",
    }
)


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """An isolated database session backed by its own SQLite file."""
    from sqlmodel import Session, SQLModel

    import polgate.db as db

    engine = db.make_engine(f"sqlite:///{tmp_path / 'unit.db'}")
    monkeypatch.setattr(db, "_engine", engine)
    import polgate.models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """A test client with a fresh database, seeded admin and no background jobs."""
    from fastapi.testclient import TestClient
    from sqlmodel import SQLModel

    import polgate.db as db
    import polgate.main as main
    from polgate.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    engine = db.make_engine(f"sqlite:///{tmp_path / 'web.db'}")
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(main, "PURGE_INTERVAL_SECONDS", 3600)
    import polgate.models  # noqa: F401

    SQLModel.metadata.create_all(engine)

    with TestClient(main.create_app(), follow_redirects=False) as client:
        yield client


@pytest.fixture
def htmx_config(app_client):
    """The htmx settings every page carries in its `htmx-config` meta element."""
    import html
    import json
    import re

    page = app_client.get("/login").text
    content = re.search(r'<meta name="htmx-config" content=\'([^\']*)\'', page).group(1)
    return json.loads(html.unescape(content))


@pytest.fixture
def admin_client(app_client):
    """A client already signed in as the seeded administrator."""
    response = app_client.post(
        "/login", data={"username": "admin", "password": "admin-password", "next": "/chat"}
    )
    assert response.status_code == 303, response.text
    return app_client
