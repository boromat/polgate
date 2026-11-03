"""Chat, upload and admin flows through the HTTP layer."""

from pathlib import Path

import pytest
from sqlmodel import Session, select

from polgate.auth.passwords import hash_password, verify_password
from polgate.db import get_engine
from polgate.models import (
    Conversation,
    FileAsset,
    FileStatus,
    HilMode,
    Message,
    Policy,
    User,
)

#: What the browser's EventSource sends, and the stream insists on.
STREAM = {"Accept": "text/event-stream"}


def _conversation_id() -> int:
    with Session(get_engine()) as session:
        return session.exec(select(Conversation)).first().id


def test_chat_creates_a_conversation_on_first_visit(admin_client):
    response = admin_client.get("/chat")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/chat/")


def test_conversation_page_renders_the_composer(admin_client):
    admin_client.get("/chat")
    page = admin_client.get(f"/chat/{_conversation_id()}")
    assert page.status_code == 200
    assert "dane osobowe zostaną zamaskowane" in page.text


def test_another_users_conversation_is_not_reachable(admin_client):
    admin_client.get("/chat")
    with Session(get_engine()) as session:
        other = User(username="other", password_hash="x")
        session.add(other)
        session.commit()
        session.refresh(other)
        conversation = Conversation(user_id=other.id)
        session.add(conversation)
        session.commit()
        foreign_id = conversation.id
    assert admin_client.get(f"/chat/{foreign_id}").status_code == 404


def test_review_policy_shows_the_mask_before_sending(admin_client):
    with Session(get_engine()) as session:
        policy = session.exec(select(Policy)).first() or Policy()
        policy.hil_chat = HilMode.REVIEW
        session.add(policy)
        session.commit()

    admin_client.get("/chat")
    response = admin_client.post(
        f"/chat/{_conversation_id()}/send", data={"text": "PESEL 50810112354", "extra_terms": ""}
    )
    assert response.status_code == 200
    assert "&lt;PESEL_1&gt;" in response.text
    assert "50810112354" not in response.text.split("<pre>")[1].split("</pre>")[0]


def test_upload_masks_the_document_before_it_can_be_attached(admin_client):
    admin_client.get("/chat")
    conversation_id = _conversation_id()
    response = admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("klient.txt", b"Jan, PESEL 50810112354", "text/plain")},
    )
    assert response.status_code == 200

    with Session(get_engine()) as session:
        asset = session.exec(select(FileAsset)).one()
    assert "50810112354" not in asset.text_sent
    assert "<PESEL_1>" in asset.text_sent
    assert asset.status is FileStatus.PENDING_REVIEW


def test_an_upload_name_is_kept_masked_at_rest_and_shown_whole_to_its_owner(admin_client):
    """A file name is often the most personal part of an upload, so it is stored like the text."""
    from polgate.vault import store as vault

    admin_client.get("/chat")
    response = admin_client.post(
        f"/files/upload?conversation_id={_conversation_id()}",
        files={"file": ("CV Jan Kowalski.txt", b"Doswiadczenie: 5 lat", "text/plain")},
    )

    with Session(get_engine()) as session:
        asset = session.exec(select(FileAsset)).one()
    assert "Kowalski" not in asset.filename
    assert "<OSOBA_1>" in asset.filename
    assert vault.decrypt(asset.filename_enc) == "CV Jan Kowalski.txt"
    assert "CV Jan Kowalski.txt" in response.text


def test_unsupported_upload_is_reported_to_the_user(admin_client):
    admin_client.get("/chat")
    response = admin_client.post(
        f"/files/upload?conversation_id={_conversation_id()}",
        files={"file": ("a.zip", b"PK\x03\x04", "application/zip")},
    )
    assert response.status_code == 400
    assert "Nieobsługiwany" in response.text


def test_admin_can_change_the_policy(admin_client):
    response = admin_client.post(
        "/admin/policy",
        data={
            "hil_chat": "review",
            "hil_files": "auto",
            "judge_fallback": "block",
            "vault_ttl_days": "7",
            "entities": ["PERSON", "PESEL"],
            "denylist": "ACME",
            "allowlist": "Facebook\nCanva",
        },
    )
    assert response.status_code == 303
    with Session(get_engine()) as session:
        policy = session.exec(select(Policy)).one()
    assert policy.hil_chat is HilMode.REVIEW
    assert policy.judge_enabled is False
    assert policy.vault_ttl_days == 7
    assert policy.enabled_entities == "PERSON,PESEL"
    assert policy.allowlist == "Facebook\nCanva"


