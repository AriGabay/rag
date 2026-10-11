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

Beside the answer checks, the other layers' pure checks live here too, so a rescore regrades them from stored
evidence: ``structure_check`` (a value in a named table row and column, or in a paragraph under a heading),
``retrieval_check`` (every required text among the top passages) and ``calc_check`` (a calculation result shown
at a stated precision).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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
    # a cited listing (a count or a list over a set) names its documents: they are what the answer is about
    listed = {d for s in answer.get("sources") or [] if s.get("kind") == "listing"
              for d in s.get("listed_document_ids") or []}
    cited |= {m.get("title") or "" for m in (answer.get("ledger") or {}).get("matching") or []
              if m.get("document_id") in listed}
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
        # a membership answer (a count from a listing) says that the documents' content was not read
        partial_data = bool(ledger.get("retrieved_only")) or bool(ledger.get("membership")) or (
            ledger.get("scope_kind") == "set" and any(t.get("presented", 0) < (t.get("rows") or 0)
                                                    for t in ledger.get("tables") or []))
        if ledger.get("complete") is False and not noted:
            problems.append("הכיסוי חלקי אבל התשובה אינה אומרת זאת")
        if ledger.get("complete") is True and noted and not partial_data:
            problems.append("הכיסוי מלא אבל התשובה מסויגת כחלקית")
    return problems


# --- ingestion: a value where the document puts it ---------------------------------------------------------------

def _key(s) -> str:
    return re.sub(r"\s+", " ", norm(str(s or ""))).strip()


def _has(hay, needle) -> bool:
    return _key(needle) in _key(hay)


def _value_in(text, value) -> bool:
    """A number is matched in any written form (9,500 and 9500); anything else as text."""
    forms = numbers_in(str(value))
    return bool(forms & numbers_in(norm(str(text or "")))) if forms else _has(text, value)


def _lines(x) -> list[str]:
    return [x] if isinstance(x, str) else list(x or [])


def _rows(block: dict) -> list[tuple[int | None, list[str]]]:
    """A table block's rows as (page, cells): a row's own page when it has one, else the table's."""
    out = []
    for r in (block.get("table") or {}).get("rows") or []:
        page, cells = (r.get("page"), r.get("cells")) if isinstance(r, dict) else (None, r)
        out.append((page if page is not None else block.get("page"), [str(c or "") for c in cells or []]))
    return out


def _column(table: dict, rows: list, column: str | None) -> tuple[int | None, str, list] | None:
    """The named column's index and header, and the data rows under it: from the table's headers, or from a
    header row read into the body (OCR and pictures often have one); None when no header names it."""
    if not column:
        return None, "", rows
    for i, h in enumerate(table.get("headers") or []):
        if _has(h, column):
            return i, str(h), rows
    for n, (_, cells) in enumerate(rows[:3]):
        for i, h in enumerate(cells):
            if _has(h, column):
                return i, h, rows[n + 1:]
    return None


def _label(cells: list[str], skip: int | None) -> str:
    return " | ".join(c for j, c in enumerate(cells) if j != skip and c.strip())[:80]


def _table_check(blocks: list[dict], item: dict) -> list[str]:
    value, unit, page = item["value"], item.get("unit"), item.get("page")
    tables = [b for b in blocks if b.get("table")]
    if item.get("table"):
        tables = [b for b in tables if _has(" ".join([*_lines(b["table"].get("caption")), *_lines(b["table"].get(
            "title")), b.get("section") or "", *(b.get("section_path") or [])]), item["table"])]
    if not tables:
        return [f"לא נמצאה טבלה «{item['table']}»" if item.get("table") else "לא נמצאה טבלה במסמך"]
    # the closest miss over the candidate tables: 0 page or unit, 1 another value in the named cell, 2 no such
    # row, 3 no such column
    best: tuple[int, str] = (9, "")
    found_at: list[str] = []
    for b in tables:
        t = b["table"]
        rows = _rows(b)
        col = _column(t, rows, item.get("column"))
        for _, cells in rows:
            for j, c in enumerate(cells):
                if _value_in(c, value):
                    found_at.append(f"«{_label(cells, j)}»")
        if col is None:
            best = min(best, (3, f"לא נמצאה עמודה «{item['column']}»"))
            continue
        ci, header, data = col
        row = item.get("row")
        named = [(p, cs) for p, cs in data if not row or any(_key(c) == _key(row) for j, c in enumerate(cs) if j != ci)]
        if row and not named:
            named = [(p, cs) for p, cs in data if any(_has(c, row) for j, c in enumerate(cs) if j != ci)]
        if not named:
            best = min(best, (2, f"לא נמצאה שורה «{row}»"))
            continue
        for p, cs in named:
            cell = cs[ci] if ci is not None and ci < len(cs) else " ".join(cs)
            if not _value_in(cell, value):
                best = min(best, (1, f"בתא שצוין ({_label(cs, ci)} / {header or 'כל השורה'}) נקרא «{cell}», לא {value}"))
                continue
            units = list(t.get("units") or [])
            places = [cell, header, units[ci] if ci is not None and ci < len(units) else "", *cs,
                      *_lines(t.get("caption")), *_lines(t.get("title"))]
            if unit and not any(_has(x, unit) for x in places if x):
                best = min(best, (0, f"הערך {value} נמצא בשורה ובעמודה שצוינו, בלי היחידה «{unit}»"))
                continue
            if page is not None and p != page:
                best = min(best, (0, f"הערך {value} נמצא בשורה ובעמודה שצוינו בעמוד {p}, לא בעמוד {page}"))
                continue
            return []
    problems = [best[1]]
    if best[0] >= 1 and found_at:
        problems.append(f"הערך {value} נמצא בשורה " + ", ".join(found_at[:4]))
    return problems


