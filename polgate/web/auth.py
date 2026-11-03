"""Login, logout and the landing page."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, select

from polgate.auth.passwords import decoy_hash, verify_password
from polgate.auth.session import end_all_sessions, login_user, logout_user
from polgate.auth.throttle import SUCCESS_EVENT, begin_attempt, settle
from polgate.db import get_session
from polgate.models import User
from polgate.rendering import render
from polgate.security import safe_next

router = APIRouter()

LOCKED_STATUS = 429


@router.get("/")
def index(request: Request) -> Response:
    """Show the landing page, or send a signed-in user straight to the chat."""
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse("/chat", status_code=303)
    return render(request, "index.html")


@router.get("/login")
def login_form(request: Request, next: str = "/chat") -> Response:
    """Render the login form."""
    return render(request, "login.html", next_url=safe_next(next))


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/chat"),
    session: Session = Depends(get_session),
) -> Response:
    """Verify credentials and start a session.

    The "no such account", "account disabled" and "wrong password" paths render the same message
    and all run argon2, so neither the answer nor the time it takes tells which usernames exist.
    """
    target = safe_next(next)
    user = session.exec(select(User).where(User.username == username)).first()
    attempt = begin_attempt(session, username, user.id if user is not None else None)
    if attempt is None:
        return render(
            request, "login.html", locked=True, next_url=target, status_code=LOCKED_STATUS
        )

    # One read of the row, after begin_attempt's commit expired it: the hash checked and the epoch
    # the cookie is issued for belong to the same moment. A password changed during the check then
    # leaves this cookie already dead instead of handing the old password a fresh session.
    stored_hash = user.password_hash if user is not None else decoy_hash()
    epoch = user.session_epoch if user is not None else 0
    matches = verify_password(password, stored_hash)
    if user is None or not user.is_active or not matches:
        return render(
            request,
            "login.html",
            error=True,
            username=username,
            next_url=target,
            status_code=401,
        )

    settle(session, attempt, SUCCESS_EVENT)
    response = RedirectResponse(target, status_code=303)
    login_user(response, user, epoch=epoch)
    return response


@router.post("/logout")
def logout(request: Request, session: Session = Depends(get_session)) -> Response:
    """End the account's sessions and return to the landing page.

    A POST, so that a link or an image on another site cannot sign the employee out. Deleting the
    cookie alone would leave a copy of it working; raising the epoch ends that copy as well.
    """
    signed_in = getattr(request.state, "user", None)
    if isinstance(signed_in, User):
        end_all_sessions(session, signed_in)
        session.commit()
    response = RedirectResponse("/", status_code=303)
    logout_user(response)
    return response