def test_the_policy_page_offers_both_name_lists(admin_client):
    page = admin_client.get("/admin/policy").text
    assert 'name="denylist"' in page
    assert 'name="allowlist"' in page
    assert "Facebook" in page


def test_a_new_policy_leaves_the_big_platforms_unmasked(db_session):
    """A marketing team's "zrób post na Facebooku" went out as "zrób post na <FIRMA_1>"."""
    from polgate.web.chat import build_anonymizer

    anonymizer = build_anonymizer(db_session)
    assert anonymizer.allowlist is not None
    assert anonymizer.allowlist.allows("Facebooku")
    assert anonymizer.allowlist.allows("Google Ads")
    assert not anonymizer.allowlist.allows("ACME")


def test_admin_can_create_and_disable_an_account(admin_client):
    admin_client.post(
        "/admin/users",
        data={
            "username": "nowy",
            "password": "dostatecznie-dlugie",
            "role": "user",
            "language": "pl",
        },
    )
    with Session(get_engine()) as session:
        created = session.exec(select(User).where(User.username == "nowy")).one()
    assert created.is_active

    admin_client.post(f"/admin/users/{created.id}/toggle")
    with Session(get_engine()) as session:
        assert session.get(User, created.id).is_active is False


def test_admin_cannot_create_an_account_with_a_short_password(admin_client):
    response = admin_client.post(
        "/admin/users",
        data={"username": "krotki", "password": "abc", "role": "user", "language": "pl"},
    )
    assert response.status_code == 400
    with Session(get_engine()) as session:
        assert session.exec(select(User).where(User.username == "krotki")).first() is None


def test_admin_can_reset_another_accounts_password(admin_client):
    admin_client.post(
        "/admin/users",
        data={
            "username": "zapominalski",
            "password": "pierwsze-haslo",
            "role": "user",
            "language": "pl",
        },
    )
    with Session(get_engine()) as session:
        target = session.exec(select(User).where(User.username == "zapominalski")).one()

    response = admin_client.post(
        f"/admin/users/{target.id}/password", data={"password": "zupelnie-inne-haslo"}
    )
    assert response.status_code == 303
    with Session(get_engine()) as session:
        refreshed = session.get(User, target.id)
    assert verify_password("zupelnie-inne-haslo", refreshed.password_hash)


def test_admin_cannot_disable_themselves(admin_client):
    with Session(get_engine()) as session:
        admin = session.exec(select(User).where(User.username == "admin")).one()
    assert admin_client.post(f"/admin/users/{admin.id}/toggle").status_code == 400


def test_audit_page_lists_recorded_events(admin_client):
    assert "login" in admin_client.get("/admin/audit").text


def test_queued_message_is_stored_encrypted_until_it_streams(admin_client):
    """The hand-off between sending and streaming must survive a worker restart, encrypted."""
    from polgate.models import PendingTurn
    from polgate.vault import store as vault

    admin_client.get("/chat")
    conversation_id = _conversation_id()
    admin_client.post(
        f"/chat/{conversation_id}/send",
        data={"text": "PESEL 50810112354", "extra_terms": "", "approved": "1"},
    )
    with Session(get_engine()) as session:
        row = session.exec(select(PendingTurn)).one()
    assert "50810112354" not in row.text_enc
    assert vault.decrypt(row.text_enc) == "PESEL 50810112354"


def test_surrounding_whitespace_is_dropped_before_the_message_is_queued(admin_client):
    """Blank lines left by an Enter pressed in hope of sending used to reach the stored preview."""
    from polgate.models import PendingTurn
    from polgate.vault import store as vault

    admin_client.get("/chat")
    conversation_id = _conversation_id()
    admin_client.post(
        f"/chat/{conversation_id}/send",
        data={"text": "  co teraz?\n\n\n", "extra_terms": "", "approved": "1"},
    )
    with Session(get_engine()) as session:
        row = session.exec(select(PendingTurn)).one()
    assert vault.decrypt(row.text_enc) == "co teraz?"


