"""The conversation's slots (round 10 U7; KTD4, KTD5, KTD6, KTD7; R10-R15; round 22 U12: KTD14, R20-R22): what an answer leaves for the
next turn, kept apart so that one never erases another.

Every assistant message stores ``answer.context`` (schema version ``VERSION``; server-only, ``api._message_json`` never sends it, and it
is not a document of the message, so a later professional answer is not hidden by the property it carries):

- ``lists``: up to ``MAX_LISTS`` named lists the last ``LIST_AGE`` answers printed, newest first - each ``{id, title, kind, items:
  [{label, ref?}], total, message_id, document_ids, age}``. The answer's own structure builds them **when the answer is composed**
  (``lists_of``: its numbered and bulleted lists, the rows of its tables, the citation an item carries as ``ref``); a later turn never
  parses them from the stored Markdown. ``title`` is the heading or lead line of the list, ``kind`` the noun its items share, ``total``
  the count of items the list printed (the items stored are at most ``MAX_LIST_ITEMS``, so "ה-12" of a list of 15 exists), and
  ``document_ids`` the documents its items cite (else the documents of the answer): the list is dropped, alone, when one is no longer
  visible (``gate_lists``), its id kept in ``tombstones``, and no other list is renumbered - an id is ``<message>#<position>``, never a
  place in the carried order. ``topic``: ``{text, message_id}`` - the professional subject in the user's words;
- ``property``: ``{document_ids, located_unit, subject}`` - the property in the centre of the conversation - and ``params``: the task
  parameters of its datum (metric, unit, period, VAT, area basis, the date field, and the wording and role the focus carried). A
  professional answer leaves both **unchanged**: they are dormant, not dropped, and a later "בנכס הזה" restores them together;
- ``period``: ``{requested, anchor, source_message_id}`` - the time the question of the property turn stated (``timeframe.TimeMode``).
  It belongs to the property: it carries to a follow-up that names no time and keeps the property, a professional turn leaves it dormant,
  and it expires when the property changes or the new question states time (round 22 U12, R21);
- ``sources``: what the answer cited - ``{document_id, source_kind, standard, section}``, the kind and the identity (number, name,
  edition, effective date, section) as the cited source carried them (round 10 U8);
- ``pending``: ``{kind, payload, asked_at}`` - the clarification this answer asked (``candidates`` or ``parameter``). It lives exactly one
  message: each turn writes only its own, so a pending one that the next message did not answer is simply not carried.

A context of version 1 (``topic.items``) is read as one list (``upgrade_v1``); an answer stored before the slots has none and is read by
``api._legacy_context``, the only caller of the Markdown extractor (``legacy_lists``).

Everything here is pure: the functions take and return plain data, so the slots are tested without a database.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from app.extraction.normalize_text import FINALS

VERSION = 2
MAX_LISTS = 3
LIST_AGE = 3  # an answer's lists are carried while it is one of the last three answers (the last six messages)
MAX_LIST_ITEMS = 40
ITEM_CHARS = 80
TITLE_CHARS = 80
TOPIC_CHARS = 200
MAX_SOURCES = 30
MAX_TOMBSTONES = 6
# the intents that are about the trade (``intent.Value``); defined here, the leaf ``intent`` imports, so that
# ``TOPIC_INTENTS`` below can name them
PROFESSIONAL = "professional"
MIXED = "mixed"
# the intents whose answer is about a topic of the trade: they write the topic slot
TOPIC_INTENTS = (PROFESSIONAL, MIXED)
# the task parameters of the datum a property turn answered (the focus fields that are not the property itself); ``date_field`` is the
# kind of date the datum carries (round 22 U12: "ומה לגבי המועד" refers to the date field of the same datum)
PARAM_KEYS = ("metric_as_written", "metric_kind", "unit", "period", "vat", "area_basis", "value_role", "date_field")
_UNKNOWN_FOCUS = {"metric_as_written": "", "metric_kind": "unknown", "unit": "unknown", "period": "unknown",
                  "area_basis": "", "vat": "unknown", "value_role": "unknown"}


def clip(text: str, limit: int) -> str:
    """The text on one line, at most ``limit`` characters."""
    return " ".join((text or "").split())[:limit].rstrip()


# --- the structure of a composed answer: its lists and tables ------------------------------------------------------------------

_LIST_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<mark>\d{1,3}[.)]|[-*•])[ \t]+(?P<body>\S.*)$")
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_CITATION = re.compile(r"\s*\\?\[[SMCPVA]\d+\\?\]")
_REF = re.compile(r"\\?\[([SMCVA]\d+)")
_BOLD_LEAD = re.compile(r"^\s*(\*\*|__)(.+?)\1")
_LABEL_END = re.compile(r"\s*[—–]\s*|:(?:\s|$)")
_EDGE_MARKUP = re.compile(r"(?<!\w)[*_]+|[*_]+(?!\w)|`")
_TOTAL_ROW = re.compile(r"^\W*סה[\"״']?כ")


@dataclass
class _Block:
    """One list or table of a text: its ``shape`` (numbered, bulleted, table), the line of text before it (``lead``), the first column's
    header for a table, and per item its label source (``bodies``) and the whole text it stands in (``texts``)."""

    shape: str
    lead: str
    header: str = ""
    bodies: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)


def _label(body: str) -> str:
    """An item's label: its bold lead, else the text before "-" or ":" - without markup, citations and closing
    punctuation."""
    text = _CITATION.sub("", body)
    lead = _BOLD_LEAD.match(text)
    label = lead.group(2) if lead else _LABEL_END.split(text, 1)[0]
    label = _EDGE_MARKUP.sub("", label)
    return clip(label, ITEM_CHARS).strip(" \t:.,;-–—")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    body = line.strip()
    return "-" in body and set(body) <= set("|-: \t")


def _structure(markdown: str) -> list[_Block]:
    """The lists and tables of a text in order, each with the line of text before it. A nested list is not an item, an
    indented line continues its item, a paragraph of text ends the list."""
    blocks: list[_Block] = []
    cur: _Block | None = None
    table: _Block | None = None
    body_rows = False
    base, gap, lead = 0, False, ""
    for line in (markdown or "").splitlines():
        if _TABLE_LINE.match(line):
            cur, gap = None, False
            cells = _cells(line)
            if table is None:
                table, body_rows = _Block("table", lead, header=_EDGE_MARKUP.sub("", cells[0] if cells else "").strip()), False
                blocks.append(table)
            elif not body_rows:
                if _is_separator(line):
                    body_rows = True
                else:  # a row of pipes with no separator under it is text, not a table
                    blocks.remove(table)
                    table, lead = None, line.strip()
            else:
                first = next((c for c in cells if c), "")
                table.bodies.append(first)
                table.texts.append(line)
            continue
        table = None
        m = _LIST_LINE.match(line)
        if m:
            indent, numbered = len(m.group("indent").expandtabs(4)), m.group("mark")[0].isdigit()
            shape = "numbered" if numbered else "bulleted"
            if cur is not None and (indent < base or (indent == base and shape != cur.shape)):
                cur = None
            if cur is None:
                cur, base = _Block(shape, lead), indent
                blocks.append(cur)
            if indent == base:
                cur.bodies.append(m.group("body"))
                cur.texts.append(m.group("body"))
            gap = False
        elif not line.strip():
            gap = True
        else:
            if not (cur is not None and not gap and line[:1].isspace()):  # an indented line continues its item
                cur, lead = None, line.strip()
            gap = False
    return blocks


def _title(lead: str) -> str:
    """The title of a list from the line before it: a heading or a lead sentence, without markup and the closing colon."""
    text = _CITATION.sub("", lead or "").lstrip("#").strip()
    return clip(_EDGE_MARKUP.sub("", text), TITLE_CHARS).strip(" \t:.,;-–—")


def _first_word(label: str) -> str:
    words = re.findall(r"[א-תA-Za-z]+", label or "")
    return words[0] if words else ""


def _kind(labels: list[str], header: str = "") -> str:
    """The noun the items of a list share: the first word most of its labels start with ("תשלום" of "תשלום ראשון", "תשלום שני"), else
    the header of a table's first column."""
    counts = Counter(w for w in (_first_word(label) for label in labels) if len(w) >= 2)
    if counts:
        word, n = counts.most_common(1)[0]
        if n >= 2 and n * 2 >= len(labels):
            return word
    return _first_word(header)


