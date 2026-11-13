"""Encrypted store for the token -> original value mapping.

Originals are the crown jewels of this project: they are what must never reach a provider. They are
encrypted with Fernet (AES-128-CBC + HMAC) so a stolen database file is not a data breach on its
own, and they expire according to the admin's retention policy.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from cryptography.fernet import Fernet
from sqlmodel import Session, col, delete, select

from polgate.config import get_settings
from polgate.keys import read_or_create, state_dir
from polgate.models import MAX_TTL_DAYS, VaultEntry, utcnow

logger = logging.getLogger(__name__)


@lru_cache
def get_fernet() -> Fernet:
    """Return the process-wide Fernet instance, generating a key file on first use."""
    return Fernet(_vault_key().encode())


@lru_cache
def _fingerprint_key() -> bytes:
    """Return the key of :func:`value_hash`, derived from the vault key and never stored."""
    return hashlib.sha256(b"polgate value fingerprint:" + _vault_key().encode()).digest()


def _vault_key() -> str:
    settings = get_settings()
    if settings.vault_key:
        return settings.vault_key
    key_path = state_dir(settings) / "vault.key"
    key, created = read_or_create(key_path, lambda: Fernet.generate_key().decode())
    if created:
        logger.warning(
            "Generated a new vault key at %s. Back it up: without it history is unreadable.",
            key_path,
        )
    return key


def encrypt(value: str) -> str:
    """Encrypt a plaintext value for storage."""
    return get_fernet().encrypt(value.encode()).decode()


def decrypt(value_enc: str) -> str:
    """Decrypt a value produced by :func:`encrypt`."""
    return get_fernet().decrypt(value_enc.encode()).decode()


def value_hash(value: str) -> str:
    """Return a stable fingerprint of a value for audit correlation, keyed with the vault key.

    A plain sha256 of a PIN, a PESEL or a phone number gives the value back to anyone holding the
    database: there are only so many of them, and trying them all took seconds for a PIN and
    minutes for a PESEL. Keyed, the fingerprint tells nothing without the vault key, which already
    opens the encrypted value next to it.
    """
    return hmac.new(_fingerprint_key(), value.encode(), hashlib.sha256).hexdigest()


def load_mapping(session: Session, conversation_id: int) -> dict[str, str]:
    """Return the live token -> original mapping for a conversation, skipping expired entries."""
    now = utcnow()
    entries = session.exec(
        select(VaultEntry).where(col(VaultEntry.conversation_id) == conversation_id)
    ).all()
    return {entry.token: decrypt(entry.value_enc) for entry in entries if _is_live(entry, now)}


def save_mapping(
    session: Session,
    conversation_id: int,
    mapping: dict[str, str],
    entity_types: dict[str, str],
    ttl_days: int | None,
) -> None:
    """Insert vault rows for tokens that are not stored yet.

    Args:
        session: open database session; the caller commits.
        conversation_id: conversation the tokens belong to.
        mapping: token -> original value, may contain already stored tokens.
        entity_types: token -> canonical entity type, used for statistics.
        ttl_days: retention in days, or None to keep the mapping forever.

    An expired row does not count as stored. :func:`load_mapping` skips it, so the allocator has
    already handed its token to somebody else; treating it as known would drop the new value on the
    floor and leave a token nothing can restore. The stale row is removed by the next purge.
    """
    now = utcnow()
    known = {
        entry.token
        for entry in session.exec(
            select(VaultEntry).where(col(VaultEntry.conversation_id) == conversation_id)
        ).all()
        if _is_live(entry, now)
    }
    expires_at = _expiry(now, ttl_days)
    for token, value in mapping.items():
        if token in known:
            continue
        session.add(
            VaultEntry(
                conversation_id=conversation_id,
                token=token,
                entity_type=entity_types.get(token, "UNKNOWN"),
                value_enc=encrypt(value),
                value_hash=value_hash(value),
                expires_at=expires_at,
            )
        )


def apply_ttl(session: Session, ttl_days: int | None) -> int:
    """Give every stored mapping the expiry a new retention period sets, counted from its creation.

    Expiry is written into each row when it is stored, so without this a shorter period set in the
    admin panel applied only to mappings stored afterwards, and switching "forever" to 30 days left
    every existing one forever. The caller commits.

    Returns:
        How many rows changed.
    """
    changed = 0
    for entry in session.exec(select(VaultEntry)).all():
        expires_at = _expiry(_as_utc(entry.created_at), ttl_days)
        current = None if entry.expires_at is None else _as_utc(entry.expires_at)
        if current != expires_at:
            entry.expires_at = expires_at
            session.add(entry)
            changed += 1
    return changed


def _expiry(start: datetime, ttl_days: int | None) -> datetime | None:
    """Return when a row stored at ``start`` expires, or None to keep it forever."""
    if ttl_days is None:
        return None
    return start + timedelta(days=min(ttl_days, MAX_TTL_DAYS))


def purge_expired(session: Session) -> int:
    """Delete vault entries past their expiry date and return how many were removed."""
    now = utcnow()
    expired = session.exec(select(VaultEntry).where(col(VaultEntry.expires_at).is_not(None))).all()
    ids = [e.id for e in expired if e.expires_at is not None and _as_utc(e.expires_at) <= now]
    if not ids:
        return 0
    session.exec(delete(VaultEntry).where(col(VaultEntry.id).in_(ids)))
    session.commit()
    logger.info("Purged %d expired vault entries", len(ids))
    return len(ids)


def _is_live(entry: VaultEntry, now: datetime) -> bool:
    """Return whether an entry is still inside its retention window.

    Reading and writing have to agree on this, or a token the reader treats as gone is one the
    writer refuses to store again.
    """
    return entry.expires_at is None or _as_utc(entry.expires_at) > now


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
