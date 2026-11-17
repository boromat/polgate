"""Data subject requests: one person found in every shape the hub keeps, exported, erased."""

import json

import httpx
import pytest
from polanon import Anonymizer, PersonNameGuard, RegexGuard
from polanon.policy import ENTITY_TYPES
from sqlmodel import Session, select

from polgate import subjects
from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.db import get_engine
from polgate.llm.router import run_turn
from polgate.models import (
    AuditEvent,
    Conversation,
    FileAsset,
    FileStatus,
    Message,
    MessageRole,
    Policy,
    User,
    VaultEntry,
)
from polgate.subjects import ERASED
from polgate.vault import fingerprints
from polgate.vault import store as vault

PESEL = "50810112354"  # born in 1850: a valid checksum that belongs to nobody


class Provider:
    """Answers every turn by naming the person in another case."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        chunk = json.dumps({"choices": [{"delta": {"content": "Pismo do <OSOBA_1:dopełniacz>."}}]})
        return httpx.Response(200, content=f"data: {chunk}\n\ndata: [DONE]\n\n".encode())


@pytest.fixture
def people(db_session):
    worker = User(username="worker", password_hash=hash_password("x"))
    db_session.add(worker)
    db_session.commit()
    first = Conversation(user_id=worker.id)
    second = Conversation(user_id=worker.id)
    db_session.add_all([first, second])
    db_session.commit()
    for item in (first, second):
        db_session.refresh(item)
    return worker, first, second


async def _turn(session, user, conversation, text):
    anonymizer = Anonymizer([RegexGuard(), PersonNameGuard()], enabled=set(ENTITY_TYPES))
    async with httpx.AsyncClient(transport=httpx.MockTransport(Provider().handler)) as client:
        return [
            event
            async for event in run_turn(
                session,
                conversation,
                user,
                text,
                policy=Policy(),
                anonymizer=anonymizer,
                settings=get_settings(),
                client=client,
            )
        ]


async def test_a_person_is_found_in_every_spelling_and_conversation(db_session, people):
    worker, first, second = people
    await _turn(db_session, worker, first, "Napisz do Jana Kowalskiego, PESEL 50810112354.")
    await _turn(db_session, worker, second, "Jan Kowalski prosi o urlop.")
    await _turn(db_session, worker, second, "A Anna Nowak też?")

    report = subjects.find(db_session, "Jan Kowalski")

    assert set(report.tokens) == {first.id, second.id}
    assert {"Jana Kowalskiego", "Jan Kowalski"} <= report.forms
    # His two messages and the three answers that name <OSOBA_1>, his token in both; not the
    # question about Anna Nowak.
    assert len(report.messages) == 5
    assert report.counts()["conversations"] == 2


def test_a_value_too_short_to_name_one_person_is_refused(db_session):
    with pytest.raises(ValueError, match="too short"):
        subjects.find(db_session, " Li ")


async def test_the_export_restores_only_the_person_asked_about(db_session, people):
    worker, first, _ = people
    await _turn(db_session, worker, first, "Jan Kowalski i Anna Nowak, PESEL 50810112354.")

    exported = json.loads(subjects.export(db_session, subjects.find(db_session, "Jan Kowalski")))

    texts = [m["text"] for c in exported["conversations"] for m in c["messages"]]
    assert any("Jan Kowalski" in text for text in texts)
    assert not any("Anna Nowak" in text or PESEL in text for text in texts)
    assert exported["conversations"][0]["owner"] == "worker"


async def test_erasing_a_person_leaves_no_spelling_or_token_that_restores(db_session, people):
    worker, first, second = people
    await _turn(db_session, worker, first, "Napisz do Jana Kowalskiego w sprawie umowy.")
    await _turn(db_session, worker, second, "Anna Nowak zostaje.")

    counts = subjects.erase(db_session, subjects.find(db_session, "Jan Kowalski"))

    assert counts["messages"] == 2
    for message in db_session.exec(select(Message)).all():
        original = vault.decrypt(message.content_original_enc)
        assert "Kowalsk" not in original
        assert "Jan" not in original or message.conversation_id != first.id
    tokens = {
        (e.conversation_id, vault.decrypt(e.value_enc)) for e in db_session.exec(select(VaultEntry))
    }
    assert all("Kowalsk" not in value for _, value in tokens)
    assert (second.id, "Anna Nowak") in tokens
    question = db_session.exec(
        select(Message).where(Message.conversation_id == first.id, Message.role == MessageRole.USER)
    ).one()
    assert ERASED in question.content_sent
    assert (
        fingerprints.stale_tokens(question.content_sent, question.token_fingerprints, {}) == set()
    )
    assert subjects.find(db_session, "Jan Kowalski").found is False


def test_erasing_a_person_deletes_the_upload_that_held_them(db_session, people, tmp_path):
    worker, first, _ = people
    stored = tmp_path / "cv.pdf"
    stored.write_bytes(b"%PDF-1.4 Jan Kowalski")
    asset = FileAsset(
        conversation_id=first.id,
        user_id=worker.id,
        filename="CV <OSOBA_1>.pdf",
        filename_enc=vault.encrypt("CV Jan Kowalski.pdf"),
        mime_type="application/pdf",
        size_bytes=20,
        storage_path=str(stored),
        text_original_enc=vault.encrypt("Jan Kowalski, doświadczenie 5 lat"),
        text_sent="<OSOBA_1>, doświadczenie 5 lat",
        status=FileStatus.APPROVED,
    )
    db_session.add(asset)
    vault.save_mapping(db_session, first.id, {"<OSOBA_1>": "Jan Kowalski"}, {}, None)
    db_session.commit()

    counts = subjects.erase(db_session, subjects.find(db_session, "jan kowalski"))

    db_session.refresh(asset)
    assert counts["uploads_deleted"] == 1
    assert not stored.exists()
    assert asset.storage_path == ""
    assert asset.text_sent == f"{ERASED}, doświadczenie 5 lat"
    assert asset.filename == f"CV {ERASED}.pdf"
    assert "Kowalski" not in vault.decrypt(asset.text_original_enc)
    assert "Kowalski" not in vault.decrypt(asset.filename_enc)


def test_a_short_spelling_is_erased_only_as_a_word_of_its_own(db_session, people):
    _worker, first, _ = people
    db_session.add(
        Message(
            conversation_id=first.id,
            role=MessageRole.USER,
            content_original_enc=vault.encrypt("Lista płac: pan Lis"),
            content_sent="Lista płac: pan <OSOBA_1>",
        )
    )
    vault.save_mapping(db_session, first.id, {"<OSOBA_1>": "Lis"}, {}, None)
    db_session.commit()

    subjects.erase(db_session, subjects.find(db_session, "Lis"))

    message = db_session.exec(select(Message)).one()
    assert vault.decrypt(message.content_original_enc) == f"Lista płac: pan {ERASED}"


def _admin_page(client, path, **data):
    return client.post(path, data=data)


def test_the_subject_pages_are_for_administrators_only(app_client):
    with Session(get_engine()) as session:
        session.add(User(username="pracownik", password_hash=hash_password("dlugie-haslo-x")))
        session.commit()
    app_client.post("/login", data={"username": "pracownik", "password": "dlugie-haslo-x"})

    assert app_client.get("/admin/subjects").status_code == 403
    assert _admin_page(app_client, "/admin/subjects", value=PESEL).status_code == 403
    assert (
        _admin_page(app_client, "/admin/subjects/erase", value=PESEL, confirm="yes").status_code
        == 403
    )


@pytest.mark.parametrize(("language", "label"), [("pl", "Wartość"), ("en", "Value")])
def test_the_subject_page_renders_in_both_languages(admin_client, language, label):
    assert label in admin_client.get(f"/admin/subjects?lang={language}").text


def test_a_request_is_found_exported_and_erased_without_the_value_in_the_audit(admin_client):
    conversation = admin_client.post("/chat/new").headers["location"]
    conversation_id = int(conversation.rstrip("/").rsplit("/", 1)[-1])
    with Session(get_engine()) as session:
        session.add(
            Message(
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content_original_enc=vault.encrypt(f"PESEL {PESEL} do akt"),
                content_sent="PESEL <PESEL_1> do akt",
            )
        )
        vault.save_mapping(session, conversation_id, {"<PESEL_1>": PESEL}, {}, None)
        session.commit()

    found = _admin_page(admin_client, "/admin/subjects?lang=en", value=PESEL)
    assert found.status_code == 200
    assert "Erase" in found.text

    exported = _admin_page(admin_client, "/admin/subjects/export", value=PESEL)
    assert exported.headers["content-type"].startswith("application/json")
    assert "attachment" in exported.headers["content-disposition"]
    assert PESEL in exported.text

    unconfirmed = _admin_page(admin_client, "/admin/subjects/erase", value=PESEL)
    assert unconfirmed.status_code == 200
    with Session(get_engine()) as session:
        assert session.exec(select(VaultEntry)).all()

    erased = _admin_page(admin_client, "/admin/subjects/erase", value=PESEL, confirm="yes")
    assert erased.status_code == 200
    with Session(get_engine()) as session:
        assert session.exec(select(VaultEntry)).all() == []
        rows = session.exec(select(AuditEvent).where(AuditEvent.event.startswith("subject_"))).all()
    assert {row.event for row in rows} >= {"subject_found", "subject_exported", "subject_erased"}
    assert all(PESEL not in row.detail for row in rows)


def test_a_value_too_short_is_refused_on_the_page(admin_client):
    response = _admin_page(admin_client, "/admin/subjects", value="ab")
    assert response.status_code == 400


def _knowledge_document(session, admin_id):
    from polgate.knowledge import index

    settings = get_settings()
    anonymizer = Anonymizer([RegexGuard(), PersonNameGuard()], enabled=set(ENTITY_TYPES))
    return index.ingest(
        session,
        anonymizer,
        settings,
        user_id=admin_id,
        filename="regulamin Jan Kowalski.txt",
        text="Regulamin\n\nWnioski zatwierdza Jan Kowalski, a urlopy Anna Nowak.",
        mime_type="text/plain",
        size_bytes=60,
    ).document


def test_a_person_in_the_knowledge_base_is_exported_and_erased_there_too(db_session, people):
    from polgate.knowledge import store
    from polgate.models import KnowledgeToken

    worker, _, _ = people
    document = _knowledge_document(db_session, worker.id)

    report = subjects.find(db_session, "Jana Kowalskiego")
    assert report.knowledge_tokens
    assert report.counts()["knowledge_documents"] == 1

    exported = json.loads(subjects.export(db_session, report))
    passages = " ".join(item["text"] for item in exported["knowledge_base"])
    assert "Jan Kowalski" in passages
    assert "Anna Nowak" not in passages

    subjects.erase(db_session, report)

    db_session.refresh(document)
    stored = " ".join(row.text_sent for row in store.masked_passages(db_session, document.id))
    assert ERASED in stored
    assert ERASED in document.title
    assert "Kowalski" not in vault.decrypt(document.title_enc)
    values = {vault.decrypt(row.value_enc) for row in db_session.exec(select(KnowledgeToken))}
    assert not any("Kowalsk" in value for value in values)
    assert "Anna Nowak" in values


def test_a_queued_message_that_names_the_person_is_deleted(db_session, people):
    from polgate.models import PendingTurn

    worker, first, _ = people
    db_session.add(
        PendingTurn(
            conversation_id=first.id,
            user_id=worker.id,
            text_enc=vault.encrypt(f"PESEL {PESEL} do akt"),
        )
    )
    db_session.commit()

    report = subjects.find(db_session, PESEL)
    assert len(report.pending) == 1
    subjects.erase(db_session, report)

    assert db_session.exec(select(PendingTurn)).all() == []


def test_a_message_whose_mapping_expired_is_found_and_exported_as_typed(db_session, people):
    """No vault row holds the value any more; the original text still names the person."""
    _worker, first, _ = people
    db_session.add(
        Message(
            conversation_id=first.id,
            role=MessageRole.USER,
            content_original_enc=vault.encrypt(f"PESEL {PESEL} do akt"),
            content_sent="PESEL <PESEL_1> do akt",
        )
    )
    db_session.commit()

    exported = json.loads(subjects.export(db_session, subjects.find(db_session, PESEL)))

    (message,) = exported["conversations"][0]["messages"]
    assert message["as_typed"] is True
    assert PESEL in message["text"]


def test_a_file_is_exported_with_only_the_person_restored(db_session, people):
    worker, first, _ = people
    db_session.add(
        FileAsset(
            conversation_id=first.id,
            user_id=worker.id,
            filename="lista.txt",
            mime_type="text/plain",
            size_bytes=10,
            storage_path="",
            text_original_enc=vault.encrypt("Jan Kowalski i Anna Nowak"),
            text_sent="<OSOBA_1> i <OSOBA_2>",
            status=FileStatus.APPROVED,
        )
    )
    vault.save_mapping(
        db_session, first.id, {"<OSOBA_1>": "Jan Kowalski", "<OSOBA_2>": "Anna Nowak"}, {}, None
    )
    db_session.commit()

    exported = json.loads(subjects.export(db_session, subjects.find(db_session, "Jan Kowalski")))

    (item,) = exported["conversations"][0]["files"]
    assert item["text"] == "Jan Kowalski i <OSOBA_2>"


@pytest.mark.parametrize("path", ["/admin/subjects/export", "/admin/subjects/erase"])
def test_a_value_too_short_is_refused_before_export_or_erasure(admin_client, path):
    response = _admin_page(admin_client, path, value="ab", confirm="yes")
    assert response.status_code == 400


def test_a_surname_another_person_bears_too_is_not_erased_with_the_first():
    """Piotr Kowalski keeps the lone "Kowalski" when Jan Kowalski asks to be erased."""
    from polgate.knowledge.index import pseudonyms

    scope = [
        ("<OSOBA_1>", "PERSON", "Jan Kowalski"),
        ("<OSOBA_2>", "PERSON", "Kowalski"),
        ("<OSOBA_3>", "PERSON", "Piotr Kowalski"),
    ]
    assert subjects._matches(pseudonyms(), "Jan Kowalski", scope) == {"<OSOBA_1>": "Jan Kowalski"}
    alone = scope[:2]
    assert set(subjects._matches(pseudonyms(), "Jan Kowalski", alone)) == {"<OSOBA_1>", "<OSOBA_2>"}