def test_a_message_with_nothing_masked_says_so(admin_client):
    """The reported leak went unnoticed because a message with no tokens looked like any other."""
    from polgate.models import MessageRole
    from polgate.vault import store as vault

    admin_client.get("/chat")
    conversation_id = _conversation_id()
    with Session(get_engine()) as session:
        for text, sent, count in (
            ("co to jest SEO?", "co to jest SEO?", 0),
            ("zadzwoń do Jana Kowalskiego", "zadzwoń do <OSOBA_1>", 1),
        ):
            session.add(
                Message(
                    conversation_id=conversation_id,
                    role=MessageRole.USER,
                    content_original_enc=vault.encrypt(text),
                    content_sent=sent,
                    entity_count=count,
                )
            )
        session.commit()

    page = admin_client.get(f"/chat/{conversation_id}").text
    assert page.count("nic nie zamaskowano") == 1
    assert "data-local-time" in page
    assert "+00:00" in page


def test_sending_twice_leaves_only_the_latest_queued_message(admin_client):
    from polgate.models import PendingTurn

    admin_client.get("/chat")
    conversation_id = _conversation_id()
    for text in ("pierwsza", "druga"):
        admin_client.post(
            f"/chat/{conversation_id}/send",
            data={"text": text, "extra_terms": "", "approved": "1"},
        )
    with Session(get_engine()) as session:
        rows = session.exec(select(PendingTurn)).all()
    assert len(rows) == 1


def test_streaming_without_a_queued_message_conflicts(admin_client):
    admin_client.get("/chat")
    response = admin_client.get(f"/chat/{_conversation_id()}/stream", headers=STREAM)
    assert response.status_code == 409


def test_approving_a_file_reports_it_on_the_next_page(admin_client):
    """The redirect carries a whitelisted key, so the chat page can say what happened."""
    admin_client.get("/chat")
    conversation_id = _conversation_id()
    admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("notatka.txt", b"Jan Kowalski", "text/plain")},
    )
    with Session(get_engine()) as session:
        file_id = session.exec(select(FileAsset)).one().id

    response = admin_client.post(f"/files/{file_id}/approve")
    assert response.headers["location"].endswith("flash=files.approved_flash")

    page = admin_client.get(f"/chat/{conversation_id}?flash=files.approved_flash")
    assert "Plik dołączony do rozmowy." in page.text


def test_an_unknown_flash_value_is_ignored(admin_client):
    """A crafted link must not be able to put arbitrary text on the page."""
    admin_client.get("/chat")
    page = admin_client.get(f"/chat/{_conversation_id()}?flash=Twoje+konto+wygasło")
    assert "wygasło" not in page.text


def test_chat_page_exposes_a_skip_link_and_a_main_landmark(admin_client):
    admin_client.get("/chat")
    page = admin_client.get(f"/chat/{_conversation_id()}")
    assert 'class="skip-link"' in page.text
    assert 'id="main"' in page.text


def _open_conversation(client) -> int:
    """Create a conversation the way the interface does, and return its id."""
    response = client.get("/chat")
    return int(response.headers["location"].rsplit("/", 1)[1])


def _fake_stream(chunks):
    async def stream_chat(messages, **kwargs):
        _fake_stream.sent = messages
        for chunk in chunks:
            yield chunk

    return stream_chat


def test_streaming_returns_the_preview_then_the_restored_answer(admin_client, monkeypatch):
    """The provider sees tokens; the employee gets the original name back in the answer."""
    from polgate.llm import router

    monkeypatch.setattr(
        router, "stream_chat", _fake_stream(["Dzień dobry <OSO", "BA_1>, faktura gotowa."])
    )
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/chat/{conversation_id}/send",
        data={"text": "Napisz do Jan Kowalski, PESEL 50810112354", "extra_terms": ""},
    )

    body = admin_client.get(f"/chat/{conversation_id}/stream", headers=STREAM).text

    assert "<PESEL_1>" in body
    assert "50810112354" not in body
    assert "Jan Kowalski, faktura gotowa." in body
    assert "event: done" in body
    payload = str(_fake_stream.sent)
    assert "50810112354" not in payload
    assert "Jan Kowalski" not in payload


def test_the_preview_shows_the_original_beside_what_was_sent(admin_client, monkeypatch):
    from polgate.llm import router

    monkeypatch.setattr(router, "stream_chat", _fake_stream(["ok"]))
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/chat/{conversation_id}/send", data={"text": "PESEL 50810112354", "extra_terms": ""}
    )
    admin_client.get(f"/chat/{conversation_id}/stream", headers=STREAM)

    with Session(get_engine()) as session:
        message = session.exec(select(Message).order_by(Message.id)).first()
    body = admin_client.get(f"/chat/{conversation_id}/messages/{message.id}/preview").text
    assert "50810112354" in body
    assert "&lt;PESEL_1&gt;" in body or '<span class="token">' in body


