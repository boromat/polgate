"""What each token of a stored text stood for, so a token that now means somebody else is caught.

A vault row expires after ``vault_ttl_days``, and the allocator, which counts from the live mapping,
then hands the freed number to the next value it meets: after Anna Nowak's row expires, Piotr Lis
becomes ``<OSOBA_1>``. The texts stored before still say ``<OSOBA_1>`` for Anna, and replayed to the
model, as history, an attached file or a passage of one, they make it answer about Anna with Piotr's
name restored into the answer.

So each stored text records, beside it, a fingerprint of the value each of its tokens stood for
(:func:`polgate.vault.store.value_hash`, keyed, so it gives no value back). Before the text is
sent again, a token whose current value has another fingerprint, or no value at all any more, is
replaced with :data:`EXPIRED`. The rest of the text keeps its meaning; only the reference that would
now point at the wrong person goes.

A text stored before fingerprints existed has none, so for it only a token the mapping no longer
has at all is caught; a number already given to somebody else cannot be told from the original.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from polanon.policy import TOKEN_PATTERN

from polgate.vault.store import value_hash

#: What the model reads in place of a token that no longer stands for what it did.
EXPIRED = "[dane wygasłe / expired data]"

_TOKEN_RE = re.compile(TOKEN_PATTERN)


def record(text: str, mapping: Mapping[str, str]) -> str:
    """Return the fingerprints of the values the tokens in ``text`` stand for now, as JSON.

    Tokens the mapping does not hold, such as one a model made up, are left out.
    """
    tokens = {match.group(0) for match in _TOKEN_RE.finditer(text)}
    return json.dumps(
        {token: value_hash(mapping[token]) for token in sorted(tokens) if token in mapping}
    )


def expire(text: str, recorded: str, mapping: Mapping[str, str]) -> tuple[str, int]:
    """Replace each token that no longer stands for what it did when the text was stored.

    Args:
        text: a stored masked text: a message as sent, a file's ``text_sent`` or a passage of it.
        recorded: the JSON :func:`record` wrote beside it, or empty for a text stored before.
        mapping: the conversation's mapping for the current turn.

    Returns:
        The text with :data:`EXPIRED` in place of every such token, and how many places changed.
    """
    stale = stale_tokens(text, recorded, mapping)
    if not stale:
        return text, 0
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(0) not in stale:
            return match.group(0)
        count += 1
        return EXPIRED

    return _TOKEN_RE.sub(replace, text), count


def stale_tokens(text: str, recorded: str, mapping: Mapping[str, str]) -> set[str]:
    """Return the tokens of ``text`` that the mapping has lost or now gives another value."""
    known: dict[str, str] = json.loads(recorded) if recorded else {}
    stale = set()
    for token in {match.group(0) for match in _TOKEN_RE.finditer(text)}:
        current = mapping.get(token)
        moved = token in known and current is not None and known[token] != value_hash(current)
        if current is None or moved:
            stale.add(token)
    return stale
