"""Data subject requests: find what the hub holds about one person, export it, erase it.

The value that names the person travels in the body of a POST and nowhere else: never in a URL,
which a browser's history, a proxy's log and a Referer header keep, and never in the audit trail,
which records the counts and a keyed fingerprint of the value instead.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlmodel import Session

from polgate import audit, subjects
from polgate.auth.deps import require_admin
from polgate.db import get_session
from polgate.knowledge import store as knowledge_store
from polgate.models import User, utcnow
from polgate.rendering import render
from polgate.vault import store as vault

router = APIRouter(prefix="/admin/subjects")

#: Characters of the value's keyed fingerprint in the audit trail: enough to tie the find, the
#: export and the erasure of one request together, and nothing to read the value from.
_FINGERPRINT_CHARS = 16


def _detail(value: str, counts: dict[str, int]) -> str:
    fingerprint = vault.value_hash(value.strip())[:_FINGERPRINT_CHARS]
    return f"subject={fingerprint} " + " ".join(f"{key}={n}" for key, n in counts.items())


@router.get("")
def subject_form(
    request: Request,
    admin: User = Depends(require_admin),
) -> Response:
    """Show the form that names the person a request is about."""
    return render(request, "admin/subjects.html", value="", report=None, erased=None)


@router.post("")
def find_subject(
    request: Request,
    value: str = Form(""),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Count what the hub holds about the person, before anything is exported or erased."""
    try:
        report = subjects.find(session, value)
    except ValueError:
        return render(
            request,
            "admin/subjects.html",
            status_code=400,
            value=value,
            report=None,
            erased=None,
            error="subjects.too_short",
        )
    audit.log(
        session, event="subject_found", user_id=admin.id, detail=_detail(value, report.counts())
    )
    return render(request, "admin/subjects.html", value=value, report=report, erased=None)


@router.post("/export")
def export_subject(
    request: Request,
    value: str = Form(""),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Download what the hub holds about the person, with only this person restored."""
    try:
        report = subjects.find(session, value)
    except ValueError:
        return find_subject(request, value, admin, session)
    audit.log(
        session,
        event="subject_exported",
        user_id=admin.id,
        detail=_detail(value, report.counts()),
    )
    filename = f"polgate-subject-{utcnow():%Y%m%d-%H%M%S}.json"
    return Response(
        subjects.export(session, report),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/erase")
def erase_subject(
    request: Request,
    value: str = Form(""),
    confirm: str = Form(""),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Erase the person from every conversation, file and knowledge base document."""
    if confirm != "yes":
        return find_subject(request, value, admin, session)
    # The same lock as a knowledge base upload: both write the knowledge base's tokens.
    with knowledge_store.WRITE_LOCK:
        try:
            report = subjects.find(session, value)
        except ValueError:
            return find_subject(request, value, admin, session)
        counts = subjects.erase(session, report)
    audit.log(session, event="subject_erased", user_id=admin.id, detail=_detail(value, counts))
    return render(request, "admin/subjects.html", value="", report=None, erased=counts)