def test_a_conversation_belonging_to_someone_else_is_not_found(admin_client):
    with Session(get_engine()) as session:
        stranger = User(username="obcy", password_hash="x")
        session.add(stranger)
        session.commit()
        session.refresh(stranger)
        other = Conversation(user_id=stranger.id)
        session.add(other)
        session.commit()
        session.refresh(other)
    assert admin_client.get(f"/chat/{other.id}").status_code == 404


def test_extra_terms_from_the_composer_are_masked_too(admin_client, monkeypatch):
    from polgate.llm import router

    monkeypatch.setattr(router, "stream_chat", _fake_stream(["ok"]))
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/chat/{conversation_id}/send",
        data={"text": "Projekt Wichura rusza w maju", "extra_terms": "Wichura"},
    )
    body = admin_client.get(f"/chat/{conversation_id}/stream", headers=STREAM).text
    assert "<FIRMA_1>" in body
    assert "Wichura" not in str(_fake_stream.sent)


def test_rejecting_a_file_deletes_the_stored_bytes(admin_client):
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("notatka.txt", b"Jan Kowalski, PESEL 50810112354", "text/plain")},
    )
    with Session(get_engine()) as session:
        asset = session.exec(select(FileAsset)).one()
    stored = Path(asset.storage_path)
    assert stored.exists()

    response = admin_client.post(f"/files/{asset.id}/reject")

    assert response.headers["location"].endswith("flash=files.rejected_flash")
    assert not stored.exists()
    with Session(get_engine()) as session:
        assert session.get(FileAsset, asset.id).status is FileStatus.REJECTED


def test_an_upload_over_the_size_limit_is_refused(admin_client, monkeypatch):
    from polgate.web import files as files_web

    monkeypatch.setattr(files_web, "MAX_BYTES", 64)
    conversation_id = _open_conversation(admin_client)
    response = admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("duzy.txt", b"x" * 512, "text/plain")},
    )
    assert response.status_code == 413
    with Session(get_engine()) as session:
        assert session.exec(select(FileAsset)).all() == []


def test_someone_elses_file_cannot_be_reviewed_or_approved(admin_client):
    with Session(get_engine()) as session:
        stranger = User(username="obca", password_hash="x")
        session.add(stranger)
        session.commit()
        session.refresh(stranger)
        conversation = Conversation(user_id=stranger.id)
        session.add(conversation)
        session.commit()
        session.refresh(conversation)
        asset = FileAsset(
            conversation_id=conversation.id,
            user_id=stranger.id,
            filename="cudze.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_path="/nie/istnieje",
        )
        session.add(asset)
        session.commit()
        session.refresh(asset)

    assert admin_client.get(f"/files/{asset.id}/review").status_code == 404
    assert admin_client.post(f"/files/{asset.id}/approve").status_code == 404


def test_uploading_into_someone_elses_conversation_is_refused(admin_client):
    with Session(get_engine()) as session:
        stranger = session.exec(select(User).where(User.username == "obca")).first()
        if stranger is None:
            stranger = User(username="obca", password_hash="x")
            session.add(stranger)
            session.commit()
            session.refresh(stranger)
        conversation = Conversation(user_id=stranger.id)
        session.add(conversation)
        session.commit()
        session.refresh(conversation)

    response = admin_client.post(
        f"/files/upload?conversation_id={conversation.id}",
        files={"file": ("notatka.txt", b"tekst", "text/plain")},
    )
    assert response.status_code == 404


def test_the_review_page_shows_the_original_next_to_the_mask(admin_client):
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("cv.txt", b"PESEL 50810112354", "text/plain")},
    )
    with Session(get_engine()) as session:
        asset = session.exec(select(FileAsset).order_by(FileAsset.id.desc())).first()

    body = admin_client.get(f"/files/{asset.id}/review").text
    assert "50810112354" in body
    assert "PESEL_1" in body


def test_the_admin_pages_are_closed_to_ordinary_employees(app_client):
    with Session(get_engine()) as session:
        session.add(User(username="pracownik", password_hash=hash_password("dlugie-haslo-x")))
        session.commit()
    app_client.post("/login", data={"username": "pracownik", "password": "dlugie-haslo-x"})
    assert app_client.get("/admin").status_code == 403
    assert app_client.get("/admin/users").status_code == 403


