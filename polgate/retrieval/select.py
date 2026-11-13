"""Cut a masked document into passages and pick the ones that answer a masked question.

Everything here is a pure function of masked text and vectors: no database, no network, no
original value. The search in attachments and, later, a company knowledge base share it, and the
evaluation in ``evals/`` calls it directly to score each way of ranking on its own.

Ranking is hybrid. The cosine between the question's embedding and a passage's catches a passage
that says the same thing in other words; matching words catches what an embedding is poor at, above
all tokens: ``<OSOBA_1>`` in the question and in a passage is the same person, letter for letter,
and a passage about that person should come first. Words are matched by their first five letters,
which is crude but enough for Polish case endings: "umowy" and "umowa", "Kowalskiego" and
"Kowalski" meet.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from polanon.policy import TOKEN_PATTERN

Vector = NDArray[np.float32]
Mode = Literal["hybrid", "vector", "words"]

_TOKEN_RE = re.compile(TOKEN_PATTERN)
_WORD_RE = re.compile(r"[^\W\d_]{4,}")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
#: Letters a word is matched by, so the case endings of Polish do not keep it apart from itself.
_STEM = 5
#: How much a token in common counts against a word in common: a token names one entity exactly.
_TOKEN_WEIGHT = 3.0
#: Share of the hybrid score that comes from the vectors; the rest comes from matching words.
_VECTOR_SHARE = 0.6

#: What the model reads above passages chosen from a file, in place of the whole file.
PASSAGES_MARKER = (
    "[wybrane fragmenty pasujące do pytania, nie cały dokument / "
    "selected passages that match the question, not the whole document]"
)
#: Between two passages that are not next to each other in the file.
GAP_MARKER = "[...]"
#: Below the passages when the file was longer than the passages the hub indexes.
CAPPED_MARKER = (
    "[przeszukano tylko pierwsze {count} fragmentów dokumentu / "
    "only the first {count} passages of the document were searched]"
)


@dataclass(frozen=True)
class Passage:
    """One passage of a document, with its embedding when it has one."""

    position: int
    text: str
    vector: Vector | None = None


def split_passages(text: str, target_chars: int) -> list[str]:
    """Cut text into passages of about ``target_chars``, along paragraphs where it can.

    Paragraphs are kept whole and packed together up to the target. One longer than the target is
    cut between sentences, and a sentence longer still is cut where the target falls. No word is
    dropped and none repeats; only the whitespace between pieces may change.
    """
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    pieces = [
        piece
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
        for piece in _fit(paragraph.strip(), target_chars)
    ]
    passages: list[str] = []
    for piece in pieces:
        if passages and len(passages[-1]) + 2 + len(piece) <= target_chars:
            passages[-1] = f"{passages[-1]}\n\n{piece}"
        else:
            passages.append(piece)
    return passages


def _fit(paragraph: str, target_chars: int) -> Iterable[str]:
    """Yield a paragraph in pieces no longer than the target, cut between sentences first."""
    if len(paragraph) <= target_chars:
        yield paragraph
        return
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > target_chars:
            if current:
                yield current
                current = ""
            cut = _cut_point(sentence, target_chars)
            yield sentence[:cut].rstrip()
            sentence = sentence[cut:].lstrip()
        if current and len(current) + 1 + len(sentence) > target_chars:
            yield current
            current = ""
        current = f"{current} {sentence}" if current else sentence
    if current:
        yield current


def _cut_point(text: str, target_chars: int) -> int:
    """Return where to cut text longer than the target: between words, and never inside a token.

    A table row extracted with no sentence ends was cut where the target fell, which split
    "<OSOBA_12>" into "<OSO" and "BA_12>" and "1200" into "120" and "0". Half a token is no token:
    the stale-token check and the matching of tokens do not see it, and a model given one half may
    complete it into another, valid token. The last space in the second half of the target wins;
    without one the cut falls at the target, moved back to the start of a token it would split.
    """
    cut = text.rfind(" ", target_chars // 2, target_chars + 1)
    if cut <= 0:
        cut = target_chars
    for match in _TOKEN_RE.finditer(text):
        if match.start() >= cut:
            break
        if match.start() < cut < match.end():
            cut = match.start() if match.start() > 0 else match.end()
            break
    return cut


def unit(vector: Sequence[float]) -> Vector:
    """Return a vector as float32 of unit length, so a dot product is the cosine."""
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    return array / norm if norm > 0 else array


def to_blob(vector: Vector) -> bytes:
    """Serialize a vector for the database: little-endian float32."""
    return np.asarray(vector, dtype="<f4").tobytes()


def from_blob(blob: bytes) -> Vector:
    """Read a vector written by :func:`to_blob`."""
    return np.frombuffer(blob, dtype="<f4").astype(np.float32)


def tokens_in(text: str) -> list[str]:
    """Return every token in the text, whole: the pattern has groups, so `findall` would not."""
    return [match.group(0) for match in _TOKEN_RE.finditer(text)]


def rank(
    question: str,
    question_vector: Vector | None,
    passages: Sequence[Passage],
    mode: Mode = "hybrid",
) -> list[int]:
    """Return the indexes of ``passages``, best match for the question first.

    Args:
        question: the masked question.
        question_vector: its embedding, unit length; required unless ``mode`` is ``"words"``.
        passages: the candidates; each needs a vector unless ``mode`` is ``"words"``.
        mode: ``"hybrid"`` (both), ``"vector"`` (cosine only) or ``"words"`` (matching words only).

    Returns:
        Every index, ordered by score, ties broken by position in the document.
    """
    if not passages:
        return []
    scores = np.zeros(len(passages), dtype=np.float64)
    if mode != "words":
        scores += (_VECTOR_SHARE if mode == "hybrid" else 1.0) * _scaled(
            _cosines(question_vector, passages)
        )
    if mode != "vector":
        scores += (1 - _VECTOR_SHARE if mode == "hybrid" else 1.0) * _scaled(
            word_scores(question, [p.text for p in passages])
        )
    order = sorted(range(len(passages)), key=lambda i: (-scores[i], passages[i].position))
    return order


def _cosines(question_vector: Vector | None, passages: Sequence[Passage]) -> NDArray[np.float64]:
    """Return the cosine of every passage with the question."""
    if question_vector is None or any(p.vector is None for p in passages):
        raise ValueError("ranking by vectors needs a vector for the question and every passage")
    matrix = np.stack([p.vector for p in passages if p.vector is not None])
    return (matrix @ question_vector).astype(np.float64)


def word_scores(question: str, texts: Sequence[str]) -> NDArray[np.float64]:
    """Score each text by the question's terms it contains, rarer terms counting more.

    A term is a token, matched exactly, or a word of four letters or more, matched by its first
    five letters regardless of case. Each term found adds its inverse document frequency, and a
    token counts three times as much as a word.
    """
    wanted = _terms(question)
    if not wanted or not texts:
        return np.zeros(len(texts), dtype=np.float64)
    found = [_terms(text) for text in texts]
    total = len(texts)
    scores = np.zeros(total, dtype=np.float64)
    for term, weight in wanted.items():
        holders = [i for i, terms in enumerate(found) if term in terms]
        if not holders:
            continue
        idf = math.log(1 + total / len(holders))
        for i in holders:
            scores[i] += weight * idf
    return scores


def _terms(text: str) -> dict[str, float]:
    """Return the terms of a text with their weight: tokens exactly, words by their stem."""
    terms = dict.fromkeys(tokens_in(text), _TOKEN_WEIGHT)
    for word in _WORD_RE.findall(_TOKEN_RE.sub(" ", text)):
        terms.setdefault(word.casefold()[:_STEM], 1.0)
    return terms


def _scaled(values: NDArray[np.float64]) -> NDArray[np.float64]:
    """Scale scores to 0..1 across the candidates, so the two kinds can be added."""
    low, high = float(values.min()), float(values.max())
    if high - low < 1e-12:
        return np.zeros_like(values)
    return (values - low) / (high - low)


def choose(
    passages: Sequence[Passage], order: Sequence[int], top_k: int, budget_chars: int
) -> list[Passage]:
    """Take the best ``top_k`` passages that fit the budget, back in document order.

    The best passage is always taken, cut to the budget if it alone is longer, so a question never
    gets nothing back from a file that holds an answer.
    """
    chosen: list[Passage] = []
    used = 0
    for index in order[: max(top_k, 1)]:
        passage = passages[index]
        if not chosen and len(passage.text) > budget_chars:
            chosen.append(Passage(passage.position, passage.text[:budget_chars], passage.vector))
            break
        if used + len(passage.text) > budget_chars:
            continue
        chosen.append(passage)
        used += len(passage.text)
    return sorted(chosen, key=lambda p: p.position)


def render(heading: str, chosen: Sequence[Passage], *, capped_at: int | None = None) -> str:
    """Write the chosen passages as the model reads them, marked as passages and not the file.

    Args:
        heading: the first line of the file's masked text, its masked name.
        chosen: the passages, in document order.
        capped_at: the number of passages indexed, when the file had more than that.
    """
    parts = [heading, PASSAGES_MARKER]
    previous: int | None = None
    for passage in chosen:
        if previous is not None and passage.position != previous + 1:
            parts.append(GAP_MARKER)
        parts.append(passage.text)
        previous = passage.position
    if capped_at is not None:
        parts.append(CAPPED_MARKER.format(count=capped_at))
    return "\n\n".join(parts)
