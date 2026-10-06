"""Locate scoring without a database: negation scope, completeness, variant merging, scope SQL."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.extraction.normalize_text import NEGATION_WORDS, is_negation
from app.platform.search import (
    LocateOutcome,
    RankedDocument,
    SearchScope,
    _Group,
    _merge_variants,
    _question_groups,
    _scope_sql,
    _score_passage,
    _term_forms,
    _word_forms,
)

# --- one negation vocabulary ---------------------------------------------------------------------


def test_negation_words_cover_the_shared_vocabulary():
    assert {"לא", "אין", "אינו", "אינה", "אינם", "אינן", "ללא", "בלי", "אל"} <= NEGATION_WORDS


@pytest.mark.parametrize("word", ["אין", "ואין", "שאין", "כשאין", "שלא", "ולא", "וללא", "ואינו", "שאינה",
                                  "בלי", "מבלי", "והיעדר", "בהיעדר"])
def test_is_negation_bare_and_after_a_conjunction_prefix(word):
    assert is_negation(word)


@pytest.mark.parametrize("word", ["מלא", "כלא", "הלא", "מאין", "לאן", "אינטרנט", "שאל", "ואל", "בית"])
def test_is_negation_rejects_words_that_only_contain_one(word):
    assert not is_negation(word)


def test_ambiguous_negation_counts_only_bare_and_only_when_asked():
    assert is_negation("אל") and not is_negation("אל", ambiguous=False)
    assert not is_negation("ואל") and not is_negation("שאל")


# --- negation must govern the topic term -------------------------------------------------------------

def _groups(*words: str, negation: bool = True) -> list[_Group]:
    groups = [_Group(w, i, forms=_term_forms(w), df=1, weight=1.0) for i, w in enumerate(words)]
    if negation:
        groups.append(_Group("אין", len(words), kind="negation", weight=1.0))
    return groups


def _row(text: str, kind: str = "text") -> SimpleNamespace:
    return SimpleNamespace(text=text, kind=kind)


def _negated(passage: str, term: str = "סוכה") -> bool:
    groups = _groups(term)
    p = _score_passage(_row(passage), groups, [], sum(g.weight for g in groups), ())
    return any(g.kind == "negation" for g in p.supported)


@pytest.mark.parametrize("passage", [
    "בבניין ללא סוכה",
    "הבניין אינו כולל סוכה",
    "בבניין אין סוכה משותפת.",
    "הבניין נבנה בשנת 1972 ואינו כולל סוכה.",
    "אין בו סוכה",
    "לא נמצאה סוכה בבניין",
    "סוכה: אין",
    "סוכה לא קיימת",
    "עקב היעדר סוכה בבניין",
])
def test_negation_governing_the_term_counts(passage):
    assert _negated(passage)


@pytest.mark.parametrize("passage", [
    "לבניין יש סוכה ואין גינה",
    "אין גינה, אך הבניין כולל סוכה",
    "אין גינה ויש סוכה",
    "יש סוכה אין גינה",
    "הסוכה מטופחת ומוארת. אין גינה.",
    "בבניין סוכה אחת וגינה משותפת לא מסומנת.",
    "סוכה שלא הוחלפה מאז הבנייה",
    "אין גינה או סוכה",
    "סוכה ללא גג",
    "מלא סוכה",
])
def test_negation_of_another_clause_or_term_does_not_count(passage):
    assert not _negated(passage)


@pytest.mark.parametrize("question,negated", [
    ("סוכה נבנתה בלי אישור", ("אישור",)),
    ("באילו שומות אין סוכה", ("סוכה",)),
    ("איפה מצוין שאין סוכה בכלל?", ("סוכה",)),
    ("סוכה לא קיימת", ("קיימת", "סוכה")),
])
def test_question_negation_knows_the_word_it_negates(question, negated):
    [negation] = [g for g in _question_groups(question, []) if g.kind == "negation"]
    assert negation.governs == negated


def test_passage_negation_must_govern_the_word_the_question_negates():
    """GQ57: "בלי היתר" negates the permit, a less distinctive word than "נסגרה"; the passage's "ללא היתר"
    counts, while a negation of an unrelated word does not."""
    groups = [g for g in _question_groups("סוכה נבנתה בלי אישור", []) if g.kind != "negation"]
    weights = {"סוכה": 0.5, "נבנתה": 3.0, "אישור": 1.0}
    for g in groups:
        g.forms, g.df, g.weight = _term_forms(g.word), 1, weights[g.word]
    [negation] = [g for g in _question_groups("סוכה נבנתה בלי אישור", []) if g.kind == "negation"]
    negation.weight = 1.5
    groups.append(negation)
    total = sum(g.weight for g in groups)
    yes = _score_passage(_row("הסוכה שבחזית נבנתה בתריסים ללא אישור בנייה."), groups, [], total, ())
    no = _score_passage(_row("הסוכה נבנתה ללא תריסים, עם אישור בנייה."), groups, [], total, ())
    assert yes.complete and negation in yes.supported
    assert negation not in no.supported and not no.complete


# --- completeness ------------------------------------------------------------------------------

def test_a_passage_is_never_complete_when_completion_is_impossible():
    groups = _groups("גינה", negation=False)
    assert _score_passage(_row("גינה גדולה"), groups, [], 1.0, ()).complete
    assert not _score_passage(_row("גינה גדולה"), groups, [], 1.0, (), completable=False).complete


def test_word_forms_are_cached():
    _word_forms.cache_clear()
    assert _word_forms("בגינות") is _word_forms("בגינות")
    assert _word_forms.cache_info().hits >= 1


# --- variants are alternatives: union of fully supporting passages -----------------------------

def _doc(version, score, chunks, full=True, matched=("a",)):
    return RankedDocument(document_id=version, version_id=version, title="ת", score=score, full_support=full,
                          matched_terms=list(matched), missing_terms=[],
                          pages=sorted({p for _, _, pages in chunks for p in pages}),
                          passages=[{"chunk_id": c, "score": s, "page_list": list(pages)} for c, s, pages in chunks])


def test_merge_keeps_the_passages_of_every_fully_supporting_variant():
    v = uuid4()
    one = LocateOutcome([_doc(v, 1.0, [("c12", 1.0, [1, 2])], matched=("a", "b"))], ["a", "b"])
    two = LocateOutcome([_doc(v, 1.25, [("c2", 1.25, [2]), ("c12", 0.9, [1, 2])], matched=("a", "c"))], ["a", "c"])
    [merged] = _merge_variants([one, two])
    assert merged.score == 1.25
    assert [p["chunk_id"] for p in merged.passages] == ["c2", "c12"]
    assert merged.passages[1]["score"] == 1.0  # the better score of a passage two variants share
    assert merged.pages == [1, 2] and merged.matched_terms == ["a", "c", "b"]


def test_merge_ignores_a_variant_that_only_partly_supports_a_document():
    v, w = uuid4(), uuid4()
    one = LocateOutcome([_doc(v, 1.0, [("c1", 1.0, [1])])], ["a"])
    two = LocateOutcome([_doc(v, 0.6, [("c9", 0.6, [9])], full=False), _doc(w, 1.0, [("c5", 1.0, [5])])], ["a"])
    merged = _merge_variants([one, two])
    assert [d.version_id for d in merged] == sorted([v, w], key=str)
    assert [p["chunk_id"] for p in next(d for d in merged if d.version_id == v).passages] == ["c1"]


# --- scope conditions: one rule for chunks and versions ----------------------------------------

def test_scope_sql_current_only_unless_named_versions_ask_for_older():
    assert _scope_sql(None).where == "v.is_current"
    v = uuid4()
    named = _scope_sql(SearchScope(version_ids=(v,)))
    assert named.where == "c.version_id = ANY(:scope_v) AND v.is_current" and named.params == {"scope_v": [v]}
    older = _scope_sql(SearchScope(version_ids=(v,), include_noncurrent=True), [v])
    assert "(v.is_current OR v.id = ANY(:scope_v))" in older.where and "c.version_id = ANY(:scope_allowed)" in older.where
    d = uuid4()
    assert _scope_sql(SearchScope(document_ids=(d,))).where == "v.is_current AND c.document_id = ANY(:scope_d)"
