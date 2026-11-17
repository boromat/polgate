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
from polanon.stream_restore import restored as restore_text
from sqlmodel import Session, col, select

from polgate import audit
from polgate.config import Settings
from polgate.files.extract import TRUNCATION_MARKER
from polgate.knowledge.search import KnowledgeFound, KnowledgeQuery, KnowledgeSearch
from polgate.llm.openai_compat import LLMError, assert_no_raw_values, stream_chat
from polgate.llm.prompts import system_prompt
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
from polgate.tokens import WRITTEN_TOKEN_RE, base_token
from polgate.vault import fingerprints
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
    session: Session,
    conversation: Conversation,
    budget_chars: int,
    mapping: dict[str, str],
    expired: list[int],
) -> list[dict[str, str]]:
    """Rebuild the provider-side conversation from what was sent before, never from originals.

    Only the most recent turns that fit inside ``budget_chars`` are replayed. Without a ceiling the
    request grows with every message until the provider rejects it, and the employee sees a failure
    whose cause is fifty messages back. The newest turn is always included, even alone.

    A token that stands for somebody else now than when the message was sent is replaced, see
    :mod:`polgate.vault.fingerprints`; how many places changed is added to ``expired``.
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
        content, count = fingerprints.expire(
            message.content_sent, message.token_fingerprints, mapping
        )
        expired.append(count)
        kept.append({"role": message.role.value, "content": content})
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
    files: list[FileAsset],
    budget_chars: int,
    searched: dict[int, str],
    mapping: dict[str, str],
    expired: list[int],
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
    texts = []
    for f in files:
        if not f.text_sent:
            continue
        searched_text = searched.get(f.id or 0)
        if searched_text:
            # Passages with a stale token were left out by the search already.
            texts.append(searched_text)
            continue
        text, count = fingerprints.expire(f.text_sent, f.token_fingerprints, mapping)
        expired.append(count)
        texts.append(_trim(text, budget_chars))
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


async def _prepare_knowledge(
    session: Session,
    conversation: Conversation,
    user: User,
    knowledge: KnowledgeSearch,
    text: str,
    mapping: dict[str, str],
) -> KnowledgeQuery | None:
    """Mask the message for the knowledge base, or return None when it is off, empty or failed."""
    if not knowledge.enabled or not knowledge.has_passages():
        return None
    try:
        return await knowledge.prepare(text, mapping)
    except SearchUnavailableError as exc:
        _knowledge_failed(session, conversation, user, exc)
        return None


async def _search_knowledge(
    session: Session,
    conversation: Conversation,
    user: User,
    knowledge: KnowledgeSearch,
    query: KnowledgeQuery,
    question: str,
    mapping: dict[str, str],
) -> KnowledgeFound:
    """Find the knowledge base's passages; when the search cannot run, the turn goes on without."""
    try:
        return await knowledge.search(query, question, mapping)
    except SearchUnavailableError as exc:
        _knowledge_failed(session, conversation, user, exc)
        return KnowledgeFound()


def _knowledge_failed(
    session: Session, conversation: Conversation, user: User, exc: SearchUnavailableError
) -> None:
    """Record, in a word, why the knowledge base was not searched for this turn."""
    logger.warning("knowledge base search unavailable (%s); the turn goes on without", exc.reason)
    audit.log(
        session,
        event="knowledge_search_failed",
        user_id=user.id,
        conversation_id=conversation.id,
        detail=f"reason={exc.reason}",
    )


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


