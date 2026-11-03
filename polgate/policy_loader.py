"""Access to the single admin policy row."""

from __future__ import annotations

from polanon.policy import ENTITY_TYPES, enabled_entities
from sqlmodel import Session, select

from polgate import audit
from polgate.models import OFF_BY_DEFAULT, Policy

#: The entity types a policy saved before `Policy.entity_types_known` existed had been offered.
#: It describes the past, so it stays as it is when a release adds a type.
TYPES_BEFORE_TRACKING = frozenset(
    {
        "PERSON",
        "ORG",
        "LOCATION",
        "PESEL",
        "NIP",
        "REGON",
        "KRS",
        "PL_ID",
        "PL_PASSPORT",
        "IBAN",
        "CREDIT_CARD",
        "EMAIL",
        "PHONE",
        "POSTAL_CODE",
        "SECRET",
        "IP_ADDRESS",
        "MAC_ADDRESS",
        "ADDRESS",
        "PLATE",
        "VIN",
        "DATE",
        "OTHER",
    }
)


def masking_is_off(policy: Policy) -> bool:
    """Return whether this policy leaves nothing to mask.

    An administrator who clears every entity type turns the hub into a plain proxy: text reaches the
    provider exactly as it was typed. That is a legitimate choice and a catastrophic accident, and
    the two look identical in the database, so every screen that implies protection asks this first.
    """
    return not enabled_entities(policy.enabled_entities)


def get_policy(session: Session) -> Policy:
    """Return the policy, creating the default row on first use.

    Entity types added by a release since the policy was last reconciled are switched on on the way,
    see :func:`switch_on_new_entity_types`.
    """
    policy = session.exec(select(Policy)).first()
    if policy is None:
        policy = Policy()
        session.add(policy)
        session.commit()
        session.refresh(policy)
    switch_on_new_entity_types(session, policy)
    return policy


def switch_on_new_entity_types(session: Session, policy: Policy) -> set[str]:
    """Enable the entity types the code gained after this policy was last reconciled.

    The policy stores the types that are on, so a detector a release adds would otherwise stay off
    on every installation that already has a policy row, with nothing on screen to say so. A new
    type joins the enabled set when a new policy would have it on. Two choices are respected: a type
    the administrator has already been offered and unticked stays off, because it is recorded in
    `entity_types_known`, and a policy with masking switched off is left that way.

    Args:
        session: open database session; this function commits when it changes anything.
        policy: the policy row to reconcile.

    Returns:
        The entity types that were switched on, usually none.
    """
    known = {name for name in policy.entity_types_known.split(",") if name}
    added_to_code = set(ENTITY_TYPES) - (known or TYPES_BEFORE_TRACKING)
    if not added_to_code:
        return set()

    enabled = enabled_entities(policy.enabled_entities)
    switched_on: set[str] = set()
    if enabled:
        switched_on = added_to_code - OFF_BY_DEFAULT - enabled
    if switched_on:
        policy.enabled_entities = ",".join(sorted(enabled | switched_on))
    policy.entity_types_known = ",".join(sorted(ENTITY_TYPES))
    session.add(policy)
    session.commit()
    session.refresh(policy)

    if switched_on:
        audit.log(
            session,
            event="policy_changed",
            detail=f"fields=enabled_entities new_entity_types={','.join(sorted(switched_on))}",
        )
    return switched_on
