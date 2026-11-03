"""The employee's own account page, and the sign-in lockout that protects it."""

import pytest
from sqlmodel import Session, select

from polgate.auth.passwords import verify_password
from polgate.config import get_settings
from polgate.db import get_engine
from polgate.models import AuditEvent, User

CURRENT = "admin-password"
NEW = "wystarczajaco-dlugie-haslo"


def _admin() -> User:
    with Session(get_engine()) as session:
        return session.exec(select(User).where(User.username == "admin")).one()


def _events(name: str) -> list[AuditEvent]:
    with Session(get_engine()) as session:
        return list(session.exec(select(AuditEvent).where(AuditEvent.event == name)).all())


def test_the_account_page_is_only_for_signed_in_users(app_client):
    response = app_client.get("/account")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_account_page_shows_the_password_form(admin_client):
    body = admin_client.get("/account").text
    assert 'action="/account/password"' in body
    assert 'name="current"' in body


def test_a_valid_change_replaces_the_hash_and_keeps_the_session(admin_client):
    response = admin_client.post(
        "/account/password", data={"current": CURRENT, "new": NEW, "confirm": NEW}
    )
    assert response.status_code == 303
    assert "flash=account.password_changed" in response.headers["location"]
    assert verify_password(NEW, _admin().password_hash)
    assert admin_client.get("/account").status_code == 200
    assert _events("password_changed")


def test_the_wrong_current_password_changes_nothing(admin_client):
    response = admin_client.post(
        "/account/password", data={"current": "nie-to-haslo", "new": NEW, "confirm": NEW}
    )
    assert response.status_code == 400
    assert verify_password(CURRENT, _admin().password_hash)


def test_two_different_new_passwords_are_refused(admin_client):
    response = admin_client.post(
        "/account/password", data={"current": CURRENT, "new": NEW, "confirm": NEW + "x"}
    )
    assert response.status_code == 400
    assert verify_password(CURRENT, _admin().password_hash)


def test_a_short_new_password_is_refused(admin_client):
    response = admin_client.post(
        "/account/password", data={"current": CURRENT, "new": "krotkie", "confirm": "krotkie"}
    )
    assert response.status_code == 400
    assert verify_password(CURRENT, _admin().password_hash)


def test_a_password_equal_to_the_username_is_refused(app_client):
    long_username = "administratorka"
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.username == "admin")).one()
        user.username = long_username
        session.add(user)
        session.commit()
    app_client.post("/login", data={"username": long_username, "password": CURRENT})

    response = app_client.post(
        "/account/password",
        data={"current": CURRENT, "new": long_username, "confirm": long_username},
    )
    assert response.status_code == 400


def test_the_language_preference_is_stored_on_the_account(admin_client):
    assert admin_client.post("/account/language", data={"language": "en"}).status_code == 303
    assert _admin().language == "en"


def test_an_unknown_language_falls_back_to_polish(admin_client):
    admin_client.post("/account/language", data={"language": "de"})
    assert _admin().language == "pl"


def test_a_failed_sign_in_is_recorded_without_the_password(app_client):
    app_client.post("/login", data={"username": "admin", "password": "zle-haslo"})
    details = [event.detail for event in _events("login_failed")]
    assert details == ["username=admin"]
    assert all("zle-haslo" not in detail for detail in details)


def test_the_account_locks_after_too_many_failures(app_client):
    for attempt in range(get_settings().login_max_attempts):
        app_client.post("/login", data={"username": "admin", "password": f"zle-{attempt}"})

    response = app_client.post("/login", data={"username": "admin", "password": CURRENT})
    assert response.status_code == 429
    assert "auth.locked" not in response.text  # the message is translated, not a raw key


def test_signing_in_successfully_clears_the_counter(app_client):
    for attempt in range(get_settings().login_max_attempts - 1):
        app_client.post("/login", data={"username": "admin", "password": f"zle-{attempt}"})
    assert (
        app_client.post("/login", data={"username": "admin", "password": CURRENT}).status_code
        == 303
    )

    for attempt in range(get_settings().login_max_attempts - 1):
        app_client.post("/login", data={"username": "admin", "password": f"znowu-{attempt}"})
    assert (
        app_client.post("/login", data={"username": "admin", "password": CURRENT}).status_code
        == 303
    )


def test_an_unknown_username_never_reveals_that_it_is_unknown(app_client):
    unknown = app_client.post("/login", data={"username": "nikt", "password": "cokolwiek"})
    known = app_client.post("/login", data={"username": "admin", "password": "cokolwiek"})
    assert unknown.status_code == known.status_code == 401


def test_setting_the_attempt_limit_to_zero_switches_the_lockout_off(app_client, monkeypatch):
    """Documented as a setting, so it has to actually do nothing rather than lock at zero."""
    from polgate.config import get_settings

    monkeypatch.setattr(get_settings(), "login_max_attempts", 0)
    for attempt in range(8):
        app_client.post("/login", data={"username": "admin", "password": f"zle-{attempt}"})

    assert (
        app_client.post("/login", data={"username": "admin", "password": CURRENT}).status_code
        == 303
    )


def test_the_first_admin_is_seeded_once_and_not_again(app_client):
    """ensure_admin runs on every start; a second account would be a surprise."""
    from sqlmodel import Session, select

    from polgate.auth.bootstrap import ensure_admin
    from polgate.db import get_engine

    ensure_admin()
    ensure_admin()

    with Session(get_engine()) as session:
        assert len(session.exec(select(User).where(User.username == "admin")).all()) == 1


