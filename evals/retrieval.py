"""Measure whether the passage that answers a question reaches the model, searched or cut.

``retrieval.json`` holds long documents, masked the way the hub stores an attachment, and questions
whose answer lies past the attachment budget: cut to the budget, as the hub did before the search
in attachments, the file never shows the model the answer. For every question this script ranks the
document's passages with the hub's own code (``polgate.retrieval.select``) in three ways, by
vectors alone, by words alone and by both, and counts:

* **hit@1** and **hit@3**: the passage holding the answer is first, or among the first three;
* **reaches the model**: the answer is in the text the hub would send, the best ``top_k`` passages
  that fit the budget, the way a turn chooses them.

The cut file is scored on the last count alone, by whether the answer falls inside the budget.

By default the vectors come from a stand-in model (``bag_of_words.py``) that only sees words in
common, so the vector column says little about a real model. To measure one, start an
OpenAI-compatible embeddings endpoint, such as LM Studio with bge-m3 loaded, and point the script
at it::

    python evals/retrieval.py                          # stand-in vectors, no network
    python evals/retrieval.py --embeddings-url http://localhost:1234/v1 --embeddings-model bge-m3
    python evals/retrieval.py --json out.json          # also write every question's result

LM Studio lists the exact model name under its loaded models (it may read, for example,
``text-embedding-bge-m3``); ``--embeddings-model`` takes that name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bag_of_words import vector as stand_in

from polgate.retrieval.select import Mode

DATASET = Path(__file__).resolve().parent / "retrieval.json"
MODES: tuple[Mode, ...] = ("words", "vector", "hybrid")


@dataclass
class Score:
    """One question's result in one way of choosing what reaches the model."""

    document: str
    question: str
    mode: str
    hit_at_1: bool
    hit_at_3: bool
    reaches_model: bool


def load(path: Path = DATASET) -> dict[str, Any]:
    """Read the dataset and check every answer is in its document, past the budget, in one passage.

    Raises:
        ValueError: when an answer is missing, inside the budget or split between two passages,
            any of which would make the question measure nothing.
    """
    from polgate.retrieval.select import split_passages

    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for document in data["documents"]:
        text = _text(document)
        passages = split_passages(_body(document), data["chunk_chars"])
        for item in document["questions"]:
            answer = item["answer"]
            if answer not in text:
                raise ValueError(f"{document['id']}: {answer!r} is not in the document")
            if text.index(answer) < data["budget_chars"]:
                raise ValueError(f"{document['id']}: {answer!r} is inside the budget")
            if not any(answer in passage for passage in passages):
                raise ValueError(f"{document['id']}: {answer!r} is split between passages")
    return data


def _body(document: dict[str, Any]) -> str:
    return "\n\n".join(document["paragraphs"])


def _text(document: dict[str, Any]) -> str:
    """The document as the hub stores it: its masked name, then its masked text."""
    return f"{document['heading']}\n\n{_body(document)}"


async def _vectors(texts: list[str], args: argparse.Namespace) -> list[list[float]]:
    """Embed with the endpoint given, or the stand-in model when there is none."""
    if not args.embeddings_url:
        return [stand_in(text) for text in texts]
    from polgate.llm.openai_compat import embed

    vectors: list[list[float]] = []
    for start in range(0, len(texts), 64):
        vectors += await embed(
            texts[start : start + 64],
            model=args.embeddings_model,
            base_url=args.embeddings_url,
            api_key=args.embeddings_key,
        )
    return vectors


async def score(data: dict[str, Any], args: argparse.Namespace) -> list[Score]:
    """Rank every document's passages for each of its questions, in every mode."""
    from polgate.retrieval.select import Passage, choose, rank, render, split_passages, unit

    budget, top_k = data["budget_chars"], data["top_k"]
    scores = []
    for document in data["documents"]:
        texts = split_passages(_body(document), data["chunk_chars"])
        vectors = await _vectors(texts, args)
        passages = [
            Passage(i, t, unit(v)) for i, (t, v) in enumerate(zip(texts, vectors, strict=True))
        ]
        questions = [item["question"] for item in document["questions"]]
        question_vectors = await _vectors(questions, args)
        cut = _text(document)[:budget]
        for item, question_vector in zip(document["questions"], question_vectors, strict=True):
            answer = item["answer"]
            scores.append(
                Score(document["id"], item["question"], "cut", False, False, answer in cut)
            )
            for mode in MODES:
                order = rank(item["question"], unit(question_vector), passages, mode)
                holding = [i for i in order if answer in passages[i].text]
                first = order.index(holding[0])
                sent = render(document["heading"], choose(passages, order, top_k, budget))
                scores.append(
                    Score(
                        document["id"],
                        item["question"],
                        mode,
                        first == 0,
                        first < 3,
                        answer in sent,
                    )
                )
    return scores


def report(scores: Sequence[Score], data: dict[str, Any], model: str) -> str:
    """Return one line per way of choosing, with the three counts out of all questions."""
    total = sum(len(d["questions"]) for d in data["documents"])
    lines = [
        f"embeddings: {model}",
        f"documents {len(data['documents'])}, questions {total}, budget {data['budget_chars']} "
        f"characters, passages of {data['chunk_chars']}, top_k {data['top_k']}",
        "",
        f"{'':22}{'hit@1':>8}{'hit@3':>8}{'reaches the model':>20}",
    ]
    for mode, label in (("cut", "cut to the budget"), *((m, f"search: {m}") for m in MODES)):
        rows = [s for s in scores if s.mode == mode]
        hit1 = "-" if mode == "cut" else f"{sum(s.hit_at_1 for s in rows)}/{total}"
        hit3 = "-" if mode == "cut" else f"{sum(s.hit_at_3 for s in rows)}/{total}"
        reach = f"{sum(s.reaches_model for s in rows)}/{total}"
        lines.append(f"{label:22}{hit1:>8}{hit3:>8}{reach:>20}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scoring and print the table."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--embeddings-url", default="", help="OpenAI-compatible API root")
    parser.add_argument("--embeddings-model", default="bge-m3", help="model name at that endpoint")
    parser.add_argument("--embeddings-key", default="", help="key for that endpoint, if any")
    parser.add_argument("--json", type=Path, help="also write every result to this file")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    data = load()
    scores = asyncio.run(score(data, args))
    model = args.embeddings_model if args.embeddings_url else "stand-in bag of words"
    print(report(scores, data, model))
    if args.json:
        args.json.write_text(
            json.dumps([asdict(s) for s in scores], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