def _item_label(shape: str, body: str, header: str) -> str:
    label = _label(body)
    if shape == "table" and header and label and (label.isdigit() or len(label) <= 3):
        return clip(f"{header} {label}", ITEM_CHARS)  # a table whose first column is a bare number is named by its header
    return label


def lists_of(markdown: str, docs_of: Callable[[str], Iterable[str]] | None = None,
             fallback_documents: Iterable[str] = ()) -> list[dict]:
    """The lists of a **composed** answer (round 22 U12, KTD14), at compose time: each numbered or bulleted list and each table of at
    least two items, in the order of the text, as ``{title, kind, items: [{label, ref?}], total, document_ids}`` (``carry`` stamps the id
    and the message). An item's ``ref`` is the first citation it carries (a V#, S#, C#). ``docs_of(text)``: the documents an item's text
    cites; a list whose items cite none takes ``fallback_documents`` (the documents the answer cites). An answer that enumerates in prose
    only (bold terms) has one list of those. The text is the answer without the server's own spans."""
    out: list[dict] = []
    for b in _structure(markdown):
        items, docs = [], set()
        for body, text in zip(b.bodies, b.texts, strict=True):
            label = _item_label(b.shape, body, b.header)
            if not label or (b.shape == "table" and _TOTAL_ROW.match(label)):
                continue
            ref = _REF.search(text)
            items.append({"label": label} | ({"ref": ref.group(1)} if ref else {}))
            if docs_of is not None:
                docs |= {str(d) for d in docs_of(text) if d}
        if len(items) >= 2:
            out.append({"title": _title(b.lead), "kind": _kind([i["label"] for i in items], b.header), "items": items,
                        "total": len(items), "document_ids": sorted(docs or {str(d) for d in fallback_documents})})
    if out:
        return out
    terms = _bold_items(markdown)
    return [{"title": "", "kind": _kind(terms), "items": [{"label": t} for t in terms], "total": len(terms),
             "document_ids": sorted(str(d) for d in fallback_documents)}] if terms else []


