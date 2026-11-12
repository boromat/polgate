"""A turn with a long attachment: indexed on first use, searched, and never a leak on the way."""

import hashlib
import json
import re

import httpx
import pytest
from polanon import Anonymizer, DenylistGuard, RegexGuard, SecretsGuard
from polanon.policy import ENTITY_TYPES
from sqlmodel import select

from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.files.extract import TRUNCATION_MARKER
from polgate.llm.router import run_turn
from polgate.models import (
    AttachmentChunk,
    AuditEvent,
    Conversation,
    FileAsset,
    FileStatus,
    Policy,
    User,
)
from polgate.retrieval import search as search_module
from polgate.retrieval.select import CAPPED_MARKER, PASSAGES_MARKER

FILLER = [
    f"Rozdział {i}. Ogólne postanowienia regulaminu biura dotyczące porządku numer {i}."
    for i in range(1, 9)
]
ANSWER = "Kaucja za klucze do magazynu wynosi 4600 zł i jest zwracana po roku."
DOCUMENT = "\n\n".join(["regulamin.txt", *FILLER, ANSWER])


def _vector(text: str) -> list[float]:
    """A bag of word stems hashed into 64 numbers: enough for the right passage to come first."""
    vector = [0.0] * 64
    for word in re.findall(r"[^\W\d_]{4,}", text.casefold()):
        vector[hashlib.sha256(word[:5].encode()).digest()[0] % 64] += 1.0
    return vector


class Provider:
    """Records what reached /embeddings and /chat/completions, and answers both."""

    def __init__(self, embeddings_status: int = 200) -> None:
        self.embedded: list[list[str]] = []
        self.chats: list[dict] = []
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
        body = 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body.encode())

    def attachment(self, turn: int = -1) -> str:
        return next(
            m["content"]
            for m in self.chats[turn]["messages"]
            if m["content"].startswith("Załączony plik:")
        )


@pytest.fixture
def settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_model", "test-embed")
    monkeypatch.setattr(settings, "embedding_base_url", "http://embed.test/v1")
    monkeypatch.setattr(settings, "attachment_budget_chars", 300)
    monkeypatch.setattr(settings, "retrieval_chunk_chars", 120)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    return settings


@pytest.fixture
def setup(db_session):
    user = User(username="worker", password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    conversation = Conversation(user_id=user.id)
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)
    db_session.add(
        FileAsset(
            conversation_id=conversation.id,
            user_id=user.id,
            filename="regulamin.txt",
            mime_type="text/plain",
            size_bytes=len(DOCUMENT),
            storage_path="none",
            text_sent=DOCUMENT,
            status=FileStatus.APPROVED,
        )
    )
    db_session.commit()
    guards = [RegexGuard(), SecretsGuard(), DenylistGuard(["Jan Kowalski"])]
    anonymizer = Anonymizer(guards, enabled=set(ENTITY_TYPES))
    return db_session, conversation, user, anonymizer


async def _turn(setup, settings, provider, text, policy=None):
    session, conversation, user, anonymizer = setup
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)) as client:
        return [
            event
            async for event in run_turn(
                session,
                conversation,
                user,
                text,
                policy=policy or Policy(),
                anonymizer=anonymizer,
                settings=settings,
                client=client,
            )
        ]


def _audit(session, event: str) -> list[str]:
    rows = session.exec(select(AuditEvent).where(AuditEvent.event == event)).all()
    return [row.detail for row in rows]


async def test_a_long_attachment_sends_the_passage_that_answers_the_question(setup, settings):
    provider = Provider()
    events = await _turn(setup, settings, provider, "Ile wynosi kaucja za klucze do magazynu?")

    assert [e.detail for e in events if e.type == "status"] == ["indexing"]
    sent = provider.attachment()
    assert ANSWER in sent
    assert PASSAGES_MARKER in sent
    assert TRUNCATION_MARKER not in sent
    assert len(sent) < len(DOCUMENT)
    assert "search_files=1" in _audit(setup[0], "message_sent")[0]


async def test_the_answer_past_the_cut_never_reached_the_model_before(setup, settings, monkeypatch):
    monkeypatch.setattr(settings, "embedding_model", "")
    provider = Provider()
    await _turn(setup, settings, provider, "Ile wynosi kaucja za klucze do magazynu?")

    assert provider.embedded == []
    assert ANSWER not in provider.attachment()
    assert TRUNCATION_MARKER in provider.attachment()


async def test_a_policy_with_the_search_off_cuts_the_file(setup, settings):
    provider = Provider()
    await _turn(setup, settings, provider, "Ile wynosi kaucja?", Policy(attachment_retrieval=False))

    assert provider.embedded == []
    assert TRUNCATION_MARKER in provider.attachment()


