"""Which document a follow-up names, among the documents the user may see.

When a follow-up changes the property, the unit within it, or the document ("טעיתי, התכוונתי לדירה B7 בהסנונית 12"),
the documents of the previous turn are no filter for the new request. The user's own words are looked up instead,
under the user's permissions (``documents_matching``: every visible current document holding all the words, in its
title or its text), and the outcome decides the scope:

- **tier:** the documents with the most of the words in their title. A house number counts there too ("הנרקיס 4"),
  though one-character words are not title words elsewhere. When no title holds any of the words, every document
  that holds them all in its text. The number of passages that hold them never decides between documents: a report
  that mentions a unit or an address more often is not more about it;
- one document in the tier → it is the scope;
- several, of which one is a document the conversation was about → that one (another unit of the same building, or
  a word that every report holds);
- several otherwise → a clarification that names them;
- none, even with only the identifying words (a number, Latin letters, a word of some title) → a clarification that
  says no such document was found. The previous property is never the answer instead;
- a document the words name by more of its title than any readable document does, whose latest version was not read
  (failed, waiting or being processed: ``unread_matching``, KTD11) → ``not_read``: the document with its state and
  reason, never a search over it. When nothing is located and the user may see files that were not read, the outcome
  carries them, so the message does not say the data is absent (R5).

A reply to such a clarification is resolved among the documents it named, and only among them.

**Units** (round 9 U9, KTD7, KTD8; R11–R14): in a file of several appraisals the lookup is per unit
(``units_matching``): the words must all be inside one unit, found by its title or an alias the document states, or
in its passages. A candidate is then a unit, carrying its anchor — the file, the reading, the normalized title and
aliases, the first printed page label and the identifiers — which does not depend on the unit's number. The
candidates are ordered by how well the unit's name fits the words (the words a title or alias holds, then the fewest
words of that name left unsaid) and then by document order, never by the number of passages; a unique name match
selects its unit even when another unit has more passages. Several plausible units give a clarification that lists
title, pages, identifiers and file. A reply is matched by an ordinal ("השני", "2") or by a word of a name or an
identifier that only some candidates hold; a reply that fits none or several asks again; the unit chosen is the
request's unit. The names of units join the vocabulary of identifying words (``titles_of``), so a project name that
is in no file title still names a document in a follow-up.

A reply that states a candidate's complete title or alias as a whole phrase ("מתחם הגפן ב": the one letter that tells it
apart is dropped as a word) selects it when no other candidate's name is stated (``named_unit``); after the model's own
question between appraisals, which offered no candidates, the reply is matched the same way against the units of the files
that turn touched (``units_of_documents``). A conversation's first message is checked once, from the unit names alone
(``first_message_ambiguity``): when its words fit two or more units equally well by name, the server asks as it does for a
follow-up and the model is not called; a first message (or a new question with no unit kept) that names exactly one unit —
by its name, else, in a file whose units state none, by the words only that unit holds in its passages — makes that unit
the turn's (``first_message``, ``tools.locate_unit``).

**The unit in the conversation** (round 9 U10, KTD8, R14): the conversation's focus carries the unit it is about as its
anchor (``located_unit``), and a follow-up keeps it across a metric or computation change. Two anchors are the same unit
(``same_unit``) in the same reading by number and across readings by names, never by number; a follow-up that names
another unit of the same file therefore moves the conversation although the document is the same. When the file was
read again, ``live_unit`` finds the anchor's unit in the current reading (``rematch``: the normalized title or an alias,
else the first printed page label together with an identifier); a unit no longer there is dropped and the answer asks,
listing the file's appraisals, so an old unit number never selects a different appraisal.

**Derivations side by side** (round 15 U3, KTD6, R7): the units of an earlier derivation stay in ``document_units`` beside
the current ones, and an anchor carries the tag of the derivation that made it (``units_tag``; none: ``units-v4``). The
unit number of an anchor of the current derivation still says which unit it is; for an older anchor ``live_unit`` reads the
kept row of its unit and finds the unit by name, else by the one current unit that holds that row's whole block range
(``resolve_older``), and a range that spans several units is "gone": the number alone is never used.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from app.chat import contexts, slots
from app.chat import units as unit_store
from app.db import TenantContext, tenant_tx
from app.extraction.normalize_text import base_normalize
from app.platform.search import (
    UnitMatches,
    _best_name_fit,
    _scope_terms,
    _term_regexes,
    _title_words,
    document_term_holders,
    printed_labels,
    unit_opening_text,
    unit_term_holders,
    units_matching,
    unread_documents,
    unread_matching,
)
from app.platform.source_kinds import APPRAISALS_SQL

CLARIFY_TITLES = 5
_TOKEN = re.compile(r"[\w\"״׳'/-]+")
_ORDINALS = {"הראשון": 0, "הראשונה": 0, "הראשונים": 0, "השני": 1, "השנייה": 1, "השניה": 1, "השלישי": 2,
             "השלישית": 2, "הרביעי": 3, "הרביעית": 3, "החמישי": 4, "החמישית": 4}


@dataclass
class Candidate:
    document_id: str
    title: str  # a unit's title (the file's when it has none)
    title_terms: int = 0
    hits: int = 0
    unread: dict | None = None  # a document that was not read: its state (``search.unread_documents``)
    unit: dict | None = None  # a unit of a file of several: its anchor (``unit_anchor``); None: the file is the answer
    file_title: str = ""
    name_extra: int = 0  # words of the best-fitting name that the user's words left unsaid (fewer fit better)

    def as_dict(self) -> dict:
        out = {"document_id": self.document_id, "title": self.title}
        if self.unit:
            out |= {"unit": self.unit, "file_title": self.file_title}
        return out


@dataclass
class Outcome:
    kind: Literal["resolved", "ambiguous", "not_found", "not_read"]
    documents: list[Candidate] = field(default_factory=list)
    query: str = ""
    # a ``not_found`` outcome: the visible files that were not read (``search.unread_documents``), so what is said
    # does not claim the data is absent
    unread: list[dict] = field(default_factory=list)
    # a ``not_found`` outcome: files that hold the words in different units (``units_matching``), so that no single
    # appraisal holds them all (R11)
    split: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "query": self.query, "documents": [d.as_dict() for d in self.documents]}


def unit_anchor(u: dict) -> dict:
    """What identifies a unit across readings (KTD8): the file, the reading it was found in, the normalized title and
    aliases, the first printed page label and the identifiers. Its number (``unit_no``) is only of that reading and is
    never reused across readings, nor across derivations of one reading: ``units_tag`` is the tag of the derivation that
    numbered it (none: ``units-v4``, the anchors stored before round 15). ``pages`` and ``display`` are for showing it."""
    return {"document_id": str(u["document_id"]), "reading_id": u.get("reading_id") or None, "unit_no": u["unit_no"],
            "units_tag": u.get("units_tag") or None,
            "title": contexts.name_key(u.get("title")) or None, "display": u.get("title"),
            "aliases": [k for a in u.get("aliases") or () if (k := contexts.name_key(a))],
            "first_label": u.get("first_label"), "identifiers": list(u.get("identifiers") or ()),
            "pages": [u.get("page_first"), u.get("page_last")]}


def unit_pages(anchor: dict) -> str:
    first, last = (anchor.get("pages") or [None, None])[:2]
    return contexts.pages_text(first, last)


def focus_text(anchor: dict) -> str:
    """How a gap line names a unit the turn is in when it read nothing in it: «title» and its pages."""
    pages = unit_pages(anchor)
    return f"«{anchor.get('display') or contexts.MSG_NO_TITLE}»" + (f" ({pages})" if pages else "")


def candidate_label(c: dict) -> str:
    """How a clarification names a candidate: a document by its title; a unit by its title (or that it has none), its
    pages, its identifiers and the file."""
    unit = c.get("unit")
    if not unit:
        return f"«{c['title']}»"
    parts = [p for p in (unit_pages(unit), " · ".join(unit.get("identifiers") or ())) if p]
    return (f"«{unit.get('display') or contexts.MSG_NO_TITLE}» ({'; '.join(parts)}{'; ' if parts else ''}"
            f"בקובץ «{c.get('file_title') or c['title']}»)")


def _names(x: dict) -> set[str]:
    """A unit's normalized title and aliases: ``x`` is an anchor (already normalized) or a unit row."""
    return {k for n in [x.get("title"), *(x.get("aliases") or ())] if (k := contexts.name_key(n))}


