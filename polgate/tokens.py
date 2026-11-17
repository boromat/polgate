"""Tokens as a model writes them: ``<OSOBA_1>``, or a person's with a case, ``<OSOBA_1:wołacz>``.

The system prompt asks the model to write the case a person stands in after the token, and
polanon's restore gives the name back declined. A stored answer therefore holds cased tokens, and
everything that reads a stored text for its tokens has to see them: the fingerprints that catch a
token whose number went to somebody else, the last check before sending and the preview. The
pattern is the one polanon's restore reads, so the hub finds exactly the tokens that get restored.
"""

from __future__ import annotations

import re

from polanon.stream_restore import RESTORED_TOKEN_PATTERN

#: A token, with the case a model may write after a colon.
WRITTEN_TOKEN_RE = re.compile(RESTORED_TOKEN_PATTERN)


def base_token(match: re.Match[str]) -> str:
    """Return the token a match of :data:`WRITTEN_TOKEN_RE` declines, as the mapping holds it."""
    return f"<{match['label']}_{match['n']}>"
