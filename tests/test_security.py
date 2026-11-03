"""HTTP hardening: headers, cross-site posts, redirects and the no-inline-code rule."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from polgate.security import BASE_HEADERS, CONTENT_SECURITY_POLICY, safe_next

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "polgate" / "templates"
APP_JS = (ROOT / "polgate" / "static" / "app.js").read_text(encoding="utf-8")

JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
#: A statement or an expression: `{% if count > 1 %}` holds a `>` that would end the tag around it
#: and hide whatever attribute the block adds, so each one is replaced by a space before parsing.
JINJA_BLOCK = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
#: Attributes htmx evaluates as JavaScript whatever their value.
EVALUATED_ATTRIBUTES = ("hx-on", "data-hx-on", "hx-vars", "data-hx-vars")
#: htmx evaluates these when their value starts with `js:`, and a trigger's `[filter]` always.
VALUES = (
    "hx-vals",
    "data-hx-vals",
    "hx-headers",
    "data-hx-headers",
    "hx-request",
    "data-hx-request",
)
TRIGGERS = ("hx-trigger", "data-hx-trigger")


class _InlineCode(HTMLParser):
    """Collect every piece of markup that would need `unsafe-inline` or a string evaluated.

    A parser rather than patterns: attribute names arrive lowercased and unquoted values arrive
    whole, which is where the patterns this replaced let `ONCLICK=`, `onclick=x` and `<SCRIPT>`
    through.
    """

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            text = (value or "").strip().lower()
            if name.startswith("on"):
                self.found.append(f"inline event handler {name}")
            elif name == "style":
                self.found.append("inline style attribute")
            elif name.startswith(EVALUATED_ATTRIBUTES):
                self.found.append(f"{name} is evaluated from a string")
            elif name in VALUES and text.startswith(("js:", "javascript:")):
                self.found.append(f"{name} asks htmx to evaluate {value!r}")
            elif name in TRIGGERS and "[" in text:
                self.found.append(f"{name} filters with an evaluated expression")
            elif text.startswith("javascript:"):
                self.found.append(f"javascript: URL in {name}")
        if tag == "script" and not any(name == "src" for name, _ in attrs):
            self.found.append("inline <script> block")
        if tag == "style":
            self.found.append("inline <style> block")

    handle_startendtag = handle_starttag


def inline_code(markup: str) -> list[str]:
    """Return what in `markup` the Content-Security-Policy would have to allow."""
    parser = _InlineCode()
    parser.feed(JINJA_BLOCK.sub(" ", JINJA_COMMENT.sub("", markup)))
    parser.close()
    return parser.found


DATA_HOOK = re.compile(
    r"\s(data-(?:stream|sink|status|autogrow|submit-on-enter|local-time|sent-meta|masked-count|"
    r"count|nothing-masked|preview-button|preview-target|"
    r"reset-on-success|remove-on-success|autosubmit|names|name|print|width))"
)


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_every_response_carries_the_hardening_headers(app_client):
    response = app_client.get("/login")
    for header, value in BASE_HEADERS.items():
        assert response.headers[header] == value
    assert response.headers["cache-control"] == "no-store"


def test_the_policy_allows_no_inline_or_evaluated_script():
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in CONTENT_SECURITY_POLICY


def test_static_assets_stay_cacheable(app_client):
    response = app_client.get("/static/app.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") != "no-store"


@pytest.mark.parametrize("path", _templates(), ids=lambda p: p.name)
def test_no_template_carries_inline_code(path):
    """The strict Content-Security-Policy is only true while this holds."""
    assert inline_code(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "markup",
    [
        '<button onclick="alert(1)">',
        "<button onclick=alert(1)>",
        '<a ONCLICK="x">',
        "<div style=color:red>",
        '<div STYLE="color: red">',
        "<SCRIPT>alert(1)</SCRIPT>",
        "<script data-src=x>alert(1)</script>",
        "<style>body { color: red }</style>",
        '<div hx-on:click="alert(1)">',
        '<div data-hx-on:click="alert(1)">',
        '<div hx-vars="a: alert(1)">',
        '<div hx-vals="js:{a: alert(1)}">',
        '<input hx-trigger="keyup[alert(1)]">',
        '<a href="javascript:alert(1)">',
        '<a href=" JavaScript:alert(1)">',
        '<form action="javascript:alert(1)">',
        "<div hx-headers='js:{\"X\": alert(1)}'>",
        "<div hx-request='js:{timeout: alert(1)}'>",
        "<div data-hx-request='js:{timeout: alert(1)}'>",
        '<button {% if unread > 0 %} onclick="markRead()"{% endif %}>',
        '<li {% if count > 1 %} style="font-weight: bold"{% endif %}>',
        '<div {% if n > 0 %} hx-on:click="go()"{% endif %}>',
    ],
)
def test_the_inline_code_check_catches_every_form(markup):
    """The check this replaced missed most of these; a guard that cannot fail guards nothing."""
    assert inline_code(markup)


@pytest.mark.parametrize(
    "markup",
    [
        '<script src="/static/app.js" defer></script>',
        '<div hx-vals=\'{"page": 2}\' hx-trigger="click">',
        '<a href="/chat/7" data-print>',
        "{# a <style> element, mentioned in a comment #}",
    ],
)
def test_the_inline_code_check_leaves_ordinary_markup_alone(markup):
    assert inline_code(markup) == []


def _every_kind_of_page(client) -> dict[str, str]:
    """Render every template there is, partials included, with something in it."""
    from fastapi.testclient import TestClient
    from sqlmodel import Session, select

    from polgate.db import get_engine
    from polgate.models import FileAsset, HilMode, Message, MessageRole, Policy
    from polgate.vault import store as vault

    htmx = {"HX-Request": "true"}
    conversation = client.post("/chat/new").headers["location"]
    conversation_id = int(conversation.rsplit("/", 1)[1])
    upload = f"/files/upload?conversation_id={conversation_id}"
    with Session(get_engine()) as session:
        message = Message(
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content_original_enc=vault.encrypt("Jan Kowalski"),
            content_sent="<OSOBA_1>",
        )
        session.add(message)
        session.commit()
        message_id = message.id
    pages = {
        "file row": client.post(upload, files={"file": ("a.txt", b"Jan", "text/plain")}),
        "file error": client.post(upload, files={"file": ("a.zip", b"PK", "application/zip")}),
        "notice": client.post(f"{conversation}/send", data={"text": " "}),
        "message pair": client.post(f"{conversation}/send", data={"text": "Jan Kowalski"}),
        "preview": client.get(f"{conversation}/messages/{message_id}/preview"),
        "error line": client.get("/chat/999999", headers=htmx),
        "error page": client.get("/chat/999999", headers={"Accept": "text/html"}),
    }
    with Session(get_engine()) as session:
        file_id = session.exec(select(FileAsset)).first().id
        policy = session.exec(select(Policy)).one()
        policy.hil_chat = HilMode.REVIEW
        session.add(policy)
        session.commit()
    pages["send review"] = client.post(f"{conversation}/send", data={"text": "Jan Kowalski"})
    for path in (
        "/login",
        conversation,
        f"/files/{file_id}/review",
        "/account",
        "/admin",
        "/admin/users",
        "/admin/policy",
        "/admin/audit",
    ):
        pages[path] = client.get(path)
    pages["landing"] = TestClient(client.app).get("/")
    return {name: response.text for name, response in pages.items()}


def test_no_rendered_page_carries_inline_code(admin_client):
    """Templates build attributes at render time too; what the browser gets is what counts."""
    pages = _every_kind_of_page(admin_client)
    assert len(pages) == 17
    for name, markup in pages.items():
        assert markup.strip(), name
        assert inline_code(markup) == [], name


def test_htmx_never_evaluates_strings_or_runs_swapped_scripts(htmx_config):
    """Kept off even though the policy would block both, in case the policy is ever loosened."""
    assert htmx_config["allowEval"] is False
    assert htmx_config["allowScriptTags"] is False


def test_every_behaviour_hook_a_template_declares_is_handled():
    """A renamed data- attribute would otherwise silently stop doing anything."""
    declared = {
        match
        for path in _templates()
        for match in DATA_HOOK.findall(path.read_text(encoding="utf-8"))
    }
    assert declared, "the selector list is out of date"
    unhandled = sorted(hook for hook in declared if hook not in APP_JS)
    assert not unhandled, f"declared in a template but ignored by app.js: {unhandled}"


def test_a_cross_site_post_is_refused(app_client):
    response = app_client.post(
        "/login",
        data={"username": "admin", "password": "admin-password"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_a_same_origin_post_is_accepted(app_client):
    response = app_client.post(
        "/login",
        data={"username": "admin", "password": "admin-password"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 303


@pytest.mark.parametrize(
    "target",
    ["https://evil.example", "//evil.example", "/\\evil.example", "/%2fevil.example", "chat"],
)
def test_an_off_site_next_is_replaced_by_the_default(target):
    assert safe_next(target) == "/chat"


@pytest.mark.parametrize("target", ["/chat/7", "/admin/policy?page=2"])
def test_a_local_next_survives(target):
    assert safe_next(target) == target


def test_login_never_redirects_off_site(app_client):
    response = app_client.post(
        "/login",
        data={"username": "admin", "password": "admin-password", "next": "https://evil.example"},
    )
    assert response.headers["location"] == "/chat"


def test_the_session_cookie_is_locked_down(app_client):
    response = app_client.post("/login", data={"username": "admin", "password": "admin-password"})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_hsts_appears_only_when_cookies_are_marked_secure(app_client, monkeypatch):
    from polgate.config import get_settings

    assert "strict-transport-security" not in app_client.get("/login").headers
    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    assert "strict-transport-security" in app_client.get("/login").headers


def test_a_post_from_a_sibling_subdomain_is_refused(admin_client):
    """A page elsewhere on the company's domain sends the admin's Lax cookie along."""
    from sqlmodel import Session, select

    from polgate.db import get_engine
    from polgate.models import User

    response = admin_client.post(
        "/admin/users",
        data={"username": "mallory", "password": "mallory-password-1", "role": "admin"},
        headers={"Sec-Fetch-Site": "same-site", "Origin": "https://wiki.company.example"},
    )
    assert response.status_code == 403
    with Session(get_engine()) as session:
        assert session.exec(select(User).where(User.username == "mallory")).first() is None


def test_a_request_the_user_started_is_accepted(app_client):
    response = app_client.post(
        "/login",
        data={"username": "admin", "password": "admin-password"},
        headers={"Sec-Fetch-Site": "none"},
    )
    assert response.status_code == 303


def test_a_cross_site_refusal_carries_the_hardening_headers(app_client):
    response = app_client.post("/chat/new", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    for header, value in BASE_HEADERS.items():
        assert response.headers[header] == value
    assert response.headers["cache-control"] == "no-store"


def _failing_login_page(app_client, monkeypatch):
    from fastapi.testclient import TestClient

    from polgate.web import auth as auth_web

    def broken(*_args, **_kwargs):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(auth_web, "render", broken)
    return TestClient(app_client.app, raise_server_exceptions=False)


def test_an_unhandled_error_is_a_translated_page_with_the_hardening_headers(
    app_client, monkeypatch
):
    client = _failing_login_page(app_client, monkeypatch)
    response = client.get("/login", headers={"Accept": "text/html"})
    assert response.status_code == 500
    assert "Coś poszło nie tak" in response.text
    assert "template exploded" not in response.text
    for header, value in BASE_HEADERS.items():
        assert response.headers[header] == value
    assert response.headers["cache-control"] == "no-store"


def test_an_error_page_that_fails_itself_still_answers_hardened(app_client, monkeypatch):
    from polgate.web import errors

    client = _failing_login_page(app_client, monkeypatch)

    async def also_broken(*_args, **_kwargs):
        raise RuntimeError("error page exploded")

    monkeypatch.setattr(errors, "http_exception_handler", also_broken)
    response = client.get("/login", headers={"Accept": "text/html"})
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("target", ["/\t/evil.example", "/\x0b/evil.example", "/chat\x7f"])
def test_a_next_with_a_control_character_is_replaced(target):
    """A browser drops a tab inside a URL, which turns this into a scheme-relative address."""
    assert safe_next(target) == "/chat"


def test_the_decoy_hash_is_ready_before_the_first_sign_in(app_client):
    """Computed on the first unknown name, it made that one answer twice as slowly."""
    from fastapi.testclient import TestClient

    from polgate.auth.passwords import decoy_hash

    decoy_hash.cache_clear()
    with TestClient(app_client.app):
        assert decoy_hash.cache_info().currsize == 1