def unit_names(anchor: dict | None) -> list[str]:
    """The words a unit is named by, as written: its title and aliases (what a follow-up may repeat)."""
    if not anchor:
        return []
    return [n for n in [anchor.get("display"), *(anchor.get("aliases") or ())] if n]


def _identifiers(x: dict) -> set[str]:
    return {" ".join(i.split()).lower() for i in x.get("identifiers") or () if i}


def _label_and_identifier(anchor: dict, u: dict) -> bool:
    """Whether a unit holds the anchor's first printed page label and one of its identifiers: what is left of a unit
    that no name identifies."""
    label = anchor.get("first_label")
    return bool(label) and label == u.get("first_label") and bool(_identifiers(anchor) & _identifiers(u))


def _numbering(x: dict) -> tuple[str | None, str | None]:
    """What numbers an anchor's unit: its reading and the derivation within it (``units_tag``)."""
    return x.get("reading_id") or None, x.get("units_tag") or None


def same_unit(a: dict | None, b: dict | None) -> bool:
    """Whether two anchors are one unit. Within a reading and a derivation its number says; across readings, and across
    derivations of one reading, only its names (or, with none in common to compare, its first page label and an
    identifier) do — the number of a unit is never reused across readings (KTD8) or derivations (KTD6)."""
    if not a or not b or str(a["document_id"]) != str(b["document_id"]):
        return False
    if _numbering(a) == _numbering(b):
        return a.get("unit_no") == b.get("unit_no")
    names, theirs = _names(a), _names(b)
    return bool(names & theirs) if names and theirs else _label_and_identifier(a, b)


def rematch(anchor: dict, units: list[dict]) -> dict | None:
    """The unit of a new reading that an anchor stands for (``units``: its rows with ``first_label``, as the lookup
    builds them), or None: the one unit whose normalized title or alias the anchor shares (several: the one that also
    holds its first printed page label and an identifier), else the one that holds its first page label and an
    identifier. An appendix or a comparables part is never the answer, and a number never is."""
    subjects = [u for u in units if u.get("kind") not in contexts.NON_SUBJECT_KINDS]
    names = _names(anchor)
    pool = [u for u in subjects if names & _names(u)] if names else []
    if not pool:
        pool = [u for u in subjects if _label_and_identifier(anchor, u)]
    elif len(pool) > 1:
        pool = [u for u in pool if _label_and_identifier(anchor, u)]
    return pool[0] if len(pool) == 1 else None


def anchor_with_id(u: dict) -> dict:
    """The anchor of a unit row with its stable id and the certainty of its opening edge, as a candidate carries it."""
    return unit_anchor(u) | {"unit_id": u.get("unit_id"), "certainty": u.get("certainty")}


