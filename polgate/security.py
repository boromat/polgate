"""HTTP hardening: response headers and a same-origin check for state-changing requests.

The hub sits between employees and a cloud provider, so a stolen session or an injected script is
not a browser nuisance here: it is a way to read the decrypted history the vault exists to protect.
Two cheap, framework-level defences cover most of that risk.

**Content Security Policy.** Every script and every style is served from ``/static``. There is no
inline script, no inline event handler and no inline ``style`` attribute anywhere in the templates,
so the policy needs neither ``unsafe-inline`` nor ``unsafe-eval``, and an injected ``<script>``
simply does not run. ``tests/test_security.py`` fails if an inline handler returns to a template.

**CSRF.** Session cookies are ``SameSite=Lax``, which already stops cross-site form posts in current
browsers. The origin check below is the belt to that pair of braces: a state-changing request whose
``Origin`` or ``Sec-Fetch-Site`` header says it came from anywhere but this origin is refused.
Requests with neither header (curl, tests, older clients) are allowed through, because rejecting
them would break scripted use without stopping any browser-based attack.

"Same site" is not good enough. ``SameSite=Lax`` cookies travel with requests from every subdomain
of the company's domain, so a wiki or an intranet tool with a script injected into it, or a
forgotten subdomain somebody took over, could otherwise create an administrator here or switch the
masking off.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from polgate.config import get_settings

logger = logging.getLogger(__name__)

#: Methods that may change state and therefore need the origin check.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        # The SSE stream is same-origin; the provider is called from the server, never the browser.
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)

BASE_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}

#: Pages carry decrypted history, so they must not sit in a shared cache. Static assets are public
#: and keep their own validators.
NO_STORE_HEADER = "no-store"
STATIC_PREFIX = "/static/"

HSTS_HEADER = "max-age=31536000; includeSubDomains"


def is_same_origin(request: Request) -> bool:
    """Return whether a state-changing request came from this origin.

    A missing ``Origin`` and a missing ``Sec-Fetch-Site`` means a non-browser client, which is
    treated as allowed: a browser sends ``Origin`` with every cross-site POST, and
    ``Sec-Fetch-Site`` with every request to an HTTPS or localhost address. A cross-site GET over
    plain HTTP can carry neither, which is why no GET may change state, and why the one that does,
    the answer stream, checks more than this. ``Sec-Fetch-Site: none`` is a request the user
    started themselves, from the address bar or a bookmark.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "")
    return urlsplit(origin).netloc == host


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the hardening headers and refuses cross-site state-changing requests."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Check the origin, run the rest of the stack, then stamp the response headers."""
        if request.method in UNSAFE_METHODS and not is_same_origin(request):
            logger.warning("refused cross-site %s to %s", request.method, request.url.path)
            return harden(request, PlainTextResponse("cross-site request refused", status_code=403))
        return harden(request, await call_next(request))


def harden(request: Request, response: Response) -> Response:
    """Stamp the hardening headers on `response`, keeping any it already carries.

    The middleware stamps everything that passes through it. A response made outside it, such as
    the page for an unhandled error, which Starlette builds further out, calls this itself.
    """
    for header, value in BASE_HEADERS.items():
        response.headers.setdefault(header, value)
    if not request.url.path.startswith(STATIC_PREFIX):
        response.headers.setdefault("Cache-Control", NO_STORE_HEADER)
    if get_settings().cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", HSTS_HEADER)
    return response


#: Prefixes a browser reads as "somewhere else", even though the string starts with a slash.
_OFF_SITE_PREFIXES = ("//", "/\\", "/%2f", "/%5c")


def safe_next(target: str, fallback: str = "/chat") -> str:
    """Return ``target`` when it points inside this hub, otherwise ``fallback``.

    ``/login?next=https://evil.example`` would otherwise let an attacker borrow the hub's domain for
    the first hop of a phishing flow: the employee clicks a link to their own company tool and lands
    on someone else's page already signed out.
    """
    candidate = target.strip()
    lowered = candidate.lower()
    if not candidate.startswith("/") or lowered.startswith(_OFF_SITE_PREFIXES):
        return fallback
    # A browser drops a tab or a line break inside a URL, so "/\t/evil.example" arrives as
    # "//evil.example". Nothing a page here links to needs a control character.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        return fallback
    return candidate
