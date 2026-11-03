"""An entity type a release adds is switched on for installations that already have a policy.

`Policy.enabled_entities` stores what is on, so without this a new detector would stay off on
every existing installation, and nothing on screen would say so.
"""

import pytest
from polanon.policy import ENTITY_TYPES
from sqlmodel import select

from polgate import policy_loader
from polgate.models import OFF_BY_DEFAULT, AuditEvent, Policy
from polgate.policy_loader import TYPES_BEFORE_TRACKING, get_policy

#: What a policy saved before the foreign-format guards held when nobody had changed it.
DEFAULTS_BEFORE_FOREIGN_FORMATS = (
    "PERSON,ORG,LOCATION,ADDRESS,PESEL,NIP,REGON,KRS,PL_ID,PL_PASSPORT,"
    "IBAN,CREDIT_CARD,EMAIL,PHONE,POSTAL_CODE,SECRET,IP_ADDRESS,MAC_ADDRESS,PLATE,VIN,OTHER"
)


@pytest.fixture
def new_type(monkeypatch):
    """Pretend a release has just added a detector, and changed nothing else."""
    monkeypatch.setitem(ENTITY_TYPES, "NEW_ID", "NOWY")
    return "NEW_ID"


def _known_without(*added: str) -> str:
    return ",".join(sorted(set(ENTITY_TYPES) - set(added)))


def _saved_policy(session, enabled: str, known: str) -> Policy:
    policy = Policy(enabled_entities=enabled, entity_types_known=known)
    session.add(policy)
    session.commit()
    return policy


def _upgrade_events(session) -> list[AuditEvent]:
    return list(session.exec(select(AuditEvent).where(AuditEvent.event == "policy_changed")).all())


def test_the_types_before_tracking_only_describe_the_past():
    assert set(TYPES_BEFORE_TRACKING) <= set(ENTITY_TYPES)


def test_a_policy_saved_before_tracking_gets_the_foreign_identifiers(db_session):
    """The owner's installation: default policy, saved before the column existed."""
    _saved_policy(db_session, DEFAULTS_BEFORE_FOREIGN_FORMATS, known="")

    enabled = set(get_policy(db_session).enabled_entities.split(","))

    assert {"TAX_ID", "COMPANY_ID", "BANK_ACCOUNT", "BIRTH_DATE"} <= enabled
    assert [event.detail for event in _upgrade_events(db_session)] == [
        "fields=enabled_entities new_entity_types=BANK_ACCOUNT,BIRTH_DATE,COMPANY_ID,TAX_ID"
    ]


def test_a_type_only_the_library_knows_about_is_switched_on(db_session, new_type):
    """Regression: with a second list of defaults in the hub, a hub that reloaded after the library
    gained the type but before that list did recorded the type as offered and left it off."""
    _saved_policy(db_session, "PERSON,PESEL", known=_known_without(new_type))

    policy = get_policy(db_session)

    assert policy.enabled_entities == "NEW_ID,PERSON,PESEL"
    assert new_type in policy.entity_types_known.split(",")


def test_the_upgrade_is_recorded_once_in_the_audit_trail(db_session, new_type):
    _saved_policy(db_session, "PERSON", known=_known_without(new_type))
    get_policy(db_session)
    get_policy(db_session)

    events = _upgrade_events(db_session)
    assert [event.detail for event in events] == ["fields=enabled_entities new_entity_types=NEW_ID"]
    assert events[0].user_id is None


def test_a_type_the_admin_unticked_after_seeing_it_stays_off(db_session, new_type):
    policy = get_policy(db_session)
    assert new_type in policy.enabled_entities.split(",")

    policy.enabled_entities = "PERSON"
    db_session.add(policy)
    db_session.commit()

    assert get_policy(db_session).enabled_entities == "PERSON"


def test_masking_switched_off_is_not_switched_back_on(db_session, new_type):
    _saved_policy(db_session, "", known="")
    assert get_policy(db_session).enabled_entities == ""
    assert not _upgrade_events(db_session)


def test_a_new_type_meant_to_be_off_by_default_stays_off(db_session, monkeypatch):
    known = _known_without()
    monkeypatch.setitem(ENTITY_TYPES, "RARE_ID", "RZADKI")
    monkeypatch.setattr(policy_loader, "OFF_BY_DEFAULT", OFF_BY_DEFAULT | {"RARE_ID"})
    _saved_policy(db_session, "PERSON", known=known)

    policy = get_policy(db_session)

    assert policy.enabled_entities == "PERSON"
    assert "RARE_ID" in policy.entity_types_known.split(",")


def test_nothing_is_written_when_no_type_was_added(db_session):
    known = _known_without()
    _saved_policy(db_session, "PERSON", known=known)

    policy = get_policy(db_session)

    assert (policy.enabled_entities, policy.entity_types_known) == ("PERSON", known)
    assert not _upgrade_events(db_session)


def test_dates_are_left_alone_by_the_default_policy():
    from polgate.models import Policy

    assert "DATE" not in Policy().enabled_entities.split(",")
