"""Administration: users, policy, audit trail and statistics."""

from __future__ import annotations

import json
from collections import Counter
from enum import StrEnum

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from polanon.policy import ENTITY_TYPES
from polanon.types import JudgeFallback
from sqlmodel import Session, col, desc, func, select

from polgate import audit
from polgate.auth.bootstrap import uses_bootstrap_password
from polgate.auth.deps import require_admin
from polgate.auth.passwords import MIN_LENGTH, hash_password, password_problem
from polgate.auth.session import end_all_sessions
from polgate.db import get_session
from polgate.models import AuditEvent, HilMode, Message, Role, User, utcnow
from polgate.policy_loader import get_policy, masking_is_off
from polgate.rendering import render

router = APIRouter(prefix="/admin")

#: Rows per page in the audit table.
PAGE_SIZE = 50


@router.get("")
def dashboard(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Show the admin landing page with headline numbers."""
    policy = get_policy(session)
    entity_counts: Counter[str] = Counter()
    stage2 = 0
    for event in session.exec(select(AuditEvent)).all():
        entity_counts.update(json.loads(event.entity_counts_json or "{}"))
        stage2 += event.stage2_hits
    return render(
        request,
        "admin/dashboard.html",
        user_count=session.exec(select(func.count()).select_from(User)).one(),
        message_count=session.exec(select(func.count()).select_from(Message)).one(),
        entity_counts=entity_counts.most_common(),
        entity_total=sum(entity_counts.values()),
        stage2_hits=stage2,
        policy=policy,
        default_password=uses_bootstrap_password(admin),
        masking_off=masking_is_off(policy),
    )


@router.get("/users")
def users(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """List every account."""
    return render(
        request,
        "admin/users.html",
        users=session.exec(select(User).order_by(col(User.id))).all(),
        min_length=MIN_LENGTH,
    )


@router.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    language: str = Form("pl"),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Create an account."""
    if session.exec(select(User).where(User.username == username)).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username taken")
    if password_problem(password, username=username) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too weak")
    account_role = _choice(Role, role)
    session.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role=account_role,
            language=language if language in ("pl", "en") else "pl",
        )
    )
    session.commit()
    audit.log(session, event="user_created", user_id=admin.id, detail=f"username={username}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: int,
    password: str = Form(...),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Set a new password for another account, ending the sessions it has.

    A reset is what an administrator does for an account that may be in someone else's hands, so
    a session opened with the old password must not outlive it. An administrator's own password
    changes on the account page instead, where the current one is checked under the lockout: here,
    holding the session alone would be enough to take the account over.
    """
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="own password: use /account"
        )
    if password_problem(password, username=target.username) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too weak")
    target.password_hash = hash_password(password)
    end_all_sessions(session, target)
    session.add(target)
    session.commit()
    audit.log(session, event="password_reset", user_id=admin.id, detail=f"user_id={user_id}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def toggle_user(
    user_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Activate or deactivate an account."""
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if target.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="cannot disable yourself"
        )
    target.is_active = not target.is_active
    # Enabling the account again must not bring back a cookie from before it was disabled.
    end_all_sessions(session, target)
    session.add(target)
    session.commit()
    audit.log(
        session,
        event="user_toggled",
        user_id=admin.id,
        detail=f"user_id={user_id} active={target.is_active}",
    )
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/policy")
def policy_form(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Show the masking and retention policy."""
    policy = get_policy(session)
    return render(
        request,
        "admin/policy.html",
        policy=policy,
        all_entities=sorted(ENTITY_TYPES),
        selected={e.strip() for e in policy.enabled_entities.split(",")},
        masking_off=masking_is_off(policy),
    )


@router.post("/policy")
async def update_policy(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Persist the policy form."""
    form = await request.form()
    policy = get_policy(session)
    # Read before anything is set, so a value the form never offers changes nothing at all.
    hil_chat = _choice(HilMode, form.get("hil_chat", policy.hil_chat.value))
    hil_files = _choice(HilMode, form.get("hil_files", policy.hil_files.value))
    judge_fallback = _choice(JudgeFallback, form.get("judge_fallback", policy.judge_fallback.value))
    changed: list[str] = []

    def _set(field: str, value: object) -> None:
        if getattr(policy, field) != value:
            changed.append(field)
            setattr(policy, field, value)

    _set("hil_chat", hil_chat)
    _set("hil_files", hil_files)
    _set("judge_enabled", form.get("judge_enabled") is not None)
    _set("judge_fallback", judge_fallback)
    _set("vault_ttl_days", _optional_int(form.get("vault_ttl_days")))
    _set("history_ttl_days", _optional_int(form.get("history_ttl_days")))
    entities = [value for value in form.getlist("entities") if isinstance(value, str)]
    _set("enabled_entities", ",".join(sorted(entities)))
    _set("denylist", str(form.get("denylist", "")).strip())
    _set("allowlist", str(form.get("allowlist", "")).strip())
    policy.updated_at = utcnow()

    session.add(policy)
    session.commit()
    # A policy with no entity types disables masking entirely, so the audit says that in words
    # rather than leaving it to be inferred from a field name.
    note = " masking_disabled" if masking_is_off(policy) else ""
    audit.log(
        session,
        event="policy_changed",
        user_id=admin.id,
        detail=f"fields={','.join(changed) or 'none'}{note}",
    )
    return RedirectResponse("/admin/policy", status_code=303)


@router.get("/audit")
def audit_log(
    request: Request,
    page: int = 0,
    event: str = "",
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Browse the audit trail."""
    query = select(AuditEvent).order_by(desc(col(AuditEvent.id)))
    if event:
        query = query.where(col(AuditEvent.event) == event)
    rows = session.exec(query.offset(max(page, 0) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    known_events = sorted({row.event for row in session.exec(select(AuditEvent)).all()})
    return render(
        request,
        "admin/audit.html",
        rows=rows,
        page=max(page, 0),
        has_next=len(rows) == PAGE_SIZE,
        event=event,
        known_events=known_events,
    )


def _choice[Choice: StrEnum](kind: type[Choice], value: object) -> Choice:
    """Return `value` as a member of `kind`, or refuse the form when it is not one.

    The forms only offer valid values, so anything else was typed by hand; it gets the translated
    400 page instead of an unhandled error.
    """
    try:
        return kind(str(value))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown {kind.__name__}"
        ) from None


def _optional_int(value: object) -> int | None:
    """Parse a retention field, where an empty value means 'keep forever'."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None
