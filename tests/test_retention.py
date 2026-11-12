"""Retention: the TTLs in the admin panel have to delete data, not just describe an intention."""

from datetime import timedelta

from sqlmodel import select

from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.models import (
    AttachmentChunk,
    Conversation,
    FileAsset,
    FileStatus,
    Message,
    MessageRole,
    PendingTurn,
    Policy,
    User,
    VaultEntry,
    utcnow,
)
from polgate.retention import PurgeReport, purge
from polgate.vault import store as vault


def _owner(session) -> User:
    """Conversations reference a real account; foreign keys are enforced, so tests need one too."""
    user = session.exec(select(User)).first()
    if user is None:
        user = User(username="pracownik", password_hash=hash_password("dlugie-haslo"))
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def _conversation(session, *, age_days: int = 0) -> Conversation:
    moment = utcnow() - timedelta(days=age_days)
    conversation = Conversation(user_id=_owner(session).id, created_at=moment, updated_at=moment)
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return conversation


def _message(session, conversation, *, age_days: int) -> Message:
    message = Message(
        conversation_id=conversation.id,
        role=MessageRole.USER,
        content_original_enc=vault.encrypt("Jan Kowalski"),
        content_sent="<OSOBA_1>",
        created_at=utcnow() - timedelta(days=age_days),
    )
    session.add(message)
    session.commit()
    return message


def test_nothing_is_removed_while_both_ttls_are_off(db_session):
    conversation = _conversation(db_session, age_days=900)
    _message(db_session, conversation, age_days=900)

    report = purge(db_session, Policy(history_ttl_days=None), get_settings())

    assert report == PurgeReport()
    assert db_session.exec(select(Message)).all()


def test_messages_past_the_history_ttl_are_deleted(db_session):
    conversation = _conversation(db_session, age_days=1)
    _message(db_session, conversation, age_days=90)
    _message(db_session, conversation, age_days=1)

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert report.messages == 1
    remaining = db_session.exec(select(Message)).all()
    assert len(remaining) == 1


def test_an_emptied_conversation_is_removed_with_its_mapping(db_session):
    conversation = _conversation(db_session, age_days=90)
    _message(db_session, conversation, age_days=90)
    db_session.add(
        VaultEntry(
            conversation_id=conversation.id,
            token="<OSOBA_1>",
            entity_type="PERSON",
            value_enc=vault.encrypt("Jan Kowalski"),
            value_hash=vault.value_hash("Jan Kowalski"),
        )
    )
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert report.conversations == 1
    assert db_session.exec(select(Conversation)).all() == []
    assert db_session.exec(select(VaultEntry)).all() == []


def test_a_conversation_still_holding_recent_messages_survives(db_session):
    conversation = _conversation(db_session, age_days=90)
    _message(db_session, conversation, age_days=1)

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert report.conversations == 0
    assert db_session.exec(select(Conversation)).all()


def test_expired_uploads_lose_their_bytes_as_well_as_their_row(db_session, tmp_path):
    conversation = _conversation(db_session, age_days=1)
    stored = tmp_path / "cv.pdf"
    stored.write_bytes(b"%PDF-1.4 fake")
    db_session.add(
        FileAsset(
            conversation_id=conversation.id,
            user_id=_owner(db_session).id,
            filename="cv.pdf",
            mime_type="application/pdf",
            size_bytes=13,
            storage_path=str(stored),
            status=FileStatus.APPROVED,
            created_at=utcnow() - timedelta(days=200),
        )
    )
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert report.files == 1
    assert not stored.exists()


def test_a_queued_turn_whose_stream_never_started_is_dropped(db_session):
    conversation = _conversation(db_session)
    db_session.add(
        PendingTurn(
            conversation_id=conversation.id,
            user_id=_owner(db_session).id,
            text_enc=vault.encrypt("Anna Wiśniewska"),
            created_at=utcnow() - timedelta(hours=6),
        )
    )
    db_session.add(
        PendingTurn(
            conversation_id=conversation.id,
            user_id=_owner(db_session).id,
            text_enc=vault.encrypt("świeże"),
        )
    )
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=None), get_settings())

    assert report.pending_turns == 1
    assert len(db_session.exec(select(PendingTurn)).all()) == 1