def extract_items(markdown: str) -> list[str]:
    """LEGACY ONLY (round 22 U12, R2): the items a stored answer listed, read from its Markdown - the top-level numbered or bulleted items
    of the first list of at least two (a numbered list before bullets), each labelled by ``_label``. The only caller is
    ``legacy_lists``, for an answer stored before it had lists of its own; nothing else reads a list out of Markdown. A table is no list
    here. At most ``MAX_LIST_ITEMS``."""
    lists = [b for b in _structure(markdown) if b.shape != "table" and len(b.bodies) >= 2]
    chosen = next((b for b in lists if b.shape == "numbered"), lists[0] if lists else None)
    if chosen is None:
        return _bold_items(markdown)
    return [label for body in chosen.bodies if (label := _label(body))][:MAX_LIST_ITEMS]


def legacy_lists(markdown: str, document_ids: Iterable[str] = ()) -> list[dict]:
    """The one list a legacy answer (stored with no ``context``) printed, as a list record (see ``lists_of``): its Markdown is read by the
    legacy extractor, and its documents are those of the answer."""
    labels = extract_items(markdown)
    return [{"title": "", "kind": _kind(labels), "items": [{"label": x} for x in labels], "total": len(labels),
             "document_ids": sorted(str(d) for d in document_ids)}] if len(labels) >= 2 else []


_BOLD = re.compile(r"(\*\*|__)(?P<b>[^*_\n]{2,80}?)\1")


def _split_enumeration(bold: str) -> list[str]:
    """A bold span that enumerates several items in one ("גישת ההשוואה, גישת ההכנסות וגישת העלות") as its items: split at
    the commas, and the last at its "ו" when the parts share their first word ("וגישת" of "גישת"); any other span is one
    item."""
    text = _EDGE_MARKUP.sub("", bold)
    parts = [p.strip(" \t:.;-–—") for p in re.split(r"\s*,\s*", text)]
    if len(parts) >= 2:
        first = parts[0].split()[0] if parts[0].split() else ""
        last = re.split(r"\s+ו(?=" + re.escape(first) + r")", parts[-1], maxsplit=1) if first else [parts[-1]]
        parts = parts[:-1] + [p.strip() for p in last]
        parts = [p[1:] if p.startswith("ו" + first) else p for p in parts]
    return [clip(p, ITEM_CHARS) for p in parts if p]


