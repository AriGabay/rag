"""What a request is about, and the only evidence that lets a lookup name a property or a document (round 10 U6;
KTD1, KTD2, KTD7; R5-R9).

A word shared with some document's title is not identity: "השומה" is in the title of every appraisal, and a general
question about the profession that used it named three unrelated properties and asked which was meant. Two things
are therefore decided apart from the words themselves.

**Intent** (``IntentClaim``, the sub-schema both parse paths carry — the follow-up's resolution
(``app.chat.resolve``) and the first turn's request analysis (``app.chat.request``)): ``professional`` (a general
question of the trade), ``property``, ``document``, ``mixed`` (a general question about a named property or document),
``set`` (a set of documents: the existing ``scope``) or ``unknown``, with what the message leans on (``reference``),
an ordinal into a list (``item``), the professional subject in the user's words (``topic``) and the standard and
section numbers as written (``standard_refs``). The part is validated apart from the rest of a parse: one that does
not validate is dropped (None) and the rest stands, as the components part is.

**Evidence** (``evidence``): what a message holds that can name a property or a document, and nothing weaker:

- an address — a word of some title the user may see, with the house number after it ("הנרקיס 4");
- a block and parcel pair, both numbers with their labels ("גוש 6995 חלקה 46");
- a Latin unit label ("B7", "A3");
- a complete document or unit name stated as a phrase (``contexts.Identity.name_spans``); a name of one word counts only
  when no other name holds that word, since a word several names share names none of them.

The number classes are told apart before a house number is looked for, by pattern and neighbours: a number after
"תקן" or "סעיף" (decimals such as 1.1 included) is a professional reference (R9), a four-digit year, a floor and a
duration are non-names, an amount is none either. A number is a house number only beside a title word.

**Settling** (``settle``): the model's claim becomes the turn's ``Intent`` and the server records why. A
``professional`` claim whose message holds evidence is ``mixed`` (the message names a property); ``property`` and
``document`` keep their value, and whether a lookup runs is the evidence's decision, not the claim's; a missing,
invalid or ``unknown`` claim is ``property`` when the message holds evidence (the request is about that property) and
``unknown`` otherwise — ``unknown`` runs no lookup and no selection. ``set`` mirrors ``scope``.

A reference to the conversation is settled the same way (round 10 U7; KTD6): ``prior_answer_item`` with ``item: n`` binds
only when the message holds an ordinal form (an ordinal word, a digit, "האחרון/ה") and the previous answer's item n
exists (``slots.check_item``); otherwise the reference becomes ``none`` and the reason is recorded — with ``no_list`` when
an ordinal was written and the previous answer gave no list. ``topic`` and ``conversation_property`` bind to the topic or
the property slot when it exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.chat import contexts, entities, slots
from app.extraction.normalize_text import base_normalize
from app.platform.search import _scope_terms

Value = Literal["professional", "property", "document", "mixed", "set", "unknown"]
Reference = Literal["none", "conversation_property", "named_entity", "prior_answer_item", "topic", "clarification"]
# the intents that refer to a property or a document: the only ones a lookup can run for, and then only on evidence
LOOKUP_VALUES = ("property", "document", "mixed")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StandardRef(_Strict):
    """A standard and a section as the user wrote them ("1", "3.2"); "" for a part not written."""

    standard: str
    section: str


class IntentClaim(_Strict):
    """The intent as the model returns it (all fields required by the strict schema)."""

    intent: Value
    reference: Reference
    item: int | None
    topic: str
    standard_refs: list[StandardRef]


POLICY = """intent — על מה הבקשה, לפי משמעות ההודעה בהקשר השיחה ולא לפי מילים שמופיעות גם בשמות מסמכים:
- professional — שאלה מקצועית כללית: מושג, שיטה, תקן, הגדרה או כלל בשמאות, שתשובתה אינה תלויה בנכס או במסמך מסוים של
  המשרד. היא נשארת כזו גם אם השיחה עד כה עסקה בנכס מסוים, וגם אם מילה כללית שבה מופיעה בשם של מסמך.