def test_toggling_or_resetting_an_account_that_does_not_exist_is_a_404(admin_client):
    assert admin_client.post("/admin/users/9999/toggle").status_code == 404
    assert (
        admin_client.post(
            "/admin/users/9999/password", data={"password": "dlugie-haslo"}
        ).status_code
        == 404
    )


def test_the_audit_page_filters_by_event_and_pages(admin_client):
    filtered = admin_client.get("/admin/audit?event=login&page=0")
    assert filtered.status_code == 200
    assert "login" in filtered.text
    assert admin_client.get("/admin/audit?page=-3").status_code == 200


def test_a_duplicate_username_is_refused(admin_client):
    data = {"username": "bliźniak", "password": "dostatecznie-dlugie", "role": "user"}
    assert admin_client.post("/admin/users", data=data).status_code == 303
    assert admin_client.post("/admin/users", data=data).status_code == 409


def test_a_conversation_with_a_file_waiting_for_review_still_renders(admin_client):
    """The list of pending files is rendered from a partial, and it has to get its variable."""
    conversation_id = _open_conversation(admin_client)
    with Session(get_engine()) as session:
        policy = session.exec(select(Policy)).first() or Policy()
        policy.hil_files = HilMode.REVIEW
        session.add(policy)
        session.commit()

    admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("umowa.txt", b"PESEL 50810112354", "text/plain")},
    )

    page = admin_client.get(f"/chat/{conversation_id}")
    assert page.status_code == 200
    assert "umowa.txt" in page.text
    assert "/review" in page.text


def test_a_rejected_file_is_still_listed_on_the_chat_page(admin_client):
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("odrzucony.txt", b"PESEL 50810112354", "text/plain")},
    )
    with Session(get_engine()) as session:
        asset = session.exec(select(FileAsset).order_by(FileAsset.id.desc())).first()
    admin_client.post(f"/files/{asset.id}/reject")

    page = admin_client.get(f"/chat/{conversation_id}")
    assert page.status_code == 200
    assert "odrzucony.txt" in page.text


def test_the_terms_an_employee_asks_to_mask_are_stored_encrypted(admin_client):
    """Those words are the ones the employee singled out as sensitive; storing them plainly is worse
    than storing the message."""
    from polgate.models import PendingTurn
    from polgate.vault import store as vault

    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/chat/{conversation_id}/send",
        data={"text": "Projekt rusza w maju", "extra_terms": "Wichura", "approved": "1"},
    )

    with Session(get_engine()) as session:
        row = session.exec(select(PendingTurn)).one()
    assert "Wichura" not in row.extra_terms_enc
    assert vault.decrypt(row.extra_terms_enc) == "Wichura"


def test_the_audit_row_records_which_stage_two_backend_ran(admin_client, monkeypatch):
    """With the remote judge the masked text leaves the network too, so the log has to say so."""
    from polgate.llm import router
    from polgate.models import AuditEvent

    monkeypatch.setattr(router, "stream_chat", _fake_stream(["ok"]))
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/chat/{conversation_id}/send", data={"text": "PESEL 50810112354", "extra_terms": ""}
    )
    admin_client.get(f"/chat/{conversation_id}/stream", headers=STREAM)

    with Session(get_engine()) as session:
        event = session.exec(select(AuditEvent).where(AuditEvent.event == "message_sent")).first()
    assert event.detail == "judge=mock"


def test_a_disabled_judge_is_recorded_as_off(admin_client, monkeypatch):
    from polgate.llm import router
    from polgate.models import AuditEvent

    monkeypatch.setattr(router, "stream_chat", _fake_stream(["ok"]))
    with Session(get_engine()) as session:
        policy = session.exec(select(Policy)).first() or Policy()
        policy.judge_enabled = False
        session.add(policy)
        session.commit()

    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/chat/{conversation_id}/send", data={"text": "PESEL 50810112354", "extra_terms": ""}
    )
    admin_client.get(f"/chat/{conversation_id}/stream", headers=STREAM)

    with Session(get_engine()) as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.event == "message_sent")).all()
    assert events[-1].detail == "judge=off"