def _bold_items(markdown: str) -> list[str]:
    """The items of an answer that enumerated them in prose, with no list (the real model's usual shape for "what are
    the three approaches"): the bold terms of the first paragraph that holds two or more of them, or - when no paragraph
    does - the bold leads of consecutive paragraphs. A bold heading line on its own is a heading, not an item."""
    paragraphs = [p for p in re.split(r"\n\s*\n", _CITATION.sub("", markdown or "")) if p.strip()]
    for para in paragraphs:
        terms = [t for m in _BOLD.finditer(para) for t in _split_enumeration(m.group("b"))]
        terms = list(dict.fromkeys(t for t in terms if t))
        if len(terms) >= 2:
            return terms[:MAX_LIST_ITEMS]
    leads = []
    for para in paragraphs:
        lead = _BOLD_LEAD.match(para)
        rest = para[lead.end():].strip() if lead else ""
        if lead and rest:
            leads.append(clip(_EDGE_MARKUP.sub("", lead.group(2)), ITEM_CHARS).strip(" \t:.,;-–—"))
        elif leads:
            break
    return leads[:MAX_LIST_ITEMS] if len(leads) >= 2 else []


# --- an ordinal that names an item --------------------------------------------------------------------------------------

ORDINAL_WORDS = {"ראשון": 1, "ראשונה": 1, "שני": 2, "שנייה": 2, "שניה": 2, "שלישי": 3, "שלישית": 3, "רביעי": 4,
                 "רביעית": 4, "חמישי": 5, "חמישית": 5, "שישי": 6, "שישית": 6, "שביעי": 7, "שביעית": 7, "שמיני": 8,
                 "שמינית": 8, "תשיעי": 9, "תשיעית": 9, "עשירי": 10, "עשירית": 10, "אחרון": -1, "אחרונה": -1}
_ORDINAL_ALTERNATIVES = "|".join(sorted(ORDINAL_WORDS, key=len, reverse=True))
# an ordinal word with the prefix letters Hebrew puts before it ("השנייה", "בשנייה", "והשלישית"), not a longer word
_ORDINAL = re.compile(r"(?<![א-ת])[ולבכמש]{0,2}ה?(" + _ORDINAL_ALTERNATIVES + r")(?![א-ת])")
# the numbers that are not places in a list (N3): a block, a parcel, a standard or a section, a floor, a duration, an amount, a percentage,
# an area, a date, a year, a decimal, any number of three digits or more
_NOT_AN_ORDINAL = re.compile(
    r"גוש\D{0,6}?\d+|חלק(?:ה|ות)?\D{0,6}?\d+"
    r"|(?<![א-ת])[והבלמכש]{0,3}(?:תקנים|תקן|סעיפים|סעיף|פרק|פרקים)\s*(?:מס[׳'.]?|מספר)?\s*\d+(?:\.\d+)*"
    r"|(?:קומה|קומת|קומות)\s*-?\d+|\d+\s*קומות"
    r"|\d+\s*(?:חודשים|חודש|שנים|שנה|ימים|יום|שבועות|שבוע|דקות)(?![א-ת])"
    r"|\d[\d,.]*\s*(?:%|אחוז|₪|ש\"ח|שקל|מ\"ר|מטר|דונם)"
    r"|\d{1,4}[./]\d{1,2}(?:[./]\d{2,4})?|\d+\.\d+|\d{3,}")
# a number of one or two digits that stands alone, or after "ה" or "ל" and a hyphen ("ה-12", "ל-5"): not part of a longer number, a decimal, a
# date or a percentage, and not after another letter and a hyphen ("ב-12")
_DIGIT_FORM = re.compile(r"(?<![\w.,/\-־])(?:[הל]\s?[-־‐]\s?)?(\d{1,2})(?![\d,/%]|\.\d)")
_WORD = re.compile(r"[א-תA-Za-z0-9\"'׳״]+")
_BOTH = re.compile(r"(?<![א-ת])[ולבכמש]{0,2}(?:שניהם|שניהן|שתיהם|שתיהן)(?![א-ת])")


