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
  says no such document was found. The previous property is never the answer instead.

A reply to such a clarification is resolved among the documents it named, and only among them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from app.db import TenantContext, tenant_tx
from app.extraction.normalize_text import base_normalize
from app.platform.search import _scope_terms, _title_words, documents_matching

CLARIFY_TITLES = 5
_TOKEN = re.compile(r"[\w\"״׳'/-]+")
_ORDINALS = {"הראשון": 0, "הראשונה": 0, "הראשונים": 0, "השני": 1, "השנייה": 1, "השניה": 1, "השלישי": 2,
             "השלישית": 2, "הרביעי": 3, "הרביעית": 3, "החמישי": 4, "החמישית": 4}


@dataclass
class Candidate:
    document_id: str
    title: str
    title_terms: int = 0
    hits: int = 0

    def as_dict(self) -> dict:
        return {"document_id": self.document_id, "title": self.title}


@dataclass
class Outcome:
    kind: Literal["resolved", "ambiguous", "not_found"]
    documents: list[Candidate] = field(default_factory=list)
    query: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "query": self.query, "documents": [d.as_dict() for d in self.documents]}


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


def choose(found: list[dict], query: str, focus_ids: set[str]) -> Outcome:
    """The outcome of a lookup (rows of ``documents_matching`` for ``query``), by the rules of the module. A number
    the user named counts as a title term however short ("הנרקיס 4"), and a title that holds other numbers but not
    the named one is another property ("הנרקיס 14")."""
    asked = _numbers(query)
    words = [f for f in _scope_terms(query) if not any(c.isdigit() for c in f[0])]
    cands = []
    for d in found:
        title = title_tokens(d["title"])
        named = {t for t in title if any(c.isdigit() for c in t)}
        # a one-digit house number the title does not hold: another property ("הנרקיס 4" is not "הנרקיס 14").
        # A longer number was a search term, so the document already holds it (a comparable it names: "האורן 30" in
        # the report on "האורן 26"); a unit label ("A2") is no house number
        house, held = {t for t in asked if t.isdigit() and len(t) == 1}, {t for t in named if t.isdigit()}
        if house and held and not house & held:
            continue
        cands.append(Candidate(str(d["document_id"]), d["title"],
                               sum(1 for f in words if title & set(f)) + len(asked & named), int(d.get("hits") or 0)))
    if not cands:
        return Outcome("not_found", query=query)
    best = max(c.title_terms for c in cands)
    tier = [c for c in cands if c.title_terms == best]  # best == 0: every document that holds the words in its text
    if len(tier) == 1:
        return Outcome("resolved", tier, query)
    focused = [c for c in tier if c.document_id in focus_ids]
    if len(focused) == 1:
        return Outcome("resolved", focused, query)
    tier.sort(key=lambda c: (-c.hits, c.title, c.document_id))
    return Outcome("ambiguous", tier[:CLARIFY_TITLES], query)


def among(candidates: list[dict], reply: str, authorized: set[str]) -> Outcome:
    """A reply to a clarification that named ``candidates``: the one candidate the reply names (by an ordinal, or
    by words of its title that the other candidates' titles lack), else the clarification again."""
    cands = [Candidate(str(c["document_id"]), c["title"]) for c in candidates if str(c["document_id"]) in authorized]
    if not cands:
        return Outcome("not_found", query=reply)
    words = [w.strip("-'״׳\"?.!,") for w in base_normalize(reply).split()]
    for w in words:
        if w in _ORDINALS and _ORDINALS[w] < len(cands):
            return Outcome("resolved", [cands[_ORDINALS[w]]], reply)
    titles = {c.document_id: title_tokens(c.title) for c in cands}
    # a word of the reply that some candidates' titles hold and others lack ("בכפר גפן", not "הנרקיס")
    telling = [set(forms) for forms in _scope_terms(reply)
               if 0 < sum(1 for t in titles.values() if t & set(forms)) < len(cands)]
    named = [c for c in cands if telling and all(titles[c.document_id] & forms for forms in telling)]
    if len(named) == 1:
        return Outcome("resolved", named, reply)
    return Outcome("ambiguous", cands, reply)


def _visible_titles(conn) -> list[str]:
    return [r.title for r in conn.execute(text(
        "SELECT d.title FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
        " WHERE d.deleted_at IS NULL"))]


def lookup(ctx: TenantContext, words: str, focus_ids: set[str],
           titles: Callable[[], list[str]] | None = None) -> Outcome:
    """The documents the user's words name, under the user's permissions (see the module docstring). ``titles``:
    every title the user may see, when the caller already holds them."""
    with tenant_tx(ctx) as conn:
        found = documents_matching(conn, words) if _scope_terms(words) else []
        query = words
        if not found:
            # relaxed: the words that can name a document, then only the address or label among them
            vocabulary = titles() if titles else _visible_titles(conn)
            for relaxed in (" ".join(identifying(words, vocabulary)),
                            " ".join(identifying(words, vocabulary, pairs_only=True))):
                if relaxed and relaxed != query:
                    query = relaxed
                    found = documents_matching(conn, relaxed)
                    if found:
                        break
    return choose(found, query, focus_ids)


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
