"""The knowledge base's tables: its documents, their passages and what its tokens stand for.

`KnowledgeMappingStore` is polanon's :class:`~polanon.MappingStore` over `KnowledgeToken`, with
the values encrypted like the vault's. Bound to a document while it is indexed, it records every
token the document stored or met already, so withdrawing the document deletes the tokens no other
document uses, its share of the mapping store, and nothing else.

A token and its use are written in one transaction, and a withdrawal deletes a token only where no
use of it is left at the moment it deletes, so an upload and a withdrawal that name the same
person cannot leave a passage with a token nobody can restore. :data:`WRITE_LOCK` keeps them from
interleaving at all within the hub's one process.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from itertools import batched

from polanon import StoredValue
from polanon.policy import entity_type_for_token
from sqlalchemy import exists
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from polgate.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeStatus,
    KnowledgeToken,
    KnowledgeTokenUse,
)
from polgate.vault import store as vault

logger = logging.getLogger(__name__)

#: Inserts tried when another writer keeps storing some of the same tokens first.
_WRITE_ATTEMPTS = 3
#: Tokens per query. polanon asks about every run of up to four words of a document that could be
#: a name, some nine tokens a word, and one query for them all ran past SQLite's limit of 32 766
#: variables at about fifty thousand characters.
_BATCH = 500

#: Held by an upload while it is masked and by a withdrawal, so neither reads what the other is
#: halfway through writing.
WRITE_LOCK = threading.Lock()


def _batches(tokens: Collection[str]) -> Iterator[list[str]]:
    for batch in batched(sorted(tokens), _BATCH):
        yield list(batch)


class KnowledgeMappingStore:
    """polanon's mapping store over the `KnowledgeToken` table.

    Args:
        session: open database session; :meth:`add` commits.
        document_id: the document being indexed, whose uses of tokens are recorded; None for a
            store that only reads, as a turn's search does.
    """

    def __init__(self, session: Session, document_id: int | None = None) -> None:
        """Bind the store to a session and, while indexing, to one document."""
        self.session = session
        self.document_id = document_id

    def get(self, tokens: Collection[str]) -> dict[str, StoredValue]:
        """Return what the table holds for each of `tokens` it has."""
        found: dict[str, StoredValue] = {}
        for batch in _batches(tokens):
            rows = self.session.exec(
                select(KnowledgeToken).where(col(KnowledgeToken.token).in_(batch))
            ).all()
            found.update(
                {
                    row.token: StoredValue(vault.decrypt(row.value_enc), row.fingerprint)
                    for row in rows
                }
            )
        return found

    def add(self, entries: Mapping[str, StoredValue]) -> dict[str, StoredValue]:
        """Store each entry whose token is not held yet; return what is held for the others.

        The tokens and the bound document's uses of them go in one commit. The unique key on the
        token makes the insert atomic per token: a writer that lost the race for one rolls back,
        reads what the other stored and tries again; one whose held token was deleted between its
        read and its commit finds it missing afterwards and stores it again.
        """
        for _ in range(_WRITE_ATTEMPTS):
            held = self.get(list(entries))
            for token, entry in entries.items():
                if token not in held:
                    self.session.add(
                        KnowledgeToken(
                            token=token,
                            entity_type=entity_type_for_token(token),
                            value_enc=vault.encrypt(entry.value),
                            fingerprint=entry.fingerprint,
                        )
                    )
            self._add_uses(entries)
            try:
                self.session.commit()
            except IntegrityError:
                self.session.rollback()
                logger.info("knowledge tokens stored by another writer; %d again", len(entries))
                continue
            if len(self.get(list(entries))) == len(entries):
                return held
            logger.info("knowledge tokens deleted while being stored; storing them again")
        raise RuntimeError("the knowledge token table kept changing under this writer")

    def _add_uses(self, tokens: Collection[str]) -> None:
        """Add the uses of these tokens by the bound document that are not recorded yet."""
        if self.document_id is None or not tokens:
            return
        recorded: set[str] = set()
        for batch in _batches(tokens):
            recorded.update(
                self.session.exec(
                    select(KnowledgeTokenUse.token)
                    .where(col(KnowledgeTokenUse.document_id) == self.document_id)
                    .where(col(KnowledgeTokenUse.token).in_(batch))
                ).all()
            )
        for token in sorted(set(tokens) - recorded):
            self.session.add(KnowledgeTokenUse(document_id=self.document_id, token=token))


def documents(session: Session) -> list[KnowledgeDocument]:
    """Return every document, newest first."""
    return list(
        session.exec(select(KnowledgeDocument).order_by(col(KnowledgeDocument.id).desc())).all()
    )


def masked_passages(session: Session, document_id: int) -> list[KnowledgeChunk]:
    """Return a document's passages as they were masked, in order, without vectors."""
    return _chunks(session, document_id, "")