def ordinal_spans(message: str, length: int | None = None) -> list[tuple[int, int, int]]:
    """The places in a list a message writes, as ``(start, end, number)``: ordinal words (1 to 10, ``-1`` for "the last") always, and
    numbers of one or two digits (also "ה-12", "ל-5") only when a list is in scope (``length``: its item count) and the number is within
    it. A number that is a year, a block or parcel, a standard or section, a floor, a duration, a percentage, an amount, an area or a date is
    never a place (N3)."""
    text = message or ""
    found = [(m.start(), m.end(), ORDINAL_WORDS[m.group(1)]) for m in _ORDINAL.finditer(text)]
    if length:
        masked = _NOT_AN_ORDINAL.sub(lambda m: " " * len(m.group()), text)
        found += [(m.start(), m.end(), int(m.group(1))) for m in _DIGIT_FORM.finditer(masked) if 1 <= int(m.group(1)) <= length]
    return sorted(found)


def ordinal_forms(message: str, length: int | None = None) -> set[int]:
    """The item numbers a message writes (``ordinal_spans``): ordinal words, and - when a list of ``length`` items is in scope - numbers
    within it; ``-1`` for "the last" (האחרון/ה)."""
    return {n for _, _, n in ordinal_spans(message, length)}


def ordinal_numbers(message: str, length: int) -> set[int]:
    """The item numbers a message writes (``ordinal_forms``) with "the last" resolved to ``length``, the number of items
    of the list it is read against."""
    return {length if f == -1 else f for f in ordinal_forms(message, length)}


# --- which list a message names, and which item of it -------------------------------------------------------------------------

# the prefix letters Hebrew puts before a word, in their order ("ובה", "שב", "מה"): at most one of each kind
_PREFIX = re.compile(r"ו?ש?[בלמכ]?ה?")
# the words that introduce a number without naming what it counts ("מספר 3", "פריט 3"): the noun is the word before them
_NUMBER_LABELS = {"מס", "מספר", "פריט", "אפשרות", "שורה"}
# the function words of a follow-up ("ומה לגבי השני"): never the noun that names a list (round 22 review #2)
_FUNCTION_WORDS = frozenset({"מה", "ומה", "זה", "אז", "עוד", "לגבי", "את"})


def _forms(word: str, base: bool = False) -> set[str]:
    """A word's forms for matching a noun to a list ("התשלומים" and "תשלום" share one): final letters unified, the prefix letters removed
    (every cut the prefix grammar allows: "ושומה" is also "שומה"), and the plural or feminine ending removed; forms of fewer than three letters
    are none. ``base``: the word is a base noun (a list's kind), which takes no prefix letters of its own: "שומה" is never "ומה"."""
    w = re.sub(r"[^א-תa-z0-9]", "", (word or "").lower()).translate(FINALS)
    stems = {w}
    if not base:
        stems |= {w[k:] for k in range(1, len(w)) if _PREFIX.fullmatch(w[:k]) and len(w) - k >= 3}
    for stem in list(stems):
        for suffix in ("ימ", "ות", "ה", "ת"):
            if stem.endswith(suffix) and len(stem) - len(suffix) >= 3:
                stems.add(stem[:-len(suffix)])
    return {x for x in stems if len(x) >= 3}


def _message_forms(word: str) -> set[str]:
    """The forms of a word of the user's message (``_forms``, with the prefix letters it may carry); none for a function word."""
    w = re.sub(r"[^א-תa-z0-9]", "", (word or "").lower())
    return set() if w in _FUNCTION_WORDS or (w[:1] == "ו" and w[1:] in _FUNCTION_WORDS) else _forms(w)


def _total(entry: dict) -> int:
    return max(int(entry.get("total") or 0), len(entry.get("items") or []))


def _label_of(entry: dict) -> str:
    """How a list is named to the user: its title, else its kind, else its first items."""
    title = entry.get("title") or entry.get("kind")
    if title:
        return title
    return ", ".join(i["label"] for i in (entry.get("items") or [])[:2]) + "…"


