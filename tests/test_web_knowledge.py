"""The knowledge base pages: only for administrators, and nothing raw on the way to the index."""

import pytest
from sqlmodel import Session, select

from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.db import get_engine
from polgate.knowledge import index
from polgate.models import AuditEvent, KnowledgeDocument, KnowledgeStatus, Policy, User

PESEL = "50810112354"  # born in 1850: a valid checksum that belongs to nobody
DOCUMENT = (
    "Regulamin pracy biura\n\n"
    f"Kierownikiem biura jest Jan Kowalski, PESEL {PESEL}.\n\n"
    "Biuro jest otwarte od 8 do 16, a klucze wydaje recepcja."
)


@pytest.fixture
def embeddings(monkeypatch):
    """An embedding model that answers, recording what it was sent."""
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_model", "test-embed")
    sent: list[list[str]] = []

    async def fake_embed(texts, **kwargs):
        sent.append(list(texts))
        return [[1.0, float(len(text)), 0.5] for text in texts]

    monkeypatch.setattr(index, "embed", fake_embed)
    return sent


def _upload(client, name="regulamin Jan Kowalski.txt", body=None):
    payload = DOCUMENT.encode() if body is None else body
    return client.post("/admin/knowledge", files={"file": (name, payload, "text/plain")})


def _document() -> KnowledgeDocument:
    with Session(get_engine()) as session:
        return session.exec(select(KnowledgeDocument)).one()


def _events(name: str) -> list[AuditEvent]:
    with Session(get_engine()) as session:
        return list(session.exec(select(AuditEvent).where(AuditEvent.event == name)).all())


def test_the_knowledge_base_sends_anonymous_visitors_to_sign_in(app_client):
    response = app_client.get("/admin/knowledge")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_knowledge_base_is_closed_to_ordinary_employees(app_client):
    with Session(get_engine()) as session:
        session.add(User(username="pracownik", password_hash=hash_password("dlugie-haslo-x")))
        session.commit()
    app_client.post("/login", data={"username": "pracownik", "password": "dlugie-haslo-x"})

    assert app_client.get("/admin/knowledge").status_code == 403
    assert _upload(app_client).status_code == 403


@pytest.mark.parametrize(
    ("language", "heading", "empty"),
    [("pl", "Baza wiedzy", "Nie ma jeszcze dokumentów."), ("en", "Knowledge base", "No documents")],
)
def test_the_knowledge_base_page_renders_in_both_languages(admin_client, language, heading, empty):
    body = admin_client.get(f"/admin/knowledge?lang={language}").text
    assert heading in body
    assert empty in body


def test_an_uploaded_document_is_kept_masked_for_review(admin_client):
    response = _upload(admin_client)

    assert response.status_code == 303
    document = _document()
    assert response.headers["location"] == f"/admin/knowledge/{document.id}"
    assert document.status is KnowledgeStatus.PENDING_REVIEW
    review = admin_client.get(response.headers["location"]).text
    assert PESEL not in review
    assert "PESEL_" in review
    listing = admin_client.get("/admin/knowledge?lang=en").text
    assert "regulamin Jan Kowalski.txt" in listing
    assert "awaiting review" in listing
    rows = _events("knowledge_uploaded")
    assert rows
    assert PESEL not in rows[-1].detail


def test_a_file_the_hub_cannot_read_is_refused_with_a_reason(admin_client):
    response = _upload(admin_client, name="archiwum.zip", body=b"PK\x03\x04")

    assert response.status_code == 400
    assert "alert error" in response.text
    with Session(get_engine()) as session:
        assert session.exec(select(KnowledgeDocument)).all() == []


def test_an_upload_is_refused_while_the_policy_masks_nothing(admin_client):
    from polgate.policy_loader import get_policy

    with Session(get_engine()) as session:
        policy = get_policy(session)
        policy.enabled_entities = ""
        session.add(policy)
        session.commit()

    response = _upload(admin_client)

    assert response.status_code == 400
    with Session(get_engine()) as session:
        assert session.exec(select(KnowledgeDocument)).all() == []


def test_publishing_without_an_embedding_model_says_what_is_missing(admin_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "embedding_model", "")
    _upload(admin_client)
    document = _document()

    response = admin_client.post(f"/admin/knowledge/{document.id}/publish?lang=en")

    assert response.status_code == 400
    assert "EMBEDDING_MODEL" in response.text
    assert _document().status is KnowledgeStatus.PENDING_REVIEW


def test_a_published_document_was_embedded_from_masked_passages_only(admin_client, embeddings):
    _upload(admin_client)
    document = _document()

    response = admin_client.post(f"/admin/knowledge/{document.id}/publish")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/knowledge?flash=knowledge.published_flash"
    assert _document().status is KnowledgeStatus.PUBLISHED
    sent = " ".join(text for batch in embeddings for text in batch)
    assert sent
    assert PESEL not in sent
    assert "Kowalski" not in sent
    assert _events("knowledge_published")


def test_a_failing_endpoint_keeps_the_document_unpublished(admin_client, monkeypatch):
    from polgate.llm.openai_compat import LLMError

    monkeypatch.setattr(get_settings(), "embedding_model", "test-embed")

    async def down(texts, **kwargs):
        raise LLMError("connection refused")

    monkeypatch.setattr(index, "embed", down)
    _upload(admin_client)
    document = _document()

    response = admin_client.post(f"/admin/knowledge/{document.id}/publish")

    assert response.status_code == 502
    assert _document().status is KnowledgeStatus.PENDING_REVIEW


def test_withdrawing_a_document_deletes_it(admin_client):
    _upload(admin_client)
    document = _document()

    response = admin_client.post(f"/admin/knowledge/{document.id}/withdraw")

    assert response.status_code == 303
    with Session(get_engine()) as session:
        assert session.exec(select(KnowledgeDocument)).all() == []
    assert "tokens_removed=" in _events("knowledge_withdrawn")[-1].detail
    assert admin_client.get(f"/admin/knowledge/{document.id}").status_code == 404


def test_a_new_embedding_model_offers_to_index_the_published_documents_again(
    admin_client, embeddings, monkeypatch
):
    _upload(admin_client)
    admin_client.post(f"/admin/knowledge/{_document().id}/publish")
    monkeypatch.setattr(get_settings(), "embedding_model", "another-embed")

    listing = admin_client.get("/admin/knowledge?lang=en").text
    assert "1 published documents have no vectors" in listing

    response = admin_client.post("/admin/knowledge/reindex")

    assert response.status_code == 303
    assert "have no vectors" not in admin_client.get("/admin/knowledge?lang=en").text
    assert "documents=1" in _events("knowledge_reindexed")[-1].detail


def test_the_policy_switches_the_knowledge_base_off_and_on(admin_client):
    def saved() -> bool:
        with Session(get_engine()) as session:
            return session.exec(select(Policy)).one().knowledge_base

    form = {"entities": ["PESEL"], "attachment_retrieval": "on", "retrieval_top_k": "6"}
    admin_client.post("/admin/policy", data=form)
    assert saved() is False
    admin_client.post("/admin/policy", data={**form, "knowledge_base": "on"})
    assert saved() is True