def test_expired_vault_entries_are_removed_whatever_the_history_ttl_says(db_session):
    conversation = _conversation(db_session)
    db_session.add(
        VaultEntry(
            conversation_id=conversation.id,
            token="<PESEL_1>",
            entity_type="PESEL",
            value_enc=vault.encrypt("45831523452"),
            value_hash=vault.value_hash("45831523452"),
            expires_at=utcnow() - timedelta(days=1),
        )
    )
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=None), get_settings())

    assert report.vault_entries == 1
    assert db_session.exec(select(VaultEntry)).all() == []


def test_the_audit_summary_carries_counts_and_nothing_else():
    detail = PurgeReport(vault_entries=2, messages=3).as_detail()
    assert detail == ("vault=2 pending=0 messages=3 files=0 passages=0 conversations=0 orphans=0")
    assert PurgeReport(files=1).total == 1


def test_a_failing_pass_is_logged_and_the_job_carries_on(app_client, monkeypatch, caplog):
    """The loop runs for the life of the process, so a bad pass must not end it.

    This is not hypothetical: a database missing a column the models had grown raised here on every
    start until the schema check was added.
    """
    import polgate.main as main
    from polgate import retention

    def explode(*_args, **_kwargs):
        raise RuntimeError("no such column: pendingturn.extra_terms_enc")

    monkeypatch.setattr(retention, "purge", explode)
    with caplog.at_level("ERROR"):
        main.run_retention_pass()

    assert "retention pass failed" in caplog.text
    assert "no such column" in caplog.text


def test_a_pass_that_removes_something_leaves_an_audit_row(app_client):
    from sqlmodel import Session, select

    import polgate.main as main
    from polgate.db import get_engine
    from polgate.models import AuditEvent, Conversation, PendingTurn, User

    with Session(get_engine()) as session:
        user = session.exec(select(User)).one()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        session.commit()
        session.refresh(conversation)
        session.add(
            PendingTurn(
                conversation_id=conversation.id,
                user_id=user.id,
                text_enc=vault.encrypt("stara"),
                created_at=utcnow() - timedelta(days=2),
            )
        )
        session.commit()

    main.run_retention_pass()

    with Session(get_engine()) as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.event == "retention_purge")).all()
    assert events
    assert "pending=1" in events[-1].detail


def test_a_quiet_pass_writes_no_audit_noise(app_client):
    from sqlmodel import Session, select

    import polgate.main as main
    from polgate.db import get_engine
    from polgate.models import AuditEvent

    main.run_retention_pass()

    with Session(get_engine()) as session:
        assert not session.exec(
            select(AuditEvent).where(AuditEvent.event == "retention_purge")
        ).all()


def _stored_upload(directory, name: str, *, age_hours: float):
    import os
    import time

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"PESEL 50810112354")
    moment = time.time() - age_hours * 3600
    os.utime(path, (moment, moment))
    return path


def test_an_upload_no_row_refers_to_is_removed_once_it_is_old(db_session, tmp_path, monkeypatch):
    """A process killed between storing and recording an upload leaves bytes nothing points to."""
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    orphan = _stored_upload(settings.upload_dir, "0" * 32 + ".txt", age_hours=2)

    report = purge(db_session, Policy(history_ttl_days=None), settings)

    assert not orphan.exists()
    assert report.orphan_uploads == 1


def test_an_upload_still_being_processed_is_left_alone(db_session, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    fresh = _stored_upload(settings.upload_dir, "1" * 32 + ".pdf", age_hours=0)

    purge(db_session, Policy(history_ttl_days=None), settings)

    assert fresh.exists()


def test_a_recorded_upload_and_a_file_the_hub_did_not_name_are_kept(
    db_session, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    recorded = _stored_upload(settings.upload_dir, "2" * 32 + ".txt", age_hours=48)
    foreign = _stored_upload(settings.upload_dir, "README.txt", age_hours=48)
    conversation = _conversation(db_session)
    db_session.add(
        FileAsset(
            conversation_id=conversation.id,
            user_id=conversation.user_id,
            filename="notatka.txt",
            mime_type="text/plain",
            size_bytes=17,
            storage_path=str(recorded),
            status=FileStatus.APPROVED,
        )
    )
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=None), settings)

    assert recorded.exists()
    assert foreign.exists()
    assert report.orphan_uploads == 0


