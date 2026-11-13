"""Orchestrates one chat turn end to end.

This is the only place where a message travels from an employee to a provider and back, which makes
it the natural place to enforce the guarantee: anonymize, verify, send, restore, sweep, record.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import httpx
from polanon import Anonymizer, StreamRestorer, mask_known_values
from polanon.judge.base import JudgeError
from polanon.policy import TOKEN_PATTERN, entity_type_for_token
from sqlmodel import Session, col, select

from polgate import audit
from polgate.config import Settings
from polgate.files.extract import TRUNCATION_MARKER
from polgate.llm.openai_compat import LLMError, assert_no_raw_values, stream_chat
from polgate.llm.prompts import SYSTEM_PROMPT
from polgate.models import (
    Conversation,
    FileAsset,
    FileStatus,
    Message,
    MessageRole,
    Policy,
    User,
    utcnow,
)
from polgate.retrieval.search import AttachmentSearch, SearchResult, SearchUnavailableError
from polgate.vault import store as vault

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(TOKEN_PATTERN)

#: ``status`` tells the page what the hub is doing before the answer starts: ``indexing`` while a
#: long attachment is indexed for the first time, ``capped`` when one was longer than it indexes.
EventType = Literal["status", "preview", "token", "done", "error"]


@dataclass
class TurnEvent:
    """One server-sent event of a streamed turn."""

    type: EventType
    text: str = ""
    detail: str = ""


def _history_messages(
    session: Session, conversation: Conversation, budget_chars: int
) -> list[dict[str, str]]:
    """Rebuild the provider-side conversation from what was sent before, never from originals.

    Only the most recent turns that fit inside ``budget_chars`` are replayed. Without a ceiling the
    request grows with every message until the provider rejects it, and the employee sees a failure
    whose cause is fifty messages back. The newest turn is always included, even alone.
    """
    previous = session.exec(
        select(Message)
        .where(col(Message.conversation_id) == conversation.id)
        .order_by(col(Message.id))
    ).all()

    kept: list[dict[str, str]] = []
    used = 0
    for message in reversed(previous):
        if not message.content_sent:
            continue
        used += len(message.content_sent)
        if used > budget_chars and kept:
            break
        kept.append({"role": message.role.value, "content": message.content_sent})
    kept.reverse()
    return kept


def _approved_files(session: Session, conversation: Conversation) -> list[FileAsset]:
    """Return the conversation's approved files that still hold text, oldest first."""
    files = session.exec(
        select(FileAsset)
        .where(col(FileAsset.conversation_id) == conversation.id)
        .where(col(FileAsset.status) == FileStatus.APPROVED)
        .order_by(col(FileAsset.id))
    ).all()
    return [f for f in files if f.text_sent]


def _attachment_messages(
    files: list[FileAsset], budget_chars: int, searched: dict[int, str]
) -> list[dict[str, str]]:
    """Return each approved file's text: the passages found for it, or the file cut to the budget.

    Only ``text_sent`` is used. It was produced at upload time from the file name followed by the
    extracted text, so the name is already masked; repeating ``filename`` here would send a raw
    "CV Jan Kowalski.pdf" straight to the provider.

    Attachments are resent with every turn, so a long document is not a one-off cost: the trim is
    what stops one 200-page PDF from being paid for again on each message. When text is dropped the
    model is told, in the same words the extractor uses, rather than left to guess; passages chosen
    by the search say so the same way.
    """
    # A file rejected while this turn indexed it has lost its text by now, and is left out.
    texts = [
        searched.get(f.id or 0) or _trim(f.text_sent, budget_chars) for f in files if f.text_sent
    ]
    return [{"role": "system", "content": f"Załączony plik:\n{text}"} for text in texts]


async def _search_attachments(
    session: Session,
    conversation: Conversation,
    user: User,
    search: AttachmentSearch,
    files: list[FileAsset],
    question: str,
    found: SearchResult,
) -> AsyncIterator[TurnEvent]:
    """Search the long files for ``question`` into ``found``, telling the page while it indexes.

    When the search cannot run, ``found`` stays empty, so every file is cut to the budget as before
    the search existed, and the audit trail records why in a word and how many files, nothing more.
    """
    documents = await asyncio.to_thread(
        search.documents, [f for f in files if search.applies_to(f)]
    )
    if not documents:
        return
    indexing = search.needs_indexing(session, documents)
    if indexing:
        yield TurnEvent("status", detail="indexing")
    try:
        result = await search.search(session, documents, question)
    except SearchUnavailableError as exc:
        logger.warning("attachment search unavailable (%s); sending the files cut", exc.reason)
        audit.log(
            session,
            event="attachment_search_failed",
            user_id=user.id,
            conversation_id=conversation.id,
            detail=f"reason={exc.reason} files={len(documents)}",
        )
        return
    found.texts.update(result.texts)
    found.passages, found.stale, found.capped = result.passages, result.stale, result.capped
    if indexing and result.capped:
        yield TurnEvent("status", detail="capped")