def _named(message: str, lists: list[dict], spans: list[tuple[int, int, int]]) -> list[dict]:
    """The lists the user's noun selects (KTD14, first in the binding order): the word just before an ordinal ("התשלום" of "התשלום השני")
    matched to a list's kind or the words of its title; else any word of the message that is a list's kind ("השלישי מבין התשלומים")."""
    tokens = [(m.end(), m.group()) for m in _WORD.finditer(message or "")]
    strong: set[str] = set()
    for start, _, _ in spans:
        before = [t for t in tokens if t[0] <= start]
        while before and before[-1][1].strip("'׳\"״.") in _NUMBER_LABELS:
            before.pop()
        if before:  # the word right before the ordinal is the noun it counts ("התשלום" of "התשלום השני")
            strong |= _message_forms(before[-1][1])
    weak: set[str] = set()
    for _, word in tokens:
        weak |= _message_forms(word)

    def kind_forms(entry: dict) -> set[str]:
        return _forms(entry.get("kind") or "", base=True)

    def title_forms(entry: dict) -> set[str]:
        out: set[str] = set()
        for word in (entry.get("title") or "").split():
            out |= _forms(word)
        return out

    by_noun = [entry for entry in lists if strong & (kind_forms(entry) | title_forms(entry))]
    return by_noun or [entry for entry in lists if weak & kind_forms(entry)]


@dataclass(frozen=True)
class Binding:
    """What a reference to a list item bound to (``bind``). ``kind``: ``item`` (``items`` holds the one place), ``set`` (both items of a
    two-item list), ``ask`` (``ask`` is the one clarification question that names the lists) or ``none`` (``reason`` says why nothing
    was bound; ``no_list``: an ordinal was written and no list was given)."""

    kind: str = "none"
    list_id: str = ""
    title: str = ""
    items: tuple[int, ...] = ()
    labels: tuple[str, ...] = ()
    reason: str = ""
    ask: str = ""
    no_list: bool = False

    @property
    def bound(self) -> bool:
        return self.kind in ("item", "set")


MSG_ASK_LISTS = "לא ברור לאיזו רשימה התכוונת: {names}. לאיזו מהן?"
MSG_ASK_BOTH = "«שניהם» מתאים לשני פריטים, אבל הרשימה «{title}» כוללת {n} פריטים. לאילו מהם התכוונת?"


def _ask_lists(pool: list[dict]) -> str:
    names = []
    for entry in pool:
        older = " - מתשובה קודמת" if entry.get("age") else ""
        names.append(f"«{_label_of(entry)}» ({_total(entry)} פריטים{older})")
    return MSG_ASK_LISTS.format(names=" או ".join(names))


def _newest(pool: list[dict]) -> list[dict]:
    """The lists of the most recent answer among ``pool`` (carried newest first)."""
    return [entry for entry in pool if entry.get("message_id") == pool[0].get("message_id")]


def _places(entry: dict, spans: list[tuple[int, int, int]]) -> set[int]:
    """The places the message writes within a list: "the last" is the list's last, a number must be within its count."""
    total = _total(entry)
    return {p for p in ((total if n == -1 else n) for _, _, n in spans) if 1 <= p <= total}


def bind(message: str, lists: Iterable[dict] | None, *, list_id: str = "", item: int | None = None,
         item_set: Iterable[int] = (), both: bool = False) -> Binding:
    """What a reference to a list item binds to, held to the message's own words and to the lists (KTD14, R20). The list is chosen in this
    order, and the model's claim (``item``; ``list_id``, ``item_set``, ``both``) is validated against the SELECTED list, never any list:

    1. the list the user's noun names ("התשלום השני" selects the payments list, by its kind or the words of its title);
    2. the only list in which the place exists;
    3. the most recent answer's list, when the places exist in lists of different answers;
    4. otherwise one clarification that names the lists (``Binding.ask``): "השני" alone over two lists of one answer asks.

    "שניהם"/"שתיהן" bind only to a list of exactly two items (the one list the noun names, else the only list of the latest answer); over a
    list of more it asks - it is never "all". A number is read as a place only when a list with at least that many items is in scope."""
    pool = [entry for entry in lists or () if entry.get("items")]
    if not pool:
        return Binding(reason="no list was given in the previous answer", no_list=bool(ordinal_forms(message)))
    spans = ordinal_spans(message, max(_total(entry) for entry in pool))
    named = _named(message, pool, spans)
    if _BOTH.search(message or ""):
        return _bind_both(named or _newest(pool))
    if both or list(item_set):
        return Binding(reason="an item set was claimed, but the message does not say «שניהם» or «שתיהן»")
    if not spans:
        return Binding(reason="no ordinal or number in the message")
    scope = named or pool
    fits = [entry for entry in scope if _places(entry, spans)]
    if not fits:
        return Binding(reason="the place the message writes does not exist in " + (
            "the list it names" if named else "any list") + ": " + ", ".join(f"{_label_of(e)} ({_total(e)})" for e in scope))
    selected = fits[0] if len(fits) == 1 else None
    if selected is None:
        newest = _newest(fits)
        if len(newest) < len(fits) and len(newest) == 1:
            selected = newest[0]
    if selected is None:
        return Binding(kind="ask", ask=_ask_lists(fits), reason="the message fits " + str(len(fits)) + " lists and names none")
    places = _places(selected, spans)
    stored = len(selected["items"])
    if item is None or not 1 <= item <= _total(selected):
        return Binding(reason=f"item {item} does not exist in the list «{_label_of(selected)}» of {_total(selected)}")
    if item not in places:
        return Binding(reason=f"item {item} contradicts the place the message writes in the list «{_label_of(selected)}»")
    if item > stored:
        return Binding(reason=f"item {item} is in the list «{_label_of(selected)}» of {_total(selected)} but only {stored} were stored")
    note = f"; the model named {list_id}" if list_id and list_id != selected.get("id") else ""
    return Binding("item", selected.get("id", ""), _label_of(selected), (item,), (selected["items"][item - 1]["label"],),
                   reason=f"list «{_label_of(selected)}»{note}")


