"""Sign-in throttling.

argon2id already makes guessing expensive, but a hub reachable from the office network still needs a
ceiling on how many guesses one account will accept. Failures are counted from the audit trail
rather than from process memory, so the limit holds across workers and survives a restart, and the
attempts show up in the same place an auditor is already looking.

An attempt is written down as a failure *before* its password is checked, and counted together with
every attempt already written; only the first ``LOGIN_MAX_ATTEMPTS`` of a window go on to the check.
Counting first and writing the failure after the check let every request of a parallel burst pass
the count before any of them had written anything: forty guesses for a limit of five. SQLite
serialises the writes, so the n-th attempt written always counts at least n.

Every check of an account's password goes through here, the "current password" on the account page
included: anything that tells a right password from a wrong one is a way to guess it.
"""

from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, col, func, select

from polgate import audit
from polgate.config import get_settings
from polgate.models import AuditEvent, utcnow

FAILURE_EVENT = "login_failed"
SUCCESS_EVENT = "login"
#: A right password that did not sign anyone in: the current password on the account page.
CONFIRMED_EVENT = "password_confirmed"


def username_tag(username: str, *, known: bool) -> str:
    """Return the audit ``detail`` marker that attributes an attempt, and its lockout, to a name.

    A name that belongs to no account is kept only as its SHA-256. What arrives in that field is
    often not a username at all: a password typed into the wrong box, a PESEL, or as much text as
    an anonymous client cares to send. The hash still counts attempts at the same name.
    """
    if known:
        return f"username={username}"
    return f"username_sha256={audit.sha256(username)}"


def is_locked(session: Session, tag: str) -> bool:
    """Return whether this account has spent its attempts inside the lockout window.

    Only failures newer than the last successful sign-in count, so getting the password right
    clears the counter without a separate table to keep tidy.
    """
    settings = get_settings()
    if settings.login_max_attempts <= 0:
        return False
    return recent_failures(session, tag) >= settings.login_max_attempts


def begin_attempt(session: Session, username: str, user_id: int | None) -> AuditEvent | None:
    """Record a password attempt before it is checked; return None when the account is locked.

    The returned row says ``login_failed`` until the caller settles it with :func:`settle`, so an
    attempt that is abandoned halfway, or whose password was wrong, counts as the failure it is.

    Args:
        session: open database session; this function commits.
        username: the name exactly as it was typed.
        user_id: the account with that name, or None when there is none.
    """
    tag = username_tag(username, known=user_id is not None)
    if is_locked(session, tag):
        return None
    attempt = audit.log(session, event=FAILURE_EVENT, user_id=user_id, detail=tag)
    limit = get_settings().login_max_attempts
    if limit > 0 and recent_failures(session, tag) > limit:
        return None
    return attempt


def settle(session: Session, attempt: AuditEvent, event: str) -> None:
    """Record that an attempt's password was right, as `event` instead of a failure.

    `SUCCESS_EVENT` also resets the count, since failures are counted from the last sign-in.
    """
    attempt.event = event
    session.add(attempt)
    session.commit()


def recent_failures(session: Session, tag: str) -> int:
    """Count failed sign-ins under `tag` since the last success, inside the lockout window."""
    settings = get_settings()
    cutoff = utcnow() - timedelta(minutes=settings.login_lockout_minutes)
    query = (
        select(func.count())
        .select_from(AuditEvent)
        .where(col(AuditEvent.event) == FAILURE_EVENT)
        .where(col(AuditEvent.detail) == tag)
        .where(col(AuditEvent.created_at) >= cutoff)
    )
    last_success = session.exec(
        select(func.max(col(AuditEvent.id)))
        .where(col(AuditEvent.event) == SUCCESS_EVENT)
        .where(col(AuditEvent.detail) == tag)
    ).one()
    if last_success is not None:
        query = query.where(col(AuditEvent.id) > last_success)
    return int(session.exec(query).one())