async def test_passages_are_indexed_once_in_batches(setup, settings):
    provider = Provider()
    await _turn(setup, settings, provider, "Ile wynosi kaucja?")
    *index_batches, question = provider.embedded
    assert [len(batch) for batch in index_batches] == [4, 4, 1]
    assert question == ["Ile wynosi kaucja?"]

    events = await _turn(setup, settings, provider, "A kiedy jest zwracana?")
    assert not [e for e in events if e.type == "status"]
    assert provider.embedded[-1] == ["Ile wynosi kaucja?\nA kiedy jest zwracana?"]
    assert len(provider.embedded) == len(index_batches) + 2


async def test_nothing_sent_for_embedding_holds_an_original_value(setup, settings):
    session, *_ = setup
    asset = session.exec(select(FileAsset)).one()
    # Masked at upload as the conversation then knew it; the turn below teaches it the name.
    asset.text_sent = DOCUMENT.replace(ANSWER, f"{ANSWER} Klucze wydaje Jan Kowalski.")
    session.add(asset)
    session.commit()
    provider = Provider()

    await _turn(setup, settings, provider, "Czy Jan Kowalski wydaje klucze do magazynu?")

    sent = json.dumps(provider.embedded, ensure_ascii=False)
    assert "Kowalski" not in sent
    assert "<FIRMA_1>" in sent


async def test_a_failing_embeddings_endpoint_falls_back_to_cutting(setup, settings):
    provider = Provider(embeddings_status=500)
    events = await _turn(setup, settings, provider, "Ile wynosi kaucja?")

    assert events[-1].type == "done"
    assert TRUNCATION_MARKER in provider.attachment()
    assert _audit(setup[0], "attachment_search_failed") == ["reason=endpoint files=1"]
    assert "search_files" not in _audit(setup[0], "message_sent")[0]


async def test_a_text_that_still_holds_a_value_is_never_embedded(setup, settings, monkeypatch):
    monkeypatch.setattr(search_module, "mask_known_values", lambda text, mapping: text)
    session, *_ = setup
    asset = session.exec(select(FileAsset)).one()
    asset.text_sent = DOCUMENT.replace(ANSWER, f"{ANSWER} Klucze wydaje Jan Kowalski.")
    session.add(asset)
    session.commit()
    provider = Provider()

    await _turn(setup, settings, provider, "Czy Jan Kowalski wydaje klucze do magazynu?")

    # Batches before the one holding the name may go; that one, and the question, may not.
    assert "Kowalski" not in json.dumps(provider.embedded, ensure_ascii=False)
    assert _audit(session, "attachment_search_failed") == ["reason=refused files=1"]
    assert TRUNCATION_MARKER in provider.attachment()


async def test_a_passage_with_an_expired_token_is_skipped_and_counted(setup, settings):
    """<OSOBA_7> stood for someone whose vault row expired; the number may mean another person."""
    session, *_ = setup
    asset = session.exec(select(FileAsset)).one()
    asset.text_sent = DOCUMENT.replace(ANSWER, "Kaucję za klucze do magazynu wpłaciła <OSOBA_7>.")
    session.add(asset)
    session.commit()
    provider = Provider()

    await _turn(setup, settings, provider, "Kto wpłacił kaucję za klucze do magazynu?")

    assert "<OSOBA_7>" not in provider.attachment()
    assert "stale_passages=1" in _audit(session, "message_sent")[0]


async def test_a_file_longer_than_the_index_limit_is_searched_in_part_and_says_so(
    setup, settings, monkeypatch
):
    monkeypatch.setattr(settings, "retrieval_max_chunks_per_file", 2)
    provider = Provider()
    events = await _turn(setup, settings, provider, "Ile wynosi kaucja?")

    assert [e.detail for e in events if e.type == "status"] == ["indexing", "capped"]
    assert CAPPED_MARKER.format(count=2) in provider.attachment()
    assert ANSWER not in provider.attachment()
    assert session_rows(setup[0]) == 2


async def test_passages_cut_to_another_length_are_indexed_again(setup, settings, monkeypatch):
    provider = Provider()
    await _turn(setup, settings, provider, "Ile wynosi kaucja?")
    before = session_rows(setup[0])
    monkeypatch.setattr(settings, "retrieval_chunk_chars", 250)

    events = await _turn(setup, settings, provider, "Ile wynosi kaucja?")

    assert [e.detail for e in events if e.type == "status"] == ["indexing"]
    assert 0 < session_rows(setup[0]) < before
    assert ANSWER in provider.attachment()


async def test_a_file_within_the_budget_is_sent_whole_and_not_indexed(setup, settings, monkeypatch):
    monkeypatch.setattr(settings, "attachment_budget_chars", 10_000)
    provider = Provider()
    await _turn(setup, settings, provider, "Ile wynosi kaucja?")

    assert provider.embedded == []
    assert provider.attachment() == f"Załączony plik:\n{DOCUMENT}"


def session_rows(session) -> int:
    return len(session.exec(select(AttachmentChunk)).all())
