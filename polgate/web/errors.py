"""What a visitor sees when a request fails.

FastAPI answers an ``HTTPException`` with ``{"detail": "..."}``. That is right for an API and wrong
for a hub an employee opens in a browser: the detail strings here are notes to a developer ("admin
only", "nothing to stream") and they are in English, so neither the format nor the content belongs
in front of somebody who has just clicked a stale link.

Three shapes, chosen by what the client asked for:

* an HTMX request gets one translated line, because its response is swapped into a corner of a page
  that is otherwise fine;
* a browser gets a full page with a way back;
* anything else keeps the JSON, so scripts and tests are unaffected.

The status code is shown; the detail never is.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from polgate.rendering import render

#: Status code -> the pair of translation keys describing it. Written out so both keys appear
#: verbatim in the source and the i18n coverage test can find them.
MESSAGES: dict[int, tuple[str, str]] = {
    403: ("error.403_title", "error.403_body"),
    404: ("error.404_title", "error.404_body"),
    409: ("error.409_title", "error.409_body"),
    413: ("error.413_title", "error.413_body"),
    422: ("error.422_title", "error.422_body"),
    429: ("error.429_title", "error.429_body"),
}
FALLBACK = ("error.generic_title", "error.generic_body")


def wants_html(request: Request) -> bool:
    """Return whether this is a browser navigation rather than a script or a test.

    ``*/*`` deliberately does not count: a client that expresses no preference gets the JSON it
    would have got before.
    """
    return "text/html" in request.headers.get("accept", "")


def is_htmx(request: Request) -> bool:
    """Return whether HTMX sent this request and will swap the answer into part of a page."""
    return request.headers.get("hx-request") == "true"


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Render an HTTP error the way this client can use it."""
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    title_key, body_key = MESSAGES.get(status, FALLBACK)

    if is_htmx(request):
        return render(request, "partials/error_line.html", status_code=status, reason=body_key)
    if wants_html(request):
        return render(
            request,
            "error.html",
            status_code=status,
            status=status,
            title_key=title_key,
            body_key=body_key,
        )
    detail = getattr(exc, "detail", "error")
    return JSONResponse({"detail": detail}, status_code=status)


async def validation_error_handler(request: Request, exc: Exception) -> Response:
    """Answer a malformed form the way this client can use it.

    FastAPI raises its own class here and answers with a list of field errors, which is useful to a
    developer reading a stack trace and meaningless to somebody whose browser submitted the form.
    """
    if is_htmx(request) or wants_html(request):
        return await http_exception_handler(request, StarletteHTTPException(status_code=422))
    return JSONResponse({"detail": getattr(exc, "errors", lambda: "invalid")()}, status_code=422)
