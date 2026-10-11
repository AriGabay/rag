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
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from app.chat import contexts
from app.db import TenantContext, tenant_tx
from app.extraction.normalize_text import base_normalize
from app.platform.search import (
    UnitMatches,
    _scope_terms,
    _title_words,
    units_matching,
    unread_documents,
    unread_matching,
)

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
    never reused across readings. ``pages`` and ``display`` are for showing it."""
    return {"document_id": str(u["document_id"]), "reading_id": u.get("reading_id") or None, "unit_no": u["unit_no"],
            "title": contexts.name_key(u.get("title")) or None, "display": u.get("title"),
            "aliases": [k for a in u.get("aliases") or () if (k := contexts.name_key(a))],
            "first_label": u.get("first_label"), "identifiers": list(u.get("identifiers") or ()),
            "pages": [u.get("page_first"), u.get("page_last")]}


def unit_pages(anchor: dict) -> str:
    first, last = (anchor.get("pages") or [None, None])[:2]
    return "" if first is None else f"עמוד {first}" if first == last else f"עמודים {first}–{last}"


def candidate_label(c: dict) -> str:
    """How a clarification names a candidate: a document by its title; a unit by its title (or that it has none), its
    pages, its identifiers and the file."""
    unit = c.get("unit")
    if not unit:
        return f"«{c['title']}»"
    parts = [p for p in (unit_pages(unit), " · ".join(unit.get("identifiers") or ())) if p]
    return (f"«{unit.get('display') or 'ללא כותרת'}» ({'; '.join(parts)}{'; ' if parts else ''}"
            f"בקובץ «{c.get('file_title') or c['title']}»)")


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
            c.unit = unit_anchor(unit) | {"unit_id": unit.get("unit_id"), "certainty": unit.get("certainty")}
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


def among(candidates: list[dict], reply: str, authorized: set[str]) -> Outcome:
    """A reply to a clarification that named ``candidates``: the one candidate the reply names (by an ordinal word, by
    a word of its name or identifiers that the other candidates lack, or by its number in the list), else the
    clarification again."""
    cands = [Candidate(str(c["document_id"]), c["title"], unit=c.get("unit"), file_title=c.get("file_title") or "")
             for c in candidates if str(c["document_id"]) in authorized]
    if not cands:
        return Outcome("not_found", query=reply)
    words = [w.strip("-'״׳\"?.!,") for w in base_normalize(reply).split()]
    for w in words:
        if w in _ORDINALS and _ORDINALS[w] < len(cands):
            return Outcome("resolved", [cands[_ORDINALS[w]]], reply)
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


def _unit_names(conn) -> list[str]:
    """The titles and aliases of the units of the readable documents' current readings (an appendix or a comparables
    part names no document): the names a document states for the appraisals in it, which a follow-up may use
    ("הרקפות") though no file title holds them. Under row security."""
    names: list[str] = []
    for r in conn.execute(text(
            "SELECT u.title, u.aliases FROM document_units u JOIN document_versions v ON v.id = u.version_id"
            " AND v.is_current AND u.reading_id = COALESCE(v.ingestion->>'reading_id', '')"
            " JOIN documents d ON d.id = u.document_id AND d.deleted_at IS NULL"
            " WHERE u.kind NOT IN ('appendix', 'comparables')")):
        names += ([r.title] if r.title else []) + list(r.aliases or ())
    return list(dict.fromkeys(names))


def _visible_titles(conn) -> list[str]:
    """Every name that can name a document the user may see: the readable documents' titles, the names of their
    units (round 9 U9: an alias is an identifying word) and the titles of those whose latest version was not read."""
    return [r.title for r in conn.execute(text(
        "SELECT d.title FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
        " WHERE d.deleted_at IS NULL"))] + _unit_names(conn) + [d["title"] for d in unread_documents(conn)]


def lookup(ctx: TenantContext, words: str, focus_ids: set[str],
           titles: Callable[[], list[str]] | None = None) -> Outcome:
    """The documents the user's words name, under the user's permissions (see the module docstring). ``titles``:
    every title the user may see, when the caller already holds them."""
    with tenant_tx(ctx) as conn:
        searchable = bool(_scope_terms(words))
        matches = units_matching(conn, words) if searchable else UnitMatches()
        found = candidate_rows(matches)
        unread = unread_matching(conn, words) if searchable else []
        query = words
        if not found and not unread:
            # relaxed: the words that can name a document, then only the address or label among them
            vocabulary = titles() if titles else _visible_titles(conn)
            for relaxed in (" ".join(identifying(words, vocabulary)),
                            " ".join(identifying(words, vocabulary, pairs_only=True))):
                if relaxed and relaxed != query:
                    query = relaxed
                    matches = units_matching(conn, relaxed)
                    found, unread = candidate_rows(matches), unread_matching(conn, relaxed)
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
            outcome.unread = unread_documents(conn)
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
