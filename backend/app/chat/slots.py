"""The conversation's slots (round 10 U7; KTD4, KTD5, KTD6, KTD7; R10-R15): what an answer leaves for the next turn,
kept apart so that one never erases another.

Every assistant message stores ``answer.context`` (schema version ``VERSION``; server-only, ``api._message_json`` never
sends it, and it is not a document of the message, so a later professional answer is not hidden by the property it
carries):

- ``topic``: ``{text, items, message_id}`` — the professional subject in the user's words and the items the latest
  answer that listed any printed (labels of at most ``ITEM_CHARS``, at most ``MAX_ITEMS``), with the message they came
  from. The items persist until a later answer lists new ones, so "הסבר את השנייה" and then "ואת השלישית" both bind to
  the same list;
- ``property``: ``{document_ids, located_unit, subject}`` — the property in the centre of the conversation — and
  ``params``: the task parameters of its datum (metric, unit, period, VAT, area basis, and the wording and role the
  focus carried). A professional answer leaves both **unchanged**: they are dormant, not dropped, and a later "בנכס הזה"
  restores them together;
- ``sources``: what the answer cited — ``{document_id, source_kind, standard, section}``, the kind and the identity
  (number, name, edition, effective date, section) as the cited source carried them (round 10 U8);
- ``pending``: ``{kind, payload, asked_at}`` — the clarification this answer asked (``candidates`` or ``parameter``). It
  lives exactly one message: each turn writes only its own, so a pending one that the next message did not answer is
  simply not carried.

Everything here is pure: the functions take and return plain data, so the slots are tested without a database.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

VERSION = 1
MAX_ITEMS = 10
ITEM_CHARS = 80
TOPIC_CHARS = 200
MAX_SOURCES = 30
# the intents whose answer is about a topic of the trade: they write the topic slot
TOPIC_INTENTS = ("professional", "mixed")
# the task parameters of the datum a property turn answered (the focus fields that are not the property itself)
PARAM_KEYS = ("metric_as_written", "metric_kind", "unit", "period", "vat", "area_basis", "value_role")
_UNKNOWN_FOCUS = {"metric_as_written": "", "metric_kind": "unknown", "unit": "unknown", "period": "unknown",
                  "area_basis": "", "vat": "unknown", "value_role": "unknown"}


def clip(text: str, limit: int) -> str:
    """The text on one line, at most ``limit`` characters."""
    return " ".join((text or "").split())[:limit].rstrip()


# --- the items a list answer printed ------------------------------------------------------------------------------------

_LIST_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<mark>\d{1,3}[.)]|[-*•])[ \t]+(?P<body>\S.*)$")
_CITATION = re.compile(r"\s*\\?\[[SMCPVA]\d+\\?\]")
_BOLD_LEAD = re.compile(r"^\s*(\*\*|__)(.+?)\1")
_LABEL_END = re.compile(r"\s*[—–]\s*|:(?:\s|$)")
_EDGE_MARKUP = re.compile(r"(?<!\w)[*_]+|[*_]+(?!\w)|`")


def _label(body: str) -> str:
    """An item's label: its bold lead, else the text before "—" or ":" — without markup, citations and closing
    punctuation."""
    text = _CITATION.sub("", body)
    lead = _BOLD_LEAD.match(text)
    label = lead.group(2) if lead else _LABEL_END.split(text, 1)[0]
    label = _EDGE_MARKUP.sub("", label)
    return clip(label, ITEM_CHARS).strip(" \t:.,;-–—")


def extract_items(markdown: str) -> list[str]:
    """The items a final answer listed, deterministically (KTD6): the top-level numbered or bulleted items of the
    first list of at least two (a numbered list before bullets), each labelled by ``_label``. A nested list is not an
    item, a single item is no list, and a text with no list has none. At most ``MAX_ITEMS``."""
    blocks: list[dict] = []
    cur: dict | None = None
    gap = False
    for line in (markdown or "").splitlines():
        m = _LIST_LINE.match(line)
        if m:
            indent, numbered = len(m.group("indent").expandtabs(4)), m.group("mark")[0].isdigit()
            if cur is not None and (indent < cur["base"] or (indent == cur["base"] and numbered != cur["numbered"])):
                cur = None
            if cur is None:
                cur = {"base": indent, "numbered": numbered, "items": []}
                blocks.append(cur)
            if indent == cur["base"]:
                cur["items"].append(m.group("body"))
            gap = False
        elif not line.strip():
            gap = True
        elif cur is not None and (gap or not line[:1].isspace()):
            cur = None  # a paragraph of text ends the list (an indented line continues its item)
            gap = False
        else:
            gap = False
    lists = [b for b in blocks if len(b["items"]) >= 2]
    chosen = next((b for b in lists if b["numbered"]), lists[0] if lists else None)
    if chosen is None:
        return _bold_items(markdown)
    return [label for body in chosen["items"] if (label := _label(body))][:MAX_ITEMS]


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
    the three approaches"): the bold terms of the first paragraph that holds two or more of them, or — when no paragraph
    does — the bold leads of consecutive paragraphs. A bold heading line on its own is a heading, not an item."""
    paragraphs = [p for p in re.split(r"\n\s*\n", _CITATION.sub("", markdown or "")) if p.strip()]
    for para in paragraphs:
        terms = [t for m in _BOLD.finditer(para) for t in _split_enumeration(m.group("b"))]
        terms = list(dict.fromkeys(t for t in terms if t))
        if len(terms) >= 2:
            return terms[:MAX_ITEMS]
    leads = []
    for para in paragraphs:
        lead = _BOLD_LEAD.match(para)
        rest = para[lead.end():].strip() if lead else ""
        if lead and rest:
            leads.append(clip(_EDGE_MARKUP.sub("", lead.group(2)), ITEM_CHARS).strip(" \t:.,;-–—"))
        elif leads:
            break
    return leads[:MAX_ITEMS] if len(leads) >= 2 else []