def _bind_both(pool: list[dict]) -> Binding:
    if len(pool) == 1 and _total(pool[0]) == 2 and len(pool[0]["items"]) == 2:
        entry = pool[0]
        return Binding("set", entry.get("id", ""), _label_of(entry), (1, 2), tuple(i["label"] for i in entry["items"]),
                       reason=f"both items of the list «{_label_of(entry)}»")
    if len(pool) == 1:
        return Binding(kind="ask", ask=MSG_ASK_BOTH.format(title=_label_of(pool[0]), n=_total(pool[0])),
                       reason=f"«שניהם» over a list of {_total(pool[0])}")
    return Binding(kind="ask", ask=_ask_lists(pool), reason="«שניהם» fits several lists")


# --- the property and its parameters ------------------------------------------------------------------------------------------


def split_focus(focus: dict | None) -> tuple[dict | None, dict | None]:
    """A focus (the datum at the centre of the conversation) as the property slot and its task parameters."""
    if focus is None:
        return None, None
    prop = {"document_ids": list(focus.get("document_ids") or []), "located_unit": focus.get("located_unit"),
            "subject": focus.get("subject") or ""}
    return prop, {k: focus[k] for k in PARAM_KEYS if k in focus}


def join_focus(context: dict | None) -> dict | None:
    """The focus the engine reads, from the property slot and its parameters; None when there is no property."""
    prop = (context or {}).get("property")
    if not prop:
        return None
    out = _UNKNOWN_FOCUS | ((context or {}).get("params") or {}) | {
        "subject": prop.get("subject") or "", "document_ids": list(prop.get("document_ids") or [])}
    if prop.get("located_unit"):
        out["located_unit"] = prop["located_unit"]
    return out


def property_documents(context: dict | None) -> set[str]:
    """The documents the property slot rests on (its files and the file of its unit): what is re-authorized each turn."""
    prop = (context or {}).get("property") or {}
    ids = {str(d) for d in prop.get("document_ids") or []}
    if prop.get("located_unit"):
        ids.add(str(prop["located_unit"].get("document_id")))
    return ids


def list_documents(context: dict | None) -> set[str]:
    """The documents the carried lists rest on: what is re-authorized each turn, list by list."""
    return {str(d) for entry in (context or {}).get("lists") or [] for d in entry.get("document_ids") or []}


def gate_lists(context: dict, visible: set[str]) -> tuple[dict, list[str]]:
    """The context with each list checked against the documents the user sees now (R22): a list with a document no longer visible is
    dropped alone, whole, and its id kept in ``tombstones``; no other list is renumbered (an id is never a place). Returns the context and the
    ids dropped."""
    kept, dropped = [], []
    for entry in context.get("lists") or []:
        (kept if {str(d) for d in entry.get("document_ids") or []} <= visible else dropped).append(entry)
    if not dropped:
        return context, []
    ids = [entry.get("id", "") for entry in dropped]
    return context | {"lists": kept, "tombstones": [*(context.get("tombstones") or []), *ids][-MAX_TOMBSTONES:]}, ids


