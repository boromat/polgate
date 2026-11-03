"""Secrets a hub generates for itself when the deployment leaves them out."""

import stat
import sys

import pytest
from sqlmodel import select

from polgate import keys
from polgate.auth.bootstrap import ensure_admin, uses_bootstrap_password
from polgate.auth.passwords import hash_password, verify_password
from polgate.config import get_settings
from polgate.models import User

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX mode bits")


@pytest.fixture
def bare_settings(tmp_path, monkeypatch):
    """Settings as a ``docker run`` with no environment sees them, keeping state in tmp_path."""
    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", "")
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'hub.db'}")
    keys._generated_session_secret.cache_clear()
    yield settings
    keys._generated_session_secret.cache_clear()


def test_a_secret_is_generated_once_and_read_back_afterwards(tmp_path):
    path = tmp_path / "state" / "secret"
    assert keys.read_or_create(path, lambda: "one") == ("one", True)
    assert keys.read_or_create(path, lambda: "two") == ("one", False)


@posix_only
def test_a_generated_secret_is_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "secret"
    keys.read_or_create(path, lambda: "value")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_configured_secret_key_signs_sessions_as_given(bare_settings, monkeypatch):
    monkeypatch.setattr(bare_settings, "secret_key", "configured-by-the-deployment")
    assert keys.session_secret() == "configured-by-the-deployment"


def test_without_a_secret_key_the_hub_generates_one_and_keeps_it(bare_settings, tmp_path):
    secret = keys.session_secret()
    keys._generated_session_secret.cache_clear()  # as after a restart
    assert keys.session_secret() == secret
    assert (tmp_path / keys.SESSION_KEY_FILE).read_text(encoding="utf-8") == secret
    assert len(secret) >= 32


@pytest.mark.parametrize("placeholder", sorted(keys.PLACEHOLDER_SECRET_KEYS))
def test_a_placeholder_secret_key_is_never_used_to_sign(bare_settings, monkeypatch, placeholder):
    """A key copied unchanged from the example file is public, so it counts as none at all."""
    monkeypatch.setattr(bare_settings, "secret_key", placeholder)
    assert keys.session_secret() != placeholder


def test_the_first_admin_gets_a_generated_password_when_none_is_configured(
    bare_settings, db_session, tmp_path
):
    ensure_admin()
    password = (tmp_path / keys.ADMIN_PASSWORD_FILE).read_text(encoding="utf-8")
    admin = db_session.exec(select(User)).one()
    assert len(password) >= 12
    assert verify_password(password, admin.password_hash)
    assert uses_bootstrap_password(admin)


def test_a_configured_admin_password_leaves_no_file_behind(
    bare_settings, db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(bare_settings, "admin_password", "chosen-by-the-admin")
    ensure_admin()
    admin = db_session.exec(select(User)).one()
    assert verify_password("chosen-by-the-admin", admin.password_hash)
    assert not (tmp_path / keys.ADMIN_PASSWORD_FILE).exists()


def test_the_admin_is_not_warned_once_the_generated_password_is_changed(bare_settings, db_session):
    ensure_admin()
    admin = db_session.exec(select(User)).one()
    admin.password_hash = hash_password("a-password-only-the-admin-knows")
    assert not uses_bootstrap_password(admin)
