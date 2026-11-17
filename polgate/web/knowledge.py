"""Administration of the company knowledge base: upload, review, publish and withdraw documents."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session

from polgate import audit
from polgate.auth.deps import require_admin
from polgate.config import get_settings
from polgate.db import get_session
from polgate.files.extract import SUPPORTED_SUFFIXES, UnsupportedFileError, extract_text
from polgate.knowledge import index, store
from polgate.knowledge.index import KnowledgeError
from polgate.models import KnowledgeDocument, KnowledgeStatus, User
from polgate.policy_loader import get_policy, masking_is_off
from polgate.rendering import render
from polgate.vault import store as vault
from polgate.web.chat import build_anonymizer
from polgate.web.files import EXTRACTION_ERRORS, discard_upload, read_capped, store_upload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/knowledge")

#: Why a document was not added or published -> what the administrator is told. Written out so
#: every key appears verbatim in the source and the i18n coverage test can find it.
KNOWLEDGE_ERRORS = {
    "empty": "knowledge.error_empty",
    "too_long": "knowledge.error_too_long",
    "masking": "knowledge.error_masking",
    "refused": "knowledge.error_refused",
    "endpoint": "knowledge.error_endpoint",
    "no_model": "knowledge.error_no_model",
    "indexing": "knowledge.error_indexing",
}


def _load(session: Session, document_id: int) -> KnowledgeDocument:
    document = session.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return document


def _outdated(session: Session, model: str) -> list[KnowledgeDocument]:
    """Return the published documents with passages the configured model has not embedded."""
    if not model:
        return []
    return [
        document
        for document in store.documents(session)
        if document.status is KnowledgeStatus.PUBLISHED
        and len(store.vectors(session, _require_id(document), model)) < document.passages
    ]


def _list_page(
    request: Request, session: Session, error: str | None = None, status_code: int = 200
) -> Response:
    settings = get_settings()
    policy = get_policy(session)
    documents = store.documents(session)
    return render(
        request,
        "admin/knowledge.html",
        status_code=status_code,
        documents=documents,
        # The names they were uploaded under, for the administrators who uploaded them.
        titles={document.id: _original_title(document) for document in documents},
        configured=bool(settings.embedding_model),
        enabled=policy.knowledge_base,
        masking_off=masking_is_off(policy),
        outdated=len(_outdated(session, settings.embedding_model)),
        error=error,
    )


@router.get("")
def knowledge_list(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """List the knowledge base's documents, with the form that adds one."""
    return _list_page(request, session)


@router.post("")
async def upload(
    request: Request,
    upload_file: UploadFile = File(..., alias="file"),
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Extract a document, mask it with stable tokens and keep it for review."""
    if masking_is_off(get_policy(session)):
        return _list_page(request, session, "knowledge.error_masking_off", 400)
    payload = await read_capped(upload_file)
    if payload is None:
        return _list_page(request, session, "files.too_large", 413)
    filename = upload_file.filename or "upload"
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return _list_page(request, session, "files.unsupported", 400)
    mime_type = upload_file.content_type or "application/octet-stream"
    try:
        text = await asyncio.to_thread(_extract, payload, suffix, mime_type, filename)
    except UnsupportedFileError as exc:
        return _list_page(
            request, session, EXTRACTION_ERRORS.get(str(exc), "files.unsupported"), 400
        )
    try:
        ingested = await asyncio.to_thread(
            index.ingest,
            session,
            build_anonymizer(session),
            get_settings(),
            user_id=_require_user_id(admin),
            filename=filename,
            text=text,
            mime_type=mime_type,
            size_bytes=len(payload),
        )
    except KnowledgeError as exc:
        return _list_page(request, session, KNOWLEDGE_ERRORS[exc.reason], 400)
    document = ingested.document
    audit.log(
        session,
        event="knowledge_uploaded",
        user_id=admin.id,
        entity_counts=ingested.entity_counts,
        detail=f"document_id={document.id} passages={document.passages}",
    )
    return RedirectResponse(f"/admin/knowledge/{document.id}", status_code=303)


def _extract(payload: bytes, suffix: str, mime_type: str, filename: str) -> str:
    """Read the text of an upload from a file that is deleted again when this returns.

    The extractors read from a path. A knowledge base document's bytes are not kept: its masked
    passages are all the search needs, and the original stays with whoever uploaded it. The file
    is stored the way an attachment is, so one that Windows would not let go of yet is a stored
    upload no row refers to, which the retention job deletes within the hour.
    """
    stored = store_upload(payload, suffix)
    try:
        return extract_text(stored, mime_type, filename)
    finally:
        discard_upload(stored)


@router.get("/{document_id}")
def review(
    request: Request,
    document_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Show a document's masked passages, to publish it or withdraw it."""
    return _review_page(request, session, _load(session, document_id))


def _review_page(
    request: Request,
    session: Session,
    document: KnowledgeDocument,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "admin/knowledge_document.html",
        status_code=status_code,
        document=document,
        original_title=_original_title(document),
        passages=store.masked_passages(session, _require_id(document)),
        configured=bool(get_settings().embedding_model),
        error=error,
    )


@router.post("/{document_id}/publish")
async def publish(
    request: Request,
    document_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Embed a reviewed document's passages, so every conversation's turns may draw on it."""
    document = _load(session, document_id)
    try:
        embedded = await index.publish(session, document, get_settings())
    except KnowledgeError as exc:
        code = status.HTTP_502_BAD_GATEWAY if exc.reason == "endpoint" else 400
        return _review_page(request, session, document, KNOWLEDGE_ERRORS[exc.reason], code)
    audit.log(
        session,
        event="knowledge_published",
        user_id=admin.id,
        detail=f"document_id={document_id} embedded={embedded}",
    )
    return RedirectResponse("/admin/knowledge?flash=knowledge.published_flash", status_code=303)


@router.post("/{document_id}/withdraw")
def withdraw(
    document_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Delete a document, its passages and the tokens no other document uses."""
    # Waits for an upload being masked: withdrawn halfway, it lost tokens it had just stored.
    with store.WRITE_LOCK:
        removed = store.withdraw(session, _load(session, document_id))
    audit.log(
        session,
        event="knowledge_withdrawn",
        user_id=admin.id,
        detail=f"document_id={document_id} tokens_removed={removed}",
    )
    return RedirectResponse("/admin/knowledge?flash=knowledge.withdrawn_flash", status_code=303)


@router.post("/reindex")
async def reindex(
    request: Request,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    """Embed the published documents again for the configured model, after it changed."""
    settings = get_settings()
    documents = _outdated(session, settings.embedding_model)
    embedded = 0
    try:
        for document in documents:
            embedded += await index.publish(session, document, settings)
    except KnowledgeError as exc:
        code = status.HTTP_502_BAD_GATEWAY if exc.reason == "endpoint" else 400
        return _list_page(request, session, KNOWLEDGE_ERRORS[exc.reason], code)
    audit.log(
        session,
        event="knowledge_reindexed",
        user_id=admin.id,
        detail=f"documents={len(documents)} embedded={embedded}",
    )
    return RedirectResponse("/admin/knowledge?flash=knowledge.reindexed_flash", status_code=303)


def _original_title(document: KnowledgeDocument) -> str:
    return vault.decrypt(document.title_enc) if document.title_enc else document.title


def _require_id(document: KnowledgeDocument) -> int:
    if document.id is None:  # pragma: no cover - rows come from the database
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return document.id


def _require_user_id(user: User) -> int:
    if user.id is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user.id