def _search_question(anonymized: str, history: list[dict[str, str]]) -> str:
    """Return what the search looks for: this message, after the employee's previous one.

    A follow-up such as "and how much does she earn?" names nobody; the message before it does.
    Both are masked: the previous one is what the provider received at the time.
    """
    previous = [m["content"] for m in history if m["role"] == MessageRole.USER.value]
    return f"{previous[-1]}\n{anonymized}" if previous else anonymized


def _mask_replay(messages: list[dict[str, str]], mapping: dict[str, str]) -> list[dict[str, str]]:
    """Mask every value the conversation now has a token for in messages that are sent again."""
    return [
        {**message, "content": mask_known_values(message["content"], mapping)}
        for message in messages
    ]


def _trim(text: str, budget_chars: int) -> str:
    """Cut text to the budget, saying so where the cut happens."""
    if len(text) <= budget_chars:
        return text
    return text[:budget_chars] + TRUNCATION_MARKER


def _title_from(anonymized: str, limit: int = 44) -> str:
    """Build a conversation title from the masked text.

    The original can never be used, because the title is stored unencrypted, and a title made of
    placeholders is unreadable. So the longest run of real words between tokens is taken: in
    practice that is the sentence carrying the request, not the data around it.
    """
    # Tokens are replaced with a separator first: TOKEN_PATTERN has capture groups, and re.split
    # would return their contents as fragments of their own.
    parts = _TOKEN_RE.sub("\x00", anonymized).split("\x00")
    fragments = [" ".join(part.split()).strip(" ,.;:-") for part in parts]
    best = max(fragments, key=lambda part: len(part.split()), default="")
    if not best:
        return utcnow().strftime("%Y-%m-%d %H:%M")
    return best if len(best) <= limit else best[:limit].rsplit(" ", 1)[0] + "…"


