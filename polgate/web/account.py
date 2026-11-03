"""The signed-in employee's own account: password and interface language."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session

from polgate import audit
from polgate.auth.deps import current_user
from polgate.auth.passwords import MIN_LENGTH, hash_password, password_problem, verify_password
from polgate.auth.session import end_all_sessions, login_user
from polgate.auth.throttle import CONFIRMED_EVENT, begin_attempt, settle
from polgate.db import get_session
from polgate.models import User
from polgate.rendering import render

router = APIRouter(prefix="/account")

#: Rejection reason -> translation key. Written out rather than built from the code so every key
#: appears verbatim in the source and the i18n coverage test can find it.
PASSWORD_ERRORS = {
    "wrong_current": "account.error_current",
    "mismatch": "account.error_mismatch",
    "too_short": "account.error_short",
    "same_as_username": "account.error_username",
}


@router.get("")
def account_page(request: Request, user: User = Depends(current_user)) -> Response:
    """Show the account page with the password form."""
    return render(request, "account.html", min_length=MIN_LENGTH)


@router.post("/password")
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Change the signed-in user's password after re-checking the current one.

    The current password is checked under the same lockout as signing in. Otherwise anybody holding
    the session for a while, through a stolen cookie or an unlocked workstation, could try
    passwords here without limit and leave with the one that outlives the session.
    """
    attempt = begin_attempt(session, user.username, user.id)
    if attempt is None:
        return _refuse(request, "auth.locked", status_code=429)
    # The row as it is now, not the copy loaded when the request came in: the current password is
    # checked against the hash the account has at this moment.
    account = session.get(User, user.id)
    if account is None or not verify_password(current, account.password_hash):
        return _refuse(request, PASSWORD_ERRORS["wrong_current"])
    settle(session, attempt, CONFIRMED_EVENT)
    reason = _rejection(account, new=new, confirm=confirm)
    if reason is not None:
        return _refuse(request, PASSWORD_ERRORS[reason])

    account.password_hash = hash_password(new)
    # A new password is what somebody sets when they think another person has the old one, or a
    # cookie: every other browser is signed out, and this one gets a cookie for the new epoch.
    epoch = end_all_sessions(session, account)
    session.add(account)
    session.commit()
    audit.log(session, event="password_changed", user_id=account.id)

    response = RedirectResponse("/account?flash=account.password_changed", status_code=303)
    login_user(response, account, epoch=epoch)
    return response


@router.post("/language")
def change_language(
    language: str = Form(...),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Persist the interface language so it follows the user to another browser."""
    user.language = language if language in ("pl", "en") else "pl"
    session.add(user)
    session.commit()
    return RedirectResponse(f"/account?lang={user.language}", status_code=303)


def _refuse(request: Request, error: str, status_code: int = 400) -> Response:
    return render(
        request, "account.html", min_length=MIN_LENGTH, error=error, status_code=status_code
    )


def _rejection(user: User, *, new: str, confirm: str) -> str | None:
    """Return why a new password is refused, or None when it may go ahead."""
    if new != confirm:
        return "mismatch"
    return password_problem(new, username=user.username)
