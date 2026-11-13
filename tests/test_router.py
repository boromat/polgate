"""One full chat turn, against a fake provider, with the no-leak guarantee asserted."""

import json

import httpx
import pytest
from polanon import (
    Anonymizer,
    DenylistGuard,
    MockJudge,
    RegexGuard,
    SecretsGuard,
    mask_known_values,
)
from polanon.policy import ENTITY_TYPES
from sqlmodel import select

from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.llm.openai_compat import LLMError, assert_no_raw_values
from polgate.llm.router import run_turn
from polgate.models import AuditEvent, Conversation, Message, Policy, User, VaultEntry
from polgate.vault import store as vault

SECRET_TEXT = "Klient Jan Kowalski, PESEL 50810112354, mail jan@example.com prosi o fakturę."


def _sse(chunks: list[str]) -> bytes:
    body = "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n" for c in chunks
    )
    return (body + "data: [DONE]\n\n").encode()


def _provider(chunks: list[str], captured: list[dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, content=_sse(chunks), headers={"content-type": "text/event-stream"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def turn_setup(db_session):
    user = User(username="worker", password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    conversation = Conversation(user_id=user.id)
    db_session.add(conversation)
    db_session.commit()
    db_session.refresh(conversation)
    anonymizer = Anonymizer([RegexGuard(), SecretsGuard()], enabled=set(ENTITY_TYPES))
    return db_session, conversation, user, anonymizer, Policy()


async def _collect(
    session, conversation, user, anonymizer, policy, chunks, captured, text=SECRET_TEXT
):
    events = []
    async with _provider(chunks, captured) as client:
        async for event in run_turn(
            session,
            conversation,
            user,
            text,
            policy=policy,
            anonymizer=anonymizer,
            settings=get_settings(),
            client=client,
        ):
            events.append(event)
    return events


async def test_provider_never_receives_original_values(turn_setup):
    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []
    await _collect(session, conversation, user, anonymizer, policy, ["ok"], captured)

    payload = json.dumps(captured[0], ensure_ascii=False)
    for value in ("50810112354", "jan@example.com"):
        assert value not in payload
    assert "<PESEL_1>" in payload


async def test_answer_is_restored_even_when_a_token_is_split_across_chunks(turn_setup):
    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []
    events = await _collect(
        session,
        conversation,
        user,
        anonymizer,
        policy,
        ["Mail ", "<EMA", "IL_1> ", "wysłany."],
        captured,
    )
    text = "".join(e.text for e in events if e.type == "token")
    assert text == "Mail jan@example.com wysłany."


async def test_the_preview_event_names_the_stored_message(turn_setup):
    """The page uses the id to offer the stored preview for a message it has not reloaded."""
    from sqlmodel import select

    from polgate.models import Message, MessageRole

    session, conversation, user, anonymizer, policy = turn_setup
    events = await _collect(session, conversation, user, anonymizer, policy, ["ok"], [])
    preview = next(event for event in events if event.type == "preview")
    stored = session.exec(select(Message).where(Message.role == MessageRole.USER)).one()
    assert preview.detail == str(stored.id)
    assert "<PESEL_1>" in preview.text


async def test_turn_persists_messages_vault_and_audit(turn_setup):
    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []
    await _collect(session, conversation, user, anonymizer, policy, ["gotowe"], captured)

    messages = session.exec(select(Message)).all()
    assert [m.role.value for m in messages] == ["user", "assistant"]
    assert vault.decrypt(messages[0].content_original_enc) == SECRET_TEXT
    assert "50810112354" not in messages[0].content_sent

    tokens = {entry.token for entry in session.exec(select(VaultEntry)).all()}
    assert "<PESEL_1>" in tokens
    assert {e.event for e in session.exec(select(AuditEvent)).all()} >= {"message_sent"}


async def test_first_message_titles_the_conversation_with_masked_text(turn_setup):
    session, conversation, user, anonymizer, policy = turn_setup
    await _collect(session, conversation, user, anonymizer, policy, ["x"], [])
    assert "50810112354" not in conversation.title
    assert conversation.title.startswith("Klient")


async def test_provider_error_yields_an_error_event(turn_setup):
    session, conversation, user, anonymizer, policy = turn_setup

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async for event in run_turn(
            session,
            conversation,
            user,
            SECRET_TEXT,
            policy=policy,
            anonymizer=anonymizer,
            settings=get_settings(),
            client=client,
        ):
            events.append(event)
    assert events[-1].type == "error"
    assert {e.event for e in session.exec(select(AuditEvent)).all()} == {"provider_error"}


async def test_blocking_policy_stops_the_turn_when_the_judge_fails(turn_setup):
    from polanon.types import JudgeFallback

    session, conversation, user, _, policy = turn_setup
    anonymizer = Anonymizer(
        [RegexGuard()], judge=MockJudge(fail=True), judge_fallback=JudgeFallback.BLOCK
    )
    events = []
    async for event in run_turn(
        session,
        conversation,
        user,
        SECRET_TEXT,
        policy=policy,
        anonymizer=anonymizer,
        settings=get_settings(),
    ):
        events.append(event)
    assert [e.type for e in events] == ["error"]
    assert events[0].detail == "judge_unavailable"
    assert session.exec(select(Message)).all() == []


def test_assert_no_raw_values_rejects_a_leaking_payload():
    with pytest.raises(LLMError, match="refusing to send"):
        assert_no_raw_values([{"content": "PESEL 50810112354"}], {"<PESEL_1>": "50810112354"})


def test_assert_no_raw_values_accepts_a_masked_payload():
    assert_no_raw_values([{"content": "PESEL <PESEL_1>"}], {"<PESEL_1>": "50810112354"})


async def test_attached_file_name_is_never_sent_in_raw_form(turn_setup):
    """A file called "CV Jan Kowalski.pdf" must not leak the name through the attachment message."""
    from polgate.models import FileAsset, FileStatus

    session, conversation, user, anonymizer, policy = turn_setup
    session.add(
        FileAsset(
            conversation_id=conversation.id,
            user_id=user.id,
            filename="CV Jan Kowalski.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            storage_path="/dev/null",
            text_original_enc=vault.encrypt("CV Jan Kowalski.pdf\n\ntreść"),
            text_sent="<OSOBA_1>.pdf\n\ntreść",
            status=FileStatus.APPROVED,
        )
    )
    session.commit()

    captured: list[dict] = []
    await _collect(
        session, conversation, user, anonymizer, policy, ["ok"], captured, text="Podsumuj plik."
    )
    payload = json.dumps(captured[0], ensure_ascii=False)
    assert "Jan Kowalski" not in payload
    assert "<OSOBA_1>.pdf" in payload


class TestConversationTitles:
    """Titles are stored unencrypted, so they are built from the masked text, never the original."""

    def test_longest_real_fragment_becomes_the_title(self):
        from polgate.llm.router import _title_from

        masked = "<OSOBA_1>, PESEL <PESEL_1>, <ADRES_1>. Proszę o przygotowanie umowy najmu."
        assert _title_from(masked) == "Proszę o przygotowanie umowy najmu"

    def test_a_message_that_is_only_tokens_falls_back_to_a_timestamp(self):
        from polgate.llm.router import _title_from

        assert _title_from("<OSOBA_1> <PESEL_1>")[:2] == "20"

    def test_long_titles_are_cut_on_a_word_boundary(self):
        from polgate.llm.router import _title_from

        title = _title_from("Podsumuj proszę tę bardzo długą wiadomość o wielu szczegółach sprawy")
        assert title.endswith("…")
        assert len(title) <= 45
        assert "  " not in title

    async def test_the_stored_title_never_contains_personal_data(self, turn_setup):
        session, conversation, user, anonymizer, policy = turn_setup
        await _collect(session, conversation, user, anonymizer, policy, ["x"], [])
        assert "50810112354" not in conversation.title
        assert "jan@example.com" not in conversation.title


def _settings_with(**overrides):
    """A copy of the test settings with a few fields changed."""
    return get_settings().model_copy(update=overrides)


async def _run(session, conversation, user, anonymizer, policy, captured, *, settings, text):
    async with _provider(["ok"], captured) as client:
        async for _ in run_turn(
            session,
            conversation,
            user,
            text,
            policy=policy,
            anonymizer=anonymizer,
            settings=settings,
            client=client,
        ):
            pass


async def test_only_the_recent_turns_are_replayed_to_the_provider(turn_setup):
    """An unbounded history grows every request until the provider refuses it."""
    session, conversation, user, anonymizer, policy = turn_setup
    settings = _settings_with(history_budget_chars=120)
    captured: list[dict] = []

    for index in range(6):
        await _run(
            session,
            conversation,
            user,
            anonymizer,
            policy,
            captured,
            settings=settings,
            text=f"Wiadomość numer {index} " + "x" * 60,
        )

    sent = "".join(message["content"] for message in captured[-1]["messages"])
    assert "numer 5" in sent
    assert "numer 0" not in sent
    assert len(captured[-1]["messages"]) < 2 + 2 * 6


async def test_the_newest_turn_survives_even_if_it_alone_exceeds_the_budget(turn_setup):
    session, conversation, user, anonymizer, policy = turn_setup
    settings = _settings_with(history_budget_chars=10)
    captured: list[dict] = []

    await _run(
        session, conversation, user, anonymizer, policy, captured, settings=settings, text="a" * 500
    )
    await _run(
        session, conversation, user, anonymizer, policy, captured, settings=settings, text="druga"
    )

    roles = [message["role"] for message in captured[-1]["messages"]]
    assert roles[-1] == "user"
    assert "druga" in captured[-1]["messages"][-1]["content"]


async def test_a_long_attachment_is_trimmed_before_it_is_resent_on_every_turn(turn_setup):
    """The whole document rides along with each message, so an untrimmed PDF is paid for twice."""
    from polgate.models import FileAsset, FileStatus

    session, conversation, user, anonymizer, policy = turn_setup
    session.add(
        FileAsset(
            conversation_id=conversation.id,
            user_id=user.id,
            filename="umowa.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            storage_path="/nie/istnieje",
            text_sent="POCZATEK " + "y" * 5000 + " KONIEC",
            status=FileStatus.APPROVED,
        )
    )
    session.commit()
    captured: list[dict] = []

    await _run(
        session,
        conversation,
        user,
        anonymizer,
        policy,
        captured,
        settings=_settings_with(attachment_budget_chars=200),
        text="Podsumuj",
    )

    attachment = next(
        message["content"]
        for message in captured[0]["messages"]
        if "Załączony plik" in message["content"]
    )
    assert "POCZATEK" in attachment
    assert "KONIEC" not in attachment
    assert "truncated" in attachment
    assert len(attachment) < 400


async def test_an_attachment_that_fits_is_sent_whole(turn_setup):
    from polgate.models import FileAsset, FileStatus

    session, conversation, user, anonymizer, policy = turn_setup
    session.add(
        FileAsset(
            conversation_id=conversation.id,
            user_id=user.id,
            filename="notatka.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_path="/nie/istnieje",
            text_sent="krótka notatka",
            status=FileStatus.APPROVED,
        )
    )
    session.commit()
    captured: list[dict] = []

    await _run(
        session,
        conversation,
        user,
        anonymizer,
        policy,
        captured,
        settings=_settings_with(attachment_budget_chars=5000),
        text="Podsumuj",
    )

    attachment = next(
        message["content"]
        for message in captured[0]["messages"]
        if "Załączony plik" in message["content"]
    )
    assert attachment.endswith("krótka notatka")


async def test_personal_data_the_model_invented_is_swept_and_recorded(turn_setup):
    """The answer is checked too: a PESEL nobody sent is worth a warning and an audit row."""
    from polgate.models import AuditEvent

    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []

    events = await _collect(
        session,
        conversation,
        user,
        anonymizer,
        policy,
        ["Znalazłem też PESEL 45831523452 w innym dokumencie."],
        captured,
    )

    done = next(event for event in events if event.type == "done")
    assert done.detail == "sweep_warning"
    rows = session.exec(select(AuditEvent).where(AuditEvent.event == "output_sweep_hit")).all()
    assert rows
    assert "new_entities=1" in rows[-1].detail
    assert "45831523452" not in rows[-1].detail


async def test_a_clean_answer_leaves_no_warning(turn_setup):
    from polgate.models import AuditEvent

    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []

    events = await _collect(
        session, conversation, user, anonymizer, policy, ["Faktura gotowa."], captured
    )

    assert next(event for event in events if event.type == "done").detail == ""
    assert not session.exec(select(AuditEvent).where(AuditEvent.event == "output_sweep_hit")).all()


REFUSAL = "refusing to send: original value for <PESEL_1> present in the payload"


async def test_a_turn_that_would_leak_is_refused_before_anything_is_stored_or_sent(
    turn_setup, monkeypatch
):
    """The safety net used to raise out of the stream, after the message was already saved."""
    from polgate.llm import router

    def refuse(messages, mapping):
        raise LLMError(REFUSAL, token="<PESEL_1>")

    monkeypatch.setattr(router, "assert_no_raw_values", refuse)
    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []

    events = await _collect(session, conversation, user, anonymizer, policy, ["ok"], captured)

    assert [(event.type, event.detail) for event in events] == [("error", "refused_unmasked")]
    assert captured == []
    assert session.exec(select(Message)).all() == []
    rows = session.exec(select(AuditEvent).where(AuditEvent.event == "turn_refused_unmasked")).all()
    assert rows
    assert "<PESEL_1>" in rows[-1].detail
    assert "50810112354" not in rows[-1].detail


async def test_a_refused_turn_does_not_poison_the_next_one(turn_setup, monkeypatch):
    """Had the refused text been saved, it would be replayed as history and refused forever."""
    from polgate.llm import router

    real_check = router.assert_no_raw_values
    calls = {"count": 0}

    def refuse_once(messages, mapping):
        calls["count"] += 1
        if calls["count"] == 1:
            raise LLMError(REFUSAL, token="<PESEL_1>")
        return real_check(messages, mapping)

    monkeypatch.setattr(router, "assert_no_raw_values", refuse_once)
    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []

    await _collect(session, conversation, user, anonymizer, policy, ["ok"], captured)
    events = await _collect(
        session, conversation, user, anonymizer, policy, ["gotowe"], captured, text="Druga prośba"
    )

    assert events[-1].type == "done"
    assert len(captured) == 1
    roles = [message["role"] for message in captured[0]["messages"]]
    assert roles == ["system", "user"]


async def test_a_word_masked_after_it_went_out_does_not_block_the_conversation(turn_setup):
    """The employee asks to mask a word an earlier turn already sent in the clear."""
    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []
    first = "Pracujemy nad Projekt Atlas."
    await _collect(session, conversation, user, anonymizer, policy, ["ok"], captured, text=first)

    with_term = Anonymizer(
        [*anonymizer.guards, DenylistGuard(["Projekt Atlas"])], enabled=set(ENTITY_TYPES)
    )
    events = await _collect(
        session, conversation, user, with_term, policy, ["ok"], captured, text="Ile Projekt Atlas?"
    )

    assert events[-1].type == "done"
    replay = captured[-1]["messages"]
    assert not any("Projekt Atlas" in message["content"] for message in replay)
    assert replay[1]["content"] == "Pracujemy nad <FIRMA_1>."
    # The stored turn is the record of what the provider received, so it is not rewritten.
    stored = session.exec(select(Message).order_by(Message.id)).first()
    assert stored.content_sent == first


async def test_history_sent_with_masking_off_is_masked_once_it_is_back_on(turn_setup):
    session, conversation, user, anonymizer, policy = turn_setup
    captured: list[dict] = []
    masking_off = Anonymizer(anonymizer.guards, enabled=set())
    await _collect(session, conversation, user, masking_off, policy, ["ok"], captured)

    events = await _collect(session, conversation, user, anonymizer, policy, ["ok"], captured)

    assert events[-1].type == "done"
    assert_no_raw_values(captured[-1]["messages"], vault.load_mapping(session, conversation.id))


def test_assert_no_raw_values_ignores_a_value_that_is_part_of_a_token():
    """A denylisted "KARTA" is part of "<KARTA_1>"; the payload holds no original value."""
    mapping = {"<KARTA_1>": "4111 1111 1111 1111", "<FIRMA_2>": "KARTA"}
    assert_no_raw_values([{"content": "Płatność <KARTA_1>, faktura do <FIRMA_2>."}], mapping)


def test_assert_no_raw_values_skips_a_token_that_stands_for_itself():
    """A template's literal placeholder stands for itself in the mapping and in the payload."""
    assert_no_raw_values([{"content": "Pole <PESEL_1> w szablonie."}], {"<PESEL_1>": "<PESEL_1>"})


def test_assert_no_raw_values_still_finds_a_value_written_next_to_a_token():
    with pytest.raises(LLMError):
        assert_no_raw_values([{"content": "<KARTA_1>KARTA"}], {"<FIRMA_2>": "KARTA"})


def test_assert_no_raw_values_finds_a_value_written_inside_a_typed_placeholder():
    mapping = {"<ADMIN_2024>": "<ADMIN_2024>", "<SEKRET_1>": "ADMIN_2024"}
    with pytest.raises(LLMError):
        assert_no_raw_values([{"content": "Wpis <ADMIN_2024> w sekcji users."}], mapping)


def test_assert_no_raw_values_passes_a_token_the_model_invented():
    """A made-up "<KARTA_9>" in replayed history holds no value, though its label is one."""
    assert_no_raw_values([{"content": "Odpowiedź dla <KARTA_9>."}], {"<FIRMA_1>": "KARTA"})


@pytest.mark.parametrize(
    "content", ["Termin 15.04.2025.", "FV/2025/03 na 2025,50 zł", "zamówienie 120256"]
)
def test_assert_no_raw_values_passes_a_short_pin_inside_a_longer_number(content):
    """The library leaves "15.04.2025" alone when the PIN is 2025, so the check does too."""
    assert_no_raw_values([{"content": content}], {"<SEKRET_1>": "2025", "<SEKRET_2>": "1202"})


@pytest.mark.parametrize("content", ["PIN 2025.", "w 2025 roku", "zapisany jako bank2025"])
def test_assert_no_raw_values_finds_a_short_pin_standing_as_a_number_of_its_own(content):
    with pytest.raises(LLMError):
        assert_no_raw_values([{"content": content}], {"<SEKRET_1>": "2025"})


@pytest.mark.parametrize(
    "content",
    ["Lista płac jest gotowa, <OSOBA_2> też.", "Kotłownia jest w piwnicy.", "lis przebiegł"],
)
def test_assert_no_raw_values_passes_a_short_surname_inside_another_word(content):
    """polanon masks "Lis" on its own, not the "Lis" in "Lista", so the check does not either."""
    mapping = {"<OSOBA_1>": "Anna Lis", "<OSOBA_2>": "Lis", "<OSOBA_3>": "Kot"}
    assert_no_raw_values([{"content": content}], mapping)


@pytest.mark.parametrize("content", ["Lis też ją dostanie.", "Nie ma pani Lisa.", "z Lisem"])
def test_assert_no_raw_values_finds_a_short_surname_as_a_word_or_in_another_case(content):
    with pytest.raises(LLMError):
        assert_no_raw_values([{"content": content}], {"<OSOBA_2>": "Lis"})


async def test_a_provider_error_quoting_a_typed_placeholder_is_reported(turn_setup):
    """The placeholder stands for itself in the mapping, and is no personal data to keep out."""
    session, conversation, user, anonymizer, policy = turn_setup

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)["messages"][-1]["content"]
        return httpx.Response(400, json={"error": {"message": f"content rejected: {sent}"}})

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async for event in run_turn(
            session,
            conversation,
            user,
            "Wypełnij pole <PESEL_1> w szablonie.",
            policy=policy,
            anonymizer=anonymizer,
            settings=get_settings(),
            client=client,
        ):
            events.append(event)
    assert events[-1].type == "error"
    assert {e.event for e in session.exec(select(AuditEvent)).all()} == {"provider_error"}


def test_assert_no_raw_values_finds_a_value_written_like_a_token():
    """Once a placeholder row expires, "<ADMIN_2024>" is the password the mapping hides."""
    with pytest.raises(LLMError):
        assert_no_raw_values(
            [{"content": "Zostaw <ADMIN_2024> bez zmian."}], {"<SEKRET_1>": "<ADMIN_2024>"}
        )


@pytest.mark.parametrize(
    "content",
    [
        "PIN 2025.",
        "PIN/PUK 2025/36912874",
        "bank2025",
        "Termin 15.04.2025.",
        "Lata 2024" + chr(0x2013) + "2025",
        "FV/2025/03 na 2025,50 zł",
        "zamówienie 120256",
        "godz. 20:25",
    ],
)
def test_assert_no_raw_values_reads_a_short_pin_as_the_library_masks_it(content):
    """The check refuses exactly where the library would have put the PIN's token."""
    mapping = {"<SEKRET_1>": "2025"}
    library_masks = mask_known_values(content, mapping) != content
    if library_masks:
        with pytest.raises(LLMError):
            assert_no_raw_values([{"content": content}], mapping)
    else:
        assert_no_raw_values([{"content": content}], mapping)


async def test_a_provider_error_quoting_a_masked_number_still_ends_the_turn(turn_setup):
    """The provider's words went into the audit detail, and "Limit 30000" holds the PIN 3000.

    The audit then refused its own row inside the error handler, and the stream died with no
    error event for the page.
    """
    session, conversation, user, anonymizer, policy = turn_setup

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Rate limit: Limit 30000, Used 29854")

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async for event in run_turn(
            session,
            conversation,
            user,
            "Mój PIN to 3000, zmień go.",
            policy=policy,
            anonymizer=anonymizer,
            settings=get_settings(),
            client=client,
        ):
            events.append(event)

    assert (events[-1].type, events[-1].detail) == ("error", "provider_error")
    row = session.exec(select(AuditEvent).where(AuditEvent.event == "provider_error")).one()
    assert row.detail == "status=429"


def test_a_refusal_names_the_token_and_nothing_else():
    with pytest.raises(LLMError) as raised:
        assert_no_raw_values([{"content": "PESEL 50810112354"}], {"<PESEL_1>": "50810112354"})
    assert raised.value.audit_detail == "token=<PESEL_1>"
    assert LLMError("connection reset").audit_detail == "transport"
