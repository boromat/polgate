"""Cutting a masked document into passages and ranking them against a masked question."""

import numpy as np
import pytest

from polgate.retrieval.select import (
    CAPPED_MARKER,
    GAP_MARKER,
    PASSAGES_MARKER,
    Passage,
    choose,
    from_blob,
    has_stale_token,
    rank,
    render,
    split_passages,
    to_blob,
    tokens_in,
    unit,
    word_scores,
)


def _words(text: str) -> list[str]:
    return text.split()


def test_short_paragraphs_are_packed_up_to_the_target():
    text = "Jeden akapit.\n\nDrugi akapit.\n\n\nTrzeci akapit, nieco dłuższy od pozostałych."
    passages = split_passages(text, target_chars=50)
    assert passages == [
        "Jeden akapit.\n\nDrugi akapit.",
        "Trzeci akapit, nieco dłuższy od pozostałych.",
    ]


def test_a_long_paragraph_is_cut_between_sentences_and_keeps_every_word():
    paragraph = " ".join(f"Zdanie numer {i} opisuje warunki umowy." for i in range(40))
    passages = split_passages(paragraph, target_chars=200)
    assert all(len(p) <= 200 for p in passages)
    assert all(p.endswith(".") for p in passages)
    assert _words(" ".join(passages)) == _words(paragraph)


def test_a_sentence_longer_than_the_target_is_cut_where_the_target_falls():
    sentence = "x" * 450
    passages = split_passages(f"Krótkie. {sentence} Koniec.", target_chars=200)
    assert all(len(p) <= 200 for p in passages)
    assert "".join(passages).replace(" ", "") == f"Krótkie.{sentence}Koniec."


def test_a_target_that_is_not_positive_is_refused():
    with pytest.raises(ValueError, match="positive"):
        split_passages("tekst", 0)


def test_a_vector_survives_the_database_round_trip_at_unit_length():
    vector = unit([3.0, 4.0])
    assert np.allclose(vector, [0.6, 0.8])
    assert np.allclose(from_blob(to_blob(vector)), vector)
    assert np.allclose(unit([0.0, 0.0]), [0.0, 0.0])


def test_a_token_the_mapping_no_longer_has_makes_a_passage_stale():
    mapping = {"<OSOBA_1>": "Anna Lis", "<FIRMA_1>": "<FIRMA_1>"}
    assert not has_stale_token("Umowę podpisała <OSOBA_1> dla <FIRMA_1>.", mapping)
    assert has_stale_token("Umowę podpisała <OSOBA_2>.", mapping)
    assert not has_stale_token("Bez tokenów.", {})


def test_a_token_in_the_question_outweighs_words_in_common():
    texts = [
        "Wynagrodzenie zasadnicze pracownika działu sprzedaży wynosi 6200 zł.",
        "<OSOBA_1> pracuje w dziale kadr od marca.",
        "Regulamin pracy zdalnej obowiązuje od kwietnia.",
    ]
    scores = word_scores("Ile wynosi wynagrodzenie <OSOBA_1>?", texts)
    assert scores[1] > scores[0] > scores[2] == 0


def test_words_meet_across_polish_case_endings():
    scores = word_scores("Co mówi umowa o kaucji?", ["Zapisy umowy dotyczące kaucja", "Inne"])
    assert scores[0] > scores[1]


def test_a_question_with_no_terms_scores_nothing():
    assert word_scores("a i o?", ["cokolwiek"]).tolist() == [0.0]
    assert word_scores("umowa", []).tolist() == []


def _passages() -> list[Passage]:
    return [
        Passage(0, "Wstęp do regulaminu firmy.", unit([1.0, 0.0, 0.0])),
        Passage(1, "Kaucja wynosi 4600 zł i jest zwrotna.", unit([0.0, 1.0, 0.0])),
        Passage(2, "<OSOBA_1> odpowiada za klucze.", unit([0.0, 0.0, 1.0])),
    ]


def test_each_mode_ranks_by_its_own_evidence():
    passages = _passages()
    question = "Kto ma klucze, <OSOBA_1>?"
    near_kaucja = unit([0.1, 1.0, 0.0])
    assert rank(question, near_kaucja, passages, mode="vector")[0] == 1
    assert rank(question, None, passages, mode="words")[0] == 2
    assert rank(question, near_kaucja, passages, mode="hybrid")[:2] in ([1, 2], [2, 1])


def test_ties_keep_the_order_of_the_document():
    passages = [Passage(i, "bez związku", unit([1.0, 0.0])) for i in range(3)]
    assert rank("pytanie", unit([0.0, 1.0]), passages) == [0, 1, 2]
    assert rank("pytanie", unit([0.0, 1.0]), []) == []


def test_ranking_by_vectors_needs_every_vector():
    with pytest.raises(ValueError, match="needs a vector"):
        rank("pytanie", None, _passages(), mode="vector")
    with pytest.raises(ValueError, match="needs a vector"):
        rank("pytanie", unit([1.0, 0, 0]), [Passage(0, "x")], mode="hybrid")


def test_the_best_passages_that_fit_come_back_in_document_order():
    passages = [Passage(i, "x" * 100) for i in range(5)]
    chosen = choose(passages, order=[3, 0, 4, 1], top_k=3, budget_chars=250)
    assert [p.position for p in chosen] == [0, 3]


def test_the_best_passage_is_cut_rather_than_dropped_when_it_alone_is_too_long():
    chosen = choose([Passage(0, "y" * 500)], order=[0], top_k=2, budget_chars=120)
    assert [len(p.text) for p in chosen] == [120]


def test_the_model_is_told_it_reads_passages_and_where_they_skip():
    chosen = [Passage(0, "pierwszy"), Passage(1, "drugi"), Passage(5, "szósty")]
    text = render("<OSOBA_1> umowa.pdf", chosen)
    assert text.splitlines()[0] == "<OSOBA_1> umowa.pdf"
    assert PASSAGES_MARKER in text
    assert text.count(GAP_MARKER) == 1
    assert text.index("drugi") < text.index(GAP_MARKER) < text.index("szósty")
    assert "przeszukano" not in text


def test_the_model_is_told_when_only_part_of_the_file_was_searched():
    text = render("plik.txt", [Passage(0, "tekst")], capped_at=500)
    assert text.endswith(CAPPED_MARKER.format(count=500))


def test_a_long_row_is_cut_between_words_and_never_inside_a_token():
    """A table row cut where the target fell gave "<OSO" and "BA_12>", and "120" and "0"."""
    row = " ".join(f"<OSOBA_{i}> 1200 zł <EMAIL_{i}>" for i in range(1, 40))
    passages = split_passages(row, target_chars=60)
    tokens = [t for p in passages for t in tokens_in(p)]
    assert tokens == tokens_in(row)
    assert all(len(p) <= 60 for p in passages)
    assert all("1200" in p.split() or "1200" not in p for p in passages)
    assert " ".join(passages).split() == row.split()


def test_a_cut_that_would_split_a_token_moves_to_its_start():
    text = "x" * 50 + "<OSOBA_12>" + "y" * 50
    passages = split_passages(text, target_chars=55)
    assert "<OSOBA_12>" in "".join(passages)
    assert all(p.count("<") == p.count(">") for p in passages)
