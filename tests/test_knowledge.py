"""The company knowledge base: masked with stable tokens, published, searched in every turn."""

import hashlib
import json
import re

import httpx
import pytest
from polanon import Anonymizer, PersonNameGuard, RegexGuard
from polanon.policy import ENTITY_TYPES
from sqlmodel import select

from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.knowledge import index, store
from polgate.knowledge.index import KnowledgeError
from polgate.knowledge.search import KNOWLEDGE_MARKER
from polgate.llm.router import run_turn
from polgate.models import (
    AuditEvent,
    Conversation,
    KnowledgeChunk,
    KnowledgeStatus,
    KnowledgeToken,
    KnowledgeTokenUse,
    Policy,
    User,
    VaultEntry,
)
from polgate.vault import store as vault

PESEL = "50810112354"  # born in 1850: a valid checksum that belongs to nobody
LEASE = (
    "Umowa najmu biura\n\n"
    f"Umowę najmu podpisał Jan Kowalczyk, PESEL {PESEL}, w imieniu spółki.\n\n"
    "Kaucja za klucze do magazynu wynosi 4600 zł i jest zwracana po roku."
)
LEAVE = (
    "Regulamin urlopów\n\n"
    "Wniosek o urlop zatwierdza Jan Kowalczyk z działu kadr.\n\n"
    "Urlop na żądanie zgłasza się telefonicznie przed rozpoczęciem pracy."
)
STABLE = re.compile(r"<[A-Z_]+_\d{12,}>")


def _vector(text: str) -> list[float]:
    """A bag of word stems hashed into 64 numbers: enough for the right passage to come first."""
    vector = [0.0] * 64
    for word in re.findall(r"[^\W\d_]{4,}", text.casefold()):
        vector[hashlib.sha256(word[:5].encode()).digest()[0] % 64] += 1.0
    return vector


class Provider:
    """Records what reached /embeddings and /chat/completions, and answers both."""

    def __init__(self, answer: str = "ok", embeddings_status: int = 200) -> None:
        self.embedded: list[list[str]] = []
        self.chats: list[dict] = []
        self.answer = answer
        self.embeddings_status = embeddings_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            self.embedded.append(payload["input"])
            if self.embeddings_status != 200:
                return httpx.Response(self.embeddings_status, text="model not loaded")
            rows = [{"index": i, "embedding": _vector(t)} for i, t in enumerate(payload["input"])]
            return httpx.Response(200, json={"data": rows})
        self.chats.append(payload)
        chunk = json.dumps({"choices": [{"delta": {"content": self.answer}}]})
        return httpx.Response(200, content=f"data: {chunk}\n\ndata: [DONE]\n\n".encode())

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def knowledge(self, turn: int = -1) -> str:
        return next(
            m["content"]
            for m in self.chats[turn]["messages"]
            if m["content"].startswith(KNOWLEDGE_MARKER)
        )


@pytest.fixture
def settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_model", "test-embed")
    monkeypatch.setattr(settings, "embedding_base_url", "http://embed.test/v1")
    monkeypatch.setattr(settings, "retrieval_chunk_chars", 120)
    monkeypatch.setattr(settings, "embedding_batch_size", 2)
    return settings


@pytest.fixture
def anonymizer():
    return Anonymizer([RegexGuard(), PersonNameGuard()], enabled=set(ENTITY_TYPES))