def test_only_the_seeded_account_is_checked_against_the_env_password(app_client):
    from sqlmodel import Session

    from polgate.auth.bootstrap import uses_bootstrap_password
    from polgate.auth.passwords import hash_password
    from polgate.db import get_engine

    with Session(get_engine()) as session:
        other = User(username="ktos-inny", password_hash=hash_password(CURRENT))
        session.add(other)
        session.commit()
        session.refresh(other)
        assert uses_bootstrap_password(other) is False
        assert uses_bootstrap_password(_admin()) is True


async def test_a_burst_that_passes_the_lockout_check_together_is_still_limited(
    app_client, monkeypatch
):
    """Counting first and recording afterwards let every request of a burst pass the count.

    A barrier holds each request right after the lockout check until all of them have passed it,
    the worst interleaving there is, so the test does not depend on timing.
    """
    import asyncio
    import threading

    import httpx

    from polgate.auth import throttle
    from polgate.web import auth as auth_web

    burst = 12
    barrier = threading.Barrier(burst, timeout=20)
    real_is_locked = throttle.is_locked

    def is_locked_then_wait(session, tag):
        locked = real_is_locked(session, tag)
        barrier.wait()
        return locked

    checked: list[str] = []

    def counting_verify(password, password_hash):
        checked.append(password)
        return verify_password(password, password_hash)

    monkeypatch.setattr(throttle, "is_locked", is_locked_then_wait)
    monkeypatch.setattr(auth_web, "verify_password", counting_verify)
    transport = httpx.ASGITransport(app=app_client.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        answers = await asyncio.gather(
            *(
                client.post("/login", data={"username": "admin", "password": f"zle-{n}"})
                for n in range(burst)
            )
        )

    assert len(checked) <= get_settings().login_max_attempts
    assert {answer.status_code for answer in answers} <= {401, 429}


def test_an_unknown_or_disabled_account_still_runs_the_password_hash(app_client, monkeypatch):
    """A check that runs only for active accounts makes them six times slower to refuse."""
    from polgate.auth.passwords import decoy_hash, hash_password
    from polgate.web import auth as auth_web

    with Session(get_engine()) as session:
        session.add(
            User(username="frozen", password_hash=hash_password("frozen-pass"), is_active=False)
        )
        session.commit()
    checked: list[str] = []

    def recording(password, password_hash):
        checked.append(password_hash)
        return verify_password(password, password_hash)

    monkeypatch.setattr(auth_web, "verify_password", recording)
    assert app_client.post("/login", data={"username": "nikt", "password": "x"}).status_code == 401
    assert (
        app_client.post(
            "/login", data={"username": "frozen", "password": "frozen-pass"}
        ).status_code
        == 401
    )
    assert checked[0] == decoy_hash()
    assert verify_password("frozen-pass", checked[1])


def test_a_sign_in_is_recorded_once_as_a_success(app_client):
    app_client.post("/login", data={"username": "admin", "password": CURRENT})
    assert len(_events("login")) == 1
    assert _events("login_failed") == []


def test_wrong_current_passwords_count_towards_the_lockout(admin_client):
    """Anybody holding the session could otherwise guess the password here without limit."""
    for attempt in range(get_settings().login_max_attempts):
        admin_client.post(
            "/account/password", data={"current": f"zle-{attempt}", "new": NEW, "confirm": NEW}
        )

    response = admin_client.post(
        "/account/password", data={"current": CURRENT, "new": NEW, "confirm": NEW}
    )

    assert response.status_code == 429
    assert "Za dużo nieudanych prób" in response.text
    assert verify_password(CURRENT, _admin().password_hash)
    locked = admin_client.post("/login", data={"username": "admin", "password": CURRENT})
    assert locked.status_code == 429


def test_a_right_current_password_is_not_counted_as_a_failure(admin_client):
    admin_client.post("/account/password", data={"current": CURRENT, "new": NEW, "confirm": "x"})
    assert [event.detail for event in _events("password_confirmed")] == ["username=admin"]
    assert _events("login_failed") == []


@pytest.mark.parametrize("typed", ["50810112354", "admin-password-typed-in-the-wrong-box"])
def test_a_name_that_matches_no_account_is_recorded_only_as_its_hash(app_client, typed):
    """What lands in the username field is often a password or a PESEL."""
    from polgate.audit import sha256

    app_client.post("/login", data={"username": typed, "password": "cokolwiek"})

    details = [event.detail for event in _events("login_failed")]
    assert details == [f"username_sha256={sha256(typed)}"]
    app_client.post("/login", data={"username": "admin", "password": CURRENT})
    page = app_client.get("/admin/audit")
    assert page.status_code == 200
    assert typed not in page.text


def test_attempts_at_an_unknown_name_still_lock_it(app_client):
    for attempt in range(get_settings().login_max_attempts):
        app_client.post("/login", data={"username": "nikt", "password": f"zle-{attempt}"})
    response = app_client.post("/login", data={"username": "nikt", "password": "zle"})
    assert response.status_code == 429


def test_a_long_unknown_name_takes_no_more_room_than_a_short_one(app_client):
    app_client.post("/login", data={"username": "x" * 100_000, "password": "cokolwiek"})
    (event,) = _events("login_failed")
    assert len(event.detail) < 100
