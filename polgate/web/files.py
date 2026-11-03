"""File uploads: extract, anonymize, review and attach to a conversation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from polanon import AnonymizationResult, JudgeError
from polanon.policy import entity_type_for_token
from sqlmodel import Session

from polgate import audit
from polgate.auth.deps import current_user
from polgate.config import get_settings
from polgate.db import get_session
from polgate.files.extract import (
    MAX_BYTES,
    OCR_UNAVAILABLE,
    SUPPORTED_SUFFIXES,
    UNPACKS_TOO_LARGE,
    UNREADABLE,
    UnsupportedFileError,
    extract_text,
)
from polgate.models import Conversation, FileAsset, FileStatus, HilMode, User
from polgate.policy_loader import get_policy
from polgate.rendering import render
from polgate.vault import store as vault
from polgate.web.chat import build_anonymizer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files")

#: Read size for uploads; small enough that the limit is enforced long before memory is.
CHUNK_BYTES = 64 * 1024

#: Why a file could not be read -> what the employee is told. Written out so every key appears
#: verbatim in the source and the i18n coverage test can find it.
EXTRACTION_ERRORS = {
    OCR_UNAVAILABLE: "files.ocr_unavailable",
    UNREADABLE: "files.unreadable",
    UNPACKS_TOO_LARGE: "files.unpacks_too_large",
}


def _load_conversation(session: Session, conversation_id: int, user: User) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return conversation


def _load_asset(session: Session, file_id: int, user: User) -> FileAsset:
    asset = session.get(FileAsset, file_id)
    if asset is None or asset.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return asset


@router.post("/upload")
async def upload(
    request: Request,
    conversation_id: int,
    upload_file: UploadFile = File(..., alias="file"),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Store an upload, extract its text and anonymize it before it can be attached."""
    conversation = _load_conversation(session, conversation_id, user)
    payload = await _read_capped(upload_file)
    if payload is None:
        return render(
            request, "partials/file_error.html", reason="files.too_large", status_code=413
        )
    filename = upload_file.filename or "upload"
    # Checked before anything is written: the stored name then never carries more of the
    # employee's file name than an ending the hub knows, and a file it cannot read never lands.
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return render(
            request, "partials/file_error.html", reason="files.unsupported", status_code=400
        )

    stored = _store(payload, suffix)
    try:
        asset, result = await _ingest(session, conversation, user, upload_file, filename, stored)
    except UnsupportedFileError as exc:
        _discard(stored)
        reason = EXTRACTION_ERRORS.get(str(exc), "files.unsupported")
        return render(request, "partials/file_error.html", reason=reason, status_code=400)
    except JudgeError as exc:
        _discard(stored)
        logger.warning("upload blocked: judge unavailable (%s)", type(exc).__name__)
        audit.log(
            session,
            event="file_blocked_judge",
            user_id=user.id,
            conversation_id=conversation.id,
            detail="stage-2 judge unavailable and policy is 'block'",
        )
        return render(
            request, "partials/file_error.html", reason="files.judge_unavailable", status_code=503
        )
    except BaseException:
        # Nothing refers to the bytes until the row is committed. Whatever went wrong, the
        # employee's document goes now, not an hour later with the retention job's orphan sweep.
        _discard(stored)
        raise

    audit.log(
        session,
        event="file_uploaded",
        user_id=user.id,
        conversation_id=conversation.id,
        entity_counts=result.entity_counts,
        stage2_hits=result.judge_hits,
        detail=f"bytes={asset.size_bytes}",
    )
    return render(request, "partials/file_row.html", file=asset)


def _store(payload: bytes, suffix: str) -> Path:
    """Write an upload under a random name and return where it went."""
    upload_dir = get_settings().upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"{uuid.uuid4().hex}{suffix}"
    stored.write_bytes(payload)
    # The bytes are the employee's document, not a public asset: keep them to the service account.
    stored.chmod(0o600)
    return stored


def _discard(stored: Path) -> None:
    """Delete a stored upload without raising, so a failed clean-up never hides why it was needed.

    Windows will not delete a file another thread still has open; the caller has either arranged a
    second try or leaves the file to the retention job's orphan sweep.
    """
    try:
        stored.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove upload %s yet: %s", stored.name, type(exc).__name__)


def _discard_when_done(extraction: asyncio.Task[str], stored: Path) -> None:
    """Delete an upload once the extraction of a request that went away has let go of it."""
    if not extraction.cancelled():
        # Retrieved, so asyncio does not report an exception nobody was waiting for any more.
        extraction.exception()
    _discard(stored)


