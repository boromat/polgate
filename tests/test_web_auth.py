"""Login, session cookies and access control."""

from sqlmodel import Session, select

from polgate.auth.passwords import hash_password
from polgate.auth.session import COOKIE_NAME
from polgate.db import get_engine
from polgate.models import Role, User


def _add_user(username: str, password: str, role: Role = Role.USER) -> None:
    with Session(get_engine()) as session:
        session.add(User(username=username, password_hash=hash_password(password), role=role))
        session.commit()


def test_landing_page_renders_for_anonymous_visitors(app_client):
    response = app_client.get("/")
    assert response.status_code == 200
    assert "Bezpieczny hub AI" in response.text


def test_landing_page_can_switch_to_english(app_client):
    assert "A safe AI hub" in app_client.get("/?lang=en").text


def test_chat_redirects_anonymous_visitors_to_login(app_client):
    response = app_client.get("/chat")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_login_with_valid_credentials_starts_a_session(app_client):
    response = app_client.post(
        "/login", data={"username": "admin", "password": "admin-password", "next": "/chat"}
    )
    assert response.status_code == 303
    assert "polgate_session" in response.cookies


def test_login_with_wrong_password_is_rejected(app_client):
    response = app_client.post("/login", data={"username": "admin", "password": "nope"})
    assert response.status_code == 401
    assert "Nieprawidłowy" in response.text


def test_inactive_account_cannot_log_in(app_client):
    _add_user("frozen", "secret-pass")
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.username == "frozen")).one()
        user.is_active = False
        session.add(user)
        session.commit()
    assert (
        app_client.post(
            "/login", data={"username": "frozen", "password": "secret-pass"}
        ).status_code
        == 401
    )


def test_logout_clears_the_session(admin_client):
    admin_client.post("/logout")
    assert admin_client.get("/chat").status_code == 303


def test_admin_panel_is_forbidden_for_regular_users(app_client):
    _add_user("worker", "worker-pass")
    app_client.post("/login", data={"username": "worker", "password": "worker-pass"})
    assert app_client.get("/admin").status_code == 403


def test_admin_panel_opens_for_administrators(admin_client):
    assert admin_client.get("/admin").status_code == 200


def test_icon_only_navigation_links_carry_accessible_names(admin_client):
    """The labels are hidden on narrow screens, so the links need names of their own."""
    page = admin_client.get("/chat", follow_redirects=True)
    for label in ("Czat", "Administracja", "Wyloguj"):
        assert f'aria-label="{label}"' in page.text


def test_the_page_language_follows_the_interface_language(app_client):
    assert '<html lang="pl">' in app_client.get("/?lang=pl").text
    assert '<html lang="en">' in app_client.get("/?lang=en").text


def test_a_tampered_session_cookie_is_ignored(app_client):
    """The cookie is signed, so editing the user id in it just signs the visitor out."""
    app_client.post("/login", data={"username": "admin", "password": "admin-password"})
    app_client.cookies.set(COOKIE_NAME, "1.fake-signature")
    response = app_client.get("/chat")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_a_cookie_signed_for_a_missing_account_is_ignored(app_client):
    from polgate.auth.session import _serializer

    app_client.cookies.set(COOKIE_NAME, _serializer().dumps({"uid": 4242, "epoch": 0}))
    assert app_client.get("/chat").headers["location"].startswith("/login")


def test_a_cookie_without_a_user_id_is_ignored(app_client):
    from polgate.auth.session import _serializer

    app_client.cookies.set(COOKIE_NAME, _serializer().dumps({"kto": "admin"}))
    assert app_client.get("/chat").headers["location"].startswith("/login")


def test_deactivating_an_account_ends_its_session(app_client):
    from sqlmodel import Session, select

    from polgate.db import get_engine
    from polgate.models import User

    app_client.post("/login", data={"username": "admin", "password": "admin-password"})
    assert app_client.get("/chat").status_code == 303  # redirect into a conversation

    with Session(get_engine()) as session:
        admin = session.exec(select(User).where(User.username == "admin")).one()
        admin.is_active = False
        session.add(admin)
        session.commit()

    assert app_client.get("/chat").headers["location"].startswith("/login")


def test_logging_out_clears_the_cookie(app_client):
    app_client.post("/login", data={"username": "admin", "password": "admin-password"})
    response = app_client.post("/logout")
    assert response.status_code == 303
    assert 'polgate_session=""' in response.headers["set-cookie"]


def test_the_health_endpoint_reports_the_version(app_client):
    payload = app_client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["version"]


def test_the_language_switch_is_remembered_in_a_cookie(app_client):
    response = app_client.get("/login?lang=en")
    assert "lang=en" in response.headers["set-cookie"]
    assert "Sign in" in response.text or "Log in" in response.text


def test_the_engine_is_built_once_and_reused(monkeypatch, tmp_path):
    """`get_engine` caches in a module global; a second call must not open a second pool."""
    import polgate.db as db
    from polgate.config import get_settings

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite:///{tmp_path / 'once.db'}")

    first = db.get_engine()
    assert first is db.get_engine()
    assert first.url.database.endswith("once.db")


