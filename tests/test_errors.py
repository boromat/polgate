"""What a failed request looks like, and the readiness probe Docker restarts on."""

import pytest
from sqlalchemy.exc import OperationalError

HTML = {"Accept": "text/html,application/xhtml+xml"}
HTMX = {"HX-Request": "true"}


def test_a_browser_gets_a_page_it_can_read(admin_client):
    response = admin_client.get("/chat/999999", headers=HTML)
    assert response.status_code == 404
    assert "Nie ma takiej strony" in response.text
    assert "Not Found" not in response.text  # the developer's detail stays out of the page


def test_the_page_offers_a_way_back(admin_client):
    body = admin_client.get("/chat/999999", headers=HTML).text
    assert 'href="/"' in body
    assert ">404<" in body


def test_an_htmx_request_gets_one_line_rather_than_a_whole_page(admin_client):
    response = admin_client.get("/chat/999999", headers={**HTML, **HTMX})
    assert response.status_code == 404
    assert "<html" not in response.text
    assert response.text.strip().startswith("<li")


def test_a_script_still_gets_json(admin_client):
    """No Accept preference means no change: `*/*` is what a client sends when it does not care."""
    response = admin_client.get("/chat/999999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_a_forbidden_page_explains_itself_without_naming_the_rule(app_client):
    from sqlmodel import Session

    from polgate.auth.passwords import hash_password
    from polgate.db import get_engine
    from polgate.models import User

    with Session(get_engine()) as session:
        session.add(User(username="szeregowy", password_hash=hash_password("dlugie-haslo-x")))
        session.commit()
    app_client.post("/login", data={"username": "szeregowy", "password": "dlugie-haslo-x"})

    response = app_client.get("/admin", headers=HTML)

    assert response.status_code == 403
    assert "Nie masz tu dostępu" in response.text
    assert "admin only" not in response.text


def test_an_english_visitor_reads_english(admin_client):
    body = admin_client.get("/chat/999999?lang=en", headers=HTML).text
    assert "Nothing here" in body


def test_a_status_without_its_own_message_falls_back(admin_client):
    """Any code the table does not name still produces a page rather than a JSON blob."""
    from polgate.web.errors import FALLBACK, MESSAGES

    assert 418 not in MESSAGES
    assert FALLBACK == ("error.generic_title", "error.generic_body")


def test_an_anonymous_visitor_is_still_redirected_rather_than_shown_a_403(app_client):
    """LoginRequiredError has its own handler and must keep it."""
    response = app_client.get("/chat", headers=HTML)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_health_reports_the_database_it_can_read(app_client):
    payload = app_client.get("/health").json()
    assert payload == {"status": "ok", "version": payload["version"], "database": "ok"}


def test_health_turns_red_when_the_storage_is_gone(app_client, monkeypatch):
    """A probe that says ok while the database is unreachable tells Docker the wrong thing."""
    import polgate.main as main

    class _Broken:
        def __enter__(self):
            raise OperationalError("select 1", {}, Exception("no such table: user"))

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(main, "Session", lambda _engine: _Broken())

    response = app_client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "unreachable"


@pytest.mark.parametrize("accept", ["", "*/*", "application/json"])
def test_only_an_explicit_html_preference_gets_a_page(admin_client, accept):
    response = admin_client.get("/chat/999999", headers={"Accept": accept} if accept else {})
    assert response.headers["content-type"].startswith("application/json")


def test_a_message_of_spaces_is_refused_before_it_costs_anything(admin_client):
    """`required` on the textarea only rejects an empty string; a browser sends spaces happily."""
    from sqlmodel import Session, select

    from polgate.db import get_engine
    from polgate.models import PendingTurn

    admin_client.get("/chat")
    conversation_id = _conversation_id()

    response = admin_client.post(
        f"/chat/{conversation_id}/send", data={"text": "   \n\t ", "extra_terms": ""}
    )

    assert response.status_code == 400
    assert "Pusta wiadomość" in response.text
    with Session(get_engine()) as session:
        assert session.exec(select(PendingTurn)).all() == []


def test_a_real_message_still_goes_through(admin_client):
    admin_client.get("/chat")
    conversation_id = _conversation_id()
    response = admin_client.post(
        f"/chat/{conversation_id}/send", data={"text": "Dzień dobry", "extra_terms": ""}
    )
    assert response.status_code == 200
    assert "Dzień dobry" in response.text


def test_a_malformed_form_reaches_a_browser_as_a_page(admin_client):
    admin_client.get("/chat")
    conversation_id = _conversation_id()

    response = admin_client.post(f"/chat/{conversation_id}/send", data={}, headers=HTML)

    assert response.status_code == 422
    assert "Formularz jest niekompletny" in response.text


def test_a_malformed_form_still_reaches_a_script_as_json(admin_client):
    admin_client.get("/chat")
    conversation_id = _conversation_id()
    response = admin_client.post(f"/chat/{conversation_id}/send", data={})
    assert response.status_code == 422
    assert "detail" in response.json()


def _conversation_id() -> int:
    from sqlmodel import Session, select

    from polgate.db import get_engine
    from polgate.models import Conversation

    with Session(get_engine()) as session:
        return session.exec(select(Conversation)).first().id


@pytest.mark.parametrize("status", [400, 404, 409, 413, 503])
def test_htmx_swaps_an_error_answer_into_the_page(htmx_config, status):
    """Its default leaves a 4xx or 5xx out, and every error partial is written to be swapped in."""
    import re

    rules = htmx_config["responseHandling"]
    rule = next(rule for rule in rules if re.fullmatch(rule["code"], str(status)))
    assert rule["swap"] is True
    assert rule["error"] is True


def test_htmx_leaves_an_empty_answer_alone(htmx_config):
    rules = htmx_config["responseHandling"]
    assert rules[0] == {"code": "204", "swap": False}


def test_an_htmx_request_without_a_session_loads_the_login_page(app_client):
    """Following the redirect itself, HTMX swapped the whole login page into the chat."""
    response = app_client.post(
        "/files/upload?conversation_id=1",
        files={"file": ("notatka.txt", b"notatka", "text/plain")},
        headers={**HTMX, "HX-Current-URL": "http://hub.example/chat/7?flash=x"},
    )
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/login?next=/chat/7%3Fflash%3Dx"
    assert response.text == ""


def test_an_htmx_request_from_nowhere_returns_to_the_chat_after_login(app_client):
    response = app_client.get("/chat/7/messages/1/preview", headers=HTMX)
    assert response.headers["hx-redirect"] == "/login?next=/chat"


@pytest.mark.parametrize(
    "page", ["http://hub.example//evil.example/x", r"http://hub.example/\evil.example"]
)
def test_an_htmx_redirect_never_points_off_site(app_client, page):
    response = app_client.get("/chat", headers={**HTMX, "HX-Current-URL": page})
    assert response.headers["hx-redirect"] == "/login?next=/chat"
