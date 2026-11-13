"""A token whose vault row expired and whose number went to somebody else is not sent again."""

import json

import httpx
import pytest
from polanon import Anonymizer, DenylistGuard
from polanon.policy import ENTITY_TYPES
from sqlmodel import delete, select

from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.llm.router import run_turn
from polgate.models import AuditEvent, Conversation, Message, MessageRole, Policy, User, VaultEntry
from polgate.vault import fingerprints
from polgate.vault import store as vault
from polgate.vault.fingerprints import EXPIRED


def test_a_token_that_still_means_the_same_value_is_kept():
    mapping = {"<OSOBA_1>": "Anna Nowak"}
    recorded = fingerprints.record("Umowa: <OSOBA_1>.", mapping)
    assert fingerprints.expire("Umowa: <OSOBA_1>.", recorded, mapping) == ("Umowa: <OSOBA_1>.", 0)


def test_a_token_given_to_somebody_else_is_replaced():
    recorded = fingerprints.record("<OSOBA_1> zarabia 9000.", {"<OSOBA_1>": "Anna Nowak"})
    text, count = fingerprints.expire(
        "<OSOBA_1> zarabia 9000, <OSOBA_1> pracuje w kadrach.",
        recorded,
        {"<OSOBA_1>": "Piotr Lis"},
    )
    assert text == f"{EXPIRED} zarabia 9000, {EXPIRED} pracuje w kadrach."
    assert count == 2


def test_a_token_the_mapping_lost_is_replaced_even_without_fingerprints():
    """A text stored before fingerprints existed: only a token missing altogether can be told."""
    assert fingerprints.expire("Dla <OSOBA_2>.", "", {}) == (f"Dla {EXPIRED}.", 1)
    assert fingerprints.expire("Dla <OSOBA_2>.", "", {"<OSOBA_2>": "x"}) == ("Dla <OSOBA_2>.", 0)


def test_the_record_holds_keyed_fingerprints_and_no_value():
    recorded = fingerprints.record("<OSOBA_1> i <OSOBA_9>", {"<OSOBA_1>": "Anna Nowak"})
    assert json.loads(recorded) == {"<OSOBA_1>": vault.value_hash("Anna Nowak")}
    assert "Anna" not in recorded


@pytest.fixture
def conversation(db_session):
    user = User(username="u", password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    item = Conversation(user_id=user.id)
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return db_session, item, user


async def _turn(db_session, conversation, user, text, captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        body = 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body.encode())

    anonymizer = Anonymizer([DenylistGuard(["Anna Nowak", "Piotr Lis"])], enabled=set(ENTITY_TYPES))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return [
            event
            async for event in run_turn(
                db_session,
                conversation,
                user,
                text,
                policy=Policy(),
                anonymizer=anonymizer,
                settings=get_settings(),
                client=client,
            )
        ]


async def test_history_about_somebody_whose_token_was_reused_is_not_replayed_as_them(conversation):
    """Anna's vault row expired, Piotr got her number, and her salary came back under his name."""
    db_session, item, user = conversation
    captured: list[dict] = []
    await _turn(db_session, item, user, "Umowa: Anna Nowak, pensja 9000.", captured)
    first = db_session.exec(select(Message).where(Message.role == MessageRole.USER)).one()
    assert first.content_sent == "Umowa: <FIRMA_1>, pensja 9000."

    db_session.exec(delete(VaultEntry))  # the mapping retention ran out
    db_session.commit()
    await _turn(db_session, item, user, "Co z Piotr Lis?", captured)

    sent = [m["content"] for m in captured[-1]["messages"]]
    assert sent[-1] == "Co z <FIRMA_1>?"
    assert f"Umowa: {EXPIRED}, pensja 9000." in sent
    assert "Umowa: <FIRMA_1>, pensja 9000." not in sent
    events = db_session.exec(select(AuditEvent).where(AuditEvent.event == "message_sent")).all()
    assert "expired_tokens=1" in events[-1].detail


async def test_history_whose_tokens_still_hold_is_replayed_unchanged(conversation):
    db_session, item, user = conversation
    captured: list[dict] = []
    await _turn(db_session, item, user, "Umowa: Anna Nowak, pensja 9000.", captured)
    await _turn(db_session, item, user, "A Anna Nowak ma urlop?", captured)

    sent = [m["content"] for m in captured[-1]["messages"]]
    assert "Umowa: <FIRMA_1>, pensja 9000." in sent
    assert vault.load_mapping(db_session, item.id) == {"<FIRMA_1>": "Anna Nowak"}
    events = db_session.exec(select(AuditEvent).where(AuditEvent.event == "message_sent")).all()
    assert "expired_tokens" not in events[-1].detail


async def test_two_turns_at_once_give_two_people_two_tokens(conversation):
    """Both read the same empty mapping and named Anna and Piotr <FIRMA_1>; the vault kept Anna."""
    import asyncio

    db_session, item, user = conversation
    captured: list[dict] = []
    await asyncio.gather(
        _turn(db_session, item, user, "Umowa: Anna Nowak.", captured),
        _turn(db_session, item, user, "Faktura: Piotr Lis.", captured),
    )

    mapping = vault.load_mapping(db_session, item.id)
    assert sorted(mapping.values()) == ["Anna Nowak", "Piotr Lis"]
    sent = {
        m.content_sent
        for m in db_session.exec(select(Message).where(Message.role == MessageRole.USER)).all()
    }
    by_value = {value: token for token, value in mapping.items()}
    assert sent == {f"Umowa: {by_value['Anna Nowak']}.", f"Faktura: {by_value['Piotr Lis']}."}
