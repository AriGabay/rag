"""Structured checks of an answer, beside the regular-expression checks of ``chat_eval``.

The regular expressions stay as a regression layer, but an answer can contain every expected phrase and still
leave out a document, change a unit, or show one value where the question asked about a set. These checks read
the answer's payload — its sentences with the sources each cites, and the server's coverage ledger — and fail
such answers:

- ``required_documents`` / ``forbidden_documents``: title fragments of documents the answer must (must not) cite;
- ``value_set``: values that must all appear; or, with ``example_count``, one of them presented explicitly as an
  example together with the size of the set;
- ``value_meaning``: for a value, words that must (must not) stand in a sentence stating it (unit, period, VAT);
- ``attribution``: a value must be stated in a sentence that cites the given document (and not with words of
  another role);
- ``coverage``: whether the ledger must be complete, documents it must list as not covered, and
  (``expect_all_rows``) whether every cited table must be presented in full;
- ``absence``: the requested datum is not in the documents — the answer must say so first (its first statement),
  and each of ``near_values`` (a nearby datum) may appear only labelled as another datum ("נתון אחר");
- hedging (always, when the answer has a ledger): an incomplete ledger needs the coverage note in the answer,
  and a complete one must not carry it.

Pure functions over the stored payload, so stored results can be rescored with new expectations.
"""

from __future__ import annotations

import re

from app.answering.verify import numbers_in
from app.chat.verify import split_units

EXAMPLE_WORDS = r"לדוגמה|לדוגמא|למשל|דוגמה|דוגמא|כגון"
ABSENT_WORDS = r"לא נמצא|לא מופיע|אינו מופיע|אינה מופיעה|לא צוין|לא מצוין|לא נכתב|אין (?:נתון|אזכור|מידע)"
OTHER_DATUM = r"נתון אחר|אינו .{0,30}המבוקש|ולא .{0,30}המבוקש|במקום"
NOTE_PATTERN = r"\*\*כיסוי:\*\*|\*\*שימו לב:\*\*"


def norm(s: str) -> str:
    return (s or "").replace("״", '"').replace("׳", "'").replace("–", "-").replace("‏", "")


def _forms(value) -> set[str]:
    """The normalized forms of a number (9,500 and 9500 are the same)."""
    return numbers_in(str(value)) or {str(value)}


def sentences(answer: dict) -> list[tuple[str, set[str]]]:
    """Each statement of the answer with the titles of the documents its citations point to."""
    title_of = {s.get("id"): s.get("title") or "" for s in answer.get("sources") or []}
    title_of |= {m.get("id"): m.get("title") or "" for m in answer.get("measurements") or []}
    out = []
    for u in split_units(answer.get("markdown") or ""):
        out.append((norm(u.text), {title_of[i] for i in u.ids if i in title_of}))
    return out


def _with_value(sents: list[tuple[str, set[str]]], value) -> list[tuple[str, set[str]]]:
    forms = _forms(value)
    return [(t, d) for t, d in sents if forms & numbers_in(t)]


