"""Search the long attachments of a conversation for the passages that answer the current turn.

A file that fits the attachment budget is sent whole, as before. A longer one used to be cut to its
first characters, so whatever came after the cut never reached the model; with an embedding model
configured, its masked text is indexed in passages the first time a turn needs it, and each turn
sends the passages that match the question instead.

Only masked text is involved, and the same mapping as the turn's: the file was masked with the
conversation's vault mapping when it was uploaded, the question is masked with it now, so a token
means the same entity on both sides. Everything sent to the embeddings endpoint, passages and
question alike, is checked with :func:`~polgate.llm.openai_compat.assert_no_raw_values` first.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice

import httpx
from polanon import mask_known_values
from sqlmodel import Session

from polgate.config import Settings
from polgate.llm.openai_compat import LLMError, assert_no_raw_values, embed
from polgate.models import AttachmentChunk, FileAsset, Policy
from polgate.retrieval import store
from polgate.retrieval.select import (
    Passage,
    Vector,
    choose,
    from_blob,
    has_stale_token,
    rank,
    render,
    split_passages,
    to_blob,
    unit,
)

logger = logging.getLogger(__name__)


class SearchUnavailableError(RuntimeError):
    """The passages could not be searched; the caller falls back to cutting the file."""

    def __init__(self, reason: str) -> None:
        """Record why, as a word the audit trail can hold: ``endpoint`` or ``refused``."""
        super().__init__(reason)
        self.reason = reason


@dataclass
class SearchResult:
    """The text to send for each searched file, and counts for the audit trail."""

    texts: dict[int, str] = field(default_factory=dict)
    passages: int = 0
    stale: int = 0
    capped: int = 0


@dataclass(frozen=True)
class _Document:
    """A long attachment, cut into the passages the hub indexes."""

    asset: FileAsset
    heading: str
    passages: list[str]
    capped_at: int | None

    @property
    def file_id(self) -> int:
        """The file's primary key."""
        if self.asset.id is None:  # pragma: no cover - rows come from the database
            raise ValueError("file must be persisted")
        return self.asset.id


class AttachmentSearch:
    """Index and search the long attachments of one turn."""

    def __init__(
        self,
        settings: Settings,
        policy: Policy,
        mapping: Mapping[str, str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Bind the search to one turn: its settings, policy, mapping and HTTP client."""
        self.settings = settings
        self.policy = policy
        self.mapping = mapping
        self.client = client

    @property
    def enabled(self) -> bool:
        """Whether an embedding model is configured and the policy allows the search."""
        return bool(self.settings.embedding_model) and self.policy.attachment_retrieval

    def applies_to(self, asset: FileAsset) -> bool:
        """Whether a file is long enough to be searched rather than sent whole."""
        return self.enabled and len(asset.text_sent) > self.settings.attachment_budget_chars

    def documents(self, assets: Sequence[FileAsset]) -> list[_Document]:
        """Cut each long file into the passages that are indexed."""
        documents = []
        limit = self.settings.retrieval_max_chunks_per_file
        for asset in assets:
            heading, _, body = asset.text_sent.partition("\n")
            passages = split_passages(body, self.settings.retrieval_chunk_chars)
            capped = len(passages) > limit
            documents.append(_Document(asset, heading, passages[:limit], limit if capped else None))
        return documents

    def needs_indexing(self, session: Session, documents: Sequence[_Document]) -> bool:
        """Whether some passage of these files has no vector for the configured model yet."""
        return any(len(self._current(session, doc)) < len(doc.passages) for doc in documents)

    async def search(
        self, session: Session, documents: Sequence[_Document], question: str
    ) -> SearchResult:
        """Index what is missing, then choose each file's passages for the question.

        Raises:
            SearchUnavailableError: when the embeddings endpoint fails, or a text bound for it
                still holds an original value.
        """
        result = SearchResult()
        for document in documents:
            await self._index(session, document)
        question_vector = (await self._embed([question]))[0]
        for document in documents:
            result.texts[document.file_id] = self._choose(
                session, document, question, question_vector, result
            )
            result.capped += document.capped_at is not None
        return result

    async def _index(self, session: Session, document: _Document) -> None:
        """Embed and store the passages of one file that have no vector yet, in batches."""
        model = self.settings.embedding_model
        rows = store.load(session, document.file_id, model)
        current = self._current(session, document, rows)
        kept = {row.id for row in current}
        outdated = [row.id for row in rows if row.id not in kept and row.id is not None]
        if outdated:
            store.delete_rows(session, outdated)
            session.commit()
        known = {row.position for row in current}
        missing = [(i, text) for i, text in enumerate(document.passages) if i not in known]
        for batch in _batched(missing, self.settings.embedding_batch_size):
            vectors = await self._embed([text for _, text in batch])
            store.add(
                session,
                [
                    AttachmentChunk(
                        file_id=document.file_id,
                        conversation_id=document.asset.conversation_id,
                        position=position,
                        text_sent=text,
                        vector=to_blob(vector),
                        embedding_model=model,
                    )
                    for (position, text), vector in zip(batch, vectors, strict=True)
                ],
            )

    def _choose(
        self,
        session: Session,
        document: _Document,
        question: str,
        question_vector: Vector,
        result: SearchResult,
    ) -> str:
        """Rank one file's current passages and write the best ones for the model."""
        rows = self._current(session, document)
        candidates = [
            Passage(row.position, row.text_sent, from_blob(row.vector))
            for row in rows
            if not has_stale_token(row.text_sent, self.mapping)
        ]
        result.stale += len(rows) - len(candidates)
        order = rank(question, question_vector, candidates)
        chosen = choose(
            candidates,
            order,
            self.policy.retrieval_top_k,
            self.settings.attachment_budget_chars,
        )
        result.passages += len(chosen)
        return render(document.heading, chosen, capped_at=document.capped_at)

    def _current(
        self,
        session: Session,
        document: _Document,
        rows: Sequence[AttachmentChunk] | None = None,
    ) -> list[AttachmentChunk]:
        """Return the stored passages that still match the file as it is cut now.

        A passage stored before the passage length was changed holds other text at its position;
        it is replaced rather than searched.
        """
        if rows is None:
            rows = store.load(session, document.file_id, self.settings.embedding_model)
        passages = document.passages
        return [
            row
            for row in rows
            if row.position < len(passages) and row.text_sent == passages[row.position]
        ]

    async def _embed(self, texts: list[str]) -> list[Vector]:
        """Mask what the turn knows, check, and embed; any failure is the search's failure."""
        inputs = [mask_known_values(text, dict(self.mapping)) for text in texts]
        try:
            assert_no_raw_values([{"content": text} for text in inputs], self.mapping)
        except LLMError as exc:
            raise SearchUnavailableError("refused") from exc
        base_url, api_key = self.settings.embedding_endpoint()
        try:
            vectors = await embed(
                inputs,
                model=self.settings.embedding_model,
                base_url=base_url,
                api_key=api_key,
                client=self.client,
            )
        except LLMError as exc:
            raise SearchUnavailableError("endpoint") from exc
        return [unit(vector) for vector in vectors]


def _batched[T](items: Sequence[T], size: int) -> Iterator[list[T]]:
    """Yield ``items`` in lists of at most ``size``."""
    iterator = iter(items)
    while batch := list(islice(iterator, max(size, 1))):
        yield batch
