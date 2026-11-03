"""Vault encryption, retention and the audit trail's refusal to store personal data."""

import hashlib
import json
import stat
import sys

import pytest
from sqlmodel import select

from polgate import audit
from polgate.auth.passwords import hash_password, verify_password
from polgate.models import AuditEvent, Conversation, User, VaultEntry
from polgate.vault import store as vault


@pytest.fixture
def conversation(db_session):
    user = User(username="u", password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    item = Conversation(user_id=user.id)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def test_encrypt_round_trips_and_hides_the_plaintext():
    encrypted = vault.encrypt("Jan Kowalski")
    assert "Jan" not in encrypted
    assert vault.decrypt(encrypted) == "Jan Kowalski"


def test_saved_mapping_is_readable_again(db_session, conversation):
    mapping = {"<OSOBA_1>": "Jan Kowalski", "<PESEL_1>": "50810112354"}
    vault.save_mapping(db_session, conversation.id, mapping, {"<OSOBA_1>": "PERSON"}, 30)
    db_session.commit()
    assert vault.load_mapping(db_session, conversation.id) == mapping


def test_stored_rows_never_contain_the_plaintext(db_session, conversation):
    vault.save_mapping(db_session, conversation.id, {"<OSOBA_1>": "Jan Kowalski"}, {}, 30)
    db_session.commit()
    row = db_session.exec(select(VaultEntry)).one()
    assert "Jan Kowalski" not in row.value_enc
    assert row.value_hash == vault.value_hash("Jan Kowalski")


def test_expired_entries_are_hidden_and_purged(db_session, conversation):
    vault.save_mapping(db_session, conversation.id, {"<OSOBA_1>": "Jan"}, {}, ttl_days=-1)
    db_session.commit()
    assert vault.load_mapping(db_session, conversation.id) == {}
    assert vault.purge_expired(db_session) == 1
    assert db_session.exec(select(VaultEntry)).all() == []


def test_mapping_without_ttl_never_expires(db_session, conversation):
    vault.save_mapping(db_session, conversation.id, {"<OSOBA_1>": "Jan"}, {}, None)
    db_session.commit()
    assert vault.purge_expired(db_session) == 0
    assert vault.load_mapping(db_session, conversation.id) == {"<OSOBA_1>": "Jan"}


def test_saving_the_same_token_twice_does_not_duplicate_rows(db_session, conversation):
    for _ in range(2):
        vault.save_mapping(db_session, conversation.id, {"<OSOBA_1>": "Jan"}, {}, 30)
        db_session.commit()
    assert len(db_session.exec(select(VaultEntry)).all()) == 1


def test_a_token_reused_after_expiry_is_stored_again(db_session, conversation):
    """An expired row is invisible to the reader, so the writer must not treat it as stored.

    The purge runs hourly, so a row spends up to an hour expired but still present. In that window
    the allocator, which sees an empty mapping, hands the token to the next person it meets.
    """
    vault.save_mapping(db_session, conversation.id, {"<OSOBA_1>": "Jan"}, {}, ttl_days=-1)
    db_session.commit()
    assert vault.load_mapping(db_session, conversation.id) == {}

    vault.save_mapping(db_session, conversation.id, {"<OSOBA_1>": "Anna"}, {}, 30)
    db_session.commit()
    assert vault.load_mapping(db_session, conversation.id) == {"<OSOBA_1>": "Anna"}


def test_audit_row_records_counts_not_values(db_session):
    audit.log(db_session, event="message_sent", entity_counts={"PESEL": 2}, stage2_hits=1)
    row = db_session.exec(select(AuditEvent)).one()
    assert json.loads(row.entity_counts_json) == {"PESEL": 2}
    assert row.stage2_hits == 1


def test_audit_refuses_details_containing_personal_data(db_session):
    with pytest.raises(ValueError, match="must not contain personal data"):
        audit.log(
            db_session,
            event="message_sent",
            detail="failed for Jan Kowalski",
            forbidden_values=["Jan Kowalski"],
        )


def test_password_hashing_round_trip():
    hashed = hash_password("correct horse")
    assert verify_password("correct horse", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("x", "not-a-hash")


def test_a_missing_vault_key_is_generated_once_and_kept_private(tmp_path, monkeypatch):
    """Without VAULT_KEY the hub has to make its own, and that file is the whole archive."""
    from polgate.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "vault_key", "")
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'data' / 'hub.db'}")
    vault.get_fernet.cache_clear()
    try:
        first = vault.get_fernet()
        key_path = tmp_path / "data" / "vault.key"
        assert key_path.exists()
        if sys.platform != "win32":
            # Windows has no POSIX mode bits: os.chmod only toggles the read-only flag, and the
            # file reports 0o666 however it was created. The hub is deployed on Linux.
            assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

        vault.get_fernet.cache_clear()
        second = vault.get_fernet()
        assert second.decrypt(first.encrypt(b"Jan Kowalski")) == b"Jan Kowalski"
        assert key_path.read_bytes() == key_path.read_bytes()
    finally:
        vault.get_fernet.cache_clear()


def test_a_configured_vault_key_is_used_without_touching_the_disk(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from polgate.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "vault_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'data' / 'hub.db'}")
    vault.get_fernet.cache_clear()
    try:
        vault.get_fernet()
        assert not (tmp_path / "data" / "vault.key").exists()
    finally:
        vault.get_fernet.cache_clear()


def test_a_value_fingerprint_is_keyed_and_gives_no_value_back():
    """A plain sha256 of a PIN is reversed by trying all ten thousand of them."""
    fingerprint = vault.value_hash("4821")
    assert fingerprint == vault.value_hash("4821")
    assert fingerprint != hashlib.sha256(b"4821").hexdigest()