def test_a_missing_upload_folder_is_not_an_error(db_session, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "never-created")

    assert purge(db_session, Policy(history_ttl_days=None), settings).orphan_uploads == 0


def test_a_conversation_with_a_message_waiting_to_stream_is_kept(db_session):
    """Only old messages, so it looked empty, and the database refused to delete it."""
    conversation = _conversation(db_session, age_days=90)
    _message(db_session, conversation, age_days=90)
    db_session.add(
        PendingTurn(
            conversation_id=conversation.id,
            user_id=_owner(db_session).id,
            text_enc=vault.encrypt("właśnie piszę"),
        )
    )
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert (report.messages, report.conversations) == (1, 0)
    assert db_session.exec(select(Conversation)).all()
    assert len(db_session.exec(select(PendingTurn)).all()) == 1


def test_an_emptied_conversation_goes_with_its_mapping_and_a_stale_queued_message(db_session):
    conversation = _conversation(db_session, age_days=90)
    _message(db_session, conversation, age_days=90)
    db_session.add(
        PendingTurn(
            conversation_id=conversation.id,
            user_id=_owner(db_session).id,
            text_enc=vault.encrypt("porzucone"),
            created_at=utcnow() - timedelta(days=60),
        )
    )
    db_session.add(
        VaultEntry(
            conversation_id=conversation.id,
            token="<OSOBA_1>",
            entity_type="PERSON",
            value_enc=vault.encrypt("Jan Kowalski"),
            value_hash=vault.value_hash("Jan Kowalski"),
        )
    )
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert (report.pending_turns, report.conversations) == (1, 1)
    assert db_session.exec(select(Conversation)).all() == []
    assert db_session.exec(select(VaultEntry)).all() == []


def test_the_longest_retention_an_admin_can_set_does_not_overflow(db_session):
    """999,999 days of history put the cutoff before year 1, and every pass raised."""
    from polgate.models import MAX_TTL_DAYS

    conversation = _conversation(db_session, age_days=1)
    _message(db_session, conversation, age_days=1)
    vault.save_mapping(db_session, conversation.id, {"<OSOBA_1>": "Jan"}, {}, 999_999)
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=999_999), get_settings())

    assert report.messages == 0
    entry = db_session.exec(select(VaultEntry)).one()
    assert entry.expires_at is not None
    assert (entry.expires_at.year - utcnow().year) in (MAX_TTL_DAYS // 365, MAX_TTL_DAYS // 365 + 1)


def _chunk(file_id: int, conversation_id: int, position: int) -> AttachmentChunk:
    return AttachmentChunk(
        file_id=file_id,
        conversation_id=conversation_id,
        position=position,
        text_sent="<OSOBA_1> fragment",
        vector=bytes(4),
        embedding_model="bge-m3",
    )


def test_an_expired_file_takes_its_indexed_passages_with_it(db_session, tmp_path):
    conversation = _conversation(db_session, age_days=1)
    asset = FileAsset(
        conversation_id=conversation.id,
        user_id=_owner(db_session).id,
        filename="regulamin.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_path=str(tmp_path / "gone.txt"),
        status=FileStatus.APPROVED,
        created_at=utcnow() - timedelta(days=200),
    )
    db_session.add(asset)
    db_session.commit()
    db_session.add_all([_chunk(asset.id, conversation.id, i) for i in range(3)])
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert (report.files, report.passages) == (1, 3)
    assert db_session.exec(select(AttachmentChunk)).all() == []
    assert "passages=3" in report.as_detail()


def test_an_emptied_conversation_leaves_no_passage_behind(db_session):
    conversation = _conversation(db_session, age_days=90)
    _message(db_session, conversation, age_days=90)
    asset = FileAsset(
        conversation_id=conversation.id,
        user_id=_owner(db_session).id,
        filename="plik.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_path="none",
        created_at=utcnow() - timedelta(days=90),
    )
    db_session.add(asset)
    db_session.commit()
    db_session.add_all([_chunk(asset.id, conversation.id, i) for i in range(2)])
    db_session.commit()

    report = purge(db_session, Policy(history_ttl_days=30), get_settings())

    assert (report.files, report.passages, report.conversations) == (1, 2, 1)
    assert db_session.exec(select(AttachmentChunk)).all() == []
