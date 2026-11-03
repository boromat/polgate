"""What happens when an administrator clears every entity type.

It is a legitimate setting — the hub becomes a plain proxy — and it is also the single most
damaging mis-click available in the panel, because the database cannot tell the two apart. So every
screen that implies protection has to stop implying it.
"""

from sqlmodel import Session, select

from polgate.db import get_engine
from polgate.models import AuditEvent, Policy
from polgate.policy_loader import masking_is_off


def _clear_entities(client) -> None:
    client.post("/admin/policy", data={"hil_chat": "auto", "hil_files": "review"})


def test_a_policy_with_no_entity_types_is_recognised():
    assert masking_is_off(Policy(enabled_entities=""))
    assert masking_is_off(Policy(enabled_entities="NIE_MA_TAKIEGO"))
    assert not masking_is_off(Policy(enabled_entities="PESEL"))


def test_the_default_policy_masks_something():
    assert not masking_is_off(Policy())


def test_clearing_every_box_really_does_switch_masking_off(admin_client):
    """Proving the danger is real before checking that the interface admits it."""
    _clear_entities(admin_client)
    with Session(get_engine()) as session:
        assert session.exec(select(Policy)).first().enabled_entities == ""


def test_the_chat_badge_stops_claiming_protection(admin_client):
    admin_client.get("/chat")
    conversation_id = _conversation_id()
    assert "Maskowanie włączone" in admin_client.get(f"/chat/{conversation_id}").text

    _clear_entities(admin_client)

    page = admin_client.get(f"/chat/{conversation_id}").text
    assert "Bez maskowania" in page
    assert "Maskowanie włączone" not in page


def test_the_admin_dashboard_says_so(admin_client):
    assert "Żaden typ danych" not in admin_client.get("/admin").text
    _clear_entities(admin_client)
    assert "Żaden typ danych" in admin_client.get("/admin").text


def test_the_policy_page_says_so(admin_client):
    _clear_entities(admin_client)
    assert "Żaden typ danych" in admin_client.get("/admin/policy").text


def test_the_audit_records_it_in_words(admin_client):
    _clear_entities(admin_client)
    with Session(get_engine()) as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.event == "policy_changed")).all()
    assert "masking_disabled" in events[-1].detail


def test_turning_masking_back_on_clears_the_warning(admin_client):
    _clear_entities(admin_client)
    admin_client.post(
        "/admin/policy",
        data={"hil_chat": "auto", "hil_files": "review", "entities": ["PESEL", "PERSON"]},
    )
    assert "Żaden typ danych" not in admin_client.get("/admin").text
    with Session(get_engine()) as session:
        assert (
            "masking_disabled"
            not in session.exec(select(AuditEvent).where(AuditEvent.event == "policy_changed"))
            .all()[-1]
            .detail
        )


def _conversation_id() -> int:
    from polgate.models import Conversation

    with Session(get_engine()) as session:
        return session.exec(select(Conversation)).first().id