# --- the slots an answer leaves -----------------------------------------------------------------------------------------------


def stamp(lists: Iterable[dict], message_id: str | None) -> list[dict]:
    """This answer's lists as stored: an id of the message and the list's position in it, the items clipped and capped, the total kept."""
    out = []
    for n, entry in enumerate(lists, 1):
        items = [{k: (clip(v, ITEM_CHARS) if k == "label" else v) for k, v in i.items()}
                 for i in entry.get("items") or [] if clip(i.get("label", ""), ITEM_CHARS)]
        if len(items) < 2:
            continue
        out.append({"id": f"{message_id or 'm'}#{n}", "title": clip(entry.get("title", ""), TITLE_CHARS),
                    "kind": clip(entry.get("kind", ""), 40), "items": items[:MAX_LIST_ITEMS],
                    "total": max(int(entry.get("total") or 0), len(items)), "message_id": message_id,
                    "document_ids": sorted(str(d) for d in entry.get("document_ids") or []), "age": 0})
    return out


def carry(previous: dict | None, *, intent: str, property: dict | None = None, params: dict | None = None,
          topic: str = "", lists: Iterable[dict] = (), message_id: str | None = None,
          sources: Iterable[dict] = (), pending: dict | None = None, period: dict | None = None) -> dict:
    """The context this answer stores (KTD4, KTD14), from the previous one and this turn's: the property found (``property``,
    ``params``), the requested period (``period``), the topic text, the lists it printed (``lists_of`` at compose time), what it cited and
    what it asks. A professional turn leaves the previous property, parameters and period unchanged - dormant, not dropped; every other turn
    writes its own, and a period belongs to a property, so it is none without one. The lists of this answer come first, then those of the
    last answers still within ``LIST_AGE`` (at most ``MAX_LISTS`` in all, older ones aged by one); the topic's text is replaced by a
    professional or mixed turn that has one. Only this turn's pending clarification is kept (it lives one message)."""
    prev = previous or {}
    professional = intent == PROFESSIONAL
    prop = prev.get("property") if professional else property
    prm = (prev.get("params") if professional else params) if prop else None
    new_period = (prev.get("period") if professional else period) if prop else None
    old = prev.get("topic") or {}
    renewed = intent in TOPIC_INTENTS and bool(topic)
    text = clip(topic, TOPIC_CHARS) if renewed else old.get("text") or ""
    older = [entry | {"age": int(entry.get("age") or 0) + 1} for entry in prev.get("lists") or []
             if int(entry.get("age") or 0) + 1 < LIST_AGE]
    return {"version": VERSION,
            "topic": {"text": text, "message_id": message_id if renewed else old.get("message_id")} if text else None,
            "lists": [*stamp(lists, message_id), *older][:MAX_LISTS],
            "tombstones": list(prev.get("tombstones") or [])[-MAX_TOMBSTONES:],
            "property": prop, "params": prm, "period": new_period or None,
            "sources": [dict(s) for s in sources][:MAX_SOURCES], "pending": pending}


def upgrade_v1(stored: dict) -> dict:
    """A context of version 1 read as version 2 (no migration): its topic's items are one list, whose documents are those the answer
    cited; it carries no period."""
    topic = stored.get("topic") or {}
    items = [clip(x, ITEM_CHARS) for x in topic.get("items") or [] if clip(x, ITEM_CHARS)]
    docs = sorted({str(s["document_id"]) for s in stored.get("sources") or [] if s.get("document_id")})
    mid = topic.get("message_id")
    lists = [{"id": f"{mid or 'm'}#1", "title": clip(topic.get("text", ""), TITLE_CHARS), "kind": _kind(items),
              "items": [{"label": x} for x in items[:MAX_LIST_ITEMS]], "total": len(items), "message_id": mid,
              "document_ids": docs, "age": 0}] if len(items) >= 2 else []
    return stored | {"version": VERSION, "lists": lists, "tombstones": [], "period": None,
                     "topic": {"text": topic.get("text", ""), "message_id": mid} if topic.get("text") else None}