async def _ingest(
    session: Session,
    conversation: Conversation,
    user: User,
    upload_file: UploadFile,
    filename: str,
    stored: Path,
) -> tuple[FileAsset, AnonymizationResult]:
    """Extract, mask and record a stored upload; return its committed row and the masking."""
    conversation_id = _require_conversation_id(conversation)
    # Parsing a large document is CPU-bound and the hub is shared: on the event loop it would hold
    # every other employee's page and answer stream until it finished.
    extraction = asyncio.ensure_future(
        asyncio.to_thread(extract_text, stored, upload_file.content_type or "", filename)
    )
    try:
        # Shielded: a request cancelled because the browser went away cannot stop the thread,
        # which keeps reading the file, and Windows refuses to delete a file that is open. The
        # task is kept so the file goes as soon as the thread is done with it.
        text = await asyncio.shield(extraction)
    except asyncio.CancelledError:
        extraction.add_done_callback(lambda done: _discard_when_done(done, stored))
        raise

    policy = get_policy(session)
    anonymizer = build_anonymizer(session)
    mapping = vault.load_mapping(session, conversation_id)
    result = await anonymizer.anonymize(f"{filename}\n\n{text}", mapping)

    vault.save_mapping(
        session,
        conversation_id,
        result.mapping,
        {token: entity_type_for_token(token) for token in result.mapping},
        policy.vault_ttl_days,
    )
    asset = FileAsset(
        conversation_id=conversation_id,
        user_id=_require_user_id(user),
        # The upload name is often the most personal part of a file ("CV Jan Kowalski.pdf"). The
        # masked name is the first line of the masked text, which begins with the file name.
        filename=result.anonymized.split("\n", 1)[0],
        filename_enc=vault.encrypt(filename),
        mime_type=upload_file.content_type or "application/octet-stream",
        size_bytes=stored.stat().st_size,
        storage_path=str(stored),
        text_original_enc=vault.encrypt(text),
        text_sent=result.anonymized,
        status=(
            FileStatus.PENDING_REVIEW if policy.hil_files is HilMode.REVIEW else FileStatus.APPROVED
        ),
    )
    session.add(asset)
    session.commit()
    session.refresh(asset)
    return asset, result


@router.get("/{file_id}/review")
def review(
    request: Request,
    file_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Show the original text beside the masked version awaiting approval."""
    asset = _load_asset(session, file_id, user)
    # A rejected file keeps no text to show.
    original = vault.decrypt(asset.text_original_enc) if asset.text_original_enc else ""
    return render(request, "file_review.html", file=asset, original=original)


@router.post("/{file_id}/approve")
def approve(
    file_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Approve a file so its masked text is attached to the conversation.

    Rejection is final: a review page left open in another tab, or reached with the back button,
    must not attach a document the employee has already said must not be used.
    """
    asset = _load_asset(session, file_id, user)
    if asset.status is FileStatus.REJECTED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="file was rejected")
    asset.status = FileStatus.APPROVED
    session.add(asset)
    session.commit()
    audit.log(
        session,
        event="file_approved",
        user_id=user.id,
        conversation_id=asset.conversation_id,
        detail=f"file_id={file_id}",
    )
    return RedirectResponse(
        f"/chat/{asset.conversation_id}?flash=files.approved_flash", status_code=303
    )


@router.post("/{file_id}/reject")
def reject(
    file_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Reject a file; its text is never attached, and the bytes and the extracted text are removed.

    The row stays, with the masked file name, so the conversation still shows that a file was
    offered and turned down.
    """
    asset = _load_asset(session, file_id, user)
    asset.status = FileStatus.REJECTED
    asset.text_original_enc = ""
    asset.text_sent = ""
    # The original name goes with the text; pages fall back to the masked one.
    asset.filename_enc = ""
    Path(asset.storage_path).unlink(missing_ok=True)
    session.add(asset)
    session.commit()
    audit.log(
        session,
        event="file_rejected",
        user_id=user.id,
        conversation_id=asset.conversation_id,
        detail=f"file_id={file_id}",
    )
    return RedirectResponse(
        f"/chat/{asset.conversation_id}?flash=files.rejected_flash", status_code=303
    )


async def _read_capped(upload_file: UploadFile) -> bytes | None:
    """Read an upload in chunks, stopping at the limit.

    Reading first and measuring afterwards would let a single request decide how much memory the
    hub uses, so the read stops one byte past the limit and the request is refused.
    """
    chunks: list[bytes] = []
    size = 0
    while chunk := await upload_file.read(CHUNK_BYTES):
        size += len(chunk)
        if size > MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _require_conversation_id(conversation: Conversation) -> int:
    if conversation.id is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return conversation.id


def _require_user_id(user: User) -> int:
    if user.id is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user.id
