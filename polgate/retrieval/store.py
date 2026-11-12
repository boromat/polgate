"""The passages of attachments and their vectors, in the `AttachmentChunk` table."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, func, select

from polgate.models import AttachmentChunk

logger = logging.getLogger(__name__)


def load(session: Session, file_id: int, embedding_model: str) -> list[AttachmentChunk]:
    """Return a file's indexed passages for one embedding model, in document order."""
    return list(
        session.exec(
            select(AttachmentChunk)
            .where(col(AttachmentChunk.file_id) == file_id)
            .where(col(AttachmentChunk.embedding_model) == embedding_model)
            .order_by(col(AttachmentChunk.position))
        ).all()
    )


def add(session: Session, rows: Sequence[AttachmentChunk]) -> bool:
    """Insert passages and commit; return False when another turn stored them first.

    Two turns in one conversation can index the same file at the same time. The unique key on
    ``(file_id, position, embedding_model)`` lets only one of them write a passage; the other rolls
    back its batch and reads what the first one wrote.
    """
    session.add_all(rows)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.info("passages already indexed by another turn: %d skipped", len(rows))
        return False
    return True


def delete_for_files(session: Session, file_ids: Sequence[int]) -> int:
    """Delete every passage of these files and return how many went; the caller commits."""
    if not file_ids:
        return 0
    condition = col(AttachmentChunk.file_id).in_(file_ids)
    count = session.exec(select(func.count()).select_from(AttachmentChunk).where(condition)).one()
    session.exec(delete(AttachmentChunk).where(condition))
    return int(count)


def delete_rows(session: Session, ids: Sequence[int]) -> None:
    """Delete passages by primary key; the caller commits."""
    session.exec(delete(AttachmentChunk).where(col(AttachmentChunk.id).in_(ids)))
