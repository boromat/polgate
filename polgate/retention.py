"""Retention: delete what the policy no longer allows the hub to keep.

Storing less is the only protection that cannot be undone by a later mistake, so the two retention
settings in the admin panel have to be real deletions rather than labels on a form:

``vault_ttl_days``
    how long the token -> original mapping survives. Once it is gone the conversation is still
    readable, but the answers can no longer be de-anonymized, which is exactly the point.

``history_ttl_days``
    how long messages and uploaded files survive, with the passages the search in attachments
    indexed from a file. Messages are removed individually, so a long
    running conversation loses its old turns instead of being kept alive by its newest one, and a
    conversation left with nothing is removed with its mapping.

The job also drops queued turns whose stream never started: those hold the employee's original text,
encrypted, for what should be a few seconds, and a closed browser tab should not turn that into
forever. For the same reason it removes stored uploads that no row refers to. An upload that fails
cleans up after itself, but a process killed halfway through cannot, and nothing else would ever
find those bytes again.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, col, select

from polgate.config import Settings
from polgate.models import (
    MAX_TTL_DAYS,
    AttachmentChunk,
    Conversation,
    FileAsset,
    Message,
    PendingTurn,
    Policy,
    VaultEntry,
    utcnow,
)
from polgate.retrieval import store as passages
from polgate.vault import store as vault

logger = logging.getLogger(__name__)

#: How old a stored upload with no row must be before it counts as left behind. An upload being
#: processed right now has its bytes on disk and no row yet.
ORPHAN_UPLOAD_AGE = timedelta(hours=1)

#: The names the upload handler gives stored files. Anything else in the folder is not ours.
_STORED_UPLOAD_NAME = re.compile(r"[0-9a-f]{32}(\.[a-z0-9]+)?")


@dataclass(frozen=True)
class PurgeReport:
    """How many rows one retention pass removed, per kind."""

    vault_entries: int = 0
    pending_turns: int = 0
    messages: int = 0
    files: int = 0
    passages: int = 0
    conversations: int = 0
    orphan_uploads: int = 0

    @property
    def total(self) -> int:
        """Total number of rows removed."""
        return (
            self.vault_entries
            + self.pending_turns
            + self.messages
            + self.files
            + self.passages
            + self.conversations
            + self.orphan_uploads
        )

    def as_detail(self) -> str:
        """Return an audit-safe summary: counts only, never a value or an identifier."""
        return (
            f"vault={self.vault_entries} pending={self.pending_turns} "
            f"messages={self.messages} files={self.files} passages={self.passages} "
            f"conversations={self.conversations} "
            f"orphans={self.orphan_uploads}"
        )


def purge(session: Session, policy: Policy, settings: Settings) -> PurgeReport:
    """Run one retention pass and return what it removed.

    Args:
        session: open database session; this function commits.
        policy: the admin policy holding the two TTLs.
        settings: application settings, for the queued-turn timeout and the upload folder.

    Returns:
        A :class:`PurgeReport` with one count per kind of row.
    """
    now = utcnow()
    vault_entries = vault.purge_expired(session)
    pending = _purge_pending_turns(session, now, settings.pending_turn_ttl_minutes)
    messages, files, chunks, conversations = _purge_history(session, now, policy.history_ttl_days)
    session.commit()
    report = PurgeReport(
        vault_entries=vault_entries,
        pending_turns=pending,
        messages=messages,
        files=files,
        passages=chunks,
        conversations=conversations,
        orphan_uploads=_purge_orphan_uploads(session, settings.upload_dir, now),
    )
    if report.total:
        logger.info("retention pass removed %s", report.as_detail())
    return report


def _purge_pending_turns(session: Session, now: datetime, ttl_minutes: int) -> int:
    """Drop queued turns older than the timeout; their stream is never going to start."""
    if ttl_minutes <= 0:
        return 0
    cutoff = now - timedelta(minutes=ttl_minutes)
    stale = [
        row for row in session.exec(select(PendingTurn)).all() if _as_utc(row.created_at) < cutoff
    ]
    for row in stale:
        session.delete(row)
    return len(stale)


def _purge_history(
    session: Session, now: datetime, ttl_days: int | None
) -> tuple[int, int, int, int]:
    """Delete messages and files past the history TTL, then conversations left with neither.

    A file's passages go before the file: they refer to it, and the database enforces that.
    """
    if ttl_days is None:
        return 0, 0, 0, 0
    cutoff = now - timedelta(days=min(ttl_days, MAX_TTL_DAYS))

    messages = [
        row for row in session.exec(select(Message)).all() if _as_utc(row.created_at) < cutoff
    ]
    for message in messages:
        session.delete(message)

    files = [
        row for row in session.exec(select(FileAsset)).all() if _as_utc(row.created_at) < cutoff
    ]
    chunks = passages.delete_for_files(session, [f.id for f in files if f.id is not None])
    for asset in files:
        Path(asset.storage_path).unlink(missing_ok=True)
        session.delete(asset)
    session.flush()

    conversations = _purge_empty_conversations(session, cutoff)
    return len(messages), len(files), chunks, conversations


def _purge_orphan_uploads(session: Session, upload_dir: Path, now: datetime) -> int:
    """Delete stored uploads older than `ORPHAN_UPLOAD_AGE` that no file row refers to."""
    if not upload_dir.is_dir():
        return 0
    referenced = {Path(path).name for path in session.exec(select(FileAsset.storage_path)).all()}
    removed = 0
    for path in upload_dir.iterdir():
        if path.name in referenced or not _STORED_UPLOAD_NAME.fullmatch(path.name):
            continue
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if now - modified < ORPHAN_UPLOAD_AGE:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _purge_empty_conversations(session: Session, cutoff: datetime) -> int:
    """Remove old conversations that no longer hold a message or a file, mapping included."""
    removed = 0
    for conversation in session.exec(select(Conversation)).all():
        if _as_utc(conversation.updated_at) >= cutoff:
            continue
        if _has_content(session, conversation):
            continue
        _delete_conversation_traces(session, conversation)
        session.delete(conversation)
        removed += 1
    return removed


def _has_content(session: Session, conversation: Conversation) -> bool:
    """Return whether a conversation still holds a message, a file or a message waiting to stream.

    A queued message counts: somebody is writing in that conversation right now, even when its last
    activity was long ago. Queued messages too old to stream were dropped earlier in the same pass.
    """
    for kind in (Message, FileAsset, PendingTurn):
        found = session.exec(
            select(kind).where(col(kind.conversation_id) == conversation.id).limit(1)
        ).first()
        if found is not None:
            return True
    return False


def _delete_conversation_traces(session: Session, conversation: Conversation) -> None:
    """Delete what refers to a conversation, before the conversation itself.

    That is its vault mapping and any passage left of its files. The rows are flushed here: left to
    the end of the pass, the database may be asked to delete the conversation first, and its foreign
    keys refuse that, which rolled back the whole pass.
    """
    for chunk in session.exec(
        select(AttachmentChunk).where(col(AttachmentChunk.conversation_id) == conversation.id)
    ).all():
        session.delete(chunk)
    for entry in session.exec(
        select(VaultEntry).where(col(VaultEntry.conversation_id) == conversation.id)
    ).all():
        session.delete(entry)
    session.flush()


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as the UTC they were written as."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