def _title_from(anonymized: str, limit: int = 60) -> str:
    """Build a conversation title from the masked text: its sentence with the most words.

    The original can never be used, because the title is stored unencrypted. The sentence is kept
    with its tokens, which the page draws as redaction bars, so "Odpisz <OSOBA_1> (PESEL <PESEL_1>),
    że wniosek…" reads as the request it is; a run of words cut out between tokens read as "że
    wniosek…" and lost its verb. A token counts as two characters against the limit, the width of
    its bar, and is never cut in two.
    """
    sentences = [" ".join(s.split()) for s in _SENTENCE_BREAK.split(anonymized) if s.strip()]
    best = max(sentences, key=_word_count, default="")
    if not _word_count(best):
        return utcnow().strftime("%Y-%m-%d %H:%M")
    title = _trimmed(best)
    if _display_length(title) <= limit:
        return title
    kept: list[str] = []
    for word in title.split(" "):
        if _display_length(" ".join([*kept, word])) > limit:
            break
        kept.append(word)
    return " ".join(kept).rstrip(" ,.;:-(") + "…"


#: Where one sentence of a message ends and the next begins: a stop followed by a capital, or a
#: new line. The stop of an abbreviation, "tel. <TELEFON_1>" or "Sp. z o.o.", is followed by a
#: token, a number or a small letter, and splitting there titled a request "▮, ▮), że wniosek…".
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+(?=[„\"«(\[]?[A-ZĄĆĘŁŃÓŚŹŻ])|\n+")
#: A word for a title: letters, outside any token.
_TITLE_WORD = re.compile(r"[^\W\d_]{2,}")
#: Opening brackets and quotes a title may start with, and what closes each.
_CLOSERS = {"(": ")", "[": "]", "„": "”", "«": "»", '"': '"'}


def _word_count(text: str) -> int:
    return len(_TITLE_WORD.findall(_TOKEN_RE.sub(" ", text)))


def _display_length(text: str) -> int:
    """Return how long a title looks: a token is drawn as a short bar, two characters wide."""
    return len(_TOKEN_RE.sub("##", text))


def _trimmed(fragment: str) -> str:
    """Trim a sentence's edges: separators, a full stop, and a bracket nothing in it opened.

    Anything but a letter, a digit or a token goes from the start, except an opening bracket or
    quote the sentence closes, so a title never opens with "), że".
    """
    start = 0
    while start < len(fragment) and not fragment[start].isalnum():
        if _TOKEN_RE.match(fragment, start):
            break
        closer = _CLOSERS.get(fragment[start])
        if closer is not None and closer in fragment[start + 1 :]:
            break
        start += 1
    text = fragment[start:].rstrip(" ,.;:-(!?")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip(" ,.;:-(")
    return text


def _for_sweep(answer: str, mapping: dict[str, str]) -> str:
    """Restore an answer for the output sweep with every name in the form the mapping holds.

    A token with a case comes back declined, "Janie Kowalski" for "Janowi Kowalskiemu", and
    polanon's sweep, which knows a known name with Polish endings glued on but not one whose stem
    changes, reported every such answer as holding a person the message never named. Restored
    without the cases, the answer holds the same tokens and nothing the sweep has to guess about.
    """
    plain = WRITTEN_TOKEN_RE.sub(base_token, answer)
    return restore_text(plain, mapping)


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
    # From reading the mapping to storing it, one turn or upload at a time per conversation:
    # two at once both started from the same mapping and could give one token to two people,
    # and the search's indexing in between made that window seconds long.
    async with vault.conversation_lock(_require_id(conversation)):
        mapping = vault.load_mapping(session, _require_id(conversation))
        knowledge = KnowledgeSearch(session, settings, policy, anonymizer, client)
        query = await _prepare_knowledge(session, conversation, user, knowledge, text, mapping)
        # A name the knowledge base knows and the conversation did not, such as a surname written
        # alone, is handed to the masking as known, so the message goes out with it masked.
        known = query.known if query is not None else {}
        try:
            result = await anonymizer.anonymize(text, {**mapping, **known})
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

        expired: list[int] = []
        history = _history_messages(
            session, conversation, settings.history_budget_chars, result.mapping, expired
        )
        files = _approved_files(session, conversation)
        found = SearchResult()
        search = AttachmentSearch(settings, policy, result.mapping, client)
        question = _search_question(result.anonymized, history)
        async for event in _search_attachments(
            session, conversation, user, search, files, question, found
        ):
            yield event
        learned = KnowledgeFound()
        if query is not None:
            learned = await _search_knowledge(
                session,
                conversation,
                user,
                knowledge,
                query,
                _search_question(query.anonymized, history),
                result.mapping,
            )
        # The tokens the knowledge base's passages brought join the turn's mapping: the check
        # below reads them, the vault keeps them and the answer is restored with them.
        turn_mapping = {**result.mapping, **learned.mapping}
        replayed = [
            *_attachment_messages(
                files, settings.attachment_budget_chars, found.texts, result.mapping, expired
            ),
            *([{"role": "system", "content": learned.text}] if learned.text else []),
            *history,
        ]
        # Earlier turns are replayed from what was sent at the time, and a value that has a token
        # now may have gone out in the clear back then: a word the employee only now asked to mask,
        # a denylist entry added since, masking switched back on. Left alone, the check below finds
        # it in the replay and refuses this turn and every later one. The stored text is not
        # rewritten: it is the record of what the provider received, and the preview shows exactly
        # that.
        replayed = await asyncio.to_thread(_mask_replay, replayed, turn_mapping)
        conversation_messages = [*replayed, {"role": "user", "content": result.anonymized}]
        # The last check before anything leaves, and it runs before anything is stored.
        # `content_sent` is plaintext and is replayed as history on every later turn, so a message
        # that failed this check and was saved anyway would make every following turn fail it too.
        # It reads everything that came from people and files. The system prompt is made from code
        # alone and holds nobody's data, and checking it refused every turn of a conversation
        # in which the employee asked to mask a word it happens to contain, such as "company".
        # A system prompt built from anything but code would have to join the check again.
        try:
            assert_no_raw_values(conversation_messages, turn_mapping)
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

        entity_types = {token: entity_type_for_token(token) for token in turn_mapping}
        vault.save_mapping(
            session, _require_id(conversation), turn_mapping, entity_types, policy.vault_ttl_days
        )
        user_message = Message(
            conversation_id=_require_id(conversation),
            role=MessageRole.USER,
            content_original_enc=vault.encrypt(text),
            content_sent=result.anonymized,
            token_fingerprints=fingerprints.record(result.anonymized, result.mapping),
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

    messages = [{"role": "system", "content": system_prompt(user.language)}, *conversation_messages]
    restorer = StreamRestorer(turn_mapping)
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
    except (GeneratorExit, asyncio.CancelledError):
        # The page went away while the answer streamed: the provider has the message all the same,
        # so the audit trail records it, as interrupted, before the stream is torn down.
        audit.log(
            session,
            event="message_sent",
            user_id=user.id,
            conversation_id=conversation.id,
            provider=settings.llm_base_url,
            model=settings.llm_model,
            entity_counts=result.entity_counts,
            stage2_hits=result.judge_hits,
            detail=_sent_detail(policy, settings, result.judge_failed, found, learned, expired)
            + " interrupted",
        )
        raise
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
    report = await asyncio.to_thread(
        anonymizer.sweep, _for_sweep("".join(raw_parts), turn_mapping), turn_mapping
    )
    session.add(
        Message(
            conversation_id=_require_id(conversation),
            role=MessageRole.ASSISTANT,
            content_original_enc=vault.encrypt(restored),
            content_sent="".join(raw_parts),
            token_fingerprints=fingerprints.record("".join(raw_parts), turn_mapping),
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
        detail=_sent_detail(policy, settings, result.judge_failed, found, learned, expired),
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


def _sent_detail(
    policy: Policy,
    settings: Settings,
    judge_failed: bool,
    found: SearchResult,
    learned: KnowledgeFound,
    expired: list[int],
) -> str:
    """Describe a sent message for its audit row: the judge, the searches and expired tokens."""
    detail = _judge_detail(policy, settings, failed=judge_failed) + _search_detail(found)
    if learned.passages or learned.stale:
        detail += f" knowledge_passages={learned.passages} knowledge_stale={learned.stale}"
    return detail + (f" expired_tokens={sum(expired)}" if sum(expired) else "")


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
