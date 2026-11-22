"""Add a document to the knowledge base: mask it with stable tokens, then embed it to publish it.

A knowledge base document is shared by every conversation, so it cannot be masked with any one
conversation's numbering. polanon's pseudonyms give each value a token computed from it under the
hub's key, the same in every document and every question: ``<OSOBA_001712692378>``. polanon's
``index_chunks`` masks the passages and puts what each token stands for into the mapping store,
never into the passages. A turn translates these tokens into the conversation's own before
anything is sent (:mod:`polgate.knowledge.search`).

The stage-2 judge does not run on a document: polanon masks for an index without one, since the
passages are masked as one text outside any event loop. The administrator reviews the masked
passages before publishing instead, as an employee reviews an upload.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from itertools import batched

import httpx
from polanon import Anonymizer, MaskingUnavailableError, PseudonymCollisionError, Pseudonyms
from polanon.index import index_chunks, stored_mapping
from polanon.policy import entity_type_for_token
from sqlmodel import Session

from polgate.config import Settings
from polgate.knowledge import store
from polgate.knowledge.store import KnowledgeMappingStore
from polgate.llm.openai_compat import LLMError, assert_no_raw_values, embed
from polgate.models import KnowledgeChunk, KnowledgeDocument, KnowledgeStatus, utcnow
from polgate.retrieval.select import split_passages, to_blob, unit
from polgate.vault import store as vault

logger = logging.getLogger(__name__)

#: Tokens of a value that collided with another's at the shorter length, shared by every
#: anonymizer of the process. polanon finds a collision again in the store after a restart; the
#: record only saves it the second look.
_COLLISIONS: dict[str, str] = {}


class KnowledgeError(RuntimeError):
    """A document could not be added or published; ``reason`` names why, for the admin page."""

    def __init__(self, reason: str) -> None:
        """Record the reason: ``empty``, ``too_long``, ``masking``, ``refused``, ``endpoint``."""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Ingested:
    """A document masked and stored, waiting for review, with what was masked in it."""

    document: KnowledgeDocument
    entity_counts: dict[str, int] = field(default_factory=dict)


def pseudonyms() -> Pseudonyms:
    """Return the knowledge base's stable tokens, under the hub's key."""
    return Pseudonyms(vault.pseudonym_key(), collisions=_COLLISIONS)


def index_anonymizer(base: Anonymizer) -> Anonymizer:
    """Return the hub's anonymizer as the knowledge base masks with it.

    The same guards, types and allowlist, so a document and a question are masked by the policy
    the chat follows, with stable tokens instead of a conversation's numbering and without the
    judge, which polanon does not run for an index.
    """
    return Anonymizer(
        base.guards,
        enabled=base.enabled,
        allowlist=base.allowlist,
        pseudonyms=pseudonyms(),
    )


def ingest(
    session: Session,
    anonymizer: Anonymizer,
    settings: Settings,
    *,
    user_id: int,
    filename: str,
    text: str,
    mime_type: str,
    size_bytes: int,
) -> Ingested:
    """Mask an extracted document into passages and store it for review; CPU-bound, so threaded.

    The file name is masked with the passages, as the first of them, and becomes the title.

    Raises:
        KnowledgeError: when the document holds no text, has more passages than a file may, could
            not be masked, or still holds an original value after masking. Nothing is kept then.
    """
    passages = split_passages(text, settings.retrieval_chunk_chars)
    if not passages:
        raise KnowledgeError("empty")
    if len(passages) > settings.retrieval_max_chunks_per_file:
        raise KnowledgeError("too_long")
    with store.WRITE_LOCK:
        return _ingest(
            session,
            index_anonymizer(anonymizer),
            [filename, *passages],
            KnowledgeDocument(
                user_id=user_id,
                mime_type=mime_type,
                size_bytes=size_bytes,
                status=KnowledgeStatus.INDEXING,
            ),
            filename,
        )


def _ingest(
    session: Session,
    anonymizer: Anonymizer,
    chunks: list[str],
    document: KnowledgeDocument,
    filename: str,
) -> Ingested:
    """Mask the chunks for a document row stored as indexing, and store them for review."""
    session.add(document)
    session.commit()
    session.refresh(document)
    document_id = _require_id(document)
    mapping_store = KnowledgeMappingStore(session, document_id)
    try:
        masked = index_chunks(anonymizer, chunks, mapping_store)
        assert_no_raw_values([{"content": t} for t in masked.texts], masked.mapping)
    except (MaskingUnavailableError, PseudonymCollisionError, LLMError) as exc:
        # A failed write may have left the session waiting for a rollback, which every later
        # statement would raise instead of running.
        session.rollback()
        store.withdraw(session, document)
        logger.warning("knowledge document not added: %s", type(exc).__name__)
        raise KnowledgeError("refused" if isinstance(exc, LLMError) else "masking") from exc
    title, *bodies = masked.texts
    store.add_chunks(
        session,
        [
            KnowledgeChunk(document_id=document_id, position=position, text_sent=body)
            for position, body in enumerate(bodies)
        ],
    )
    document.title = title
    document.title_enc = vault.encrypt(filename)
    document.passages = len(bodies)
    document.status = KnowledgeStatus.PENDING_REVIEW
    session.add(document)
    session.commit()
    session.refresh(document)
    counts = Counter(entity_type_for_token(token) for token in masked.mapping)
    return Ingested(document, dict(counts))


async def publish(
    session: Session,
    document: KnowledgeDocument,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Embed the passages that have no vector for the configured model, and publish the document.

    Run again for a published document after ``EMBEDDING_MODEL`` changed, it embeds the passages
    for the new model, so the document is searched again.

    Returns:
        How many passages were embedded.

    Raises:
        KnowledgeError: ``no_model`` without an embedding model, ``indexing`` while the
            document is still being masked, ``refused`` when a passage
            holds a value the mapping store has, ``endpoint`` when the endpoint fails. The passages
            embedded before stay, and the document keeps the status it had.
    """
    model = settings.embedding_model
    if not model:
        raise KnowledgeError("no_model")
    if document.status is KnowledgeStatus.INDEXING:
        raise KnowledgeError("indexing")
    document_id = _require_id(document)
    done = {row.position for row in store.vectors(session, document_id, model)}
    passages = store.masked_passages(session, document_id)
    missing = [row for row in passages if row.position not in done]
    mapping_store = KnowledgeMappingStore(session)
    base_url, api_key = settings.embedding_endpoint()
    for batch in batched(missing, max(settings.embedding_batch_size, 1)):
        texts = [row.text_sent for row in batch]
        try:
            assert_no_raw_values(
                [{"content": t} for t in texts], stored_mapping(mapping_store, *texts)
            )
            embedded = await embed(
                texts, model=model, base_url=base_url, api_key=api_key, client=client
            )
        except LLMError as exc:
            reason = "refused" if exc.token is not None else "endpoint"
            logger.warning("knowledge document %d not embedded: %s", document_id, reason)
            raise KnowledgeError(reason) from exc
        store.add_chunks(
            session,
            [
                KnowledgeChunk(
                    document_id=document_id,
                    position=row.position,
                    text_sent=row.text_sent,
                    vector=to_blob(unit(vector)),
                    embedding_model=model,
                )
                for row, vector in zip(batch, embedded, strict=True)
            ],
        )
        session.commit()
    document.status = KnowledgeStatus.PUBLISHED
    document.published_at = document.published_at or utcnow()
    session.add(document)
    session.commit()
    return len(missing)


def _require_id(document: KnowledgeDocument) -> int:
    if document.id is None:  # pragma: no cover - rows come from the database
        raise ValueError("document must be persisted")
    return document.id