- property — שאלה על נכס מסוים: זה שהשיחה עוסקת בו, או נכס שההודעה מזהה בכתובת, בגוש וחלקה, בתווית יחידה או בשם מלא.
- document — שאלה על מסמך מסוים עצמו (מבנהו, תוכנו, מחבריו) ולא על נתון של נכס.
- mixed — שאלה שמשלבת ידע מקצועי כללי עם נכס או מסמך מסוים שההודעה נוקבת בו.
- set — שאלה על קבוצת מסמכים או נכסים (ספירה, רשימה, השוואה); תואם scope=set.
- unknown — כשההודעה וההקשר אינם מאפשרים לקבוע.
  אל תקבע property או document רק בגלל מילה שמופיעה גם בכותרת של מסמך: נכס מזוהה בכתובת, בגוש וחלקה, בתווית יחידה או
  בשם מלא.
- reference — במה ההודעה נשענת: none — בלי הפניה; conversation_property — הנכס שבמרכז השיחה ("הנכס הזה", "שם");
  named_entity — נכס או מסמך שההודעה נוקבת בו; prior_answer_item — פריט ברשימה שהתשובה הקודמת נתנה; topic — נושא
  מקצועי שהשיחה עסקה בו; clarification — תשובה לשאלת הבהרה.
- item — המספר הסידורי של הפריט שההודעה מצביעה עליו ברשימה שהתשובות נתנו ("השני", "3", "האחרון" — מספרו של הפריט האחרון);
  אחרת null. prior_answer_item רק כשההודעה כוללת מילת סדר או ספרה, והרשימה ניתנה.
- topic — הנושא המקצועי במילות המשתמש כשהבקשה professional או mixed; אחרת ריק.
- standard_refs — הפניות לתקן ולסעיף כפי שנכתבו (standard, section; חלק שלא נכתב — ריק). מספר שאחרי "תקן" או "סעיף" הוא
  הפניה מקצועית, לא כתובת ולא מספר בית."""

# the follow-up's own addition: the conversation's property does not decide the intent of a general question
FOLLOW_UP_POLICY = ("- intent בהקשר שיחה: הנכס שבמרכז השיחה אינו קובע את ה-intent. שאלה מקצועית כללית באמצע שיחה על נכס "
                    "היא professional, ו-reference שלה topic או none; הנכס מוזכר רק כשההודעה נשענת עליו. כינוי (\"אותה\", "
                    "\"זה\") מפנה ל-topic (הנושא המקצועי שבקלט) או ל-conversation_property (הנכס שבמרכז השיחה). מילת סדר "
                    "או ספרה (\"השנייה\", \"3\", \"האחרון\") שמפנה לרשימה שתשובה נתנה היא reference=prior_answer_item עם "
                    "item=מספר הפריט ברשימת הפריטים שבקלט; גם כשאין רשימה בקלט — סמן אותה והשרת יאמר שלא ניתנה רשימה.")


@dataclass(frozen=True)
class Evidence:
    """One piece of hard evidence a message holds: its ``kind`` (``address``, ``parcel``, ``label``, ``name``), the
    words a lookup is asked (``text``) and the tokens it is made of (``words``)."""

    kind: str
    text: str
    words: tuple[str, ...]

    def held_by(self, focus_text: str) -> bool:
        """Whether the text the conversation is about already holds it: every one of its words (in any form) is a word
        of the focus, or the whole is the focus's own name. Naming the focus again is no new entity."""
        tokens, norm = entities.title_tokens(focus_text), contexts.name_key(focus_text)
        if self.kind == "name":
            return contexts.name_key(self.text) in norm
        return all(entities.forms(w) & tokens or w in norm.split() for w in self.words)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text}


