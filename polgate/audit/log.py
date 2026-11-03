"""Audit logging.

The audit trail is what lets a company show *what* left the building without keeping a second copy
of the sensitive data. Only entity types, counts, identifiers and hashes are recorded, never raw
values.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable

from sqlmodel import Session

from polgate.models import AuditEvent

logger = logging.getLogger(__name__)


def sha256(value: str) -> str:
    """Return the hex SHA-256 digest of a value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def log(
    session: Session,
    *,
    event: str,
    user_id: int | None = None,
    conversation_id: int | None = None,
    provider: str = "",
    model: str = "",
    entity_counts: dict[str, int] | None = None,
    stage2_hits: int = 0,
    detail: str = "",
    forbidden_values: Iterable[str] = (),
) -> AuditEvent:
    """Write one audit row and commit it.

    Args:
        session: open database session.
        event: short machine-readable event name, e.g. ``message_sent``.
        user_id: acting user, when known.
        conversation_id: conversation the event belongs to, when applicable.
        provider: LLM provider base URL or name.
        model: model identifier.
        entity_counts: how many entities of each type were masked.
        stage2_hits: how many spans the stage-2 judge contributed.
        detail: short free-text note; must not contain personal data.
        forbidden_values: values that must not appear in `detail`, normally the token mapping
        values.

    Returns:
        The persisted audit event.

    Raises:
        ValueError: if `detail` contains one of `forbidden_values`.
    """
    for value in forbidden_values:
        if value and value in detail:
            raise ValueError("audit detail must not contain personal data")

    entry = AuditEvent(
        user_id=user_id,
        conversation_id=conversation_id,
        event=event,
        provider=provider,
        model=model,
        entity_counts_json=json.dumps(entity_counts or {}, sort_keys=True),
        stage2_hits=stage2_hits,
        detail=detail,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    logger.info("audit: %s user=%s conversation=%s", event, user_id, conversation_id)
    return entry