def test_every_sqlite_connection_gets_the_pragmas_the_hub_depends_on(tmp_path):
    """WAL keeps readers working during a write; foreign keys are off in SQLite unless asked for."""
    from sqlalchemy import text

    import polgate.db as db

    engine = db.make_engine(f"sqlite:///{tmp_path / 'pragmas.db'}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == db.BUSY_TIMEOUT_MS


def test_a_conversation_cannot_point_at_an_account_that_does_not_exist(db_session):
    """With foreign keys enforced, an orphaned row is refused instead of quietly stored."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from polgate.models import Conversation

    db_session.add(Conversation(user_id=999_999))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_signing_out_needs_a_post(admin_client):
    """A link or an image on another site must not be able to sign the employee out."""
    assert admin_client.get("/logout").status_code == 405
    assert admin_client.get("/chat").status_code == 303


def test_the_sign_out_control_is_a_form(admin_client):
    page = admin_client.get("/account").text
    assert '<form class="inline-form" method="post" action="/logout">' in page
    assert 'href="/logout"' not in page


def _another_browser(app_client):
    from fastapi.testclient import TestClient

    return TestClient(app_client.app, follow_redirects=False)


def _sign_in(client, username: str = "admin", password: str | None = None) -> None:
    password = "admin-password" if password is None else password
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 303, response.text


def _signed_out(client) -> bool:
    return client.get("/account").headers.get("location", "").startswith("/login")


def _user_id(username: str) -> int:
    with Session(get_engine()) as session:
        return session.exec(select(User).where(User.username == username)).one().id


def test_a_new_password_signs_every_other_browser_out(app_client):
    """Changing the password is the answer to a suspected compromise; a stolen cookie must end."""
    other = _another_browser(app_client)
    _sign_in(app_client)
    _sign_in(other)

    response = app_client.post(
        "/account/password",
        data={
            "current": "admin-password",
            "new": "brand-new-password",
            "confirm": "brand-new-password",
        },
    )

    assert response.status_code == 303
    assert _signed_out(other)
    assert not _signed_out(app_client)


def test_an_administrators_reset_ends_the_accounts_sessions(admin_client):
    _add_user("worker", "worker-pass")
    worker = _another_browser(admin_client)
    _sign_in(worker, "worker", "worker-pass")

    admin_client.post(
        f"/admin/users/{_user_id('worker')}/password", data={"password": "reset-by-admin-1"}
    )

    assert _signed_out(worker)


def test_an_administrator_changes_their_own_password_only_on_the_account_page(admin_client):
    """There the current password is checked; here a stolen session alone would be enough."""
    from polgate.auth.passwords import verify_password

    other = _another_browser(admin_client)
    _sign_in(other)
    own = _user_id("admin")

    response = admin_client.post(f"/admin/users/{own}/password", data={"password": "own-reset-1"})

    assert response.status_code == 400
    with Session(get_engine()) as session:
        assert verify_password("admin-password", session.get(User, own).password_hash)
    assert not _signed_out(other)
    page = admin_client.get("/admin/users").text
    assert f'action="/admin/users/{own}/password"' not in page
    assert 'href="/account"' in page


def test_a_copied_cookie_stops_working_at_sign_out(app_client):
    _sign_in(app_client)
    copied = app_client.cookies.get(COOKIE_NAME)

    app_client.post("/logout")
    app_client.cookies.set(COOKIE_NAME, copied)

    assert _signed_out(app_client)


def test_enabling_an_account_again_does_not_revive_an_old_cookie(admin_client):
    _add_user("worker", "worker-pass")
    worker = _another_browser(admin_client)
    _sign_in(worker, "worker", "worker-pass")
    worker_id = _user_id("worker")

    admin_client.post(f"/admin/users/{worker_id}/toggle")
    admin_client.post(f"/admin/users/{worker_id}/toggle")

    assert _signed_out(worker)


def test_a_cookie_without_an_epoch_is_ignored(app_client):
    from polgate.auth.session import _serializer

    app_client.cookies.set(COOKIE_NAME, _serializer().dumps({"uid": _user_id("admin")}))
    assert _signed_out(app_client)


def _admin_epoch() -> int:
    with Session(get_engine()) as session:
        return session.exec(select(User).where(User.username == "admin")).one().session_epoch


def test_a_sign_in_whose_password_changes_during_the_check_gets_a_dead_cookie(
    app_client, monkeypatch
):
    """Otherwise someone signing in with a known password keeps a session past its change."""
    from polgate.auth.passwords import verify_password
    from polgate.auth.session import end_all_sessions
    from polgate.web import auth as auth_web

    def change_meanwhile(password, password_hash):
        with Session(get_engine()) as other:
            admin = other.exec(select(User).where(User.username == "admin")).one()
            admin.password_hash = hash_password("changed-meanwhile-1")
            end_all_sessions(other, admin)
            other.add(admin)
            other.commit()
        return verify_password(password, password_hash)

    monkeypatch.setattr(auth_web, "verify_password", change_meanwhile)
    response = app_client.post("/login", data={"username": "admin", "password": "admin-password"})

    assert response.status_code == 303  # it was the password when the check began
    assert _signed_out(app_client)


def test_ending_sessions_twice_at_once_moves_the_epoch_twice(admin_client, monkeypatch):
    """A sign-out elsewhere during a password change must not be overwritten by a stale copy."""
    from polgate.auth.passwords import verify_password
    from polgate.auth.session import end_all_sessions
    from polgate.web import account as account_web

    def sign_out_elsewhere_meanwhile(password, password_hash):
        with Session(get_engine()) as other:
            end_all_sessions(other, other.exec(select(User).where(User.username == "admin")).one())
            other.commit()
        return verify_password(password, password_hash)

    before = _admin_epoch()
    monkeypatch.setattr(account_web, "verify_password", sign_out_elsewhere_meanwhile)
    response = admin_client.post(
        "/account/password",
        data={
            "current": "admin-password",
            "new": "brand-new-password",
            "confirm": "brand-new-password",
        },
    )

    assert response.status_code == 303
    assert _admin_epoch() == before + 2
    assert not _signed_out(admin_client)
