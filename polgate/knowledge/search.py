"""Find the knowledge base passages that answer a turn, written in the conversation's own tokens.

The documents are masked with stable tokens (:mod:`polgate.knowledge.index`) and the conversation
with its own numbering, so a turn works in two steps:

1. :meth:`KnowledgeSearch.prepare`, before the turn masks the message: polanon's ``mask_query``
   masks it with the same stable tokens, and finds through the mapping store the names the
   documents hold, a surname written alone among them, which no rule reads. Each such name the
   conversation has no token for yet gets one of its numbering, handed to the turn's masking as a
   known value: the message, the judge and the last check before sending all see it masked.
2. :meth:`KnowledgeSearch.search`, after: the masked question is checked and embedded, the
   published passages are ranked by :mod:`polgate.retrieval.select` as attachments are, and the
   stable tokens of the chosen ones are written as the conversation's: a person the employee named
   gets the token the message has, anybody else a new one, stored in the vault with the turn's
   mapping. The provider never sees a stable token, and the answer is restored as usual.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from polanon import (
    AnonymizationResult,
    Anonymizer,
    MaskingUnavailableError,
    PseudonymCollisionError,
    Pseudonyms,
    mask_known_values,
)
from polanon.index import mask_query, tokens_in
from polanon.policy import TOKEN_PATTERN, entity_type_for_token, token_for
from sqlmodel import Session

from polgate.config import Settings
from polgate.knowledge import store
from polgate.knowledge.index import index_anonymizer, pseudonyms
from polgate.knowledge.store import KnowledgeMappingStore
from polgate.llm.openai_compat import LLMError, assert_no_raw_values, embed
from polgate.models import KnowledgeChunk, KnowledgeDocument, Policy
from polgate.retrieval.search import SearchUnavailableError
from polgate.retrieval.select import Passage, Vector, choose, from_blob, rank, render, unit
from polgate.vault.fingerprints import EXPIRED

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(TOKEN_PATTERN)
#: Bytes per number in a stored vector, float32.
_FLOAT_BYTES = 4
#: The longest token number polanon's numbering counts from.
_COUNTED_DIGITS = 6

#: What the model reads above the passages of the knowledge base.
KNOWLEDGE_MARKER = "Baza wiedzy firmy / company knowledge base:"


@dataclass(frozen=True)
class KnowledgeQuery:
    """The message masked for the knowledge base, and the names it holds that the base knows."""

    anonymized: str
    #: stable token -> the value as the message wrote it.
    mapping: dict[str, str]
    #: conversation token -> value, for the names the base knows and the conversation did not.
    known: dict[str, str]


@dataclass
class KnowledgeFound:
    """The passages for the model, the conversation tokens they add, and counts for the audit."""

    text: str = ""
    mapping: dict[str, str] = field(default_factory=dict)
    passages: int = 0
    stale: int = 0


class KnowledgeSearch:
    """Search the published knowledge base for one turn."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        policy: Policy,
        anonymizer: Anonymizer,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Bind the search to one turn: its session, settings, policy, masking and HTTP client."""
        self.session = session
        self.settings = settings
        self.policy = policy
        self.anonymizer = anonymizer
        self.client = client

    @property
    def enabled(self) -> bool:
        """Whether an embedding model is configured and the policy allows the knowledge base."""
        return bool(self.settings.embedding_model) and self.policy.knowledge_base

    def has_passages(self) -> bool:
        """Whether a published document has passages embedded with the configured model."""
        return bool(store.searchable(self.session, self.settings.embedding_model, limit=1))

    async def prepare(self, text: str, mapping: Mapping[str, str]) -> KnowledgeQuery:
        """Mask the message for the knowledge base and give its names the base knows a token.

        Args:
            text: the message as the employee typed it.
            mapping: the conversation's mapping before this turn.

        Raises:
            SearchUnavailableError: ``masking`` when the message could not be masked for the base.
        """
        mapping_store = KnowledgeMappingStore(self.session)
        try:
            query = await asyncio.to_thread(
                mask_query, index_anonymizer(self.anonymizer), text, mapping_store
            )
        except (MaskingUnavailableError, PseudonymCollisionError) as exc:
            raise SearchUnavailableError("masking") from exc
        held = mapping_store.get(list(query.mapping))
        conversation = dict(mapping)
        values = set(conversation.values())
        known: dict[str, str] = {}
        # Every spelling, not only the first: "Wróbel" and "Wróblowi" in one message, or "Wróblowi"
        # after a turn that gave "Wróbel" a token, are one stable token, and the turn's masking
        # matches a known value only as it is written. Each spelling not known yet gets a token.
        for token, spellings in sorted(_spellings(query).items()):
            if token not in held:
                continue
            for written in spellings:
                if written in values:
                    continue
                new = _next_token(token, conversation)
                conversation[new] = known[new] = written
                values.add(written)
        return KnowledgeQuery(query.anonymized, dict(query.mapping), known)

    async def search(
        self, query: KnowledgeQuery, question: str, mapping: Mapping[str, str]
    ) -> KnowledgeFound:
        """Choose the passages for the question and write them in the conversation's tokens.

        Args:
            query: what :meth:`prepare` returned for this turn.
            question: what the search looks for: the masked message, after the previous one.
            mapping: the turn's mapping once the message is masked, the names of ``query.known``
                among it.

        Raises:
            SearchUnavailableError: ``refused`` when the question still holds an original value,
                ``endpoint`` when the embeddings endpoint fails.
        """
        rows = store.searchable(self.session, self.settings.embedding_model)
        if not rows:
            return KnowledgeFound()
        vector = await self._embed(question, query, mapping)
        candidates = [
            (document, chunk)
            for document, chunk in rows
            if len(chunk.vector) == len(vector) * _FLOAT_BYTES
        ]
        passages = [
            Passage(index, chunk.text_sent, from_blob(chunk.vector))
            for index, (_, chunk) in enumerate(candidates)
        ]
        order = await asyncio.to_thread(rank, question, vector, passages)
        chosen = choose(
            passages, order, self.policy.retrieval_top_k, self.settings.knowledge_budget_chars
        )
        return self._written(
            [(candidates[p.position][0], candidates[p.position][1], p.text) for p in chosen],
            query,
            mapping,
        )

    async def _embed(
        self, question: str, query: KnowledgeQuery, mapping: Mapping[str, str]
    ) -> Vector:
        """Mask what the conversation knows in the question, check it and embed it."""
        conversation = dict(mapping)
        text = await asyncio.to_thread(mask_known_values, question, conversation)
        try:
            assert_no_raw_values([{"content": text}], {**query.mapping, **conversation})
        except LLMError as exc:
            raise SearchUnavailableError("refused") from exc
        base_url, api_key = self.settings.embedding_endpoint()
        try:
            vectors = await embed(
                [text],
                model=self.settings.embedding_model,
                base_url=base_url,
                api_key=api_key,
                client=self.client,
            )
        except LLMError as exc:
            raise SearchUnavailableError("endpoint") from exc
        return unit(vectors[0])

    def _written(
        self,
        chosen: Sequence[tuple[KnowledgeDocument, KnowledgeChunk, str]],
        query: KnowledgeQuery,
        mapping: Mapping[str, str],
    ) -> KnowledgeFound:
        """Write the chosen passages, with their titles, in the conversation's tokens.

        A passage holding a token the store no longer has, from a document withdrawn a moment ago,
        is left out and counted.
        """
        titles = {document.id: document.title for document, _, _ in chosen}
        texts = [*titles.values(), *(text for _, _, text in chosen)]
        held = KnowledgeMappingStore(self.session).get(tokens_in(*texts))
        conversation = dict(mapping)
        found = KnowledgeFound()
        translated: dict[str, str] = {}
        stable = pseudonyms()
        for token in sorted(held):
            value = held[token].value
            written = query.mapping.get(token)
            existing = _conversation_token(written, value, token, conversation, stable)
            if existing is None:
                existing = _next_token(token, conversation)
                conversation[existing] = found.mapping[existing] = value
            translated[token] = existing
        sections: dict[int | None, list[Passage]] = {}
        for document, chunk, text in chosen:
            if not set(tokens_in(text)) <= translated.keys():
                found.stale += 1
                continue
            sections.setdefault(document.id, []).append(
                Passage(chunk.position, _replaced(text, translated))
            )
        # A title is not left out for a token the store lost, as a passage is: the token goes, so
        # neither a stable token nor a literal "<OSOBA_1>" in a file name reaches the model.
        rendered = [
            render(_replaced(titles[document_id], translated, missing=EXPIRED), passages)
            for document_id, passages in sections.items()
        ]
        found.passages = sum(len(passages) for passages in sections.values())
        if rendered:
            found.text = "\n\n".join([KNOWLEDGE_MARKER, *rendered])
        return found


def _conversation_token(
    written: str | None,
    value: str,
    token: str,
    conversation: Mapping[str, str],
    stable: Pseudonyms,
) -> str | None:
    """Return the conversation's token for a value of the base, or None when it has none.

    The value as the message wrote it is looked for first, then the value as the base holds it,
    then any value of the same type with the same stable token, which does not depend on the
    spelling: "Janowi Kowalskiemu" in the conversation is the base's "Jan Kowalski".
    """
    entity_type = entity_type_for_token(token)
    same_type = {
        existing: held
        for existing, held in conversation.items()
        if entity_type_for_token(existing) == entity_type
    }
    for candidate in (written, value):
        for existing, held in same_type.items():
            if held == candidate:
                return existing
    for existing, held in same_type.items():
        try:
            if stable.token(held, entity_type) == token:
                return existing
        except PseudonymCollisionError:
            continue
    return None


def _next_token(stable: str, conversation: Mapping[str, str]) -> str:
    """Return the next free token of the conversation's numbering for the type of ``stable``."""
    entity_type = entity_type_for_token(stable)
    label = _label(token_for(entity_type, 1))
    # As polanon counts: a number of more than six digits is an identifier somebody typed.
    numbers = [
        int(match["n"])
        for token in conversation
        if (match := _TOKEN_RE.fullmatch(token)) is not None
        and match["label"] == label
        and len(match["n"]) <= _COUNTED_DIGITS
    ]
    return token_for(entity_type, max(numbers, default=0) + 1)


def _label(token: str) -> str:
    match = _TOKEN_RE.fullmatch(token)
    if match is None:  # pragma: no cover - token_for always returns a token
        raise ValueError("not a token")
    return match["label"]


def _replaced(text: str, translated: Mapping[str, str], missing: str | None = None) -> str:
    """Write every stable token of ``text`` as the conversation's token for it.

    A token with no translation stays as it is, or becomes ``missing`` when one is given.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return translated.get(token, token if missing is None else missing)

    return _TOKEN_RE.sub(replace, text)


def _spellings(result: AnonymizationResult) -> dict[str, list[str]]:
    """Return every token of a masked text with each spelling it replaced, in reading order."""
    found: dict[str, list[str]] = {}
    shift = 0
    for span in result.spans:
        match = _TOKEN_RE.match(result.anonymized, span.start + shift)
        if match is None:  # pragma: no cover - every span was replaced by a token
            continue
        written = result.original[span.start : span.end]
        spellings = found.setdefault(match.group(0), [])
        if written not in spellings:
            spellings.append(written)
        shift += len(match.group(0)) - (span.end - span.start)
    return found
