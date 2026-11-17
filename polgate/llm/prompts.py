"""System prompt sent to the cloud model.

The model must treat tokens as opaque identifiers. This is a usability measure, not a security one:
the guarantee that originals never leave the network is enforced in code, not by asking politely.
"""

from __future__ import annotations

from polanon import token_instructions

from polgate.i18n import SUPPORTED

_ROLE = "You are a helpful assistant used inside a company."
_LANGUAGE = "Answer in the language the user writes in."


def system_prompt(language: str) -> str:
    """Return the system prompt for an employee who uses the hub in ``language``.

    The note about tokens is polanon's. In Polish it asks the model to write the case a person
    stands in after the token, ``<OSOBA_1:wołacz>``, so "Szanowny Panie Janie Kowalski" comes back
    instead of the name in the form the message had; in English, where names do not decline, it
    asks for the token exactly as written. The prompt is made from code alone, so it holds nobody's
    data, which is why the last check before sending leaves it out.

    Args:
        language: the employee's interface language; anything the hub does not offer is read as
            its default, Polish.
    """
    note = token_instructions(language if language in SUPPORTED else SUPPORTED[0])
    return "\n".join((_ROLE, note, _LANGUAGE))