def _paragraph_check(blocks: list[dict], item: dict) -> list[str]:
    value, unit, page, heading, near = (item["value"], item.get("unit"), item.get("page"), item.get("heading"),
                                        item.get("near"))
    texts = [b for b in blocks if not b.get("table")
             and (not heading or _has(" ".join([b.get("section") or "", *(b.get("section_path") or [])]), heading))]
    hits = [b for b in texts if _value_in(b.get("text"), value) and (not near or _has(b.get("text"), near))
            and (not unit or _has(b.get("text"), unit))]
    if hits and (page is None or any(b.get("page") == page for b in hits)):
        return []
    where = f" תחת «{heading}»" if heading else ""
    if hits:
        return [f"הערך {value}{where} נמצא בעמוד {hits[0].get('page')}, לא בעמוד {page}"]
    return [f"הערך {value}" + (f" ליד «{near}»" if near else "") + (f" עם «{unit}»" if unit else "")
            + f" לא נמצא בטקסט{where}"]


def structure_check(blocks: list[dict], item: dict) -> list[str]:
    """An ingestion check against the document's structure (``/blocks``): with ``row`` or ``column``, ``value``
    must sit in that row and column of a table (``table``: a caption, title or heading fragment; a number in
    another row or column fails), else in a text block under ``heading`` (with ``near`` in the same block).
    ``unit`` must be read with it, and ``page``, when given, is the row's (the block's) page."""
    if item.get("row") or item.get("column"):
        return _table_check(blocks, item)
    return _paragraph_check(blocks, item)


# --- retrieval: every required passage ----------------------------------------------------------------------------

def retrieval_check(texts: list[str], item: dict) -> tuple[list[str], dict]:
    """``expect_all``: every text must be among the top ``k`` passages (``expect``: one text). Returns the
    problems and each required text's rank (None when missing)."""
    top = [_key(t) for t in texts[:item.get("k", 8)]]
    required = list(item.get("expect_all") or []) + ([item["expect"]] if item.get("expect") else [])
    ranks = {t: next((i + 1 for i, x in enumerate(top) if _key(t) in x), None) for t in required}
    problems = [f"לא בתוצאות הראשונות: {t}" for t, r in ranks.items() if r is None]
    found = [r for r in ranks.values() if r is not None]
    rank = ranks[item["expect"]] if item.get("expect") else (max(found) if found and not problems else None)
    return problems, {"rank": rank, "ranks": ranks}


# --- calculation: a result shown at the stated precision ----------------------------------------------------------

_SHOWN = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_CITE_ID = re.compile(r"\[[A-Z]+\d+\]")


def _plain(x) -> str:
    """A number as written, without thousands separators; a YAML float in positional notation."""
    s = format(Decimal(repr(x)), "f") if isinstance(x, float) else str(x)
    return s.replace(",", "").strip()


def _places(x) -> int:
    s = _plain(x)
    return len(s.split(".", 1)[1]) if "." in s else 0


def _round(d: Decimal, places: int) -> Decimal:
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def shown_at_precision(shown: str, expected, precision: int) -> bool:
    """A displayed number is the expected result when it shows at least ``precision`` decimals, equals the result
    rounded to ``precision``, and every further decimal it shows agrees with the result as far as the result is
    known (8.87 and 8.872 for 8.8719…, not 8.9 nor 8.874). The sign is not compared: answers write a loss in
    words as often as with a minus."""
    try:
        x, e = abs(Decimal(_plain(shown))), abs(Decimal(_plain(expected)))
    except InvalidOperation:
        return False
    d = _places(shown)
    if d < precision or _round(x, precision) != _round(e, precision):
        return False
    return _places(expected) < d or x == _round(e, d)


def displayed_numbers(markdown: str) -> list[str]:
    return _SHOWN.findall(_CITE_ID.sub(" ", norm(markdown)))


def calc_check(answer: dict | None, expect: dict) -> list[str]:
    """``calc: [{value, precision}]``: each result must be shown at its precision (default: the decimals the
    value is written with)."""
    shown = displayed_numbers((answer or {}).get("markdown") or "")
    problems = []
    for c in expect.get("calc") or []:
        value = c["value"]
        p = c.get("precision")
        p = _places(value) if p is None else int(p)
        if not any(shown_at_precision(s, value, p) for s in shown):
            problems.append(f"תוצאת החישוב {_plain(value)} (בדיוק של {p} ספרות אחרי הנקודה) לא מוצגת"
                            + (f"; מספרים בתשובה: {', '.join(shown[:8])}" if shown else ""))
    return problems
