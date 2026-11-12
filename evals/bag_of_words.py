"""A stand-in embedding model: word stems hashed into a fixed number of dimensions.

It knows nothing of meaning, only of words in common, so it measures the plumbing and the matching
of words, not what a real embedding model would find. ``retrieval.py`` can call a real model
instead, such as bge-m3 in LM Studio.
"""

from __future__ import annotations

import hashlib
import re

DIMENSIONS = 256
_WORD = re.compile(r"<[A-Z_]+_\d+>|[^\W\d_]{3,}|\d+")


def vector(text: str) -> list[float]:
    """Return the text as counts of its hashed word stems (first five letters), tokens whole."""
    counts = [0.0] * DIMENSIONS
    for word in _WORD.findall(text):
        stem = word if word.startswith("<") else word.casefold()[:5]
        digest = hashlib.sha256(stem.encode()).digest()
        counts[int.from_bytes(digest[:4], "big") % DIMENSIONS] += 1.0
    return counts
