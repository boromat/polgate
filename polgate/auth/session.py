"""Signed-cookie sessions.

There is no server-side session table: the cookie carries a signed user id and is verified on every
request. That keeps the hub stateless enough to restart without logging everyone out, while a
rotated ``SECRET_KEY`` invalidates every session at once. Without one, :mod:`polgate.keys` generates
a key on first start and keeps it beside the database.

The cookie also carries the account's ``session_epoch``, and a cookie from an older epoch is
refused. Raising it is how one account's sessions end everywhere at once: a changed or reset
password is the usual answer to a suspected compromise, and without this a stolen cookie kept
working for its whole lifetime afterwards. Signing out raises it too, so a copied cookie does not
outlive the sign-out; the price is that signing out in one browser signs the account out in all.

The cookie is ``HttpOnly`` so a script cannot read it, ``SameSite=Lax`` so another site cannot post
with it, and ``Secure`` whenever ``COOKIE_SECURE`` is on, which every deployment behind TLS should
set.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import update
from sqlmodel import Session, col, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from polgate.config import get_settings
from polgate.db import get_engine
from polgate.keys import session_secret
from polgate.models import User

logger = logging.getLogger(__name__)

COOKIE_NAME = "polgate_session"
LANGUAGE_COOKIE = "lang"
_SALT = "polgate.session"


def max_age_seconds() -> int:
    """How long a session cookie stays valid, from ``SESSION_MAX_AGE_DAYS``."""
    return get_settings().session_max_age_days * 24 * 3600


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(session_secret(), salt=_SALT)


def _load_user(token: str) -> User | None:
    try:
        payload = _serializer().loads(token, max_age=max_age_seconds())
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    user_id, epoch = payload.get("uid"), payload.get("epoch")
    if not isinstance(user_id, int) or not isinstance(epoch, int):
        return None
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.id == user_id)).first()
    if user is None or not user.is_active or user.session_epoch != epoch:
        return None
    return user


class SessionMiddleware(BaseHTTPMiddleware):
    """Loads the signed-in user into ``request.state.user`` for every request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Resolve the session cookie, then run the rest of the stack."""
        token = request.cookies.get(COOKIE_NAME)
        request.state.user = _load_user(token) if token else None
        response = await call_next(request)
        language = request.query_params.get("lang")
        if language in ("pl", "en") and request.cookies.get(LANGUAGE_COOKIE) != language:
            response.set_cookie(
                LANGUAGE_COOKIE,
                language,
                max_age=max_age_seconds(),
                httponly=False,
                samesite="lax",
                secure=get_settings().cookie_secure,
            )
        return response


def login_user(response: Response, user: User, *, epoch: int | None = None) -> None:
    """Attach a session cookie for `user` to the response.

    Args:
        response: the response that signs the browser in.
        user: the account.
        epoch: the session epoch the cookie belongs to, the account's current one by default.
            Sign-in passes the epoch it read in the same moment as the password hash it checked,
            so a password changed while the check ran leaves the new cookie already dead.
    """
    issued_in = user.session_epoch if epoch is None else epoch
    token = _serializer().dumps({"uid": user.id, "epoch": issued_in})
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )


def logout_user(response: Response) -> None:
    """Clear the session cookie."""
    response.delete_cookie(COOKIE_NAME)


def end_all_sessions(session: Session, user: User) -> int:
    """Make every session cookie issued to `user` so far invalid, and return the new epoch.

    The database raises the epoch from the value it holds, not from the copy loaded with the
    request: two of these running at once must move it twice, or a cookie issued between them
    outlives the second. The caller commits. A browser that should stay signed in gets a cookie for
    the returned epoch.
    """
    statement = (
        update(User)
        .where(col(User.id) == user.id)
        .values(session_epoch=col(User.session_epoch) + 1)
        .returning(col(User.session_epoch))
    )
    return int(session.execute(statement).scalar_one())