# --- an ordinal that names an item --------------------------------------------------------------------------------------

ORDINAL_WORDS = {"ראשון": 1, "ראשונה": 1, "שני": 2, "שנייה": 2, "שניה": 2, "שלישי": 3, "שלישית": 3, "רביעי": 4,
                 "רביעית": 4, "חמישי": 5, "חמישית": 5, "שישי": 6, "שישית": 6, "שביעי": 7, "שביעית": 7, "שמיני": 8,
                 "שמינית": 8, "תשיעי": 9, "תשיעית": 9, "עשירי": 10, "עשירית": 10, "אחרון": -1, "אחרונה": -1}
_ORDINAL_ALTERNATIVES = "|".join(sorted(ORDINAL_WORDS, key=len, reverse=True))
# an ordinal word with the prefix letters Hebrew puts before it ("השנייה", "בשנייה", "והשלישית"), not a longer word
_ORDINAL = re.compile(r"(?<![א-ת])[ולבכמש]{0,2}ה?(" + _ORDINAL_ALTERNATIVES + r")(?![א-ת])")
# a number of one or two digits that stands alone: not part of a longer number, a decimal, a date or a percentage
_DIGIT = re.compile(r"(?<![\d.,/\-])(\d{1,2})(?![\d,/%]|\.\d)")


def ordinal_forms(message: str) -> set[int]:
    """The item numbers a message writes: ordinal words (1 to 10) and standalone numbers of one or two digits; ``-1``
    for "the last" (האחרון/ה)."""
    found = {ORDINAL_WORDS[m.group(1)] for m in _ORDINAL.finditer(message or "")}
    return found | {int(m.group(1)) for m in _DIGIT.finditer(message or "")}


def check_item(item: int | None, message: str, items: list[str]) -> str | None:
    """Why an item claimed for an ordinal cannot be accepted, or None when it can (KTD6): a list was given, the
    message holds an ordinal form (an ordinal word, a digit, "the last"), the item exists and it is the one the words
    name."""
    if not items:
        return "no list was given in the previous answer"
    forms = ordinal_forms(message)
    if not forms:
        return "no ordinal or number in the message"
    if item is None or not 1 <= item <= len(items):
        return f"item {item} does not exist in the list of {len(items)}"
    if item not in {len(items) if f == -1 else f for f in forms}:
        return f"item {item} contradicts the ordinal the message writes"
    return None


# --- the property and its parameters ------------------------------------------------------------------------------------


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


# --- the slots an answer leaves -----------------------------------------------------------------------------------------


def carry(previous: dict | None, *, intent: str, property: dict | None = None, params: dict | None = None,
          topic: str = "", items: Iterable[str] = (), message_id: str | None = None,
          sources: Iterable[dict] = (), pending: dict | None = None) -> dict:
    """The context this answer stores (KTD4), from the previous one and this turn's: the property found (``property``,
    ``params``), the topic text and items it printed, what it cited and what it asks. A professional turn leaves the
    previous property and parameters unchanged — dormant, not dropped; every other turn writes its own. The topic's
    items persist until an answer lists new ones; its text is replaced by a professional or mixed turn that has one.
    Only this turn's pending clarification is kept (it lives one message)."""
    prev = previous or {}
    professional = intent == "professional"
    prop = prev.get("property") if professional else property
    prm = (prev.get("params") if professional else params) if prop else None
    old = prev.get("topic") or {}
    listed = [i for i in (clip(x, ITEM_CHARS) for x in items) if i][:MAX_ITEMS]
    new_items, items_from = (listed, message_id) if listed else (list(old.get("items") or []), old.get("message_id"))
    text = clip(topic, TOPIC_CHARS) if intent in TOPIC_INTENTS and topic else old.get("text") or ""
    return {"version": VERSION,
            "topic": {"text": text, "items": new_items, "message_id": items_from} if text or new_items else None,
            "property": prop, "params": prm, "sources": [dict(s) for s in sources][:MAX_SOURCES], "pending": pending}