def test_the_audit_page_shows_entity_counts_as_labels(admin_client, monkeypatch):
    """The admin reads which kinds of data went out masked, not a JSON string."""
    from polgate.llm import router

    monkeypatch.setattr(router, "stream_chat", _fake_stream(["ok"]))
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/chat/{conversation_id}/send", data={"text": "PESEL 50810112354", "extra_terms": ""}
    )
    admin_client.get(f"/chat/{conversation_id}/stream", headers=STREAM)

    page = admin_client.get("/admin/audit?event=message_sent").text
    assert '<span class="token">PESEL</span> 1' in page
    assert "{&#34;PESEL&#34;" not in page
    assert '{"PESEL"' not in page


def _stored_uploads() -> list[Path]:
    from polgate.config import get_settings

    upload_dir = get_settings().upload_dir
    return sorted(upload_dir.iterdir()) if upload_dir.exists() else []


def test_a_damaged_upload_is_reported_and_leaves_nothing_on_disk(admin_client):
    conversation_id = _open_conversation(admin_client)
    response = admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("umowa.docx", b"PESEL 50810112354, not a zip", "application/msword")},
    )
    assert response.status_code == 400
    assert "Nie udało się odczytać pliku" in response.text
    assert _stored_uploads() == []
    with Session(get_engine()) as session:
        assert session.exec(select(FileAsset)).all() == []


def test_an_upload_held_by_the_judge_leaves_nothing_on_disk(admin_client, monkeypatch):
    """Under the 'block' fallback a judge that is down stops the file, and the file must go."""
    from polanon import JudgeError

    from polgate.models import AuditEvent
    from polgate.web import files as files_web

    class _Blocked:
        async def anonymize(self, text, mapping):
            raise JudgeError("judge unreachable")

    monkeypatch.setattr(files_web, "build_anonymizer", lambda session: _Blocked())
    conversation_id = _open_conversation(admin_client)
    response = admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("klient.txt", b"PESEL 50810112354", "text/plain")},
    )
    assert response.status_code == 503
    assert "Plik nie został zapisany" in response.text
    assert _stored_uploads() == []
    with Session(get_engine()) as session:
        events = [row.event for row in session.exec(select(AuditEvent)).all()]
    assert "file_blocked_judge" in events


def test_an_unexpected_failure_still_removes_the_stored_upload(admin_client, monkeypatch):
    from polgate.web import files as files_web

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(files_web.vault, "save_mapping", broken)
    conversation_id = _open_conversation(admin_client)
    with pytest.raises(RuntimeError, match="disk full"):
        admin_client.post(
            f"/files/upload?conversation_id={conversation_id}",
            files={"file": ("klient.txt", b"PESEL 50810112354", "text/plain")},
        )
    assert _stored_uploads() == []


def test_a_file_the_hub_cannot_read_is_never_written(admin_client, monkeypatch):
    """The stored name used to keep whatever followed the last dot of the employee's file name."""
    from polgate.web import files as files_web

    written = []
    real_store = files_web._store

    def recording_store(payload, suffix):
        written.append(suffix)
        return real_store(payload, suffix)

    monkeypatch.setattr(files_web, "_store", recording_store)
    conversation_id = _open_conversation(admin_client)
    response = admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("CV.Jan Kowalski", b"x", "text/plain")},
    )
    assert response.status_code == 400
    assert "Nieobsługiwany" in response.text
    assert written == []


def test_a_stored_upload_is_named_by_a_random_id_and_a_known_ending(admin_client):
    import re

    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("Jan Kowalski.TXT", b"notatka", "text/plain")},
    )
    (stored,) = _stored_uploads()
    assert re.fullmatch(r"[0-9a-f]{32}\.txt", stored.name)


def test_a_document_is_read_off_the_event_loop(admin_client, monkeypatch):
    """Parsing a large file on the loop held every other employee's requests until it finished."""
    import asyncio

    from polgate.web import files as files_web

    seen: list[bool] = []

    def recording_extract(path, mime, filename):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append(False)
        else:
            seen.append(True)
        return "notatka"

    monkeypatch.setattr(files_web, "extract_text", recording_extract)
    conversation_id = _open_conversation(admin_client)
    response = admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("notatka.txt", b"notatka", "text/plain")},
    )
    assert response.status_code == 200
    assert seen == [False]


def _upload_for_review(client) -> int:
    conversation_id = _open_conversation(client)
    with Session(get_engine()) as session:
        policy = session.exec(select(Policy)).first() or Policy()
        policy.hil_files = HilMode.REVIEW
        session.add(policy)
        session.commit()
    client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("tajny.txt", b"Tajny dokument ZXQ-MARKER", "text/plain")},
    )
    with Session(get_engine()) as session:
        return session.exec(select(FileAsset)).one().id


