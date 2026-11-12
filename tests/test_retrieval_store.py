"""The passages of attachments in the database: one copy per model, removed with their file."""

import pytest

from polgate.auth.passwords import hash_password
from polgate.models import AttachmentChunk, Conversation, FileAsset, FileStatus, User
from polgate.retrieval import store
from polgate.retrieval.select import to_blob, unit


@pytest.fixture
def asset(db_session):
    user = User(username="u", password_hash=hash_password("x"))
    db_session.add(user)
    db_session.commit()
    conversation = Conversation(user_id=user.id)
    db_session.add(conversation)
    db_session.commit()
    item = FileAsset(
        conversation_id=conversation.id,
        user_id=user.id,
        filename="plik.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_path="none",
        text_sent="plik.txt\n\ntekst",
        status=FileStatus.APPROVED,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def _chunk(asset, position, model="bge-m3", text="fragment"):
    return AttachmentChunk(
        file_id=asset.id,
        conversation_id=asset.conversation_id,
        position=position,
        text_sent=text,
        vector=to_blob(unit([1.0, 2.0])),
        embedding_model=model,
    )


def test_passages_come_back_in_document_order_for_their_model_only(db_session, asset):
    assert store.add(db_session, [_chunk(asset, 1), _chunk(asset, 0), _chunk(asset, 0, "other")])
    rows = store.load(db_session, asset.id, "bge-m3")
    assert [row.position for row in rows] == [0, 1]
    assert [row.position for row in store.load(db_session, asset.id, "other")] == [0]


def test_a_passage_another_turn_stored_first_is_not_stored_twice(db_session, asset):
    assert store.add(db_session, [_chunk(asset, 0)])
    assert not store.add(db_session, [_chunk(asset, 0, text="the same passage again")])
    assert [row.text_sent for row in store.load(db_session, asset.id, "bge-m3")] == ["fragment"]


def test_deleting_a_file_s_passages_counts_them(db_session, asset):
    store.add(db_session, [_chunk(asset, 0), _chunk(asset, 1), _chunk(asset, 0, "other")])
    assert store.delete_for_files(db_session, [asset.id]) == 3
    db_session.commit()
    assert store.load(db_session, asset.id, "bge-m3") == []
    assert store.delete_for_files(db_session, []) == 0