def unit_candidate(u: dict, file_title: str) -> Candidate:
    """A unit row as a candidate of a clarification: it carries its anchor."""
    return Candidate(str(u["document_id"]), u.get("title") or file_title, file_title=file_title, unit=anchor_with_id(u))


@dataclass
class LiveUnit:
    """A focus unit against the current reading of its document. ``status``: ``current`` (the same reading, the same
    unit), ``moved`` (the file was read again and the unit was found by its anchor: ``unit`` is the new anchor),
    ``gone`` (no unit of the file stands for it: ``candidates`` are the file's appraisals to ask about), ``single`` (the
    file is one unit now: the unit means nothing) or ``unreadable`` (the document is not readable now). ``version_id``:
    the version the unit is in, for the turn's handle."""

    status: Literal["current", "moved", "gone", "single", "unreadable"]
    unit: dict | None = None
    version_id: str | None = None
    candidates: list[Candidate] = field(default_factory=list)


def _current_units(conn, document_id: UUID) -> tuple[str, str, str, list[dict]] | None:
    """(the document's title, its current version, that version's reading id, its units as rows with ``first_label``),
    or None when the document is not readable now (deleted, or not visible to the user: row security decides). The
    units of the current reading are stored on first use."""
    row = conn.execute(text(
        "SELECT d.title, v.id AS version_id, COALESCE(v.ingestion->>'reading_id', '') AS reading_id"
        " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
        f" WHERE d.id = :d AND d.deleted_at IS NULL AND {APPRAISALS_SQL}"), {"d": document_id}).first()
    if row is None:
        return None
    tag = unit_store.current_tag()
    rows = [{"document_id": document_id, "version_id": row.version_id, "reading_id": row.reading_id,
             "unit_no": u.unit_no, "units_tag": tag,
             "unit_id": contexts.unit_key(row.version_id, row.reading_id, u.unit_no, tag),
             "kind": u.kind, "title": u.title, "aliases": list(u.aliases),
             "identifiers": list((u.identifiers or {}).get("shown") or ()), "page_first": u.page_first,
             "page_last": u.page_last, "block_first": u.block_first, "block_last": u.block_last,
             "certainty": u.certainty} for u in unit_store.ensure(conn, row.version_id)]
    if len(rows) > 1:
        printed_labels(conn, rows)
    return row.title, str(row.version_id), row.reading_id, rows


def resolve_older(anchor: dict, rows: list[dict], kept: dict | None) -> dict | None:
    """The unit of the current derivation that an anchor of an older derivation of the same reading stands for
    (round 15 U3, KTD6): by its names (``rematch``), else by containment — the one current subject unit whose block range
    holds the whole range of the unit the anchor was made on (``kept``: that unit's kept row, with ``block_first`` and
    ``block_last``; None when none was kept). A range that falls in no unit, in an appendix or a comparables part, or
    across several units is not resolved (None, the unit is "gone"); the number of the anchor is never used."""
    found = rematch(anchor, rows)
    if found is not None or kept is None:
        return found
    holders = [u for u in rows if u["block_first"] <= kept["block_first"] and kept["block_last"] <= u["block_last"]]
    if len(holders) == 1 and holders[0]["kind"] not in contexts.NON_SUBJECT_KINDS:
        return holders[0]
    return None


def live_unit(ctx: TenantContext, anchor: dict) -> LiveUnit:
    """The anchor's unit in the current reading of its document, under the user's permissions (see the module
    docstring). Reads the stored units of the current version (built on first use) and, for an anchor of an earlier
    derivation, the kept row it stood on."""
    try:
        document_id = UUID(str(anchor["document_id"]))
    except (KeyError, ValueError):
        return LiveUnit("unreadable")
    older = False
    kept = None
    with tenant_tx(ctx) as conn:
        current = _current_units(conn, document_id)
        if current is not None and len(current[3]) > 1 and (anchor.get("reading_id") or None) == (current[2] or None):
            older = (anchor.get("units_tag") or None) != unit_store.current_tag()
            if older:
                row = unit_store.kept(conn, current[1], current[2], int(anchor.get("unit_no") or 0), anchor.get("units_tag"))
                kept = {"block_first": row.block_first, "block_last": row.block_last} if row is not None else None
    if current is None:
        return LiveUnit("unreadable")
    title, version, reading, rows = current
    if len(rows) < 2:
        return LiveUnit("single", version_id=version)
    # the same reading and derivation: the unit with its number, if its names are still the anchor's (a derivation that
    # changed under the same reading id would have renumbered it, and carries another tag)
    if (anchor.get("reading_id") or None) == (reading or None) and not older:
        same = next((u for u in rows if u["unit_no"] == anchor.get("unit_no")
                     and u["kind"] not in contexts.NON_SUBJECT_KINDS), None)
        if same is not None and (not _names(anchor) or _names(anchor) & _names(same)):
            return LiveUnit("current", unit_anchor(same), version)
    found = resolve_older(anchor, rows, kept) if older else rematch(anchor, rows)
    if found is not None:
        return LiveUnit("moved", unit_anchor(found), version)
    subjects = [unit_candidate(u, title) for u in rows if u["kind"] not in contexts.NON_SUBJECT_KINDS]
    return LiveUnit("gone", None, version, subjects) if subjects else LiveUnit("single", version_id=version)