def check(answer: dict | None, expect: dict) -> list[str]:
    """``structured_unless_status``: answer statuses (e.g. a clarification) for which these checks do not apply."""
    answer = answer or {}
    if answer.get("status") in expect.get("structured_unless_status", []):
        return []
    md = norm(answer.get("markdown") or "")
    sents = sentences(answer)
    cited = {t for _, ds in sents for t in ds} | {s.get("title") or "" for s in answer.get("sources") or []}
    problems: list[str] = []

    for needle in expect.get("required_documents", []):
        if not any(needle in t for t in cited):
            problems.append(f"אין ציטוט מהמסמך '{needle}'")
    for needle in expect.get("forbidden_documents", []):
        if any(needle in t for t in cited):
            problems.append(f"ציטוט ממסמך אסור '{needle}'")

    vs = expect.get("value_set")
    if vs:
        in_answer = numbers_in(md)
        present = [v for v in vs["values"] if _forms(v) & in_answer]
        missing = [v for v in vs["values"] if v not in present]
        count = vs.get("example_count")
        as_example = bool(count) and present and re.search(EXAMPLE_WORDS, md) and _forms(count) & in_answer
        if missing and not as_example:
            problems.append(f"קבוצת ערכים חסרה: {len(present)} מתוך {len(vs['values'])} הוצגו, בלי לומר שזו דוגמה "
                            f"ומה היקף הקבוצה (חסרים: {', '.join(map(str, missing[:5]))})")

    for vm in expect.get("value_meaning", []):
        hits = _with_value(sents, vm["value"])
        if not hits:
            problems.append(f"הערך {vm['value']} לא מופיע בתשובה")
            continue
        ok = [t for t, _ in hits if all(re.search(norm(p), t) for p in vm.get("must_near", []))
              and not any(re.search(norm(p), t) for p in vm.get("must_not_near", []))]
        if not ok:
            problems.append(f"משמעות הערך {vm['value']} שגויה או חסרה: «{hits[0][0][:160]}»")

    ab = expect.get("absence")
    if ab:
        if not sents or not re.search(ABSENT_WORDS, sents[0][0]):
            problems.append("התשובה אינה פותחת בכך שהנתון המבוקש לא נמצא: «" + (sents[0][0][:160] if sents else "")
                            + "»")
        for v in ab.get("near_values", []):
            for t, _ in _with_value(sents, v):
                if not re.search(OTHER_DATUM, t):
                    problems.append(f"נתון קרוב {v} מוצג בלי לומר שהוא נתון אחר: «{t[:160]}»")
                    break

    for at in expect.get("attribution", []):
        hits = _with_value(sents, at["value"])
        good = [t for t, ds in hits if any(at["document"] in d for d in ds)
                and not any(re.search(norm(p), t) for p in at.get("must_not_near", []))]
        if not good:
            problems.append(f"הערך {at['value']} אינו מיוחס למסמך '{at['document']}'"
                            + (f": «{hits[0][0][:160]}»" if hits else " (לא מופיע)"))

    ledger = answer.get("ledger")
    cov = expect.get("coverage")
    if cov:
        if not ledger:
            problems.append("אין רישום כיסוי לתשובה")
        else:
            if "expect_complete" in cov and bool(ledger.get("complete")) != cov["expect_complete"]:
                problems.append(f"כיסוי {'מלא' if ledger.get('complete') else 'חלקי'} (צפוי "
                                f"{'מלא' if cov['expect_complete'] else 'חלקי'})")
            listed = {d.get("title") or "" for k in ("not_checked", "unused", "also_matching")
                      for d in ledger.get(k) or []}
            for needle in cov.get("must_list_unchecked", []):
                if not any(needle in t for t in listed):
                    problems.append(f"הכיסוי אינו מציין שהמסמך '{needle}' לא נבדק")
            # data coverage, apart from document coverage: every cited table presented row by row
            if cov.get("expect_all_rows"):
                for t in ledger.get("tables") or []:
                    if t.get("presented", 0) < (t.get("rows") or 0):
                        problems.append(f"מהטבלה ב'{t.get('title')}' הוצגו {t.get('presented')} מתוך {t.get('rows')} שורות")
    if ledger:
        noted = bool(re.search(NOTE_PATTERN, md))
        # a note is due when documents were not covered, or were covered from retrieved passages only, or a set
        # answer presented part of a table
        partial_data = bool(ledger.get("retrieved_only")) or (ledger.get("scope_kind") == "set" and any(
            t.get("presented", 0) < (t.get("rows") or 0) for t in ledger.get("tables") or []))
        if ledger.get("complete") is False and not noted:
            problems.append("הכיסוי חלקי אבל התשובה אינה אומרת זאת")
        if ledger.get("complete") is True and noted and not partial_data:
            problems.append("הכיסוי מלא אבל התשובה מסויגת כחלקית")
    return problems