def vectors(session: Session, document_id: int, embedding_model: str) -> list[KnowledgeChunk]:
    """Return a document's passages embedded with one model, in order."""
    return _chunks(session, document_id, embedding_model)


def _chunks(session: Session, document_id: int, embedding_model: str) -> list[KnowledgeChunk]:
    return list(
        session.exec(
            select(KnowledgeChunk)
            .where(col(KnowledgeChunk.document_id) == document_id)
            .where(col(KnowledgeChunk.embedding_model) == embedding_model)
            .order_by(col(KnowledgeChunk.position))
        ).all()
    )


def searchable(
    session: Session, embedding_model: str, limit: int | None = None
) -> list[tuple[KnowledgeDocument, KnowledgeChunk]]:
    """Return the passages of published documents embedded with this model, by document.

    All of them are read for every turn: the vectors are ranked in memory, which for the few
    thousand passages of a company's regulations takes milliseconds and needs no vector database.
    """
    statement = (
        select(KnowledgeDocument, KnowledgeChunk)
        .join(KnowledgeChunk, col(KnowledgeChunk.document_id) == col(KnowledgeDocument.id))
        .where(col(KnowledgeDocument.status) == KnowledgeStatus.PUBLISHED)
        .where(col(KnowledgeChunk.embedding_model) == embedding_model)
        .order_by(col(KnowledgeDocument.id), col(KnowledgeChunk.position))
    )
    rows = session.exec(statement if limit is None else statement.limit(limit)).all()
    return [(document, chunk) for document, chunk in rows]


def add_chunks(session: Session, rows: Sequence[KnowledgeChunk]) -> None:
    """Insert passages; the caller commits."""
    session.add_all(rows)


def withdraw(session: Session, document: KnowledgeDocument) -> int:
    """Delete a document, its passages and the tokens no other document uses; commit.

    A token is deleted only where no use of it is left when the deletion runs, so a document
    added in the meantime that recorded a use keeps its token.

    Returns:
        How many tokens left the mapping store with it.
    """
    document_id = document.id
    session.exec(delete(KnowledgeChunk).where(col(KnowledgeChunk.document_id) == document_id))
    used = set(
        session.exec(
            select(KnowledgeTokenUse.token).where(col(KnowledgeTokenUse.document_id) == document_id)
        ).all()
    )
    session.exec(delete(KnowledgeTokenUse).where(col(KnowledgeTokenUse.document_id) == document_id))
    unused = ~exists().where(col(KnowledgeTokenUse.token) == col(KnowledgeToken.token))
    removed = 0
    for batch in _batches(used):
        orphaned = session.exec(
            select(KnowledgeToken.token).where(col(KnowledgeToken.token).in_(batch)).where(unused)
        ).all()
        removed += len(orphaned)
        session.exec(
            delete(KnowledgeToken).where(col(KnowledgeToken.token).in_(batch)).where(unused)
        )
    session.delete(document)
    session.commit()
    return removed
