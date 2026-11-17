"""Find, export and erase what the hub holds about one person, for a data subject's request.

The hub keeps a person in two shapes. An original value is encrypted: a vault row, a message as
typed, a file's extracted text and name, a queued message, a value of the knowledge base. A token
stands for it in masked text: what the provider received, a file's masked text and its passages,
the knowledge base's passages. A request names the person by a value, a PESEL, an e-mail address
or a name, and :func:`find` looks for it in both shapes.

A value is the person's when it is the same value however it is spelled: polanon's stable token of
it does not depend on the case a name stands in or the groups an identifier is written in, so
"Jana Kowalskiego" in one conversation and "Jan Kowalski" in the request are one person. Every
token that stood for such a value, in its conversation or in the knowledge base, is the person's
too, and so is every message or file whose original text holds one of the spellings found.

:func:`export` writes what was found with only this person restored: other people stay tokens, so
an answer to one person's request does not hand over everybody else in the same conversation.
:func:`erase` replaces each spelling and each token with :data:`ERASED` and deletes the vault rows
and knowledge base values, and the bytes of an upload that held the person, since a PDF cannot be
edited in place. Nothing it touches can be restored afterwards.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from polanon import PseudonymCollisionError, Pseudonyms
from sqlmodel import Session, col, delete, select

from polgate.knowledge.index import pseudonyms
from polgate.models import (
    AttachmentChunk,
    Conversation,
    FileAsset,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeToken,
    KnowledgeTokenUse,
    Message,
    PendingTurn,
    User,
    VaultEntry,
    utcnow,
)
from polgate.tokens import WRITTEN_TOKEN_RE, base_token
from polgate.vault import store as vault

#: What stands where a person's value or token was, once erased.
ERASED = "[dane usunięte / erased data]"
#: A request shorter than this would match too much to be anybody's.
MIN_VALUE_CHARS = 3
#: A spelling shorter than this counts only as a word of its own: the surname "Lis" is a person,
#: and "Lista" is not, as the last check before sending reads it too.
_WHOLE_WORD_BELOW = 4


@dataclass
class SubjectReport:
    """Everything found about one person, in the rows that hold it."""

    #: conversation id -> token -> the value as that conversation wrote it.
    tokens: dict[int, dict[str, str]] = field(default_factory=dict)
    vault_entries: list[VaultEntry] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    files: list[FileAsset] = field(default_factory=list)
    pending: list[PendingTurn] = field(default_factory=list)
    #: stable token of the knowledge base -> its value.
    knowledge_tokens: dict[str, str] = field(default_factory=dict)
    knowledge_chunks: list[KnowledgeChunk] = field(default_factory=list)
    knowledge_documents: list[KnowledgeDocument] = field(default_factory=list)
    #: Every spelling of the value found, the request's own included.
    forms: set[str] = field(default_factory=set)

    @property
    def found(self) -> bool:
        """Whether anything at all holds the person."""
        return bool(
            self.vault_entries
            or self.messages
            or self.files
            or self.pending
            or self.knowledge_tokens
            or self.knowledge_chunks
        )

    def counts(self) -> dict[str, int]:
        """How many rows of each kind hold the person, for the page and the audit trail."""
        return {
            "conversations": len(
                {m.conversation_id for m in self.messages}
                | {f.conversation_id for f in self.files}
                | set(self.tokens)
            ),
            "messages": len(self.messages),
            "files": len(self.files),
            "vault_entries": len(self.vault_entries),
            "knowledge_documents": len(self.knowledge_documents),
            "knowledge_passages": len(self.knowledge_chunks),
        }


def find(session: Session, value: str) -> SubjectReport:
    """Return every row that holds the person a request names by ``value``.

    Raises:
        ValueError: for a value shorter than :data:`MIN_VALUE_CHARS`, which would match anybody.
    """
    value = value.strip()
    if len(value) < MIN_VALUE_CHARS:
        raise ValueError("the value is too short to name one person")
    stable = pseudonyms()
    report = SubjectReport(forms={value})
    entries = [(e, vault.decrypt(e.value_enc)) for e in session.exec(select(VaultEntry)).all()]
    scopes: dict[int, list[tuple[str, str, str]]] = {}
    for entry, held in entries:
        scopes.setdefault(entry.conversation_id, []).append((entry.token, entry.entity_type, held))
    by_token = {(e.conversation_id, e.token): e for e, _ in entries}
    for conversation_id, scope in scopes.items():
        for token, held in _matches(stable, value, scope).items():
            report.vault_entries.append(by_token[(conversation_id, token)])
            report.tokens.setdefault(conversation_id, {})[token] = held
            report.forms.add(held)
    knowledge = [
        (row.token, row.entity_type, vault.decrypt(row.value_enc))
        for row in session.exec(select(KnowledgeToken)).all()
    ]
    report.knowledge_tokens = _matches(stable, value, knowledge)
    report.forms.update(report.knowledge_tokens.values())
    _find_messages(session, report)
    _find_files(session, report)
    _find_pending(session, report)
    _find_knowledge(session, report)
    return report


def _matches(stable: Pseudonyms, request: str, scope: list[tuple[str, str, str]]) -> dict[str, str]:
    """Return the tokens of one scope, a conversation or the knowledge base, that are the person.

    A token is when its value is the request however spelled. So is a surname a token holds on
    its own, "Kowalskiego" beside "Jan Kowalski", as polanon masks the surname of a person it found
    by the full name, but only while no other person of the scope bears it: it may be Piotr
    Kowalski's as well, and erasing it would erase him.
    """
    found = {
        token: held
        for token, entity_type, held in scope
        if _same(stable, request, held, entity_type)
    }
    people = [(token, held) for token, entity_type, held in scope if entity_type == "PERSON"]
    surnames = {held.split()[-1] for token, held in people if token in found and " " in held}
    others = [held.split()[-1] for token, held in people if token not in found and " " in held]
    for token, held in people:
        if token in found or " " in held:
            continue
        if any(_same(stable, s, held, "PERSON") for s in surnames) and not any(
            _same(stable, s, held, "PERSON") for s in others
        ):
            found[token] = held
    return found


def _same(stable: Pseudonyms, request: str, held: str, entity_type: str) -> bool:
    """Whether a stored value is the requested one, spelled the same way or another."""
    if held.casefold() == request.casefold():
        return True
    try:
        return stable.token(held, entity_type) == stable.token(request, entity_type)
    except PseudonymCollisionError:
        return False


def _pattern(form: str) -> re.Pattern[str]:
    """Match a spelling anywhere, or as a word of its own when it is short."""
    escaped = re.escape(form)
    if len(form) < _WHOLE_WORD_BELOW:
        escaped = rf"(?<!\w){escaped}(?!\w)"
    return re.compile(escaped, re.IGNORECASE)


def _holds(text: str, forms: Iterable[str]) -> bool:
    return any(_pattern(form).search(text) for form in forms)


def _tokens_of(text: str) -> set[str]:
    return {base_token(match) for match in WRITTEN_TOKEN_RE.finditer(text)}


def _find_messages(session: Session, report: SubjectReport) -> None:
    for message in session.exec(select(Message).order_by(col(Message.id))).all():
        tokens = report.tokens.get(message.conversation_id, {})
        if _tokens_of(message.content_sent) & tokens.keys() or _holds(
            vault.decrypt(message.content_original_enc), report.forms
        ):
            report.messages.append(message)


def _find_files(session: Session, report: SubjectReport) -> None:
    for asset in session.exec(select(FileAsset).order_by(col(FileAsset.id))).all():
        tokens = report.tokens.get(asset.conversation_id, {})
        originals = [
            vault.decrypt(text) for text in (asset.text_original_enc, asset.filename_enc) if text
        ]
        if _tokens_of(asset.text_sent) & tokens.keys() or any(
            _holds(text, report.forms) for text in originals
        ):
            report.files.append(asset)


def _find_pending(session: Session, report: SubjectReport) -> None:
    for turn in session.exec(select(PendingTurn)).all():
        if _holds(vault.decrypt(turn.text_enc), report.forms):
            report.pending.append(turn)


def _find_knowledge(session: Session, report: SubjectReport) -> None:
    if not report.knowledge_tokens:
        return
    documents: dict[int, KnowledgeDocument] = {}
    for chunk in session.exec(select(KnowledgeChunk).order_by(col(KnowledgeChunk.id))).all():
        if _tokens_of(chunk.text_sent) & report.knowledge_tokens.keys():
            report.knowledge_chunks.append(chunk)
            document = session.get(KnowledgeDocument, chunk.document_id)
            if document is not None and document.id is not None:
                documents[document.id] = document
    for document in session.exec(select(KnowledgeDocument)).all():
        if document.id is not None and _tokens_of(document.title) & report.knowledge_tokens.keys():
            documents[document.id] = document
    report.knowledge_documents = list(documents.values())


def export(session: Session, report: SubjectReport) -> str:
    """Return what the hub holds about the person as JSON, with only this person restored.

    Each message and file is given as the provider received it, with the person's tokens written
    out and everybody else's left as tokens. A message found only by its original text, whose
    tokens expired from the vault, is given as typed, and marked so: it may name others.
    """
    owners = {user.id: user.username for user in session.exec(select(User)).all()}
    conversations: dict[int, dict[str, object]] = {}

    def conversation(conversation_id: int) -> dict[str, object]:
        if conversation_id not in conversations:
            row = session.get(Conversation, conversation_id)
            conversations[conversation_id] = {
                "id": conversation_id,
                "owner": owners.get(row.user_id, "") if row is not None else "",
                "messages": [],
                "files": [],
            }
        return conversations[conversation_id]

    for message in report.messages:
        restored, by_token = _restored(message.content_sent, report.tokens, message.conversation_id)
        item: dict[str, object] = {
            "id": message.id,
            "role": message.role.value,
            "created_at": message.created_at.isoformat(),
            "text": restored if by_token else vault.decrypt(message.content_original_enc),
            "as_typed": not by_token,
        }
        messages = conversation(message.conversation_id)["messages"]
        if isinstance(messages, list):
            messages.append(item)
    for asset in report.files:
        restored, by_token = _restored(asset.text_sent, report.tokens, asset.conversation_id)
        file_item: dict[str, object] = {
            "id": asset.id,
            "created_at": asset.created_at.isoformat(),
            "text": restored if by_token else vault.decrypt(asset.text_original_enc),
            "as_typed": not by_token,
        }
        files = conversation(asset.conversation_id)["files"]
        if isinstance(files, list):
            files.append(file_item)
    passages = [
        {
            "document_id": chunk.document_id,
            "position": chunk.position,
            "text": _replace_tokens(chunk.text_sent, report.knowledge_tokens),
        }
        for chunk in report.knowledge_chunks
        if not chunk.embedding_model
    ]
    return json.dumps(
        {
            "generated_at": utcnow().isoformat(),
            "counts": report.counts(),
            "conversations": list(conversations.values()),
            "knowledge_base": passages,
        },
        ensure_ascii=False,
        indent=2,
    )


def _restored(
    text: str, tokens: Mapping[int, Mapping[str, str]], conversation_id: int
) -> tuple[str, bool]:
    """Write the person's tokens of one conversation out in a masked text, and say if any was."""
    own = tokens.get(conversation_id, {})
    if not _tokens_of(text) & own.keys():
        return text, False
    return _replace_tokens(text, own), True


