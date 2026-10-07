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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
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
    # אקוו׳, אקווי׳, אקוי׳, אקו׳, אקוו, אקוויוולנטי(ים), אקוולנטי: one basis however it is spelt
    ("אקוו", r"(?:מ\"ר\s*)?אקו(?:ו?י?ו?ולנטי(?:ים)?|ו?י?'|ו)(?![א-ת])"),
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

    @cached_property
    def context_words(self) -> set[str]:
        return _content_words(self.context)


def _row_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


# a per-area amount: its row or column says "למ״ר", "/מ״ר", "למטר", "לדונם"
_PER_AREA = re.compile(r"(?:(?<![א-ת])ל|/\s?)(?:מ\"ר|מטר|דונם)(?![א-ת])")


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
                    occurrences.append((f, Occurrence(q, line)))
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
READS = 4  # database reads per verification for evidence beyond the cited passages


@dataclass
class Fetcher:
    """Evidence for a number beyond the passages a unit cites, read by the server for this check only: the same
    calculation's other passages that the turn already has, the whole table or section around a cited passage,
    and the stored measurements of the subject property in the cited documents. What it finds and uses is
    registered as a source of the turn (cited with the number); what it does not use is dropped. A comparables or
    survey row never counts: only the same table or section, or a measurement of the subject property."""

    ws: Workspace
    reads: int = READS
    cache: dict = field(default_factory=dict)

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

    def expansion(self, sid: str):
        """The whole table of a passage from a table, or the whole section around a passage or table."""
        from app.chat.tools import read_scope

        src = self.ws.sources.get(sid)
        if src is None:
            return None
        whole_table = src.kind == "table" and src.chunk_id is None and src.table_index is not None
        scope = "table" if src.table_index is not None and not whole_table else "section"
        if scope == "section" and src.block_start is None:
            return None
        got = self._read(("x", str(src.version_id), src.table_index, src.block_start, src.block_end, scope),
                         lambda: read_scope(self.ws, sid, scope, quiet=True))
        return got if got is not None and len(got.text) > len(src.text) else None

    def same_calculation(self, sid: str) -> list:
        """The turn's other passages of the same table or section of the same document version."""
        src = self.ws.sources.get(sid)
        if src is None:
            return []
        return [o for o in self.ws.sources.values() if o.sid != sid and o.version_id == src.version_id and (
            (src.table_index is not None and o.table_index == src.table_index)
            or (src.section and o.section == src.section))]

    def subject_measurements(self, document_ids: set) -> list:
        from sqlalchemy import text as sql

        from app.db import tenant_tx

        def load():
            with tenant_tx(self.ws.ctx) as conn:
                return conn.execute(sql(
                    "SELECT m.*, d.title FROM measurements m JOIN document_versions v ON v.id = m.version_id"
                    " AND v.is_current JOIN documents d ON d.id = m.document_id AND d.deleted_at IS NULL"
                    " WHERE m.document_id = ANY(:d) AND m.status <> 'rejected' AND m.subject_role ="
                    " 'appraised_property' AND m.value_role = ANY(:r) ORDER BY m.block_index NULLS FIRST, m.id"
                    " LIMIT 400"), {"d": sorted(document_ids), "r": list(SUBJECT_ROLES)}).all()
        return self._read(("m", tuple(sorted(str(d) for d in document_ids))), load) or []

    def adopt(self, found) -> str:
        """The id of a source or measurement the check uses: registered as the turn's, once."""
        from app.chat.tools import Measurement, Source

        if isinstance(found, Source):
            if not found.sid or self.ws.sources.get(found.sid) is not found:
                self.ws.adopt(found)
            return found.sid
        known = next((m for m in self.ws.measurements.values() if m.id == found.id), None)
        if known is None:
            known = Measurement(f"M{len(self.ws.measurements) + 1}", found.id, found.document_id,
                                found.version_id, found.title, found)
            self.ws.measurements[known.mid] = known
        return known.mid

    def attesting(self, unit: Unit, forms: frozenset[str], kind: str, key: str, words: set[str]) -> str | None:
        """The id of evidence that states ``kind`` ``key`` for the number, beyond the unit's citations."""
        cited = [i for i in unit.ids if i in self.ws.sources]
        for sid in cited:
            for cand in [*self.same_calculation(sid), self.expansion(sid)]:
                if cand is None or cand.sid in unit.ids:
                    continue
                occ, _ = source_occurrences(cand.text, set(forms))
                if any(key in o.qualifiers.keys(kind) for o in occ):
                    return self.adopt(cand)
        docs = {self.ws.sources[i].document_id for i in cited}
        docs |= {self.ws.measurements[i].document_id for i in unit.ids if i in self.ws.measurements}
        for row in self.subject_measurements(docs) if docs else []:
            if not forms & numbers_in(row.value_text or ""):
                continue
            if key in measurement_qualifiers(row).keys(kind) and words & _content_words(f"{row.metric} {row.quote}"):
                return self.adopt(row)
        return None

    def expanded_occurrences(self, unit: Unit, forms: frozenset[str]) -> list[tuple[Occurrence, object]]:
        out = []
        for sid in [i for i in unit.ids if i in self.ws.sources]:
            exp = self.expansion(sid)
            if exp is not None:
                out += [(o, exp) for o in source_occurrences(exp.text, set(forms))[0]]
        return out


def _evidence(unit: Unit, ws: Workspace, forms: frozenset[str]) -> tuple[list[Occurrence], Qualifiers]:
    occurrences: list[Occurrence] = []
    general = Qualifiers()
    for sid in unit.ids:
        if sid in ws.measurements:
            row = ws.measurements[sid].row
            if forms & numbers_in(row.value_text or ""):
                occurrences.append(Occurrence(measurement_qualifiers(row), row.quote or "", measurement=True))
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
        expanded = fetcher.expanded_occurrences(unit, forms) if fetcher else []
        for kind in ("basis", "period", "approx"):
            cite = None
            if not all(o.qualifiers.keys(kind) for o in closest):
                if kind == "approx" or not expanded:
                    continue
                near = _closest([o for o, _ in expanded], words)
                if not near or not all(o.qualifiers.keys(kind) for o in near):
                    continue
                if (kind == "basis" and whole.keys("basis")) or (kind == "period" and whole.keys("period")):
                    continue
                values = {tuple(sorted(o.qualifiers.found[kind])) for o in near}
                if len(values) != 1:
                    continue
                cite = fetcher.adopt(next(src for o, src in expanded if o is near[0]))
                o = near[0]
                keys = tuple(sorted(o.qualifiers.found[kind]))
                as_written = PERIOD_TEXT[keys[0]] if kind == "period" else display(" ".join(o.qualifiers.found[kind].values()))
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
                as_written = PERIOD_TEXT[keys[0]] if kind == "period" else display(written_q)
                problems.append(MeaningProblem(
                    written, kind, f"למספר {written} חסר {KIND_LABELS[kind]} כפי שנכתב במקור (\"{as_written}\"); "
                    "כתוב אותו ליד המספר", blocking=False, annotation=as_written))
            else:
                shown = ", ".join(f"\"{display(w)}\"" for w in needed.values())
                problems.append(MeaningProblem(
                    written, kind, f"במקורות מופיעים לגבי {written} כמה ערכים של {KIND_LABELS[kind]} ({shown}); כתוב "
                    "ליד המספר את זה שנכתב לגבי הערך שבתשובה", blocking=False))
    return problems