def test_a_rejected_file_cannot_be_approved_again(admin_client):
    """A review tab left open, or the back button, must not attach what was turned down."""
    file_id = _upload_for_review(admin_client)
    admin_client.post(f"/files/{file_id}/reject")

    response = admin_client.post(f"/files/{file_id}/approve")

    assert response.status_code == 409
    with Session(get_engine()) as session:
        assert session.get(FileAsset, file_id).status is FileStatus.REJECTED


def test_rejecting_a_file_drops_its_extracted_text(admin_client):
    file_id = _upload_for_review(admin_client)
    admin_client.post(f"/files/{file_id}/reject")

    with Session(get_engine()) as session:
        asset = session.get(FileAsset, file_id)
    assert asset.text_original_enc == ""
    assert asset.text_sent == ""


def test_the_review_page_of_a_rejected_file_offers_nothing_to_approve(admin_client):
    file_id = _upload_for_review(admin_client)
    admin_client.post(f"/files/{file_id}/reject")

    page = admin_client.get(f"/files/{file_id}/review")

    assert page.status_code == 200
    assert "Plik odrzucony i usunięty." in page.text
    assert f"/files/{file_id}/approve" not in page.text
    assert "ZXQ-MARKER" not in page.text


def test_an_approved_file_can_be_reviewed_but_offers_no_second_approval(admin_client):
    file_id = _upload_for_review(admin_client)
    admin_client.post(f"/files/{file_id}/approve")

    page = admin_client.get(f"/files/{file_id}/review")

    assert "ZXQ-MARKER" in page.text
    assert f"/files/{file_id}/approve" not in page.text


