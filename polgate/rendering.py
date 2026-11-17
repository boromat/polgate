"""Template environment and the shared ``render`` helper.

Kept separate from :mod:`polgate.main` so routers can render pages without importing the application
factory, which would create an import cycle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from markupsafe import Markup, escape

from polgate.config import get_settings
from polgate.i18n import translator
from polgate.models import FileAsset
from polgate.tokens import WRITTEN_TOKEN_RE
from polgate.vault import store as vault

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _link_open(
    renderer: RendererHTML, tokens: list[Token], index: int, options: OptionsDict, env: EnvType
) -> str:
    """Open a link from an answer so the page it leads to learns nothing about this one."""
    tokens[index].attrSet("rel", "noopener noreferrer")
    return renderer.renderToken(tokens, index, options, env)


#: Markdown as models write it, with what an answer must not do switched off. Raw HTML is
#: escaped, not rendered. Images are not rendered either: a picture loads by itself, and one whose
#: address a model wrote with a token in it would carry the restored value to whoever serves the
#: picture, before anybody clicked anything. markdown-it refuses javascript: and data: links.
_MARKDOWN = (
    MarkdownIt("commonmark", {"html": False, "breaks": True})
    .enable(["table", "strikethrough"])
    .disable("image")
)
_MARKDOWN.add_render_rule("link_open", _link_open)


def markdown(text: str) -> Markup:
    """Render a model's answer, as restored for its reader, from Markdown into safe HTML."""
    # Safe by construction: markdown-it escapes every character of the text it does not turn into
    # markup of its own, and raw HTML is off.
    return Markup(_MARKDOWN.render(text))  # noqa: S704


def highlight_tokens(text: str) -> Markup:
    """Escape `text` and wrap anonymization tokens in a highlight span.

    Tokens are matched on the raw text, not on the escaped output: escaping first would turn
    ``<OSOBA_1>`` into ``&lt;OSOBA_1&gt;`` and the pattern would never match. A token with the case
    a model wrote after it, ``<OSOBA_1:wołacz>``, is highlighted whole.
    """
    parts: list[str] = []
    cursor = 0
    for match in WRITTEN_TOKEN_RE.finditer(text):
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
templates.env.filters["markdown"] = markdown

#: What a token in a title is written as where no markup can go, such as the browser's tab.
REDACTION_MARK = "\u25ae"


def redacted(text: str) -> Markup:
    """Draw the tokens of a stored title as redaction bars, and escape everything else.

    A title is built from the masked text and keeps its tokens; on the page they are what they
    stand for, a value that did not leave, not a label to read.
    """
    parts: list[str] = []
    cursor = 0
    for match in WRITTEN_TOKEN_RE.finditer(text):
        parts.append(str(escape(text[cursor : match.start()])))
        parts.append('<span class="redacted" aria-hidden="true"></span>')
        cursor = match.end()
    parts.append(str(escape(text[cursor:])))
    # Safe by construction: every fragment above is escaped and only our own markup is added.
    return Markup("".join(parts))  # noqa: S704


def redacted_plain(text: str) -> str:
    """Write the tokens of a stored title as a mark, for the browser's tab."""
    return WRITTEN_TOKEN_RE.sub(REDACTION_MARK, text)


templates.env.filters["redacted"] = redacted
templates.env.filters["redacted_plain"] = redacted_plain

#: What each entity type is called on the page. Written out so every key appears verbatim in the
#: source, where the i18n coverage test looks for it; a type a later polanon adds and this map does
#: not know yet is shown by its code, which the page shows beside the name anyway.
ENTITY_LABELS = {
    "PERSON": "entity.person",
    "ORG": "entity.org",
    "LOCATION": "entity.location",
    "PESEL": "entity.pesel",
    "NIP": "entity.nip",
    "REGON": "entity.regon",
    "KRS": "entity.krs",
    "PL_ID": "entity.pl_id",
    "PL_PASSPORT": "entity.pl_passport",
    "DRIVING_LICENCE": "entity.driving_licence",
    "IBAN": "entity.iban",
    "CREDIT_CARD": "entity.credit_card",
    "EMAIL": "entity.email",
    "LINK": "entity.link",
    "PHONE": "entity.phone",
    "POSTAL_CODE": "entity.postal_code",
    "SECRET": "entity.secret",
    "IP_ADDRESS": "entity.ip_address",
    "MAC_ADDRESS": "entity.mac_address",
    "ADDRESS": "entity.address",
    "PLATE": "entity.plate",
    "VIN": "entity.vin",
    "DATE": "entity.date",
    "BIRTH_DATE": "entity.birth_date",
    "TAX_ID": "entity.tax_id",
    "COMPANY_ID": "entity.company_id",
    "BANK_ACCOUNT": "entity.bank_account",
    "OTHER": "entity.other",
}
templates.env.globals["entity_labels"] = ENTITY_LABELS
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
        "knowledge.published_flash",
        "knowledge.withdrawn_flash",
        "knowledge.reindexed_flash",
        "chat.deleted_flash",
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