@pytest.fixture
def admin(db_session):
    user = User(username="boss", password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _ingest(session, anonymizer, settings, admin, text, filename="dokument.txt"):
    return index.ingest(
        session,
        anonymizer,
        settings,
        user_id=admin.id,
        filename=filename,
        text=text,
        mime_type="text/plain",
        size_bytes=len(text),
    ).document


async def _published(session, anonymizer, settings, admin, text, provider, filename="doc.txt"):
    document = _ingest(session, anonymizer, settings, admin, text, filename)
    async with provider.client() as client:
        await index.publish(session, document, settings, client)
    return document


def test_a_document_is_stored_masked_with_its_values_encrypted_apart(
    db_session, anonymizer, settings, admin
):
    document = _ingest(db_session, anonymizer, settings, admin, LEASE, "umowa Jan Kowalczyk.txt")

    passages = store.masked_passages(db_session, document.id)
    stored = " ".join([document.title, *(row.text_sent for row in passages)])
    for value in ("Kowalczyk", PESEL):
        assert value not in stored
    assert STABLE.search(document.title)
    assert document.status is KnowledgeStatus.PENDING_REVIEW
    assert document.passages == len(passages) > 1
    assert vault.decrypt(document.title_enc) == "umowa Jan Kowalczyk.txt"
    tokens = db_session.exec(select(KnowledgeToken)).all()
    assert tokens
    assert all("Kowalczyk" not in row.value_enc and PESEL not in row.value_enc for row in tokens)
    assert "Jan Kowalczyk" in {vault.decrypt(row.value_enc) for row in tokens}


def test_the_same_person_has_the_same_token_in_two_documents(
    db_session, anonymizer, settings, admin
):
    lease = _ingest(db_session, anonymizer, settings, admin, LEASE)
    leave = _ingest(db_session, anonymizer, settings, admin, LEAVE)

    def people(document):
        text = " ".join(row.text_sent for row in store.masked_passages(db_session, document.id))
        return set(re.findall(r"<OSOBA_\d+>", text))

    assert people(lease) == people(leave)
    assert len(people(lease)) == 1


def test_withdrawing_a_document_takes_only_the_tokens_no_other_document_uses(
    db_session, anonymizer, settings, admin
):
    lease = _ingest(db_session, anonymizer, settings, admin, LEASE)
    leave = _ingest(db_session, anonymizer, settings, admin, LEAVE)
    pesel_tokens = {
        row.token for row in db_session.exec(select(KnowledgeToken)).all() if "PESEL" in row.token
    }
    assert pesel_tokens

    removed = store.withdraw(db_session, lease)

    remaining = {row.token for row in db_session.exec(select(KnowledgeToken)).all()}
    assert removed >= 1
    assert not pesel_tokens & remaining
    assert any(token.startswith("<OSOBA_") for token in remaining)
    assert store.masked_passages(db_session, lease.id) == []
    uses = db_session.exec(select(KnowledgeTokenUse)).all()
    assert {use.document_id for use in uses} == {leave.id}

    store.withdraw(db_session, leave)
    assert db_session.exec(select(KnowledgeToken)).all() == []


@pytest.mark.parametrize(("text", "reason"), [("   \n\n  ", "empty"), ("x " * 2000, "too_long")])
def test_a_document_that_cannot_be_indexed_is_refused_and_nothing_is_kept(
    db_session, anonymizer, settings, admin, monkeypatch, text, reason
):
    monkeypatch.setattr(settings, "retrieval_max_chunks_per_file", 5)

    with pytest.raises(KnowledgeError) as raised:
        _ingest(db_session, anonymizer, settings, admin, text)

    assert raised.value.reason == reason
    assert store.documents(db_session) == []


def test_a_document_that_fails_masking_leaves_nothing_behind(
    db_session, anonymizer, settings, admin, monkeypatch
):
    from polanon import MaskingUnavailableError

    def fail(*args, **kwargs):
        raise MaskingUnavailableError("spaCy failed")

    monkeypatch.setattr(index, "index_chunks", fail)

    with pytest.raises(KnowledgeError, match="masking"):
        _ingest(db_session, anonymizer, settings, admin, LEASE)
    assert store.documents(db_session) == []


async def test_publishing_embeds_masked_passages_only_and_once(
    db_session, anonymizer, settings, admin
):
    provider = Provider()
    document = await _published(db_session, anonymizer, settings, admin, LEASE, provider)

    sent = json.dumps(provider.embedded, ensure_ascii=False)
    assert "Kowalczyk" not in sent
    assert PESEL not in sent
    assert document.status is KnowledgeStatus.PUBLISHED
    assert len(store.vectors(db_session, document.id, "test-embed")) == document.passages

    async with provider.client() as client:
        again = await index.publish(db_session, document, settings, client)
    assert again == 0


async def test_a_new_embedding_model_gets_vectors_of_its_own(
    db_session, anonymizer, settings, admin, monkeypatch
):
    provider = Provider()
    document = await _published(db_session, anonymizer, settings, admin, LEASE, provider)
    monkeypatch.setattr(settings, "embedding_model", "other-embed")

    async with provider.client() as client:
        embedded = await index.publish(db_session, document, settings, client)

    assert embedded == document.passages
    assert len(store.vectors(db_session, document.id, "test-embed")) == document.passages
    assert len(store.vectors(db_session, document.id, "other-embed")) == document.passages


async def test_a_failing_endpoint_leaves_the_document_unpublished(
    db_session, anonymizer, settings, admin
):
    document = _ingest(db_session, anonymizer, settings, admin, LEASE)

    async with Provider(embeddings_status=503).client() as client:
        with pytest.raises(KnowledgeError, match="endpoint"):
            await index.publish(db_session, document, settings, client)

    assert document.status is KnowledgeStatus.PENDING_REVIEW


async def test_publishing_without_an_embedding_model_is_refused(
    db_session, anonymizer, settings, admin, monkeypatch
):
    document = _ingest(db_session, anonymizer, settings, admin, LEASE)
    monkeypatch.setattr(settings, "embedding_model", "")

    with pytest.raises(KnowledgeError, match="no_model"):
        await index.publish(db_session, document, settings)


@pytest.fixture
def conversation(db_session):
    user = User(username="worker", password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    item = Conversation(user_id=user.id)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item, user


async def _turn(db_session, conversation, anonymizer, settings, provider, text, policy=None):
    item, user = conversation
    async with provider.client() as client:
        return [
            event
            async for event in run_turn(
                db_session,
                item,
                user,
                text,
                policy=policy or Policy(),
                anonymizer=anonymizer,
                settings=settings,
                client=client,
            )
        ]


async def test_a_question_gets_the_passage_that_answers_it_in_the_conversations_tokens(
    db_session, anonymizer, settings, admin, conversation
):
    provider = Provider(answer="Podpisał <OSOBA_1>.")
    await _published(db_session, anonymizer, settings, admin, LEASE, provider)

    events = await _turn(
        db_session, conversation, anonymizer, settings, provider, "Kto podpisał umowę najmu?"
    )

    passages = provider.knowledge()
    assert "podpisał <OSOBA_1>" in passages
    assert not STABLE.search(json.dumps(provider.chats[-1], ensure_ascii=False))
    payload = json.dumps([provider.chats, provider.embedded[1:]], ensure_ascii=False)
    for value in ("Kowalczyk", PESEL):
        assert value not in payload
    assert "".join(e.text for e in events if e.type == "token") == "Podpisał Jan Kowalczyk."
    tokens = {row.token for row in db_session.exec(select(VaultEntry)).all()}
    assert "<OSOBA_1>" in tokens


async def test_a_surname_the_base_knows_is_masked_in_the_message_as_its_person(
    db_session, anonymizer, settings, admin, conversation
):
    """No rule reads "Kowalczyk" alone; the base knows it, so the message goes out masked."""
    provider = Provider()
    await _published(db_session, anonymizer, settings, admin, LEAVE, provider)

    await _turn(
        db_session, conversation, anonymizer, settings, provider, "Do kogo pisze Kowalczyk?"
    )

    message = provider.chats[-1]["messages"][-1]["content"]
    assert "Kowalczyk" not in message
    assert re.fullmatch(r"Do kogo pisze <OSOBA_\d>\?", message)
    assert "Kowalczyk" not in json.dumps(provider.embedded, ensure_ascii=False)


async def test_a_person_named_in_the_chat_and_in_a_document_is_one_token(
    db_session, anonymizer, settings, admin, conversation
):
    provider = Provider()
    await _published(db_session, anonymizer, settings, admin, LEAVE, provider)

    await _turn(
        db_session,
        conversation,
        anonymizer,
        settings,
        provider,
        "Czy Jan Kowalczyk zatwierdza wniosek o urlop?",
    )

    message = provider.chats[-1]["messages"][-1]["content"]
    assert message == "Czy <OSOBA_1> zatwierdza wniosek o urlop?"
    assert "zatwierdza <OSOBA_1> z działu kadr" in provider.knowledge()


async def test_the_knowledge_base_is_left_alone_when_the_policy_switches_it_off(
    db_session, anonymizer, settings, admin, conversation
):
    provider = Provider()
    await _published(db_session, anonymizer, settings, admin, LEASE, provider)
    embedded = len(provider.embedded)

    await _turn(
        db_session,
        conversation,
        anonymizer,
        settings,
        provider,
        "Ile wynosi kaucja?",
        policy=Policy(knowledge_base=False),
    )

    assert len(provider.embedded) == embedded
    contents = [m["content"] for m in provider.chats[-1]["messages"]]
    assert not any(content.startswith(KNOWLEDGE_MARKER) for content in contents)


async def test_a_failing_endpoint_sends_the_turn_without_the_knowledge_base(
    db_session, anonymizer, settings, admin, conversation
):
    await _published(db_session, anonymizer, settings, admin, LEASE, Provider())
    provider = Provider(embeddings_status=503)

    events = await _turn(
        db_session, conversation, anonymizer, settings, provider, "Ile wynosi kaucja?"
    )

    assert events[-1].type == "done"
    assert provider.chats
    failures = db_session.exec(
        select(AuditEvent).where(AuditEvent.event == "knowledge_search_failed")
    ).all()
    assert [row.detail for row in failures] == ["reason=endpoint"]


async def test_a_passage_whose_token_left_the_store_is_not_sent(
    db_session, anonymizer, settings, admin, conversation
):
    """A document withdrawn while the turn ranked it: its passage names nobody the store knows."""
    provider = Provider()
    await _published(db_session, anonymizer, settings, admin, LEASE, provider)
    for row in db_session.exec(select(KnowledgeToken)).all():
        if row.token.startswith("<OSOBA_"):
            db_session.delete(row)
    db_session.commit()

    await _turn(
        db_session, conversation, anonymizer, settings, provider, "Kto podpisał umowę najmu?"
    )

    sent = json.dumps(provider.chats[-1], ensure_ascii=False)
    assert not STABLE.search(sent)
    events = db_session.exec(select(AuditEvent).where(AuditEvent.event == "message_sent")).all()
    assert "knowledge_stale=" in events[-1].detail


def test_chunks_of_a_pending_document_are_never_searched(db_session, anonymizer, settings, admin):
    document = _ingest(db_session, anonymizer, settings, admin, LEASE)
    db_session.add(
        KnowledgeChunk(
            document_id=document.id,
            position=0,
            text_sent="x",
            vector=b"\x00\x00\x80?",
            embedding_model="test-embed",
        )
    )
    db_session.commit()
    assert store.searchable(db_session, "test-embed") == []


@pytest.mark.parametrize(
    ("target", "error", "reason"),
    [("mask_query", "masking", "masking"), ("assert_no_raw_values", "refused", "refused")],
)
async def test_a_question_the_base_cannot_take_sends_the_turn_without_it(
    db_session, anonymizer, settings, admin, conversation, monkeypatch, target, error, reason
):
    from polanon import MaskingUnavailableError

    from polgate.knowledge import search as search_module
    from polgate.llm.openai_compat import LLMError

    await _published(db_session, anonymizer, settings, admin, LEASE, Provider())

    def fail(*args, **kwargs):
        if error == "masking":
            raise MaskingUnavailableError("spaCy failed")
        raise LLMError("refusing to send", token="<PESEL_1>")

    monkeypatch.setattr(search_module, target, fail)
    provider = Provider()

    events = await _turn(
        db_session, conversation, anonymizer, settings, provider, "Ile wynosi kaucja?"
    )

    assert events[-1].type == "done"
    contents = [m["content"] for m in provider.chats[-1]["messages"]]
    assert not any(content.startswith(KNOWLEDGE_MARKER) for content in contents)
    failures = db_session.exec(
        select(AuditEvent).where(AuditEvent.event == "knowledge_search_failed")
    ).all()
    assert [row.detail for row in failures] == [f"reason={reason}"]


def test_a_token_another_writer_stored_first_is_compared_and_the_rest_stored(db_session):
    """The unique key refused the batch; the store reads what is there and inserts the rest."""
    from polanon import StoredValue

    from polgate.knowledge.store import KnowledgeMappingStore

    first = KnowledgeMappingStore(db_session)
    first.add({"<OSOBA_111111111111>": StoredValue("Anna Nowak", "f1")})
    store_under_test = KnowledgeMappingStore(db_session)
    real_get = store_under_test.get
    calls = {"count": 0}

    def get_missing_the_other_writer(tokens):
        calls["count"] += 1
        return {} if calls["count"] == 1 else real_get(tokens)

    store_under_test.get = get_missing_the_other_writer

    held = store_under_test.add(
        {
            "<OSOBA_111111111111>": StoredValue("Anna Nowak", "f1"),
            "<OSOBA_222222222222>": StoredValue("Piotr Lis", "f2"),
        }
    )

    assert set(held) == {"<OSOBA_111111111111>"}
    assert set(real_get(["<OSOBA_111111111111>", "<OSOBA_222222222222>"])) == {
        "<OSOBA_111111111111>",
        "<OSOBA_222222222222>",
    }


WROBEL = (
    "Regulamin urlopów\n\n"
    "Wniosek o urlop zatwierdza Jan Wróbel z działu kadr.\n\n"
    "Pod nieobecność Wróbla wnioski zatwierdza zastępca; kopię wysyła się Wróblowi."
)


def test_a_surname_written_alone_in_another_case_is_masked_in_the_document(
    db_session, anonymizer, settings, admin
):
    """polanon learns the surname from "Jan Wróbel"; its other forms alone were left in clear."""
    document = _ingest(db_session, anonymizer, settings, admin, WROBEL)

    stored = " ".join(row.text_sent for row in store.masked_passages(db_session, document.id))
    for form in ("Wróbel", "Wróbla", "Wróblowi"):
        assert form not in stored


@pytest.mark.parametrize(
    "turns",
    [
        ["Czy Wróbel zatwierdza urlopy? Czy wniosek przekazać Wróblowi?"],
        ["Do kogo pisze Wróbel?", "Czy wniosek o urlop przekazać Wróblowi?"],
    ],
    ids=["one message", "two turns"],
)
async def test_every_spelling_of_a_surname_the_base_knows_is_masked_in_the_message(
    db_session, anonymizer, settings, admin, conversation, turns
):
    """The turn's masking matches a known value as written, so each spelling needs its token."""
    provider = Provider()
    await _published(db_session, anonymizer, settings, admin, WROBEL, provider)

    for text in turns:
        await _turn(db_session, conversation, anonymizer, settings, provider, text)

    sent = json.dumps([provider.chats, provider.embedded], ensure_ascii=False)
    for form in ("Wróbel", "Wróbla", "Wróblowi"):
        assert form not in sent


def _pseudo_words(count: int) -> str:
    """Distinct words of letters only, as many as a long regulation has."""
    letters = "abcdefghijklmnoprstuwyz"
    words = []
    for number in range(count):
        word = ""
        while True:
            number, digit = divmod(number, len(letters))
            word += letters[digit]
            if not number:
                break
        words.append(f"sło{word}")
    return " ".join(words)


def test_a_long_document_is_masked_without_running_past_sqlite_limits(
    db_session, anonymizer, settings, admin, monkeypatch
):
    """One query for every candidate token ran past 32 766 variables at about 50 000 characters."""
    monkeypatch.setattr(settings, "retrieval_chunk_chars", 1_200)
    text = _pseudo_words(9_000)
    assert len(text) > 60_000

    document = _ingest(db_session, anonymizer, settings, admin, text)

    assert document.passages > 40


def test_a_token_withdrawn_while_another_document_stores_it_is_stored_again(
    db_session, anonymizer, settings, admin, monkeypatch
):
    """Another writer deleted the token between this one reading it as held and committing."""
    from polgate import db
    from polgate.knowledge.store import KnowledgeMappingStore
    from polgate.models import KnowledgeDocument

    lease = _ingest(db_session, anonymizer, settings, admin, LEASE)
    lease_id = lease.id
    real = KnowledgeMappingStore._add_uses
    raced = {"done": False}

    def withdraw_first_once(self, tokens):
        if self.document_id != lease_id and not raced["done"]:
            raced["done"] = True
            from sqlmodel import Session

            with Session(db._engine) as other:
                store.withdraw(other, other.get(KnowledgeDocument, lease_id))
        return real(self, tokens)

    monkeypatch.setattr(KnowledgeMappingStore, "_add_uses", withdraw_first_once)

    leave = _ingest(db_session, anonymizer, settings, admin, LEAVE)

    assert raced["done"]
    used = {
        row.token
        for row in db_session.exec(
            select(KnowledgeTokenUse).where(KnowledgeTokenUse.document_id == leave.id)
        ).all()
    }
    held = {row.token for row in db_session.exec(select(KnowledgeToken)).all()}
    assert used <= held
    passages = " ".join(row.text_sent for row in store.masked_passages(db_session, leave.id))
    assert set(re.findall(r"<[A-Z_]+_\d+>", passages)) <= held


async def test_a_document_still_being_masked_cannot_be_published(
    db_session, anonymizer, settings, admin
):
    from polgate.models import KnowledgeDocument

    document = KnowledgeDocument(user_id=admin.id, status=KnowledgeStatus.INDEXING)
    db_session.add(document)
    db_session.commit()

    with pytest.raises(KnowledgeError, match="indexing"):
        await index.publish(db_session, document, settings)


async def test_a_title_holding_a_token_the_store_lost_reaches_the_model_without_it(
    db_session, anonymizer, settings, admin, conversation
):
    provider = Provider()
    document = await _published(db_session, anonymizer, settings, admin, LEASE, provider)
    document.title = "Umowa <OSOBA_999999999999>.txt"
    db_session.add(document)
    db_session.commit()

    await _turn(db_session, conversation, anonymizer, settings, provider, "Ile wynosi kaucja?")

    passages = provider.knowledge()
    assert "<OSOBA_999999999999>" not in passages
    assert "Umowa [dane wygasłe / expired data].txt" in passages