async def run_turn(
    session: Session,
    conversation: Conversation,
    user: User,
    text: str,
    *,
    policy: Policy,
    anonymizer: Anonymizer,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[TurnEvent]:
    """Run one user turn and stream the assistant's reply back with originals restored.

    Args:
        session: open database session; this function commits.
        conversation: the conversation the turn belongs to.
        user: the author of the message.
        text: the original message as typed.
        policy: active admin policy.
        anonymizer: configured anonymization facade.
        settings: application settings, for provider credentials.
        client: optional injected HTTP client, used by tests.

    Yields:
        A ``preview`` event with the text that will be sent, then ``token`` events, then ``done``.
        Any failure produces a single ``error`` event instead.
    """
    mapping = vault.load_mapping(session, _require_id(conversation))
    try:
        result = await anonymizer.anonymize(text, mapping)
    except JudgeError as exc:
        logger.warning("turn blocked: judge unavailable (%s)", exc)
        audit.log(
            session,
            event="turn_blocked_judge",
            user_id=user.id,
            conversation_id=conversation.id,
            detail="stage-2 judge unavailable and policy is 'block'",
        )
        yield TurnEvent("error", detail="judge_unavailable")
        return

    history = _history_messages(session, conversation, settings.history_budget_chars)
    files = _approved_files(session, conversation)
    found = SearchResult()
    search = AttachmentSearch(settings, policy, result.mapping, client)
    question = _search_question(result.anonymized, history)
    async for event in _search_attachments(
        session, conversation, user, search, files, question, found
    ):
        yield event
    replayed = [
        *_attachment_messages(files, settings.attachment_budget_chars, found.texts),
        *history,
    ]
    # Earlier turns are replayed from what was sent at the time, and a value that has a token now
    # may have gone out in the clear back then: a word the employee only now asked to mask, a
    # denylist entry added since, masking switched back on. Left alone, the check below finds it in
    # the replay and refuses this turn and every later one. The stored text is not rewritten: it is
    # the record of what the provider received, and the preview shows exactly that.
    replayed = await asyncio.to_thread(_mask_replay, replayed, result.mapping)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *replayed,
        {"role": "user", "content": result.anonymized},
    ]
    # The last check before anything leaves, and it runs before anything is stored. `content_sent`
    # is plaintext and is replayed as history on every later turn, so a message that failed this
    # check and was saved anyway would make every following turn fail it too.
    try:
        assert_no_raw_values(messages, result.mapping)
    except LLMError as exc:
        logger.warning("turn refused before sending: %s", exc)
        audit.log(
            session,
            event="turn_refused_unmasked",
            user_id=user.id,
            conversation_id=conversation.id,
            detail=exc.audit_detail,
        )
        yield TurnEvent("error", detail="refused_unmasked")
        return

    entity_types = {token: entity_type_for_token(token) for token in result.mapping}
    vault.save_mapping(
        session, _require_id(conversation), result.mapping, entity_types, policy.vault_ttl_days
    )
    user_message = Message(
        conversation_id=_require_id(conversation),
        role=MessageRole.USER,
        content_original_enc=vault.encrypt(text),
        content_sent=result.anonymized,
        entity_count=sum(result.entity_counts.values()),
        judge_hits=result.judge_hits,
    )
    session.add(user_message)
    if not conversation.title:
        conversation.title = _title_from(result.anonymized)
    conversation.updated_at = utcnow()
    session.add(conversation)
    session.commit()
    session.refresh(user_message)

    # The message id lets the page offer the stored preview for a message it has not reloaded.
    yield TurnEvent("preview", text=result.anonymized, detail=str(user_message.id))

    restorer = StreamRestorer(result.mapping)
    raw_parts: list[str] = []
    restored_parts: list[str] = []
    try:
        async for delta in stream_chat(
            messages,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            client=client,
        ):
            raw_parts.append(delta)
            visible = restorer.feed(delta)
            if visible:
                restored_parts.append(visible)
                yield TurnEvent("token", text=visible)
    except LLMError as exc:
        logger.warning("provider call failed: %s", exc)
        audit.log(
            session,
            event="provider_error",
            user_id=user.id,
            conversation_id=conversation.id,
            provider=settings.llm_base_url,
            model=settings.llm_model,
            # A token, a status code or "transport", never the provider's own words: an answer
            # that quoted a number equal to a masked PIN made the audit refuse the row, and the
            # stream died without an error event.
            detail=exc.audit_detail,
        )
        yield TurnEvent("error", detail="provider_error")
        return

    tail = restorer.flush()
    if tail:
        restored_parts.append(tail)
        yield TurnEvent("token", text=tail)

    restored = "".join(restored_parts)
    # Same reason the guards run in a thread inside the pipeline: the sweep runs them again, over
    # the whole answer, and the event loop still has other people's requests to serve.
    report = await asyncio.to_thread(anonymizer.sweep, restored, result.mapping)
    session.add(
        Message(
            conversation_id=_require_id(conversation),
            role=MessageRole.ASSISTANT,
            content_original_enc=vault.encrypt(restored),
            content_sent="".join(raw_parts),
        )
    )
    session.commit()

    audit.log(
        session,
        event="message_sent",
        user_id=user.id,
        conversation_id=conversation.id,
        provider=settings.llm_base_url,
        model=settings.llm_model,
        entity_counts=result.entity_counts,
        stage2_hits=result.judge_hits,
        # The backend is recorded because it answers a compliance question the counts cannot:
        # with `remote`, the stage-1 masked text left the network as well as the final prompt.
        detail=_judge_detail(policy, settings, failed=result.judge_failed) + _search_detail(found),
    )
    if not report.is_clean:
        audit.log(
            session,
            event="output_sweep_hit",
            user_id=user.id,
            conversation_id=conversation.id,
            detail=(
                f"unknown_tokens={len(report.unknown_tokens)} new_entities={len(report.new_spans)}"
            ),
        )
    yield TurnEvent("done", detail="sweep_warning" if not report.is_clean else "")


def _judge_detail(policy: Policy, settings: Settings, *, failed: bool) -> str:
    """Describe stage 2 for the audit row: which backend ran, and whether it failed."""
    backend = settings.judge_backend if policy.judge_enabled else "off"
    return f"judge={backend}" + (" judge_failed" if failed else "")


def _search_detail(found: SearchResult) -> str:
    """Describe the search for the audit row, in counts: files, passages sent, stale passages."""
    if not found.texts:
        return ""
    return (
        f" search_files={len(found.texts)} search_passages={found.passages}"
        f" stale_passages={found.stale}"
    )


def _require_id(conversation: Conversation) -> int:
    """Return the conversation's primary key, which is always set once persisted."""
    if conversation.id is None:  # pragma: no cover - defensive
        raise ValueError("conversation must be persisted before running a turn")
    return conversation.id
