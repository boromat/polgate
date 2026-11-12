"""A well-known name a polanon release adds reaches installations that already have a policy.

`Policy.allowlist` is a copy of polanon's `WELL_KNOWN_NAMES` taken when the policy was created, so
without this "ZUS" and "Płatnik", which polanon 0.3.3 added, would stay masked as company names on
every existing installation.
"""

from polanon.allowlist import WELL_KNOWN_NAMES
from sqlmodel import select

from polgate import policy_loader
from polgate.models import AuditEvent, Policy
from polgate.policy_loader import NAMES_BEFORE_TRACKING, get_policy

ADDED_SINCE_TRACKING = set(WELL_KNOWN_NAMES) - NAMES_BEFORE_TRACKING


def _saved_policy(session, allowlist: list[str], known: str = "") -> Policy:
    policy = Policy(allowlist="\n".join(allowlist), well_known_names_known=known)
    session.add(policy)
    session.commit()
    return policy


def _name_events(session) -> list[str]:
    events = session.exec(select(AuditEvent).where(AuditEvent.event == "policy_changed")).all()
    return [event.detail for event in events if "fields=allowlist" in event.detail]


def test_the_names_before_tracking_only_describe_the_past():
    assert set(WELL_KNOWN_NAMES) >= NAMES_BEFORE_TRACKING
    assert {"ZUS", "Płatnik"} <= ADDED_SINCE_TRACKING


def test_a_policy_saved_before_tracking_gets_the_names_polanon_added(db_session):
    """The owner's installation: the old list, with one name the admin had deleted."""
    _saved_policy(db_session, [*sorted(NAMES_BEFORE_TRACKING - {"Canva"}), "Saldo"])

    policy = get_policy(db_session)
    allowlist = policy.allowlist.splitlines()

    assert {"ZUS", "Płatnik"} <= set(allowlist)
    assert "Canva" not in allowlist
    assert "Saldo" in allowlist
    assert set(policy.well_known_names_known.splitlines()) == set(WELL_KNOWN_NAMES)
    assert _name_events(db_session) == [
        f"fields=allowlist new_well_known_names={len(ADDED_SINCE_TRACKING)}"
    ]


def test_a_name_the_admin_deleted_after_seeing_it_stays_deleted(db_session):
    policy = get_policy(db_session)
    policy.allowlist = "\n".join(n for n in policy.allowlist.splitlines() if n != "ZUS")
    db_session.add(policy)
    db_session.commit()

    assert "ZUS" not in get_policy(db_session).allowlist.splitlines()


def test_a_name_only_the_library_knows_about_joins_the_allowlist(db_session, monkeypatch):
    _saved_policy(db_session, ["Saldo"], known="\n".join(WELL_KNOWN_NAMES))
    monkeypatch.setattr(policy_loader, "WELL_KNOWN_NAMES", (*WELL_KNOWN_NAMES, "NewTool"))

    policy = get_policy(db_session)

    assert policy.allowlist.splitlines() == ["Saldo", "NewTool"]
    assert "NewTool" in policy.well_known_names_known.splitlines()


def test_a_name_already_on_the_allowlist_is_not_added_twice(db_session):
    _saved_policy(db_session, ["zus", "Płatnik"])

    allowlist = get_policy(db_session).allowlist.splitlines()

    assert allowlist.count("Płatnik") == 1
    assert "ZUS" not in allowlist


def test_a_new_policy_is_recorded_without_an_audit_event(db_session):
    get_policy(db_session)
    get_policy(db_session)

    assert not _name_events(db_session)
    assert get_policy(db_session).well_known_names_known
