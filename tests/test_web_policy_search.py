"""The search in attachments on the admin policy page: the switch, top-k, and what it says."""

import pytest
from sqlmodel import Session, select

from polgate.auth.passwords import hash_password
from polgate.config import get_settings
from polgate.db import get_engine
from polgate.models import AuditEvent, Policy, User

FORM = {"hil_chat": "auto", "hil_files": "review", "entities": ["PESEL", "PERSON"]}


def _policy() -> Policy:
    with Session(get_engine()) as session:
        return session.exec(select(Policy)).one()


def test_the_settings_are_for_an_administrator_only(app_client):
    assert app_client.get("/admin/policy").status_code == 303
    with Session(get_engine()) as session:
        session.add(User(username="pracownik", password_hash=hash_password("dlugie-haslo-x")))
        session.commit()
    app_client.post("/login", data={"username": "pracownik", "password": "dlugie-haslo-x"})
    assert app_client.get("/admin/policy").status_code == 403
    assert app_client.post("/admin/policy", data=FORM).status_code == 403


def test_the_page_says_the_search_waits_for_an_embedding_model(admin_client):
    page = admin_client.get("/admin/policy").text
    assert "Wyszukiwanie w załącznikach" in page
    assert "nie ustawiono EMBEDDING_MODEL" in page
    assert 'name="retrieval_top_k" type="number" min="1" max="20" value="6"' in page

    english = admin_client.get("/admin/policy?lang=en").text
    assert "Search in attachments" in english
    assert "EMBEDDING_MODEL is not set" in english


def test_the_warning_goes_once_a_model_is_set(admin_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "embedding_model", "bge-m3")
    page = admin_client.get("/admin/policy").text
    assert "Wyszukiwanie w załącznikach" in page
    assert "nie ustawiono EMBEDDING_MODEL" not in page


def test_saving_switches_the_search_and_sets_top_k(admin_client):
    admin_client.post("/admin/policy", data={**FORM, "retrieval_top_k": "3"})
    assert (_policy().attachment_retrieval, _policy().retrieval_top_k) == (False, 3)

    admin_client.post(
        "/admin/policy", data={**FORM, "attachment_retrieval": "on", "retrieval_top_k": "4"}
    )
    assert (_policy().attachment_retrieval, _policy().retrieval_top_k) == (True, 4)

    with Session(get_engine()) as session:
        details = [
            row.detail
            for row in session.exec(
                select(AuditEvent).where(AuditEvent.event == "policy_changed")
            ).all()
        ]
    assert "attachment_retrieval" in details[-2]
    assert "retrieval_top_k" in details[-2]


@pytest.mark.parametrize(("sent", "kept"), [("50", 20), ("0", 1), ("-3", 1), ("dużo", 6), ("", 6)])
def test_top_k_stays_between_one_and_twenty(admin_client, sent, kept):
    admin_client.post(
        "/admin/policy", data={**FORM, "attachment_retrieval": "on", "retrieval_top_k": sent}
    )
    assert _policy().retrieval_top_k == kept
