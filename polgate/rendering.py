"""Template environment and the shared ``render`` helper.

Kept separate from :mod:`polgate.main` so routers can render pages without importing the application
factory, which would create an import cycle.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from polanon.policy import TOKEN_PATTERN

from polgate.config import get_settings
from polgate.i18n import translator
from polgate.models import FileAsset
from polgate.vault import store as vault

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

_TOKEN_RE = re.compile(TOKEN_PATTERN)


def highlight_tokens(text: str) -> Markup:
    """Escape `text` and wrap anonymization tokens in a highlight span.

    Tokens are matched on the raw text, not on the escaped output: escaping first would turn
    ``<OSOBA_1>`` into ``&lt;OSOBA_1&gt;`` and the pattern would never match.
    """
    parts: list[str] = []
    cursor = 0
    for match in _TOKEN_RE.finditer(text):
        parts.append(str(escape(text[cursor : match.start()])))
        parts.append(f'<span class="token">{escape(match.group(0))}</span>')
        cursor = match.end()
    parts.append(str(escape(text[cursor:])))
    # Safe by construction: every fragment above is escaped and only our own markup is added.
    return Markup("".join(parts))  # noqa: S704


def iso_utc(moment: datetime) -> str:
    """Return an ISO 8601 timestamp that says it is in UTC.

    SQLite hands datetimes back without a zone, and a browser reads a zoneless timestamp as local
    time, so the page could never turn it into the reader's own clock.
    """
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.isoformat()


STATIC_DIR = Path(__file__).parent / "static"


def asset_url(name: str) -> str:
    """Return the URL of a static file, versioned so that it changes whenever the file does.

    Static files are served cacheable. Without a version a browser kept the previous ``app.js``
    after an upgrade: the new templates declared hooks the old script did not know, and Enter
    stopped sending messages until the cache let go.
    """
    version = (STATIC_DIR / name).stat().st_mtime_ns
    return f"/static/{name}?v={version:x}"


def original_filename(asset: FileAsset) -> str:
    """Return the name a file was uploaded under, for its owner's own page.

    The ``filename`` column holds the masked name so the database at rest keeps no original. A row
    written before the encrypted column existed has only that column, holding the name as given.
    """
    return vault.decrypt(asset.filename_enc) if asset.filename_enc else asset.filename


def entity_counts(counts_json: str | None) -> list[tuple[str, int]]:
    """Return an audit row's entity counts, most frequent first, for a list of labelled counts."""
    counts: dict[str, int] = json.loads(counts_json or "{}")
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


templates.env.filters["highlight_tokens"] = highlight_tokens
templates.env.filters["entity_counts"] = entity_counts
templates.env.filters["original_filename"] = original_filename
templates.env.filters["iso_utc"] = iso_utc
templates.env.globals["asset_url"] = asset_url

# Icon macros are a global rather than a per-template import: a `{% import %}` in the base layout is
# not visible inside blocks that child templates define.
templates.env.globals["icon"] = templates.env.get_template("icons.html").module


def resolve_language(request: Request) -> str:
    """Pick the UI language: query string, then cookie, then user preference, then the default."""
    user = getattr(request.state, "user", None)
    candidates = (
        request.query_params.get("lang"),
        request.cookies.get("lang"),
        user.language if user is not None else None,
        get_settings().default_language,
    )
    for candidate in candidates:
        if candidate in ("pl", "en"):
            return candidate
    return "pl"


#: Messages a redirect may ask the next page to show. A whitelist, so a crafted link cannot put
#: arbitrary text on the page.
FLASH_KEYS = frozenset(
    {
        "files.approved_flash",
        "files.rejected_flash",
        "account.password_changed",
    }
)


def flash_key(request: Request) -> str | None:
    """Return the translation key named by ``?flash=``, if it is one we recognise."""
    key = request.query_params.get("flash", "")
    return key if key in FLASH_KEYS else None


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    """Render a template with the translator, language and current user already in context."""
    language = resolve_language(request)
    context.setdefault("flash", flash_key(request))
    context.update(
        {
            "t": translator(language),
            "lang": language,
            "user": getattr(request.state, "user", None),
        }
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)