def _replace_tokens(text: str, replacements: Mapping[str, str]) -> str:
    return WRITTEN_TOKEN_RE.sub(
        lambda match: replacements.get(base_token(match), match.group(0)), text
    )


def erase(session: Session, report: SubjectReport) -> dict[str, int]:
    """Erase the person from every row the report found, and commit.

    Returns:
        The counts of :meth:`SubjectReport.counts`, plus the uploads deleted from disk.
    """
    counts = report.counts()
    erased_tokens = {
        conversation_id: dict.fromkeys(tokens, ERASED)
        for conversation_id, tokens in report.tokens.items()
    }
    for message in report.messages:
        own = erased_tokens.get(message.conversation_id, {})
        message.content_sent = _replace_tokens(message.content_sent, own)
        message.content_original_enc = _erased(message.content_original_enc, report.forms)
        message.token_fingerprints = _without(message.token_fingerprints, own)
        session.add(message)
    counts["uploads_deleted"] = _erase_files(session, report, erased_tokens)
    for turn in report.pending:
        session.delete(turn)
    for entry in report.vault_entries:
        session.delete(entry)
    _erase_knowledge(session, report)
    session.commit()
    return counts


def _erase_files(
    session: Session, report: SubjectReport, erased_tokens: Mapping[int, Mapping[str, str]]
) -> int:
    deleted = 0
    for asset in report.files:
        own = erased_tokens.get(asset.conversation_id, {})
        asset.text_sent = _replace_tokens(asset.text_sent, own)
        asset.filename = _replace_tokens(asset.filename, own)
        asset.token_fingerprints = _without(asset.token_fingerprints, own)
        for column in ("text_original_enc", "filename_enc"):
            encrypted = getattr(asset, column)
            if encrypted:
                setattr(asset, column, _erased(encrypted, report.forms))
        if asset.storage_path:
            # The bytes as uploaded hold the person and cannot be edited in place.
            Path(asset.storage_path).unlink(missing_ok=True)
            asset.storage_path = ""
            deleted += 1
        session.add(asset)
        for chunk in session.exec(
            select(AttachmentChunk).where(col(AttachmentChunk.file_id) == asset.id)
        ).all():
            chunk.text_sent = _replace_tokens(chunk.text_sent, own)
            session.add(chunk)
    return deleted


