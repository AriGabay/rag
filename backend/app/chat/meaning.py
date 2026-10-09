"""Does each number an answer presents keep the meaning its evidence gives it?

For a number in an answer unit that cites sources, the qualifiers its evidence attaches to it are read in the
same way the VAT check reads VAT:

- **area basis** as written (מ״ר אקוו׳, פלדלת, ברוטו, נטו, עיקרי, רשום, מ״ר בנוי);
- **period** (לחודש / לשנה);
- **approximation** ("כ-21,000", "בערך").

A qualifier belongs to the nearest amount (a number written with ₪, מ״ר, דונם or %) before it in its clause, or,
with none before it, to the first amount after it; in a clause without amounts, to the nearest number before it.
In "השווי למ״ר אקוו׳ לנכס ברחוב הצאלון 7 נקבע ל-14,250 ₪" the basis is the 14,250's, not the house number's; in
"63 ₪ למ״ר לחודש" the period is the 63's. In a table row it also comes from the row's label cell and the column's header. A source states a
qualifier for all its numbers when it says it in a clause with no number, or in a table's title, caption or notes.

Two kinds of problem:

- **missing** — the evidence for the number (a cited measurement with that value, or the source passages that
  state it, the ones closest in wording to the unit) gives it a basis, period or approximation, and the unit says
  none of that kind anywhere. The number keeps a meaning only with it: "השווי למ״ר" without "אקוו׳" reads as
  another value. The unit is still judged; the repair round asks for the qualifier, and when exactly one is
  attested and the unit is otherwise supported, the server finally writes it next to the number, marked as
  taken from the source.
- **unsupported** — the unit gives the number a basis or period its evidence does not give it, or contradicts
  it ("לחודש" where the source says nothing about a period, or says "לשנה"). Nothing is added that the evidence
  lacks; unknown stays unknown.

Only the turn's own evidence is read: cited passages and measurements. No answer field is fixed in advance; the
model still writes the answer.

In a file holding several appraisals (round 7 U6, KTD7, R20), the subject property's stored measurements a check
reads (``Fetcher.subject_measurements``) are, when the context checks are enforced, only those of the appraisal
context the unit's cited evidence is in: another appraisal's, or an appendix's comparison property's, figure never
attests a number of this one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from types import SimpleNamespace
from typing import TYPE_CHECKING

from app.answering.verify import _NUMBER as _NUM_AT
from app.answering.verify import numbers_in
from app.chat.evidence import _content_words
from app.measurements.extract import PERIOD_LABELS

if TYPE_CHECKING:
    from app.chat.tools import Workspace
    from app.chat.verify import Unit

# (key, pattern) — the key compares spellings; the matched text is the qualifier as written
_BASIS = [
    # אקוו׳, אקווי׳, אקוי׳, אקו׳, אקוו, אקווי, אקוי, אקוויוולנטי(ים), אקוולנטי: one basis however it is spelt
    ("אקוו", r"(?:מ\"ר\s*)?אקו(?:ו?י?ו?ולנטי(?:ים)?|ו?י?'|ו?י|ו)(?![א-ת])"),
    ("פלדלת", r"(?:מ\"ר\s*)?(?<![א-ת])[ולב]?פלדלת"),
    ("ברוטו", r"(?:מ\"ר\s*)?(?<![א-ת])[ו]?ברוטו(?![א-ת])"),
    ("נטו", r"(?:מ\"ר\s*)?(?<![א-ת])[ו]?נטו(?![א-ת])"),
    ("עיקרי", r"(?:מ\"ר\s*|שטח\s*)?(?<![א-ת])עיקרי(?![א-ת])"),
    ("רשום", r"(?:מ\"ר\s*|שטח\s*)(?<![א-ת])רשום(?![א-ת])"),
    ("בנוי", r"מ\"ר\s+בנוי(?![א-ת])"),
]
_PERIOD = [
    # "לחודש", "חודשי"; not "לפני שנה" or "12 חודשים" (a duration, not a period)
    ("month", r"(?:(?<![א-ת])[לב]|/\s?)חודש(?![א-ת])|(?<![א-ת])(?:ו?ה?חודשי(?:ת)?|ו?החודשי(?:ים|ות)|לח'|לחו')(?![א-ת])"),
    ("year", r"(?:(?<![א-ת])[לב]|/\s?)שנה(?![א-ת])|(?<![א-ת])(?:ו?ה?שנתי(?:ת)?|ו?השנתי(?:ים|ות))(?![א-ת])"),
]
# "כ-" right before a number; not the כ that ends an abbreviation (סה"כ)
_APPROX_BEFORE = re.compile(r"(?:(?<![א-ת\"'])[בו]?כ[-־–]?\s?|בערך\s|בסביבות\s|כמעט\s)$")
_AMOUNT_AFTER = re.compile(r"\s*(?:₪|ש\"ח|מ\"ר|דונם|%|אלף|מיליון)")
_APPROX_WORDS = re.compile(r"(?<![א-ת])(?:מקורב|בקירוב|בערך|בסביבות)(?![א-ת])")
_CLAUSE_END = re.compile(r"[.;\n](?!\d)")
_ROW = " | "

KIND_LABELS = {"basis": "בסיס השטח", "period": "התקופה", "approx": "היותו ערך מקורב"}
PERIOD_TEXT = {k: PERIOD_LABELS[k] for k in ("month", "year")}
MEASUREMENT_PERIODS = set(PERIOD_TEXT)


def display(written: str) -> str:
    """A qualifier as the answer shows it, with Hebrew abbreviation marks."""
    return written.replace('"', "״").replace("'", "׳")


def _norm(text: str) -> str:
    """One spelling of the Hebrew abbreviation marks (same length, so positions stay valid)."""
    return (text or "").replace("״", '"').replace("׳", "'").replace("”", '"').replace("’", "'")


@dataclass
class Qualifiers:
    """kind -> {key: as written}"""

    found: dict[str, dict[str, str]] = field(default_factory=dict)

    def add(self, kind: str, key: str, written: str) -> None:
        self.found.setdefault(kind, {}).setdefault(key, written.strip())

    def merge(self, other: Qualifiers) -> Qualifiers:
        for kind, values in other.found.items():
            for key, written in values.items():
                self.add(kind, key, written)
        return self

    def keys(self, kind: str) -> set[str]:
        return set(self.found.get(kind, {}))


def _scan(text: str) -> list[tuple[int, str, str, str]]:
    """Every qualifier in the text, in reading order: (position, kind, key, as written)."""
    out = []
    for kind, table in (("basis", _BASIS), ("period", _PERIOD)):
        for key, pattern in table:
            for m in re.finditer(pattern, text):
                out.append((m.start(), kind, key, m.group(0)))
    return sorted(out)


def _numbers(text: str) -> list[tuple[int, int, frozenset[str]]]:
    return [(m.start(), m.end(), frozenset(numbers_in(m.group(0)))) for m in _NUM_AT.finditer(text)]


def _owner(nums: list, amounts: list[int], pos: int, clause: str) -> int:
    """The number a qualifier at ``pos`` belongs to: the nearest amount before it, else the first amount after
    it — unless a comma separates it from the amount before and not from the one after ("בשטח 120 מ"ר, דמי
    השכירות לחודש 7,560 ₪": the period is the 7,560's); in a clause without amounts, the nearest number before
    it, else the first."""
    if amounts:
        before = [i for i in amounts if nums[i][0] < pos]
        after = [i for i in amounts if nums[i][0] > pos]
        if before and after and ", " in clause[nums[before[-1]][1]:pos] and ", " not in clause[pos:nums[after[0]][0]]:
            return after[0]
        return before[-1] if before else (after[0] if after else amounts[-1])
    before = [i for i, (a, _, _) in enumerate(nums) if a < pos]
    return before[-1] if before else 0


def attached(text: str) -> tuple[list[tuple[frozenset[str], int, Qualifiers]], Qualifiers]:
    """For each number in the text, its forms, position and the qualifiers attached to it; and the qualifiers of
    clauses with no number (they cover the whole text)."""
    text = _norm(text)
    out: list[tuple[frozenset[str], int, Qualifiers]] = []
    general = Qualifiers()
    start = 0
    for end_m in [*_CLAUSE_END.finditer(text), None]:
        end = end_m.end() if end_m else len(text)
        clause = text[start:end]
        nums = _numbers(clause)
        quals = [Qualifiers() for _ in nums]
        amounts = [i for i, (_, b, _) in enumerate(nums) if _AMOUNT_AFTER.match(clause, b)]
        for pos, kind, key, written in _scan(clause):
            if not nums:
                general.add(kind, key, written)
                continue
            quals[_owner(nums, amounts, pos, clause)].add(kind, key, written)
        for i, (a, _, forms) in enumerate(nums):
            if _APPROX_BEFORE.search(clause[max(0, a - 12):a]):
                quals[i].add("approx", "approx", "מקורב")
            out.append((forms, start + a, quals[i]))
        start = end
    return out, general


def _all(text: str) -> Qualifiers:
    q = Qualifiers()
    for _, kind, key, written in _scan(_norm(text)):
        q.add(kind, key, written)
    return q


@dataclass
class Occurrence:
    """One place the evidence states the number, with what it attaches to it there."""

    qualifiers: Qualifiers
    context: str  # the words around it, to choose the occurrence closest to the unit
    measurement: bool = False
    area: bool = False  # its row or column names it a per-area amount, or its row label or cell an area

    @cached_property
    def context_words(self) -> set[str]:
        return _content_words(self.context)


def _row_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


# a per-area amount: its row or column says "למ״ר", "/מ״ר", "למטר", "לדונם"
_PER_AREA = re.compile(r"(?:(?<![א-ת])ל|/\s?)(?:מ\"ר|מטר|דונם)(?![א-ת])")


_SEGMENT_END = re.compile(r"[.;\n](?!\d)|,\s")  # a clause or a comma-separated part of one ("41,260,500" stays whole)
_AREA_AFTER = re.compile(r"\s*(?:מ\"ר|מטר|דונם)(?![א-ת])")


_AREA_WORD = re.compile(r"(?<![א-ת])[לב]?(?:מ\"ר|מטר|דונם)(?![א-ת])")


def _area_amount(text: str, pos: int, written: str, closest: list[Occurrence]) -> bool:
    """Whether the number at ``pos`` is an area or a per-area amount, the only numbers an area basis qualifies:
    the answer presents it so ("145 מ״ר", or its clause says "למ״ר"), or its own line in the source does ("שווי
    למ״ר בנוי | 8,700"). A total in ₪ or a factor in the rows of a calculation table whose area column is "מ״ר
    אקוו׳" is neither, though the column's header binds the basis to it."""
    if _AREA_AFTER.match(text, pos + len(written)):
        return True
    # its own part of the sentence: a list of figures ("2,870 ₪ למ״ר, שווי כזמין של 41,260,500 ₪, מקדם 0.8712")
    # gives "למ״ר" to the figure it follows, not to the others
    starts = [m.end() for m in _SEGMENT_END.finditer(text, 0, pos)]
    end = _SEGMENT_END.search(text, pos)
    if _PER_AREA.search(text[starts[-1] if starts else 0:end.start() if end else len(text)]):
        return True
    return any(o.area or _AREA_WORD.search(o.context) for o in closest)


def _table_definitions(lines: list[str], is_row: list[bool], heads: list[str]) -> dict[str, dict[str, str]]:
    """What a calculation table states once for its per-area amounts, by kind -> {key: as written}: the area basis
    of its area row ("סה״כ מ״ר אקווי׳ | 2,480") or of its title, caption or notes, and the period of its title,
    caption or notes ("דמי השכירות בטבלה הם לחודש"). Text around a table in a longer passage is not the table's."""
    found: dict[str, dict[str, str]] = {"basis": {}, "period": {}}
    for line, row in zip(lines, is_row, strict=True):
        if not row:
            continue
        cells = _row_cells(line)
        label = cells[0] if cells else ""
        if 'מ"ר' in label or any('מ"ר' in h for h in heads[:1]):
            for _, kind, key, written in _scan(label):
                if kind == "basis" and not _PER_AREA.search(label):
                    found["basis"].setdefault(key, written.strip())
    around = [ln for ln, row in zip(lines, is_row, strict=True) if not row]
    if len(around) <= 4:  # a caption, a title, a size line and notes; a longer passage is not the table's own text
        for line in around:
            for _, kind, key, written in _scan(line):
                found[kind].setdefault(key, written.strip())
    return found


@lru_cache(maxsize=256)
def parse_source(text: str) -> tuple[tuple[tuple[frozenset[str], Occurrence], ...], Qualifiers]:
    """Every number a source states with what it attaches to it there, and what the source states for all its
    numbers. Parsed once per source text: the same sources are read for every number, unit and round.

    In a table, a number also gets its row's label and its column's header; and a per-area amount ("דמ״ש למ״ר |
    75") gets the area basis and the period the table states once for the whole calculation — in its area row, its
    title, caption or notes — when the table states exactly one of each kind. A table with two bases binds none."""
    text = _norm(text)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    is_row = [_ROW in ln or ln.strip().startswith("|") for ln in lines]
    rows = [ln for ln, row in zip(lines, is_row, strict=True) if row]
    header = next((r for r in rows if not re.search(r"\d", r)), None)
    heads = _row_cells(header) if header else []
    table = _table_definitions(lines, is_row, heads) if rows else {"basis": {}, "period": {}}
    occurrences: list[tuple[frozenset[str], Occurrence]] = []
    general = Qualifiers()
    for line, row in zip(lines, is_row, strict=True):
        if row:
            if line is header:
                continue
            cells = _row_cells(line)
            for i, cell in enumerate(cells):
                found, _ = attached(cell)
                for f, _, q in found:
                    q = Qualifiers().merge(q)
                    if i > 0:
                        q.merge(_all(cells[0]))  # the row's label
                    if i < len(heads):
                        q.merge(_all(heads[i]))  # the column's header
                    if _PER_AREA.search(cells[0] + " " + (heads[i] if i < len(heads) else "") + " " + cell):
                        for kind, defined in table.items():
                            if len(defined) == 1 and not q.keys(kind):
                                key, written = next(iter(defined.items()))
                                q.add(kind, key, written)
                    # a per-area amount by its label, header or cell ("שווי למ״ר"); an area by its own label or cell —
                    # a column header of areas ("סה״כ מ״ר אקוו׳") also heads a calculation's totals and factors
                    own = cells[0] + " " + cell
                    area = bool(_PER_AREA.search(own + " " + (heads[i] if i < len(heads) else ""))
                                or _AREA_WORD.search(own))
                    occurrences.append((f, Occurrence(q, line, area=area)))
            continue
        found, gen = attached(line)
        # a clause with no number speaks for the source; a table's title, caption or notes for its numbers
        general.merge(_all(line) if rows else gen)
        occurrences += [(f, Occurrence(q, line)) for f, _, q in found]
    return tuple(occurrences), general


def source_occurrences(text: str, forms: set[str]) -> tuple[list[Occurrence], Qualifiers]:
    """Where a source states one of the number's forms, and what the source states for all its numbers."""
    parsed, general = parse_source(text)
    return [o for f, o in parsed if f & forms], general


def _flat(text: str) -> str:
    return " ".join(_norm(text).split())


def number_qualifiers(text: str, forms: frozenset[str], within: str) -> Qualifiers:
    """What a source attaches to a number where it states it inside ``within`` (a quote of the source, or a table
    row as read), with what the source states for all its numbers — the qualifiers a value taken from the source
    may carry as the source's own (``tools.tool_take_value``). The same number elsewhere in the source does not
    count."""
    parsed, general = parse_source(text)
    w = _flat(within)
    q = Qualifiers().merge(general)
    for f, o in parsed:
        line = _flat(o.context)
        if f & forms and line and (w in line or line in w):
            q.merge(o.qualifiers)
    return q


def basis_key(text: str | None) -> str:
    """One spelling of an area basis for comparing two values ("מ״ר אקוו׳" and "אקווי'" are one basis); a basis
    the vocabulary does not know is compared as written."""
    keys = sorted({key for _, kind, key, _ in _scan(_norm(text or "")) if kind == "basis"})
    return " ".join(keys) if keys else " ".join(_norm(text or "").split())


_CURRENCY = re.compile(r"₪|ש\"ח|(?<![א-ת])שקל")
# a scale the unit vocabulary does not carry ("באלפי ₪"): the unit is then not attested
_SCALE = re.compile(r"(?<![א-ת])(?:ב?אלפי|אלף|ב?מיליוני|מיליון|מלש\"ח|אש\"ח)(?![א-ת])")
_PER_SQM = re.compile(r"(?:(?<![א-ת])ל|/\s?)(?:מ\"ר|מטר)(?![א-ת])")


def units_attested(context: str) -> set[str]:
    """The units (measurement vocabulary) the words around a number give it: ₪, ₪ למ״ר, מ״ר, דונם or %; none when
    they state no unit, or a scale ("באלפי ₪") the vocabulary does not carry."""
    c = _norm(context)
    if "%" in c:
        return {"percent"}
    money = bool(_CURRENCY.search(c))
    if money and _SCALE.search(c):
        return set()
    if money:
        return {"ILS_per_sqm"} if _PER_SQM.search(c) else ({"ILS"} if not re.search(r"(?<![א-ת])לדונם", c) else set())
    if re.search(r"(?<![א-ת])דונם", c):
        return {"dunam"}
    if re.search(r"מ\"ר|מטר רבוע", c):
        return {"sqm"}
    return set()


# the words that name a kind of value in its row, column or sentence (professional vocabulary, R26)
KIND_WORDS = {
    "value": r"שווי", "price": r"מחיר|תמורה", "rent": r"שכירות|שכ\"ד|דמ\"ש|שכר\s+דירה",
    "management_fee": r"ניהול|דמ\"נ", "cost": r"עלות|עלויות|הוצאות|הוצאה", "income": r"הכנסות|הכנסה|תקבולים|פדיון",
    "area": r"שטח|מ\"ר", "rights_area": r"זכויות", "levy": r"היטל|(?<![א-ת])מס(?![א-ת])",
    "rate": r"שיעור|%|תשואה|היוון", "coefficient": r"מקדם", "count": r"מספר|כמות|יח\"ד", "duration": r"שנים|חודשים|תקופת",
    "profit": r"רווח",
}


def kind_attested(kind: str, context: str) -> bool:
    """Whether the words around a number name its kind (a per-area kind also needs "למ״ר")."""
    c = _norm(context)
    base = kind.removesuffix("_per_area")
    words = KIND_WORDS.get(base)
    if not words or not re.search(words, c):
        return False
    return not kind.endswith("_per_area") or bool(_PER_AREA.search(c))


def vat_attested(context: str, forms: frozenset[str], general_text: str = "") -> set[str]:
    """The VAT statuses the source gives the number: written for it in ``context`` (the VAT phrase belongs to the
    nearest number before it), or stated for the whole source in a clause with no number."""
    from app.chat.verify import vat_attachments

    pairs, general = vat_attachments(_norm(context))
    out = {pol for f, pol in pairs if f in forms} | general
    if general_text:
        out |= vat_attachments(_norm(general_text))[1]
    return out


def measurement_qualifiers(row) -> Qualifiers:
    q = Qualifiers()
    basis = (getattr(row, "area_basis", None) or "").strip()
    if basis:
        keys = _all(basis).keys("basis") or {basis}
        for key in keys:
            q.add("basis", key, basis)
    period = getattr(row, "period", None)
    if period in MEASUREMENT_PERIODS:
        q.add("period", period, PERIOD_TEXT[period])
    if getattr(row, "value_form", None) == "approx":
        q.add("approx", "approx", "מקורב")
    return q


@dataclass
class MeaningProblem:
    number: str  # as written in the unit
    kind: str
    reason: str
    blocking: bool
    annotation: str | None = None  # the qualifier as written, when exactly one is attested
    cite: str | None = None  # the source (S#/M#) the server found that states it, cited with it
    needs_citation: bool = False  # the unit is right; only the citation of ``cite`` is missing


SUBJECT_ROLES = ("appraiser_determination", "actual_contract", "calculation")
READS = 6  # database reads per verification for evidence beyond the cited passages


@dataclass
class Fetcher:
    """Evidence for a number beyond the passages a unit cites, read by the server for this check only: the same
    calculation's other passages that the turn already has, the whole table or section around a cited passage,
    and the stored measurements of the subject property in the cited documents. What it finds and uses is
    registered as a source of the turn (cited with the number); what it does not use is dropped. A comparables or
    survey row never counts: only the same table or section, or a measurement of the subject property still
    anchored in the document's current reading (``anchor_lost`` is not)."""

    ws: Workspace
    reads: int = READS  # per verification round; what was read is kept for the turn's later rounds

    @property
    def cache(self) -> dict:
        return self.ws.fetched

    def _read(self, key: tuple, load):
        if key not in self.cache:
            if self.reads <= 0:
                return None
            self.reads -= 1
            try:
                self.cache[key] = load()
            except Exception:  # noqa: BLE001 - evidence not read is evidence not found; the check stays strict
                self.cache[key] = None
        return self.cache[key]

    def expansions(self, sid: str) -> list:
        """What is around a cited passage, nearest first: the whole table of a passage from a table, then the whole
        section around the passage or its table (a table's notes are often a paragraph after it)."""
        from app.chat.tools import read_scope

        src = self.ws.sources.get(sid)
        if src is None:
            return []
        out = []
        # a part of a table read in parts (``read``) is not the whole table
        whole_table = (src.kind == "table" and src.chunk_id is None and src.table_index is not None
                       and not src.table_part)
        if src.table_index is not None and not whole_table:
            table = self._read(("x", str(src.version_id), src.table_index, src.block_start, src.block_end, "table"),
                               lambda: read_scope(self.ws, sid, "table"))
            if table is not None:
                out.append(table)
        anchor = src if src.block_start is not None else next((x for x in out if x.block_start is not None), None)
        if anchor is not None:
            ref = {"version_id": anchor.version_id, "block_start": anchor.block_start, "block_end": anchor.block_end,
                   "table_index": anchor.table_index, "chunk_id": None}
            section = self._read(("x", str(src.version_id), None, anchor.block_start, anchor.block_end, "section"),
                                 lambda: read_scope(self.ws, sid, "section", ref=ref))
            if section is not None:
                out.append(section)
        return [x for x in out if len(x.text) > len(src.text)]

    def same_calculation(self, sid: str) -> list:
        """The turn's other passages of the same table or section of the same document version."""
        src = self.ws.sources.get(sid)
        if src is None:
            return []
        return [o for o in self.ws.sources.values() if o.sid != sid and o.version_id == src.version_id and (
            (src.table_index is not None and o.table_index == src.table_index)
            or (src.section and o.section == src.section))]

    def subject_measurements(self, document_ids: set, unit: Unit | None = None) -> list:
        """The subject property's stored measurements in the documents; with ``unit`` and the context checks
        enforced, only those of the appraisal contexts the unit's cited evidence is in (KTD7)."""
        rows = self._subject_rows(document_ids)
        return rows if unit is None else self._in_context(rows, unit)

    def _in_context(self, rows: list, unit: Unit) -> list:
        from app.chat import contexts

        if not rows or not contexts.enforced():
            return rows
        cited: dict[str, set[int]] = {}
        for i in unit.ids:
            src = self.ws.sources.get(i)
            if src is not None and src.contexts:
                cited.setdefault(str(src.version_id), set()).update(src.contexts)
            v = self.ws.values.get(i)
            if v is not None and v.context:
                cited.setdefault(str(v.version_id), set()).add(v.context["number"])
            m = self.ws.measurements.get(i)
            if m is not None and m.context:
                cited.setdefault(str(m.version_id), set()).add(m.context["number"])
        out = []
        for r in rows:
            cx = self.ws.contexts.get(str(r.version_id))
            own = cited.get(str(r.version_id))
            if cx is None or not cx.multi or not own or cx.at_block(r.block_index) in own:
                out.append(r)
        return out

    def _subject_rows(self, document_ids: set) -> list:
        from sqlalchemy import text as sql

        from app.db import tenant_tx

        def load():
            with tenant_tx(self.ws.ctx) as conn:
                return conn.execute(sql(
                    "SELECT m.*, d.title, v.ingestion->>'reading_id' AS reading_id FROM measurements m"
                    " JOIN document_versions v ON v.id = m.version_id"
                    " AND v.is_current JOIN documents d ON d.id = m.document_id AND d.deleted_at IS NULL"
                    " WHERE m.document_id = ANY(:d) AND m.status <> 'rejected' AND m.anchor_lost IS NULL"
                    " AND m.subject_role ="
                    " 'appraised_property' AND m.value_role = ANY(:r) ORDER BY m.block_index NULLS FIRST, m.id"
                    " LIMIT 400"), {"d": sorted(document_ids), "r": list(SUBJECT_ROLES)}).all()
        return self._read(("m", tuple(sorted(str(d) for d in document_ids))), load) or []

    def adopt(self, found) -> str:
        """The id of a source or measurement the check uses: registered as the turn's, once."""
        from app.chat.tools import Source

        if isinstance(found, Source):
            if not found.sid or self.ws.sources.get(found.sid) is not found:
                self.ws.adopt(found)
            return found.sid
        return self.ws.add_measurement(found).mid

    def attesting(self, unit: Unit, forms: frozenset[str], kind: str, key: str, words: set[str]) -> str | None:
        """The id of evidence that states ``kind`` ``key`` for the number, beyond the unit's citations."""
        def states(cand) -> bool:
            if cand is None or cand.sid in unit.ids:
                return False
            occ, _ = source_occurrences(cand.text, set(forms))
            return any(key in o.qualifiers.keys(kind) for o in occ)

        cited = [i for i in unit.ids if i in self.ws.sources]
        for sid in cited:
            # the passages the turn already has first; the table or section is read only when they do not say it
            for cand in self.same_calculation(sid):
                if states(cand):
                    return self.adopt(cand)
            for expansion in self.expansions(sid):
                if states(expansion):
                    return self.adopt(expansion)
        docs = {self.ws.sources[i].document_id for i in cited if self.ws.sources[i].document_id}
        docs |= {self.ws.measurements[i].document_id for i in unit.ids if i in self.ws.measurements}
        for row in self.subject_measurements(docs, unit) if docs else []:
            if not forms & numbers_in(row.value_text or ""):
                continue
            if key in measurement_qualifiers(row).keys(kind) and words & _content_words(f"{row.metric} {row.quote}"):
                return self.adopt(row)
        return None

    def subject_statement(self, unit: Unit, forms: frozenset[str], kind: str, words: set[str]):
        """The subject property's stored measurement of the number with the same metric (its words in the unit),
        when every such measurement gives ``kind`` one value; else None."""
        docs = {self.ws.sources[i].document_id for i in unit.ids
                if i in self.ws.sources and self.ws.sources[i].document_id}
        rows = [r for r in self.subject_measurements(docs, unit) if forms & numbers_in(r.value_text or "")
                and words & _content_words(r.metric or "")] if docs else []
        values = {tuple(sorted(measurement_qualifiers(r).keys(kind))) for r in rows}
        if len(values) != 1 or not next(iter(values)):
            return None
        return rows[0]

    def expanded_occurrences(self, unit: Unit, forms: frozenset[str]) -> list[list[tuple[Occurrence, object]]]:
        """The number's occurrences around the unit's cited passages, by ring: the nearest expansion of each cited
        passage first (its table), then the next (its section)."""
        rings: list[list[tuple[Occurrence, object]]] = []
        for sid in [i for i in unit.ids if i in self.ws.sources]:
            for depth, exp in enumerate(self.expansions(sid)):
                while len(rings) <= depth:
                    rings.append([])
                rings[depth] += [(o, exp) for o in source_occurrences(exp.text, set(forms))[0]]
        return rings


def _evidence(unit: Unit, ws: Workspace, forms: frozenset[str]) -> tuple[list[Occurrence], Qualifiers]:
    occurrences: list[Occurrence] = []
    general = Qualifiers()
    for sid in unit.ids:
        if sid in ws.measurements:
            row = ws.measurements[sid].row
            if forms & numbers_in(row.value_text or ""):
                occurrences.append(Occurrence(measurement_qualifiers(row), row.quote or "", measurement=True))
            continue
        if sid in ws.values:  # a value the server verified in its source, with the meaning recorded for it
            v = ws.values[sid]
            if forms & numbers_in(v.written):
                row = SimpleNamespace(area_basis=v.area_basis, period=v.period,
                                      value_form="approx" if v.approx else "exact")
                occurrences.append(Occurrence(measurement_qualifiers(row), v.quote, measurement=True))
            continue
        src = ws.sources.get(sid)
        if src is None:
            continue
        occ, gen = source_occurrences(src.text, set(forms))
        occurrences += occ
        general.merge(gen)
    return occurrences, general


def _closest(occurrences: list[Occurrence], words: set[str]) -> list[Occurrence]:
    """The occurrences the unit most likely reports: a cited measurement with the value, else the passages
    sharing most of the unit's words (the same number may mean different things in one source)."""
    measured = [o for o in occurrences if o.measurement]
    if measured:
        return measured
    scored = [(len(o.context_words & words), o) for o in occurrences]
    best = max((s for s, _ in scored), default=0)
    return [o for s, o in scored if s == best]


def _as_written(kind: str, found: dict[str, str]) -> str:
    """A qualifier as the answer shows it: a period by its label, a basis in the source's words."""
    return PERIOD_TEXT[sorted(found)[0]] if kind == "period" else display(" ".join(found.values()))


def check(unit: Unit, ws: Workspace, fetcher: Fetcher | None = None) -> list[MeaningProblem]:
    """The meaning problems of one cited unit. With a ``fetcher``, a qualifier the cited passages do not state is
    looked for in the same calculation before it counts as unsupported, and a needed one the cited passage omits
    may come from the table or section around it; either is then cited (``cite``)."""
    if not unit.ids:
        return []
    text = _norm(unit.text)
    whole = _all(text + "\n" + _norm(unit.context))
    approx_anywhere = bool(_APPROX_WORDS.search(text))
    words = _content_words(text)
    problems: list[MeaningProblem] = []
    in_unit, _ = attached(text)
    for forms, pos, claimed in in_unit:
        if not forms:
            continue
        written = _NUM_AT.match(text, pos).group(0)
        occurrences, general = _evidence(unit, ws, forms)
        if not occurrences:
            continue  # not a number of the cited evidence: the number check owns it
        # attested: what the evidence attaches to the number, and anything its source line says at all — a
        # qualifier phrased before the amount ("השטח 120 מ"ר, דמי השכירות לחודש 7,560 ₪") is never "added"
        attested = Qualifiers().merge(general)
        for o in occurrences:
            attested.merge(o.qualifiers).merge(_all(o.context))
        # unsupported: a basis or period given to the number that its evidence does not give it
        for kind in ("basis", "period"):
            extra = claimed.keys(kind) - attested.keys(kind)
            found_in = {k: fetcher.attesting(unit, forms, kind, k, words) for k in sorted(extra)} if fetcher else {}
            for k, where in found_in.items():
                if where is not None:
                    # stated in the same calculation: judged and shown with that evidence, not removed
                    if where not in unit.ids:
                        unit.ids.append(where)
                    problems.append(MeaningProblem(
                        written, kind, f"{KIND_LABELS[kind]} \"{display(claimed.found[kind][k])}\" של {written} נכתב "
                        f"ב-{where}; צטט אותו", blocking=False, cite=where, needs_citation=True))
            extra = {k for k in extra if found_in.get(k) is None}
            if extra:
                said = claimed.found[kind][sorted(extra)[0]]
                problems.append(MeaningProblem(
                    written, kind, f"התשובה מייחסת ל-{written} {KIND_LABELS[kind]} \"{display(said)}\" שלא נכתב לגבי ערך זה "
                    "במקור; אין להוסיף תקופה או בסיס שטח שלא נכתבו", blocking=True))
        # missing: what every closest occurrence attaches to the number, and the unit does not say at all; when the
        # cited passage does not say it, the table or section around it may (a basis in the table's area row)
        closest = _closest(occurrences, words)
        expanded: list | None = None  # read only for a qualifier the cited passages do not attach
        for kind in ("basis", "period", "approx"):
            if kind == "basis" and not _area_amount(text, pos, written, closest):
                continue  # an area basis qualifies an area or a per-area amount only
            if not all(o.qualifiers.keys(kind) for o in closest):
                if kind == "approx" or fetcher is None or whole.keys(kind):
                    continue
                if expanded is None:
                    expanded = fetcher.expanded_occurrences(unit, forms)
                # the nearest ring that states it for every closest occurrence, with one value
                ring = next((r for r in expanded if (near := _closest([o for o, _ in r], words))
                             and all(o.qualifiers.keys(kind) for o in near)
                             and len({tuple(sorted(o.qualifiers.found[kind])) for o in near}) == 1), None)
                if ring is None:
                    # the subject property's stored measurement of the same metric states it once
                    row = fetcher.subject_statement(unit, forms, kind, words)
                    if row is None:
                        continue
                    cite = fetcher.adopt(row)
                    as_written = _as_written(kind, measurement_qualifiers(row).found[kind])
                    if cite not in unit.ids:
                        unit.ids.append(cite)
                    problems.append(MeaningProblem(
                        written, kind, f"למספר {written} חסר {KIND_LABELS[kind]} כפי שנכתב ב-{cite} (\"{as_written}\"); "
                        f"כתוב אותו ליד המספר וצטט את {cite}", blocking=False, annotation=as_written, cite=cite))
                    continue
                near = _closest([o for o, _ in ring], words)
                o = near[0]
                cite = fetcher.adopt(next(src for x, src in ring if x is o))
                as_written = _as_written(kind, o.qualifiers.found[kind])
                if cite not in unit.ids:
                    unit.ids.append(cite)
                problems.append(MeaningProblem(
                    written, kind, f"למספר {written} חסר {KIND_LABELS[kind]} כפי שנכתב ב-{cite} (\"{as_written}\"); "
                    f"כתוב אותו ליד המספר וצטט את {cite}", blocking=False, annotation=as_written, cite=cite))
                continue
            if kind == "approx":
                if claimed.keys("approx") or approx_anywhere:
                    continue
            elif whole.keys(kind):
                continue
            # one value per occurrence: "מ״ר בנוי ברוטו" is one basis, two occurrences may disagree
            needed: dict[tuple, str] = {}
            for o in closest:
                values = o.qualifiers.found[kind]
                needed.setdefault(tuple(sorted(values)), " ".join(values.values()))
            if len(needed) == 1:
                keys, written_q = next(iter(needed.items()))
                as_written = PERIOD_TEXT[keys[0]] if kind == "period" else display(written_q)  # one occurrence's words
                problems.append(MeaningProblem(
                    written, kind, f"למספר {written} חסר {KIND_LABELS[kind]} כפי שנכתב במקור (\"{as_written}\"); "
                    "כתוב אותו ליד המספר", blocking=False, annotation=as_written))
            else:
                shown = ", ".join(f"\"{display(w)}\"" for w in needed.values())
                problems.append(MeaningProblem(
                    written, kind, f"במקורות מופיעים לגבי {written} כמה ערכים של {KIND_LABELS[kind]} ({shown}); כתוב "
                    "ליד המספר את זה שנכתב לגבי הערך שבתשובה", blocking=False))
    return problems