def test_an_account_with_a_role_the_form_never_offers_is_refused(admin_client):
    response = admin_client.post(
        "/admin/users",
        data={"username": "nowy", "password": "dlugie-haslo-123", "role": "superuser"},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 400
    with Session(get_engine()) as session:
        assert session.exec(select(User).where(User.username == "nowy")).first() is None


@pytest.mark.parametrize("field", ["hil_chat", "hil_files", "judge_fallback"])
def test_a_policy_value_the_form_never_offers_changes_nothing(admin_client, field):
    admin_client.get("/admin/policy")
    with Session(get_engine()) as session:
        before = session.exec(select(Policy)).one().model_dump()

    response = admin_client.post(
        "/admin/policy",
        data={field: "bogus", "denylist": "Projekt Wichura"},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 400
    with Session(get_engine()) as session:
        assert session.exec(select(Policy)).one().model_dump() == before


def test_a_link_to_the_creation_address_creates_nothing(admin_client):
    """Lax cookies travel with a link from another site, so a GET must not change anything."""
    conversation_id = _open_conversation(admin_client)

    response = admin_client.get("/chat/new", headers={"Sec-Fetch-Site": "cross-site"})

    assert response.headers["location"] == "/chat"
    with Session(get_engine()) as session:
        assert [c.id for c in session.exec(select(Conversation)).all()] == [conversation_id]


def test_the_sidebar_creates_a_conversation_with_a_post(admin_client):
    first = _open_conversation(admin_client)
    response = admin_client.post("/chat/new", headers={"Sec-Fetch-Site": "same-origin"})
    assert response.headers["location"] != f"/chat/{first}"
    page = admin_client.get(response.headers["location"]).text
    assert '<form method="post" action="/chat/new">' in page


def test_a_link_from_another_site_cannot_start_a_queued_turn(admin_client):
    """The stream sends the queued message to the provider, so it is checked like a post."""
    from polgate.models import PendingTurn

    conversation_id = _open_conversation(admin_client)
    admin_client.post(f"/chat/{conversation_id}/send", data={"text": "Jan Kowalski"})

    response = admin_client.get(
        f"/chat/{conversation_id}/stream", headers={**STREAM, "Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    with Session(get_engine()) as session:
        assert len(session.exec(select(PendingTurn)).all()) == 1


def test_an_office_file_that_unpacks_too_far_is_refused_with_a_reason(admin_client, monkeypatch):
    import io

    from docx import Document

    from polgate.files import extract

    monkeypatch.setattr(extract, "MAX_PARSED_XML_BYTES", 1_000)
    document = Document()
    document.add_paragraph("PESEL 50810112354 " * 200)
    buffer = io.BytesIO()
    document.save(buffer)
    conversation_id = _open_conversation(admin_client)
    response = admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("umowa.docx", buffer.getvalue(), "application/msword")},
    )
    assert response.status_code == 400
    assert "Po rozpakowaniu ten plik jest zbyt duży" in response.text
    assert _stored_uploads() == []


async def test_a_cancelled_upload_is_removed_once_its_reader_lets_go(admin_client, monkeypatch):
    """A browser that goes away cancels the request, not the thread still reading the file."""
    import asyncio
    import contextlib
    import threading

    import httpx

    from polgate.web import files as files_web

    started, release = threading.Event(), threading.Event()

    def slow_extract(path, mime, filename):
        with open(path, "rb"):  # held open, as pypdf and openpyxl do while they read
            started.set()
            release.wait(5)
        return "notatka"

    monkeypatch.setattr(files_web, "extract_text", slow_extract)
    conversation_id = _open_conversation(admin_client)
    transport = httpx.ASGITransport(app=admin_client.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies=admin_client.cookies
    ) as client:
        request = asyncio.create_task(
            client.post(
                f"/files/upload?conversation_id={conversation_id}",
                files={"file": ("notatka.txt", b"PESEL 50810112354", "text/plain")},
            )
        )
        assert await asyncio.to_thread(started.wait, 5)
        request.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await request
        release.set()
        for _ in range(100):
            if not _stored_uploads():
                break
            await asyncio.sleep(0.02)

    assert _stored_uploads() == []
    with Session(get_engine()) as session:
        assert session.exec(select(FileAsset)).all() == []


def test_a_clean_up_that_fails_does_not_hide_the_original_error(admin_client, monkeypatch):
    from polgate.web import files as files_web

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    def locked(self, missing_ok=False):
        raise PermissionError("in use")

    monkeypatch.setattr(files_web.vault, "save_mapping", broken)
    monkeypatch.setattr(Path, "unlink", locked)
    conversation_id = _open_conversation(admin_client)
    with pytest.raises(RuntimeError, match="disk full"):
        admin_client.post(
            f"/files/upload?conversation_id={conversation_id}",
            files={"file": ("klient.txt", b"PESEL 50810112354", "text/plain")},
        )


def test_a_rejected_file_keeps_only_its_masked_name(admin_client):
    conversation_id = _open_conversation(admin_client)
    admin_client.post(
        f"/files/upload?conversation_id={conversation_id}",
        files={"file": ("CV Jan Kowalski.txt", b"Doswiadczenie: 5 lat", "text/plain")},
    )
    with Session(get_engine()) as session:
        file_id = session.exec(select(FileAsset)).one().id

    admin_client.post(f"/files/{file_id}/reject")

    with Session(get_engine()) as session:
        assert session.get(FileAsset, file_id).filename_enc == ""
    for page in (f"/chat/{conversation_id}", f"/files/{file_id}/review"):
        text = admin_client.get(page).text
        assert "Kowalski" not in text, page
        assert "&lt;OSOBA_1&gt;" in text, page


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Accept": "text/html,application/xhtml+xml", "Upgrade-Insecure-Requests": "1"},
        {"Accept": "*/*"},
    ],
    ids=["nothing", "a page", "anything"],
)
def test_the_stream_answers_only_an_event_source(admin_client, headers):
    """Over plain HTTP a link from another site sends neither Fetch Metadata nor an Origin."""
    from polgate.models import PendingTurn

    conversation_id = _open_conversation(admin_client)
    admin_client.post(f"/chat/{conversation_id}/send", data={"text": "Jan Kowalski"})

    response = admin_client.get(f"/chat/{conversation_id}/stream", headers=headers)

    assert response.status_code == 403
    with Session(get_engine()) as session:
        assert len(session.exec(select(PendingTurn)).all()) == 1


def test_a_refused_policy_form_sets_no_field_even_in_memory(admin_client, monkeypatch):
    """Every choice is read before any field is set, so nothing half-applied can be committed."""
    from polgate.web import admin as admin_web

    loaded = []
    real_get_policy = admin_web.get_policy

    def keeping(session):
        policy = real_get_policy(session)
        loaded.append(policy)
        return policy

    monkeypatch.setattr(admin_web, "get_policy", keeping)
    response = admin_client.post(
        "/admin/policy", data={"hil_chat": "review", "judge_fallback": "bogus"}
    )

    assert response.status_code == 400
    assert loaded[-1].hil_chat is HilMode.AUTO
