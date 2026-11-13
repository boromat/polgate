"""FastAPI application factory.

Run locally with ``uvicorn polgate.main:app --reload``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from polgate import audit, retention
from polgate.auth.deps import LoginRequiredError
from polgate.auth.session import SessionMiddleware
from polgate.config import get_settings
from polgate.db import get_engine, init_db
from polgate.policy_loader import get_policy
from polgate.rendering import TEMPLATE_DIR
from polgate.security import SecurityHeadersMiddleware, harden, safe_next
from polgate.web import account, admin, auth, chat, errors, files

logger = logging.getLogger(__name__)

STATIC_DIR = TEMPLATE_DIR.parent / "static"
PURGE_INTERVAL_SECONDS = 3600


def run_retention_pass() -> None:
    """Apply the retention policy once, and never raise.

    This runs on a loop for the life of the process, so a failure has to be survivable: a schema the
    code has outgrown, a disk that filled up, a file already deleted. It is logged with its
    traceback and the next pass tries again an hour later.
    """
    try:
        with Session(get_engine()) as session:
            report = retention.purge(session, get_policy(session), get_settings())
            if report.total:
                audit.log(session, event="retention_purge", detail=report.as_detail())
    except Exception:
        logger.exception("retention pass failed")


async def _retention_loop() -> None:
    """Apply the retention policy at startup and once an hour afterwards.

    The pass reads whole tables and deletes files, so it runs in a thread: on the event loop it held
    every request and answer stream of the hub until it finished.
    """
    while True:
        await asyncio.to_thread(run_retention_pass)
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare storage, seed the first admin account and run the retention job."""
    settings = get_settings()
    database_path = settings.database_url.removeprefix("sqlite:///")
    if database_path and ":memory:" not in database_path:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    from polgate.auth.bootstrap import ensure_admin
    from polgate.auth.passwords import decoy_hash

    ensure_admin()
    # Hashed now, not on the first unknown name, which would otherwise answer twice as slowly.
    decoy_hash()
    task = asyncio.create_task(_retention_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    """Build the application with middleware, routers and HTML-friendly error handling."""
    application = FastAPI(title="polgate", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.add_middleware(SessionMiddleware)
    # Added last so it runs first, stamping every response and rejecting cross-site posts before
    # any handler sees them.
    application.add_middleware(SecurityHeadersMiddleware)
    for module in (auth, account, chat, files, admin):
        application.include_router(module.router)

    @application.exception_handler(LoginRequiredError)
    async def _login_required(request: Request, exc: LoginRequiredError) -> Response:
        """Send anonymous visitors to the login page instead of returning a JSON error.

        An HTMX request would follow a redirect itself and swap the whole login page into a corner
        of the chat, so it is told to load the login page instead, returning to the page the
        employee was on rather than to the endpoint the request went to.
        """
        if errors.is_htmx(request):
            page = urlsplit(request.headers.get("hx-current-url", ""))
            next_url = safe_next(page.path + (f"?{page.query}" if page.query else ""))
            return Response(headers={"HX-Redirect": f"/login?next={quote(next_url, safe='/')}"})
        return RedirectResponse(f"/login?next={quote(exc.next_url, safe='/')}", status_code=303)

    # LoginRequiredError is an HTTPException too, so its own handler has to be registered first.
    application.add_exception_handler(StarletteHTTPException, errors.http_exception_handler)
    # A malformed form is a 422 that never reaches the handler above, because FastAPI raises its
    # own class for it.
    application.add_exception_handler(RequestValidationError, errors.validation_error_handler)

    @application.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception) -> Response:
        """Answer an unhandled error in the visitor's language and with the hardening headers.

        Starlette calls this from its outermost middleware, outside the one that stamps the
        headers, and still re-raises afterwards, so the traceback reaches the server log as before.
        """
        try:
            response = await errors.http_exception_handler(
                request, StarletteHTTPException(status_code=500)
            )
        except Exception:
            logger.exception("the error page itself failed")
            response = PlainTextResponse("Internal Server Error", status_code=500)
        return harden(request, response)

    @application.get("/health")
    def health() -> Response:
        """Readiness probe for Docker and uptime checks.

        It counts the accounts rather than running ``SELECT 1``, because SQLite happily creates an
        empty file when the database is missing: opening a connection proves nothing, reading a
        table the hub cannot work without proves the storage is really there.
        """
        from polgate import __version__
        from polgate.models import User

        try:
            with Session(get_engine()) as session:
                session.exec(select(func.count()).select_from(User)).one()
        except SQLAlchemyError:
            logger.exception("health check: the database is not reachable")
            return JSONResponse(
                {"status": "degraded", "version": __version__, "database": "unreachable"},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "version": __version__, "database": "ok"})

    return application


app = create_app()
