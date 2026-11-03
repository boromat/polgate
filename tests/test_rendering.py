"""Token highlighting: the one place where the hub deliberately emits markup of its own."""

import os
from datetime import UTC, datetime, timedelta, timezone

from markupsafe import Markup

from polgate import rendering
from polgate.rendering import highlight_tokens, iso_utc


def test_a_static_asset_url_changes_when_the_file_does(tmp_path, monkeypatch):
    """A cached app.js outlived an upgrade and Enter stopped sending messages."""
    asset = tmp_path / "app.js"
    asset.write_text("one", encoding="utf-8")
    monkeypatch.setattr(rendering, "STATIC_DIR", tmp_path)
    first = rendering.asset_url("app.js")
    os.utime(asset, ns=(1_000_000_000, 2_000_000_000))
    assert first.startswith("/static/app.js?v=")
    assert rendering.asset_url("app.js") != first


def test_a_zoneless_timestamp_from_the_database_is_marked_as_utc():
    """A browser reads a timestamp without a zone as local time and shows the wrong hour."""
    assert iso_utc(datetime(2026, 9, 10, 15, 23)) == "2026-09-10T15:23:00+00:00"


def test_a_timestamp_that_already_has_a_zone_is_left_as_it_is():
    warsaw = timezone(timedelta(hours=2))
    assert iso_utc(datetime(2026, 9, 10, 17, 23, tzinfo=warsaw)) == "2026-09-10T17:23:00+02:00"
    assert iso_utc(datetime(2026, 9, 10, 15, 23, tzinfo=UTC)).endswith("+00:00")


def test_tokens_are_wrapped_for_display():
    result = str(highlight_tokens("Witaj <OSOBA_1>"))
    assert result == 'Witaj <span class="token">&lt;OSOBA_1&gt;</span>'


def test_surrounding_text_is_escaped():
    result = str(highlight_tokens("<script>alert(1)</script> <OSOBA_1>"))
    assert "<script>" not in result
    assert "&lt;script&gt;" in result
    assert '<span class="token">' in result


def test_ampersands_and_quotes_are_escaped():
    assert "&amp;" in str(highlight_tokens("Kowalski & Syn"))


def test_text_without_tokens_is_returned_escaped_and_unwrapped():
    assert str(highlight_tokens("zwykły tekst")) == "zwykły tekst"


def test_result_is_markup_so_jinja_does_not_escape_it_again():
    assert isinstance(highlight_tokens("<OSOBA_1>"), Markup)


def test_every_token_in_a_long_text_is_wrapped():
    text = "<OSOBA_1> pisze do <FIRMA_2> w sprawie <PESEL_1>"
    assert str(highlight_tokens(text)).count('class="token"') == 3


def test_the_language_falls_back_through_query_cookie_account_and_default():
    """Four sources, in that order; anything unrecognised is skipped rather than trusted."""
    from types import SimpleNamespace

    from polgate.rendering import resolve_language

    def request(query=None, cookie=None, user_language=None):
        user = SimpleNamespace(language=user_language) if user_language else None
        return SimpleNamespace(
            query_params={"lang": query} if query else {},
            cookies={"lang": cookie} if cookie else {},
            state=SimpleNamespace(user=user),
        )

    assert resolve_language(request(query="en", cookie="pl", user_language="pl")) == "en"
    assert resolve_language(request(cookie="en", user_language="pl")) == "en"
    assert resolve_language(request(user_language="en")) == "en"
    assert resolve_language(request()) == "pl"
    assert resolve_language(request(query="de", cookie="kl")) == "pl"