def anchor_of(ctx: TenantContext, document_id: str, reading_id: str | None, unit_no: int) -> dict | None:
    """The anchor of a unit of a document's current reading, or None when the document is not readable, is one unit, or
    has been read again since (``reading_id`` is the one the turn worked in). An appendix or a comparables part has no
    anchor: it is never the subject of a conversation."""
    try:
        document_id = UUID(str(document_id))
    except ValueError:
        return None
    with tenant_tx(ctx) as conn:
        current = _current_units(conn, document_id)
    if current is None or len(current[3]) < 2 or (reading_id or None) != (current[2] or None):
        return None
    found = next((u for u in current[3] if u["unit_no"] == unit_no and u["kind"] not in contexts.NON_SUBJECT_KINDS), None)
    return unit_anchor(found) if found is not None else None


def title_tokens(title: str) -> set[str]:
    """The title's words with their forms, and its numbers however short ("4" in "הנרקיס 4")."""
    words = set(_title_words(title))
    words |= {t for t in (w.strip("-'״׳\"") for w in _TOKEN.findall(base_normalize(title))) if any(c.isdigit() for c in t)}
    return words


# an amount is no name: "13,700", "2.5", or a number written with its unit ("75 ₪", "120 מ"ר", "8%")
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+\s*(?:₪|ש[\"״]ח|%|(?:ל|ב)?מ[\"״]ר|דונם|אחוז)"
                     r"|[₪]\s*\d[\d,.]*")


def _numbers(text: str) -> set[str]:
    """The numbers of a text however short, with any letters they carry ("4", "143", "c14")."""
    return {t for t in (w.strip("-'״׳\"?.!,:;()") for w in _TOKEN.findall(base_normalize(text)))
            if any(c.isdigit() for c in t)}


def forms(token: str) -> set[str]:
    """A word's forms (prefix-stripped and inflected), as titles are matched."""
    found = _scope_terms(token)
    return set(found[0]) | {token} if found else {token}



def identifying(words: str, titles: list[str], bare_numbers: bool = True, pairs_only: bool = False) -> list[str]:
    """The words that can name a document: a number (a house or unit number, not an amount), Latin letters, or a
    word of some title the user may see. With ``bare_numbers`` off, a number counts only beside such a word
    ("הנרקיס 4", "דירה B7"): a floor, a year or a duration ("בקומה 21", "ב-2024") names no property. With
    ``pairs_only``, only an address — a title word with the number beside it — and Latin labels count: a common
    word that happens to share a title word's form ("שאלה" and "האלה") is dropped."""
    from app.chat.meaning import metric_word

    vocabulary = set().union(*(title_tokens(t) for t in titles)) if titles else set()
    tokens: list[tuple[str, str | None]] = []
    for raw in _TOKEN.findall(base_normalize(_AMOUNT.sub(" ", words))):
        tok = raw.strip("-'״׳\"?.!,:;()")
        if not tok:
            continue
        if re.search(r"[A-Za-z]", tok):
            tokens.append((tok, "label"))
        elif any(c.isdigit() for c in tok):
            tokens.append((tok, "number"))
        else:
            found = _scope_terms(tok)
            named = bool(found) and bool(vocabulary & set(found[0])) and not metric_word(tok)
            tokens.append((found[0][0] if found else tok, "name" if named else None))

    def beside(i: int, kinds: tuple[str, ...]) -> bool:
        return any(0 <= j < len(tokens) and tokens[j][1] in kinds for j in (i - 1, i + 1))

    out = []
    for i, (tok, kind) in enumerate(tokens):
        if kind == "label":
            out.append(tok)
        elif kind == "name" and (not pairs_only or beside(i, ("number",))):
            out.append(tok)
        elif kind == "number" and (bare_numbers and not pairs_only or beside(i, ("name", "label"))):
            out.append(tok)
    return list(dict.fromkeys(out))


def _unit_tokens(unit: dict) -> set[str]:
    """The words of a unit's title and aliases (with their forms), beside the file's title words."""
    return set().union(*(title_tokens(x) for x in [unit.get("title") or "", *(unit.get("aliases") or ())]))


def choose(found: list[dict], query: str, focus_ids: set[str]) -> Outcome:
    """The outcome of a lookup (rows of ``documents_matching`` for ``query``; for a file of several units one row per
    unit, with its ``unit``), by the rules of the module. A number the user named counts as a title term however
    short ("הנרקיס 4"), and a title that holds other numbers but not the named one is another property ("הנרקיס 14").
    Ambiguous candidates are ordered by name fit and then document order, never by their passages."""
    asked = _numbers(query)
    words = [f for f in _scope_terms(query) if not any(c.isdigit() for c in f[0])]
    cands = []
    for d in found:
        unit = d.get("unit")
        title = title_tokens(d["title"]) | (_unit_tokens(unit) if unit else set())
        named = {t for t in title if any(c.isdigit() for c in t)}
        # a one-digit house number the title does not hold: another property ("הנרקיס 4" is not "הנרקיס 14").
        # A longer number was a search term, so the document already holds it (a comparable it names: "האורן 30" in
        # the report on "האורן 26"); a unit label ("A2") is no house number
        house, held = {t for t in asked if t.isdigit() and len(t) == 1}, {t for t in named if t.isdigit()}
        if house and held and not house & held:
            continue
        terms = sum(1 for f in words if title & set(f)) + len(asked & named)
        c = Candidate(str(d["document_id"]), (unit or {}).get("title") or d["title"], terms, int(d.get("hits") or 0),
                      file_title=d["title"], name_extra=int(unit.get("name_extra") or 0) if unit else 0)
        if unit:
            c.unit = anchor_with_id(unit)
        cands.append(c)
    if not cands:
        return Outcome("not_found", query=query)
    best = max(c.title_terms for c in cands)
    tier = [c for c in cands if c.title_terms == best]  # best == 0: every document that holds the words in its text
    if len(tier) == 1:
        return Outcome("resolved", tier, query)
    focused = [c for c in tier if c.document_id in focus_ids and not c.unit]
    if len(focused) == 1:
        return Outcome("resolved", focused, query)
    # name fit, then document order (the file, then its units in reading order); never the number of passages
    tier.sort(key=lambda c: (c.name_extra, c.file_title or c.title, c.document_id, (c.unit or {}).get("unit_no") or 0))
    return Outcome("ambiguous", tier[:CLARIFY_TITLES], query)


_COUNT = re.compile(r"(?:מספר|אפשרות|#)?\s*(\d{1,2})")


def _candidate_words(c: Candidate) -> set[str]:
    """What a reply can name a candidate by: the words of its title; for a unit also its aliases, its identifiers and
    the pieces of its name that are one letter ("הגפן ב")."""
    words = title_tokens(c.title)
    if c.unit:
        words |= _unit_tokens({"title": c.unit.get("display"), "aliases": c.unit.get("aliases")})
        words |= {w for x in c.unit.get("identifiers") or () for w in _TOKEN.findall(base_normalize(x))}
        words |= {w for x in [c.unit.get("display") or "", *(c.unit.get("aliases") or ())]
                  for w in (t.strip("-'״׳\"") for t in _TOKEN.findall(base_normalize(x))) if len(w) == 1}
    return words


def _full_names(c: Candidate) -> tuple[str, ...]:
    """A candidate's complete names, normalized (``contexts.name_key``): its title and, for a unit, its display title
    and aliases. A one-character name is no name."""
    raw = [c.title] + ([c.unit.get("display"), c.unit.get("title"), *(c.unit.get("aliases") or ())] if c.unit else [])
    return tuple(dict.fromkeys(k for x in raw if len(k := contexts.name_key(x)) >= 2))


def _named_by_phrase(cands: list[Candidate], reply: str) -> list[Candidate]:
    """The candidates whose complete title or alias the reply holds as a whole phrase ("מתחם הגפן ב", with the prefix
    letters of its first word allowed): the one-letter tail the tokenizer drops is part of the phrase. A candidate
    whose phrase lies inside the longer phrase another candidate's name matched ("מגדל הים" in "מגדל הים הצפוני")
    is not named by it."""
    spans: dict[int, tuple[int, int]] = {}
    for n, c in enumerate(cands):
        found = contexts.Identity(names=_full_names(c)).name_spans(reply)
        if found:
            spans[n] = max(found, key=lambda sp: sp[1] - sp[0])
    return [cands[n] for n, (a, b) in spans.items()
            if not any(o != n and x <= a and b <= y and (y - x) > (b - a) for o, (x, y) in spans.items())]


def named_unit(candidates: list[dict], reply: str, authorized: set[str]) -> Outcome | None:
    """The one candidate (a document or a unit, as a clarification's ``as_dict`` or ``unit_candidate`` gives it) whose
    complete title or alias the reply states as a whole phrase, as a resolved outcome; None when the reply states none
    of them or several (the words of a name that several share name none)."""
    cands = [Candidate(str(c["document_id"]), c["title"], unit=c.get("unit"), file_title=c.get("file_title") or "")
             for c in candidates if str(c["document_id"]) in authorized]
    named = _named_by_phrase(cands, reply)
    return Outcome("resolved", named, reply) if len(named) == 1 else None


def among(candidates: list[dict], reply: str, authorized: set[str]) -> Outcome:
    """A reply to a clarification that named ``candidates``: the one candidate the reply names (by an ordinal word, by
    its complete title or alias, by a word of its name or identifiers that the other candidates lack, or by its number
    in the list), else the clarification again."""
    cands = [Candidate(str(c["document_id"]), c["title"], unit=c.get("unit"), file_title=c.get("file_title") or "")
             for c in candidates if str(c["document_id"]) in authorized]
    if not cands:
        return Outcome("not_found", query=reply)
    words = [w.strip("-'״׳\"?.!,") for w in base_normalize(reply).split()]
    for w in words:
        if w in _ORDINALS and _ORDINALS[w] < len(cands):
            return Outcome("resolved", [cands[_ORDINALS[w]]], reply)
    named = _named_by_phrase(cands, reply)  # "מתחם הגפן ב": the letter the tokenizer drops is part of the name
    if len(named) == 1:
        return Outcome("resolved", named, reply)
    if ordinal_reply(reply):  # "את השנייה", "לשלישית": an ordinal word with the prefix letters Hebrew puts before it
        forms = slots.ordinal_numbers(reply, len(cands))
        if len(forms) == 1 and 1 <= (n := next(iter(forms))) <= len(cands):
            return Outcome("resolved", [cands[n - 1]], reply)
    vocab = {id(c): _candidate_words(c) for c in cands}
    # a word of the reply that some candidates hold and others lack ("בכפר גפן", "ב", not "הנרקיס")
    letters = [{w} for w in words if len(w) == 1 and w.isalnum()]  # the letter or digit that tells "הגפן א" from "הגפן ב"
    telling = [forms for forms in [set(f) for f in _scope_terms(reply)] + letters
               if 0 < sum(1 for v in vocab.values() if v & forms) < len(cands)]
    named = [c for c in cands if telling and all(vocab[id(c)] & forms for forms in telling)]
    if len(named) == 1:
        return Outcome("resolved", named, reply)
    # a number alone ("2", "מספר 2") is the candidate's place in the list when no name holds it
    count = _COUNT.fullmatch(reply.strip().rstrip(".!"))
    if count and not telling and 1 <= int(count.group(1)) <= len(cands):
        return Outcome("resolved", [cands[int(count.group(1)) - 1]], reply)
    return Outcome("ambiguous", cands, reply)


_ORDINAL_REPLY = re.compile(
    r"(?:(?:אני\s+)?(?:מתכוון|מתכוונת|התכוונתי|בחרתי|רוצה)\s+)?(?:(?:ל|את|ב)\s*)?"
    r"(?:ה?(?:" + "|".join(sorted(slots.ORDINAL_WORDS, key=len, reverse=True)) + r")|(?:מספר|אפשרות|#)?\s*\d{1,2})"
    r"(?:\s+(?:בבקשה|תודה))?[.!]?")


def ordinal_reply(reply: str) -> bool:
    """Whether a reply is an ordinal or a list number and nothing more ("השנייה", "את השלישית", "2", "מספר 2",
    "התכוונתי לראשון"): the reply to a numbered question that needs no word of the model to be one (round 10 KTD5)."""
    return bool(_ORDINAL_REPLY.fullmatch(" ".join(base_normalize(reply or "").split()).strip()))


def names_several(candidates: list[dict], reply: str, authorized: set[str]) -> bool:
    """Whether a reply that picked no single candidate still names two or more of them — by a word of its own that
    several candidates hold ("הגפן" for "מתחם הגפן א" and "מתחם הגפן ב"). Only such a reply, called an answer by the model,
    is asked about again; a reply that names none ("כולן") is a new question (round 10 KTD5)."""
    cands = [Candidate(str(c["document_id"]), c["title"], unit=c.get("unit"), file_title=c.get("file_title") or "")
             for c in candidates if str(c["document_id"]) in authorized]
    if not cands or among(candidates, reply, authorized).kind == "resolved":
        return False
    vocab = [_candidate_words(c) for c in cands]
    return any(sum(1 for v in vocab if v & set(forms)) >= 2 for forms in _scope_terms(reply))


def _unit_names(conn) -> list[str]:
    """The titles and aliases of the units of the readable documents' current readings (an appendix or a comparables
    part names no document): the names a document states for the appraisals in it, which a follow-up may use
    ("הרקפות") though no file title holds them. Under row security."""
    names: list[str] = []
    for r in conn.execute(text(
            "SELECT u.title, u.aliases FROM document_units u JOIN document_versions v ON v.id = u.version_id"
            f" AND v.is_current AND u.reading_id = COALESCE(v.ingestion->>'reading_id', '') AND {unit_store.current_key_sql()}"
            f" JOIN documents d ON d.id = u.document_id AND d.deleted_at IS NULL AND {APPRAISALS_SQL}"
            " WHERE u.kind NOT IN ('appendix', 'comparables')")):
        names += ([r.title] if r.title else []) + list(r.aliases or ())
    return list(dict.fromkeys(names))


def _visible_titles(conn, unread: list[dict] | None = None) -> list[str]:
    """Every name that can name a document the user may see: the readable documents' titles, the names of their
    units (round 9 U9: an alias is an identifying word) and the titles of those whose latest version was not read
    (``unread``: those documents, when the caller has them already)."""
    if unread is None:
        unread = unread_documents(conn)
    return [r.title for r in conn.execute(text(
        "SELECT d.title FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
        f" WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}"))] + _unit_names(conn) + [d["title"] for d in unread]


def lookup(ctx: TenantContext, words: str, focus_ids: set[str],
           titles: Callable[[], list[str]] | None = None) -> Outcome:
    """The documents the user's words name, under the user's permissions (see the module docstring). ``titles``:
    every title the user may see, when the caller already holds them."""
    with tenant_tx(ctx) as conn:
        files = unread_documents(conn)  # read once: the lookups below match it, and name it when nothing is found
        searchable = bool(_scope_terms(words))
        matches = units_matching(conn, words) if searchable else UnitMatches()
        found = candidate_rows(matches)
        unread = unread_matching(conn, words, files) if searchable else []
        query = words
        if not found and not unread:
            # relaxed: the words that can name a document, then only the address or label among them
            vocabulary = titles() if titles else _visible_titles(conn, files)
            for relaxed in (" ".join(identifying(words, vocabulary)),
                            " ".join(identifying(words, vocabulary, pairs_only=True))):
                if relaxed and relaxed != query:
                    query = relaxed
                    matches = units_matching(conn, relaxed)
                    found, unread = candidate_rows(matches), unread_matching(conn, relaxed, files)
                    if found or unread:
                        break
        outcome = choose(found, query, focus_ids)
        named = choose(unread, query, set()) if unread else None
        best = max((c.title_terms for c in outcome.documents), default=-1)
        if named is not None and named.documents and named.documents[0].title_terms > best:
            # the words name an unread document by more of its title than any readable one: it is the answer
            for c in named.documents:
                c.unread = next(d for d in unread if str(d["document_id"]) == c.document_id)
            return Outcome("not_read", named.documents[:CLARIFY_TITLES], query)
        if outcome.kind == "not_found":
            outcome.unread = files
            outcome.split = matches.split
    return outcome


def candidate_rows(matches: UnitMatches) -> list[dict]:
    """The rows ``choose`` takes: a file of one unit as a document row, a file of several as one row per unit that
    holds all the words."""
    rows = []
    for d in matches.documents:
        if d.get("multi"):
            rows += [{**{k: v for k, v in d.items() if k != "units"}, "unit": u, "hits": u["hits"]}
                     for u in d["units"]]
        else:
            rows.append(d)
    return rows


def units_of_documents(ctx: TenantContext, ids) -> list[dict]:
    """The subject units of the given documents that hold several units, as clarification candidates (``Candidate.as_dict``),
    in reading order: what the model's own question between appraisals offered, when it left no candidates. Under the
    user's permissions; a document that is not readable, or is one unit, gives none."""
    uuids = []
    for i in ids or ():
        try:
            uuids.append(UUID(str(i)))
        except ValueError:
            continue
    if not uuids:
        return []
    with tenant_tx(ctx) as conn:
        files = {r.version_id: r for r in conn.execute(text(
            "SELECT d.id, d.title, v.id AS version_id, COALESCE(v.ingestion->>'reading_id', '') AS reading_id"
            " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
            f" WHERE d.id = ANY(:i) AND d.deleted_at IS NULL AND {APPRAISALS_SQL}"), {"i": uuids})}
        stored = unit_store.stored_many(conn, list(files))
        rows = []
        for version_id, units in stored.items():
            f = files[version_id]
            if len(units) < 2:
                continue
            rows += [{"document_id": f.id, "version_id": version_id, "reading_id": f.reading_id, "unit_no": u.unit_no,
                      "units_tag": unit_store.current_tag(),
                      "unit_id": contexts.unit_key(version_id, f.reading_id, u.unit_no, unit_store.current_tag()),
                      "kind": u.kind,
                      "title": u.title, "aliases": list(u.aliases),
                      "identifiers": list((u.identifiers or {}).get("shown") or ()), "page_first": u.page_first,
                      "page_last": u.page_last, "certainty": u.certainty, "file_title": f.title}
                     for u in units if u.kind not in contexts.NON_SUBJECT_KINDS]
        if rows:
            printed_labels(conn, rows)
    return [unit_candidate(u, u["file_title"]).as_dict() for u in rows]


# the words of the trade that name a kind of property or report, never one of them: a first message that holds only
# these (with the words of a unit's name) asks which appraisal, though no appraisal was named
GENERIC_NAME_WORDS = frozenset({"שומה", "שומת", "שומות", "נכס", "נכסים", "פרויקט", "פרויקטים", "מתחם", "מתחמים",
                                "בניין", "בניינים", "דוח", "דו\"ח", "חוות", "דעת", "הערכה", "שמאות", "דירה", "מגרש", "דיור"})
_UNIT_NAMES_SQL = (
    "SELECT u.document_id, u.version_id, u.reading_id, u.unit_no, u.kind, u.title, u.aliases, u.block_first,"
    " u.block_last, count(*) OVER (PARTITION BY u.version_id) AS units"
    " FROM document_units u JOIN document_versions v ON v.id = u.version_id AND v.is_current"
    f" AND u.reading_id = COALESCE(v.ingestion->>'reading_id', '') AND {unit_store.current_key_sql()}"
    f" JOIN documents d ON d.id = u.document_id AND d.deleted_at IS NULL AND {APPRAISALS_SQL}")


@dataclass
class FirstMessage:
    """What a turn's first look at its question found among the units (``first_message``): ``ambiguity`` — the unit
    clarification and the words it was looked up by; or ``unit`` — the one subject unit the question names, as its anchor
    (``anchor_with_id``) with ``version_id``, the version it is in."""

    ambiguity: tuple[Outcome, str] | None = None
    unit: dict | None = None
    version_id: str | None = None


def first_message_ambiguity(ctx: TenantContext, question: str) -> tuple[Outcome, str] | None:
    """The unit clarification a conversation's first message needs, with the words it was looked up by, or None (round 9
    R13, AE4). Follow-ups resolve names before anything is searched; a first message does not, so a question whose
    words fit two units of a file of several equally well ("מה דמי השכירות במתחם הגפן?" over "מתחם הגפן א" and "מתחם
    הגפן ב") went to the model, which answered both or asked in its own words. Only that case is decided here, from
    the names alone and in one query (the unit lookup runs only when it holds): the identifying words of the question
    are words of some unit's name, at least one of them no generic word of the trade; two or more subject units of
    files of several hold all of them in their names, and none fits strictly better (the words of a name it holds,
    then the fewest words of that name left unsaid); no unit's complete title or alias is stated in the question. The
    outcome is the lookup's, so it is the ambiguity the follow-up would have got: candidates ordered by name fit."""
    found = first_message(ctx, question, passages=False)
    return found.ambiguity if found else None


def _distinguishing(forms_of_term: list[str]) -> bool:
    """Whether a term of the question can tell one appraisal from another by its passages: no number, no word of the
    trade (``GENERIC_NAME_WORDS``) and no name of a kind of value."""
    from app.chat.meaning import metric_word

    return not (forms_of_term[0].isdigit() or set(forms_of_term) & GENERIC_NAME_WORDS or metric_word(forms_of_term[0]))


def first_message(ctx: TenantContext, question: str, passages: bool = True) -> FirstMessage | None:
    """The unit a question names before anything is searched (round 9: R13, AE4; the unit located at the turn's start),
    or None. In the order of trust, from the names the stored units state (one query):

    - the words of the question that are words of some unit's name fit two or more subject units equally well → the
      clarification (``first_message_ambiguity``); a unit fits only when the question states at least as many words of
      its best-fitting name as it leaves unsaid (one shared word of long names is no name: "הבית" of "בית גיל הזהב
      תל אביב"), and when no unit fits the passages decide;
    - they fit exactly one unit, or one of those they fit has its complete name stated → that unit;
    - no name decides (a file whose units state none: the title text was not recognised) and ``passages`` → the words
      that can tell appraisals apart (no number, no word of the trade, no name of a kind of value). Their holders are
      the subject units and the documents of the office that are not split into units. The words some holders hold
      and others do not: the one holder that holds every one of them, if it is a unit, else nothing. A word no holder
      has, or that every holder has, tells nothing; a tie by passages is no clarification. When no holder holds them
      all (an incidental word of another unit), the one holder of every word that only one holder in the office
      holds, if it is a unit that states every one of them in its opening (its title, aliases and first page); unique
      words of two holders, nothing. A document of one unit that holds the deciding words as well, or is the only
      holder, is the property's report as likely as a unit: nothing. Never chosen by counts of passages or of words
      held.

    Only the subject units of files of several count; a question with no word to decide by returns None."""
    with tenant_tx(ctx) as conn:
        units = [r for r in conn.execute(text(_UNIT_NAMES_SQL)) if r.units > 1 and r.kind not in contexts.NON_SUBJECT_KINDS]
    names = {(r.document_id, r.unit_no): ([r.title] if r.title else []) + list(r.aliases or ()) for r in units}
    if len(names) < 2:
        return None
    words = identifying(question, [n for ns in names.values() for n in ns], bare_numbers=False)
    if words and not all(forms(w) & GENERIC_NAME_WORDS for w in words):
        query = " ".join(words)
        term_sets = [set(f) for f in _scope_terms(query)]
        fits = {}  # the units whose names hold every word, by how many words of the best name the question leaves unsaid
        for key, ns in names.items():
            held, stated, extra = _best_name_fit(ns, term_sets)
            if len(held) == len(term_sets) and stated >= extra:  # one word of a long name is no name
                fits[key] = -extra
        if fits:
            best = max(fits.values())
            tied = [k for k, v in fits.items() if v == best]
            # a unit whose complete name the question states is the one asked about, however alike the others are
            stated = [key for key in tied if contexts.Identity(
                names=tuple(k for n in names[key] if (k := contexts.name_key(n)))).name_spans(question)]
            if len(tied) == 1 or len(stated) == 1:
                return _unit_found(ctx, (stated or tied)[0])
            if len(tied) > 1 and not stated:
                outcome = lookup(ctx, query, set())
                if outcome.kind == "ambiguous" and any(d.unit for d in outcome.documents):
                    return FirstMessage(ambiguity=(outcome, query))
            return None
    if not passages:
        return None
    key = _unit_by_passages(ctx, question, units)
    return _unit_found(ctx, key) if key is not None else None


def _unit_by_passages(ctx: TenantContext, question: str, units: list) -> tuple | None:
    """The (document id, unit number) of the one subject unit located by the words of the question that tell units
    apart, or None (see ``first_message``). The holders of a word are the units of ``units`` and the documents of the
    office that are not split into units. First rule: the words some holders hold and others do not; the one holder
    that has them all, if it is a unit. When none does: the unique words, those held by exactly one holder; if there is
    one and a single holder has them all, and it is a unit, and every unique word is in its opening (its title, its
    aliases, the blocks of its first page), that unit: a word deep in a unit's body is no name. A document of one unit that has all
    the deciding words, or all the unique ones, or two holders of the unique ones, None: that property's report may be
    the other file."""
    terms = [t for t in _scope_terms(_AMOUNT.sub(" ", question)) if _distinguishing(t)]
    if not terms:
        return None
    with tenant_tx(ctx) as conn:
        by_unit = unit_term_holders(conn, terms, units)
        docs = document_term_holders(conn, terms)
    held = {(u.document_id, u.unit_no): by_unit[(u.version_id, u.unit_no)] for u in units}
    held |= {(d, None): h for d, h in docs.items()}  # a document of one unit (or none) has no unit number
    deciding = [i for i in range(len(terms)) if 0 < sum(i in h for h in held.values()) < len(held)]
    holders = [k for k, h in held.items() if deciding and all(i in h for i in deciding)]
    if len(holders) == 1:
        return holders[0] if holders[0][1] is not None else None
    owners = [[k for k, h in held.items() if i in h] for i in range(len(terms))]
    sole = {o[0] for o in owners if len(o) == 1}  # the one holder of each unique word
    if len(sole) != 1 or next(iter(sole))[1] is None:
        return None
    key = next(iter(sole))
    unit = next(u for u in units if (u.document_id, u.unit_no) == key)
    patterns = _term_regexes([terms[i] for i, o in enumerate(owners) if len(o) == 1])
    with tenant_tx(ctx) as conn:
        opening = base_normalize(" ".join([unit.title or "", *(unit.aliases or ())])) + "\n" + unit_opening_text(
            conn, unit.version_id, unit.block_first, unit.block_last)
    return key if all(p.search(opening) for p in patterns) else None  # a unit names itself where it opens


def _unit_found(ctx: TenantContext, key: tuple) -> FirstMessage | None:
    """The unit ``key`` (document id, unit number) of the document's current reading, as the turn's located unit."""
    document_id, unit_no = key
    with tenant_tx(ctx) as conn:
        current = _current_units(conn, document_id)
    found = next((u for u in current[3] if u["unit_no"] == unit_no), None) if current else None
    return FirstMessage(unit=anchor_with_id(found), version_id=current[1]) if found is not None else None


def authorized(ctx: TenantContext, ids) -> set[str]:
    """The given document ids the user may see now (RLS decides; deleted documents are not seen)."""
    uuids = []
    for i in ids or ():
        try:
            uuids.append(UUID(str(i)))
        except ValueError:
            continue
    if not uuids:
        return set()
    with tenant_tx(ctx) as conn:
        return {str(r.id) for r in conn.execute(text(
            "SELECT id FROM documents WHERE id = ANY(:i) AND deleted_at IS NULL"), {"i": uuids})}


def titles_of(ctx: TenantContext) -> list[str]:
    """Every title the user may see."""
    with tenant_tx(ctx) as conn:
        return _visible_titles(conn)