def _erase_knowledge(session: Session, report: SubjectReport) -> None:
    if not report.knowledge_tokens:
        return
    erased = dict.fromkeys(report.knowledge_tokens, ERASED)
    for chunk in report.knowledge_chunks:
        chunk.text_sent = _replace_tokens(chunk.text_sent, erased)
        session.add(chunk)
    for document in report.knowledge_documents:
        document.title = _replace_tokens(document.title, erased)
        if document.title_enc:
            document.title_enc = _erased(document.title_enc, report.forms)
        session.add(document)
    tokens = list(report.knowledge_tokens)
    session.exec(delete(KnowledgeTokenUse).where(col(KnowledgeTokenUse.token).in_(tokens)))
    session.exec(delete(KnowledgeToken).where(col(KnowledgeToken.token).in_(tokens)))


def _erased(encrypted: str, forms: Iterable[str]) -> str:
    """Decrypt, replace every spelling of the person, longest first, and encrypt again."""
    text = vault.decrypt(encrypted)
    for form in sorted(forms, key=len, reverse=True):
        text = _pattern(form).sub(ERASED, text)
    return vault.encrypt(text)


def _without(recorded: str, tokens: Mapping[str, str]) -> str:
    """Drop the erased tokens from a text's recorded fingerprints."""
    if not recorded:
        return recorded
    kept = {t: v for t, v in json.loads(recorded).items() if t not in tokens}
    return json.dumps(kept)