@dataclass(frozen=True)
class Intent:
    """The turn's intent after the server's settling. ``claimed``: what the parse said ("" when it gave none or an
    invalid one); ``evidence``: the hard evidence the message holds; ``reasons``: why the value is not the claim.
    ``item`` and ``item_label``: the item of the previous answer's list the message names, once the server accepted it;
    ``bound``: what the reference bound to (``item``, ``topic``, ``property``) and "" when it bound to nothing;
    ``no_list``: an ordinal was written and the previous answer gave no list."""

    value: str = "unknown"
    claimed: str = ""
    reference: str = "none"
    item: int | None = None
    topic: str = ""
    standard_refs: tuple[dict, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    reasons: tuple[str, ...] = ()
    item_label: str = ""
    bound: str = ""
    no_list: bool = False

    @property
    def refers(self) -> bool:
        """Whether a lookup of a property or a document may run for it (still only on evidence)."""
        return self.value in LOOKUP_VALUES

    def as_dict(self) -> dict:
        return {"value": self.value, "claimed": self.claimed, "reference": self.reference, "item": self.item,
                "item_label": self.item_label, "bound": self.bound, "topic": self.topic,
                "standard_refs": [dict(r) for r in self.standard_refs],
                "evidence": [e.as_dict() for e in self.evidence], "reasons": list(self.reasons)}


# --- evidence ---------------------------------------------------------------------------------------------------------

_BLOCK = re.compile(r"גוש\D{0,6}?(\d{3,6})")
_PARCEL = re.compile(r"חלק(?:ה|ות)?\D{0,6}?(\d{1,5})(?!\d)")
# a number after "תקן" or "סעיף" (a standard or a section, decimals and sub-clauses included) is a professional reference
_PROFESSIONAL_REF = re.compile(
    r"(?<![א-ת])[והבלמכש]{0,3}(?:תקנים|תקן|סעיפים|סעיף|פרק|פרקים)\s*(?:מס[׳'.]?|מספר)?\s*"
    r"\d+(?:\.\d+)*(?:\([א-ת\d]+\))?(?:\s*(?:,|ו-?|עד|-)\s*\d+(?:\.\d+)*(?![\d.]))*")
_YEAR = re.compile(r"(?<![\d.,/])(?:19|20)\d{2}(?![\d])")
_FLOOR = re.compile(r"(?<![א-ת])[והבלמכש]{0,2}(?:קומה|קומת|קומות)\s*-?\d+|\d+\s*קומות")
_DURATION = re.compile(r"\d+\s*(?:חודשים|חודש|שנים|שנה|ימים|יום|שבועות|שבוע|דקות)")
_NUMBER_WORD = re.compile(r"(?<![א-ת])(?:מס[׳'.]?|מספר)\s+(?=\d)")
_HOUSE_NUMBER = re.compile(r"\d{1,3}[א-ת]?")
_LATIN_LABEL = re.compile(r"[a-z]{1,2}-?\d{1,3}|\d{1,3}-?[a-z]")


def _mask(text: str, *patterns: re.Pattern) -> str:
    for p in patterns:
        text = p.sub(" ", text)
    return text


def _parcels(text: str) -> list[Evidence]:
    block, parcel = _BLOCK.search(text), _PARCEL.search(text)
    if not (block and parcel):
        return []
    b, p = block.group(1).lstrip("0"), parcel.group(1).lstrip("0")
    return [Evidence("parcel", f"גוש {b} חלקה {p}", ("גוש", b, "חלקה", p))]


def _addresses(text: str, vocabulary: set[str], any_word: bool = False) -> list[Evidence]:
    """Latin unit labels and addresses in the text left after the non-names were masked: a title word with the house
    number after it, a label of a letter and digits, a single Latin letter after a title word ("בניין B"). With
    ``any_word`` (the words the model quoted as the new subject) a word that is no title word counts too when the house
    number follows it: the user named an address the office may not hold, and the answer to that is "not found"."""
    from app.chat.meaning import metric_word

    tokens: list[tuple[str, str | None]] = []
    for raw in entities._TOKEN.findall(base_normalize(entities._AMOUNT.sub(" ", text))):
        tok = raw.strip("-'״׳\"?.!,:;()")
        if not tok:
            continue
        if re.search(r"[a-z]", tok):
            tokens.append((tok, "label" if _LATIN_LABEL.fullmatch(tok) else "letter" if len(tok) == 1 else None))
        elif any(c.isdigit() for c in tok):  # a number is a house number only of up to three digits
            tokens.append((tok, "number" if _HOUSE_NUMBER.fullmatch(tok) else None))
        else:
            found = _scope_terms(tok)
            held = [f for f in found[0] if f in vocabulary] if found else []
            named = (bool(held) or (any_word and bool(found))) and not metric_word(tok)
            tokens.append((max(held, key=len) if held else tok, "name" if named else None))
    out: list[Evidence] = []
    for i, (tok, kind) in enumerate(tokens):
        before = tokens[i - 1] if i else ("", None)
        if kind == "label":
            out.append(Evidence("label", tok, (tok,)))
        elif kind == "letter" and before[1] == "name":
            out.append(Evidence("label", f"{before[0]} {tok}", (before[0], tok)))
        elif kind == "number" and before[1] == "name":
            out.append(Evidence("address", f"{before[0]} {tok}", (before[0], tok)))
    return out


def _names_one(word: str, keys: list[str]) -> bool:
    """Whether a name of one word names one thing: every longer name that holds the word is a longer form of one name
    (a unit's title and its aliases: "נחל הדס", "פרויקט נחל הדס"), not the names of several documents that share it."""
    holders = [k for k in keys if k != word and word in k.split()]
    longest = max(holders, key=len, default="")
    return all(f" {k} " in f" {longest} " for k in holders)


def _complete_names(text: str, vocabulary: list[str]) -> list[Evidence]:
    """The complete document or unit names the text states as a phrase. A name of one word counts only when no other
    name holds that word; of two names where one lies inside the other's phrase, the longer one."""
    keys = list(dict.fromkeys(k for t in vocabulary if len(k := contexts.name_key(t)) >= 2))
    names = tuple(k for k in keys if len(k.split()) > 1 or _names_one(k, keys))
    if not names:
        return []
    norm = contexts.name_key(text)
    found: list[tuple[int, int, str]] = []
    for name, pattern in zip(names, contexts._name_patterns(names), strict=True):
        found += [(*m.span(), name) for m in pattern.finditer(norm)]
    return [Evidence("name", name, tuple(name.split())) for a, b, name in found
            if not any(x <= a and b <= y and (y - x) > (b - a) for x, y, _ in found)]


PARTIAL_HOLDERS = 3  # the names a partial phrase may be part of and still refer to them


def _partial_names(text: str, vocabulary: list[str], whole: list[Evidence]) -> list[Evidence]:
    """The phrases of two words or more that the text shares with a name without stating all of it ("מתחם הגפן" of
    "מתחם הגפן א" and "מתחם הגפן ב"): a phrase that only names can hold, so the user refers to a named thing even when it
    fits several — which the lookup then asks about (round 9 AE4) — never one word, which titles share ("השומה"), and
    never a phrase of words of the trade alone. A phrase inside a complete name the text states is that name's."""
    keys = list(dict.fromkeys(k for t in vocabulary if len((k := contexts.name_key(t)).split()) > 2))
    norm = f" {contexts.name_key(text)} "
    phrases: dict[str, None] = {}
    for key in keys:
        words = key.split()
        for size in range(len(words) - 1, 1, -1):  # the longest part first
            for i in range(len(words) - size + 1):
                part = words[i:i + size]
                # words only: a number belongs to an address, a parcel or a standard, which are read apart
                if any(any(c.isdigit() for c in w) for w in part) or all(
                        entities.forms(w) & entities.GENERIC_NAME_WORDS or len(w) < 2 for w in part):
                    continue
                phrase = " ".join(part)
                if f" {phrase} " in norm or any(f" {p}{phrase} " in norm for p in ("ב", "ה", "ו", "ל", "מ", "וב", "וה")):
                    phrases[phrase] = None
    # a phrase that many names share names none of them ("שומה מקרקעין" at the head of every report): a few at most
    phrases = {p: None for p in phrases if sum(f" {p} " in f" {k} " for k in keys) <= PARTIAL_HOLDERS}
    stated = [contexts.name_key(e.text) for e in whole]
    kept = [p for p in phrases if not any(p in s for s in stated)]
    kept = [p for p in kept if not any(p != q and f" {p} " in f" {q} " for q in kept)]  # the longest phrase only
    return [Evidence("name", p, tuple(p.split())) for p in kept]


def evidence(message: str, vocabulary: list[str], quoted: bool = False) -> list[Evidence]:
    """The hard evidence of a message (see the module docstring), in the order address/label, parcel, name, each once.
    ``vocabulary``: every title the user may see, the names of units included. ``quoted``: the text is the words the
    model quoted as the user's new subject, not a whole message (see ``_addresses``)."""
    text = base_normalize(message or "")
    words: set[str] = set().union(*(entities.title_tokens(t) for t in vocabulary)) if vocabulary else set()
    out = _parcels(text)
    rest = _mask(text, _BLOCK, _PARCEL, _PROFESSIONAL_REF, _YEAR, _FLOOR, _DURATION, _NUMBER_WORD)
    out += _addresses(rest, words, quoted)
    names = _complete_names(text, vocabulary)
    # a part of a name is the weakest evidence: read only when nothing more specific (an address, a parcel, a label,
    # a complete name) is there
    out += names or (_partial_names(text, vocabulary, names) if not out else [])
    seen: set[tuple[str, str]] = set()
    return [e for e in out if not ((e.kind, e.text) in seen or seen.add((e.kind, e.text)))]


def query_of(items: list[Evidence]) -> str:
    """The words a lookup is asked for the evidence: each item's text, once."""
    return " ".join(dict.fromkeys(w for e in items for w in e.text.split()))


# --- settling ---------------------------------------------------------------------------------------------------------

def settle(claim: IntentClaim | None, message: str, vocabulary: list[str], scope: str = "entity",
           has_property: bool = False, quoted: str = "", items: list[str] | tuple[str, ...] = (),
           topic: str = "") -> Intent:
    """The turn's intent from the model's claim and the message (see the module docstring). ``scope``: the parse's
    scope (``set`` decides intent ``set``); ``has_property``: the conversation has a property in its centre (only said
    in the reasons: a claim about a property without evidence and without one runs no lookup); ``quoted``: the words
    the model quoted for a change of subject, whose evidence counts beside the message's; ``items``: the labels of the
    list the previous answer printed, which an ordinal can name; ``topic``: the conversation's topic slot, which a
    pronoun can bind to."""
    found = tuple(dict.fromkeys([*evidence(message, vocabulary), *(evidence(quoted, vocabulary, quoted=True) if quoted else ())]))
    why: list[str] = []
    claimed = claim.intent if claim is not None else ""
    if scope == "set":
        value = "set"
        if claimed not in ("", "set"):
            why.append(f"scope set overrides the claim {claimed}")
    elif claim is None:
        value = "property" if found else "unknown"
        why.append("no valid intent part: " + ("the message holds evidence of a property" if found
                                            else "no evidence, no lookup"))
    elif claimed == "professional" and found:
        value = "mixed"
        why.append("professional claim, but the message names a property or a document")
    elif claimed in ("unknown", "set"):
        value = "property" if found else "unknown"
        why.append(("claim " + claimed + ": ") + ("the message holds evidence of a property" if found
                                              else "no evidence, no lookup"))
    else:
        value = claimed
        if claimed in LOOKUP_VALUES and not found:
            why.append("no hard evidence in the message: no lookup" + ("" if has_property else ", no property in the conversation"))
    reference = claim.reference if claim else "none"
    item, label, bound, no_list = None, "", "", False
    if reference == "prior_answer_item":
        # an ordinal binds only to an item the previous answer printed, on the message's own words (KTD6)
        rejected = slots.check_item(claim.item, message, list(items))
        if rejected is None:
            item, label, bound = claim.item, items[claim.item - 1], "item"
            why.append(f"ordinal {item} bound to the previous answer's item «{label}»")
        else:
            reference, no_list = "none", not items and bool(slots.ordinal_forms(message))
            why.append("prior_answer_item rejected: " + rejected)
    elif reference == "topic":
        bound = "topic" if topic else ""
        if not topic:
            why.append("topic reference: no topic in the conversation")
    elif reference == "conversation_property":
        bound = "property" if has_property else ""
        if not has_property:
            why.append("conversation_property reference: no property in the conversation")
    return Intent(value=value, claimed=claimed, reference=reference, item=item,
                  topic=" ".join(claim.topic.split()) if claim else "",
                  standard_refs=tuple(r.model_dump() for r in claim.standard_refs) if claim else (),
                  evidence=found, reasons=tuple(why), item_label=label, bound=bound, no_list=no_list)
