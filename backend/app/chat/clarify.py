"""The structured clarification: its contract and the server's validation of it (round 26 U2; KTD1, KTD2; R1-R7, R10).

A clarification stays the model's decision (round 15 U6: a server-guessed list caused loops). What changes is its shape: the final
answer's ``clarify`` object holds one question, a short reason, a ``kind`` and two to four options, each with a ``ref`` the model
took from what the turn showed or read. The server never trusts the object. It builds the record the conversation stores
(``validate``), and everything the user sees about an option comes from the sources, not from the model's words:

- **unit**: the ref is a unit handle (U#) the turn offered, listed or read, or the full id of a document of one appraisal that the
  turn listed or read; the user must still be able to read it. The label and detail are built from the unit's identity card, the
  valuation date it states and its pages (``entities.candidate_details``, ``entities.document_candidates``), so two untitled units
  are told apart by address and date and never read «ללא כותרת».
- **period**: the ref is an ISO point or range; it is kept only when a date a read source states lies inside it
  (``source_dates``, ``period_in_sources``), and never when the question or the time switch already decides the date (R3).
- **area_basis** and **method**: the ref is a phrase a read source writes (``find_phrase``).

An option that is only a number or a value is refused (R4: the user is never asked which number is correct), as is one that
repeats another, one the server cannot ground, or one the turn does not support. Fewer than two valid options leave a text question
with no buttons and a record that says why. A clarification that repeats a kind the user already resolved for the same question, or
the third in a row, is a violation (``violation``): the engine re-prompts once and, if the model insists, keeps the text question
with no buttons (R10). A clarification the question did not need - the time preference or the question's date already decides, the unit
is already pinned, or no source writes the phrases it offers - is a violation of the same kind (R2, R3): it is never shown as a text
question, the one re-prompt tells the model what decides, and only a model that insists keeps the text. Option ids are stable hashes of the kind and the option's identity (``option_id``), not of its place.

Nothing here calls a model. The pending slot, the reply binding and the continuation are the next unit's (round 26 U3): it supplies
the state (``ClarificationState``) and stores the record."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.chat import contexts, entities, timeframe
from app.chat import tools as T
from app.db import TenantContext
from app.platform import dates
from app.providers.llm import prompt_text

UNIT, PERIOD, AREA_BASIS, METHOD = "unit", "period", "area_basis", "method"
TEXT = "text"  # a clarification with no options: the model's question alone (the kind of a record that offers none)
KINDS = (UNIT, PERIOD, AREA_BASIS, METHOD)
MIN_OPTIONS, MAX_OPTIONS = 2, 4
MAX_CONSECUTIVE = 2  # clarifications in a row for one question; the next is answered with stated assumptions (R10)
DETAIL_CHARS = 120

# why an option or a clarification was refused (the ``reason`` of a dropped option; the ``fallback`` of a check)
UNIT_NOT_OFFERED, UNIT_UNAVAILABLE = "unit_not_offered", "unit_unavailable"
PERIOD_INVALID, PERIOD_NOT_IN_SOURCES = "period_invalid", "period_not_in_sources"
PHRASE_NOT_IN_SOURCES, VALUE_OPTION, DUPLICATE, OVER_LIMIT = "phrase_not_in_sources", "value_option", "duplicate_option", "over_limit"
TOO_FEW, NO_OFFER = "too_few_options", "no_offer"
TIME_DECIDES, UNIT_PINNED = "time_preference_decides", "unit_pinned"
REPEAT, CAP = "repeat", "cap"
# a clarification the question did not need (R2, R3) is never shown as a text question: like a repeat, it gets the one re-prompt
NOT_NEEDED = (TIME_DECIDES, UNIT_PINNED, PHRASE_NOT_IN_SOURCES)

KIND_WORDS = {UNIT: "הנכס", PERIOD: "המועד או התקופה", AREA_BASIS: "בסיס השטח", METHOD: "שיטת החישוב", TEXT: "העניין"}
MSG_REPEAT = ("המשתמש כבר הכריע בשאלה הזו לגבי {what}: «{chosen}». אל תשאל שוב על אותו עניין — ענה עכשיו על השאלה המקורית עם הבחירה "
              "הזו, ממה שכבר קראת. שאלה נוספת מותרת רק על עמימות אחרת וחדשה, ואז אמור מה חדש.")
MSG_CAP = ("כבר נשאלו שתי שאלות הבהרה רצופות על השאלה הזו. אל תשאל שאלה נוספת: ענה עכשיו עם הנחות מוצהרות — אמור במפורש איזו קריאה "
           "בחרת ומה היה משתנה בקריאה האחרת.")
MSG_NOT_NEEDED = {
    TIME_DECIDES: ("העדפת הזמן (או התאריך שבשאלה) כבר מכריעה איזה מועד להציג — אל תשאל. ענה עכשיו: הנתונים העדכניים ביותר קודם "
                   "ונתונים מוקדמים יותר אחריהם, עם תאריכים ומקורות."),
    UNIT_PINNED: "היחידה כבר נקבעה — אל תשאל איזו שומה. ענה עכשיו על השאלה עבורה.",
    PHRASE_NOT_IN_SOURCES: ("הביטויים שהצעת אינם כתובים במקורות שקראת, ולכן אין מה לשאול. ענה עכשיו עם שתי הקריאות בקצרה, כל אחת "
                            "עם המקור שלה."),
}
MSG_PROGRESS = "ממשיך לענות על השאלה עם מה שהמשתמש כבר בחר"
# the question of a clarification that lost options its text listed: the server's neutral words (R6: a dropped option is never listed)
MSG_NEUTRAL = {
    UNIT: "יש יותר משומה או נכס אחד שמתאימים לשאלה. אפשר לכתוב את שם הנכס, הכתובת או התאריך של זה שהתכוונת אליו.",
    PERIOD: "יש כמה מועדים שמתאימים לשאלה. לאיזה מועד או תקופה התכוונת?",
    AREA_BASIS: "באיזה בסיס שטח להציג את הנתון?",
    METHOD: "לפי איזו שיטת חישוב להציג את הנתון?",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfferedOption(_Strict):
    """One option as the model sends it: ``key`` its place in its own list, ``label`` its words for it (a hint: the user sees the
    server's), ``ref`` what it stands for - a U# handle or a document id (unit), an ISO point or range (period), a phrase of a
    source (area basis, method)."""

    key: str
    label: str
    ref: str


class ClarifyOffer(_Strict):
    """The structured clarification of a final answer (KTD1). There is no kind for choosing a value: the model never asks which
    number is correct (R4). ``question`` is the text the user reads; the answer's ``clarification_question`` keeps its meaning."""

    question: str
    reason: str
    kind: Literal["unit", "period", "area_basis", "method"]
    options: list[OfferedOption]
    allow_all: bool


@dataclass(frozen=True)
class ClarificationState:
    """What the conversation knows about the clarifications of the question being continued (supplied by the caller, U3):
    ``resolved`` - the label the user chose for each kind already settled for this question; ``consecutive`` - how many
    clarifications were asked in a row for it; ``narrowing`` - the ids of the options the user's last reply matched (a new
    clarification among exactly those is a narrowing, not a repeat)."""

    resolved: dict = field(default_factory=dict)
    consecutive: int = 0
    narrowing: frozenset = frozenset()


@dataclass
class Verdict:
    """What the server made of the clarification of an answer. ``record``: the validated record the pending slot stores -
    ``{id, question, reason, kind, options[{id, label, detail, ref}], allow_all}``, with no options when none survived (a text
    question) - or None when the answer is no clarification. ``check``: ``{"fallback", "dropped", "violation"}``, for the
    diagnostics (no content). ``offered_units``: the candidates of the surviving options, in order, as a reply is resolved among
    them. ``shown_documents``: every document the question may name. ``violation``: ``repeat`` or ``cap`` (R10), or one of ``NOT_NEEDED`` (the
    time preference or the question's date decides, the unit is pinned, no source writes the phrases), when the clarification should not reach the user. ``question_override``: the server's neutral question, when the model's listed an option that was dropped."""

    record: dict | None
    check: dict
    offered_units: list[dict] = field(default_factory=list)
    shown_documents: list[str] = field(default_factory=list)
    violation: str | None = None
    offer: ClarifyOffer | None = None
    question: str = ""
    question_override: str | None = None


# --- option ids -----------------------------------------------------------------------------------------------------


def option_id(kind: str, ref: dict) -> str:
    """The stable id of an option: a hash of its kind and its identity, never of its place in the list or of the model's key."""
    return hashlib.sha256(f"{kind}|{json.dumps(ref, sort_keys=True, ensure_ascii=False)}".encode()).hexdigest()[:12]


def ref_identity(candidate: dict) -> dict:
    """What identifies a unit or document option across turns: the unit's file, reading and number (and the derivation that
    numbered it), or the document alone - not the words around it."""
    unit = candidate.get("unit")
    if not unit:
        return {"document_id": str(candidate["document_id"])}
    return {"document_id": str(unit.get("document_id") or candidate["document_id"]), "reading_id": unit.get("reading_id"),
            "unit_no": unit.get("unit_no"), "units_tag": unit.get("units_tag")}


# --- periods ----------------------------------------------------------------------------------------------------------


class Period(NamedTuple):
    start: str
    end: str
    precision: str


_POINT = r"\d{4}(?:-\d{2}(?:-\d{2})?)?"
_PERIOD = re.compile(rf"^\s*({_POINT})\s*(?:(?:/|\.\.)\s*({_POINT}))?\s*$")
_PRECISION = {4: "year", 7: "month", 10: "day"}


def _interval(iso: str) -> tuple[date, date] | None:
    """The first and last day an ISO year, month or day covers; None for a date that does not exist (or a year out of range)."""
    try:
        year = int(iso[:4])
        if not 1900 <= year <= 2100:
            return None
        if len(iso) == 4:
            return date(year, 1, 1), date(year, 12, 31)
        month = int(iso[5:7])
        if len(iso) == 7:
            first = date(year, month, 1)
            return first, (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
        day = date(year, month, int(iso[8:10]))
        return day, day
    except ValueError:
        return None


def parse_period(ref: str) -> Period | None:
    """An option's period reference as a period, or None: an ISO point (``2024``, ``2024-03``, ``2024-03-15``) or a range of two
    points of the same precision (``2022/2024``, ``2022-01-01..2022-12-31``), start not after end."""
    m = _PERIOD.match(ref or "")
    if m is None:
        return None
    start, end = m.group(1), m.group(2) or m.group(1)
    if len(start) != len(end):
        return None
    a, b = _interval(start), _interval(end)
    if a is None or b is None or a[0] > b[1]:
        return None
    return Period(start, end, _PRECISION[len(start)])


def period_in_sources(period: Period, stated: Iterable[tuple[str, str]]) -> bool:
    """Whether a date a source states lies inside the period: a stated date (or range) is never read finer than the source wrote it
    ("2022-09" does not make "2022-09-02" a date of the sources)."""
    lo, hi = _interval(period.start)[0], _interval(period.end)[1]
    for a, b in stated:
        first, last = _interval(a), _interval(b)
        if first is not None and last is not None and lo <= first[0] and last[1] <= hi:
            return True
    return False


def source_dates(ws) -> list[tuple[str, str]]:
    """The dates (ISO start, end) the turn's read sources state - in their text and their date line - and the dates of the units its
    time search covered; a listing and a source the server made for its own checks are no read source."""
    out: list[tuple[str, str]] = []
    for s in ws.sources.values():
        if s.is_listing or s.server_made:
            continue
        for text in (s.date_line, (s.text or "")[:20000]):
            out += [(p.iso, p.end or p.iso) for p in dates.find_all(text or "")]
    for unit in (ws.time_coverage or {}).get("units") or []:
        if unit.get("date"):
            out.append((unit["date"], unit["date"]))
    return list(dict.fromkeys(out))


def requested_of(ref: dict, text: str = "") -> timeframe.Requested:
    """A period option's ref as the time the continuation asks about (``timeframe.Requested``; KTD4): a point or a range."""
    start, end = ref["start"], ref["end"]
    return timeframe.Requested("point" if start == end else "range", start, end, ref["precision"], text)


# --- phrases ----------------------------------------------------------------------------------------------------------

_QUOTES = str.maketrans({"״": '"', "”": '"', "“": '"', "׳": "'", "’": "'", "‘": "'"})
_PUNCT = re.compile(r"[\"'.,;:()\[\]|/\\]")
_PREFIXES = "ובהלמכש"


def _flat(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", (text or "").translate(_QUOTES)).split()).lower()


def find_phrase(phrase: str, ws):
    """The first read source that writes the phrase (whatever the quotes and the spacing; a Hebrew prefix letter may precede it),
    or None. A phrase of one letter names nothing; a listing and the server's own sources are no read source."""
    flat = _flat(phrase)
    if len(flat) < 2:
        return None
    pattern = re.compile(rf"(?<!\w)[{_PREFIXES}]{{0,2}}{re.escape(flat)}(?!\w)")
    for s in ws.sources.values():
        if not s.is_listing and not s.server_made and pattern.search(_flat(s.text)):
            return s
    return None


# --- values are never options (R4) -------------------------------------------------------------------------------------

_UNITS = r"(?:₪|ש[\"״']?ח|שקל(?:ים)?|מ[\"״']ר|מטר(?:ים)?|דונם|%|אחוז|אלף|מיליון|\$|€)"
_VALUE = re.compile(rf"\d[\d,.\s]*{_UNITS}|{_UNITS}\s*\d")
_LETTER = re.compile(r"[A-Za-zא-ת]")
_YEAR = re.compile(r"(?:19|20)\d{2}")


def label_problem(kind: str, label: str) -> str | None:
    """Why an option's label cannot be offered, or None. A label that is a value (an amount, an area, a percentage) or only numbers
    is a choice of a number, which the assistant never asks for (R4); a period's label may be a date or a year."""
    text = " ".join((label or "").split())
    if not text or _VALUE.search(text):
        return VALUE_OPTION
    if not _LETTER.search(text) and not (kind == PERIOD and (dates.find_all(text) or _YEAR.fullmatch(text))):
        return VALUE_OPTION
    return None


# --- how an option reads ----------------------------------------------------------------------------------------------


def _place(details: dict) -> str:
    address, city = details.get("address"), details.get("city")
    return f"{address}, {city}" if address and city and city not in address else address or ""


def option_texts(candidate: dict) -> tuple[str, str] | None:
    """The label and the detail an option shows, from its candidate's grounded details (``entities.candidate_details``,
    ``entities.document_candidates``), or None when it has nothing that tells it from another. A unit is named by its title, else by
    its identity card (name, address, parcel), else by its pages; the detail adds what the label lacks - the address, the valuation
    date, the pages, the parcel and the file. The words «ללא כותרת» never appear (R6)."""
    d = candidate.get("details") or {}
    unit = candidate.get("unit")
    if not unit:
        label = " ".join((d.get("title") or candidate.get("title") or "").split())
        return (label, d.get("date") or "") if label else None
    title = " ".join((unit.get("display") or "").split())
    label = title or contexts.untitled_label(d)
    pages = d.get("pages") or ""
    if not label:
        label, pages = pages, ""  # nothing but its pages names it
    if not label:
        return None
    parts = [p for p in (_place(d), d.get("date"), pages, d.get("parcel")) if p and p not in label]
    if d.get("file_title"):
        parts.append(f"בקובץ «{d['file_title']}»")
    return label, " · ".join(parts)


# --- repeat and cap (R10) ------------------------------------------------------------------------------------------------


def violation(kind: str, state: ClarificationState | None, option_ids: Iterable[str], check_cap: bool = True) -> str | None:
    """``cap`` when the question was already clarified twice in a row; ``repeat`` when the kind was already resolved for it and the
    new options are not a narrowing (every id among those the user's last reply matched); else None. ``check_cap`` False: a
    clarification that asks for a calculation's missing parameter, which is never answered with an assumption (round 7 KTD9)."""
    if state is None:
        return None
    if check_cap and state.consecutive >= MAX_CONSECUTIVE:
        return CAP
    ids = set(option_ids)
    if kind in state.resolved and not (ids and ids <= set(state.narrowing)):
        return REPEAT
    return None


def reprompt(why: str, kind: str, state: ClarificationState | None) -> str:
    """The item the engine appends once when the model asks again about what was resolved, or past the cap. The chosen label is a
    document's words: it passes ``prompt_text``."""
    if why == CAP:
        return MSG_CAP
    if why in MSG_NOT_NEEDED:
        return MSG_NOT_NEEDED[why]
    chosen = prompt_text(str((state.resolved if state else {}).get(kind) or ""))
    return MSG_REPEAT.format(what=KIND_WORDS.get(kind, KIND_WORDS[TEXT]), chosen=chosen)


# --- the validation ---------------------------------------------------------------------------------------------------


def known_documents(ws, extra: Iterable[str] = ()) -> set[str]:
    """The documents the turn showed the model or the model read: those a tool listed, handed a handle (D#) or returned a passage
    of, and ``extra`` (the lookup's and the candidates' documents, which the engine knows)."""
    out = {str(d) for d in extra}
    out |= {str(h["document_id"]) for h in ws.handles.values() if h.get("kind") == "D"}
    for s in ws.sources.values():
        if s.document_id:
            out.add(str(s.document_id))
        out |= {str(d) for d in s.listed}
    return out


@dataclass
class _Grounded:
    label: str
    detail: str
    ref: dict
    identity: dict
    candidate: dict | None = None


def _ground_units(ctx: TenantContext, ws, options: list[OfferedOption], extra_documents: Iterable[str]) -> list[_Grounded | str]:
    """Each option as a grounded unit or document, or the reason it is not one, in order."""
    allowed = ws.offered_units | ws.listed_units | T.read_unit_handles(ws)
    known = known_documents(ws, extra_documents)
    plan: list[tuple[str, object]] = []
    for o in options:
        ref = (o.ref or "").strip()
        data = ws.handles.get(ref) if re.fullmatch(r"[UD]\d+", ref) else None
        if data and data.get("kind") == "U" and ref in allowed:
            plan.append(("unit", (data["document_id"], data.get("reading_id"), data["unit_no"])))
        elif data and data.get("kind") == "D":
            plan.append(("doc", str(data["document_id"])))
        else:
            try:
                doc = str(UUID(ref))
            except ValueError:
                plan.append(("drop", UNIT_NOT_OFFERED))
                continue
            plan.append(("doc", doc) if doc in known else ("drop", UNIT_NOT_OFFERED))
    units = iter(entities.candidate_details(ctx, [p for kind, p in plan if kind == "unit"]))
    documents = iter(entities.document_candidates(ctx, [p for kind, p in plan if kind == "doc"]))
    out: list[_Grounded | str] = []
    for kind, value in plan:
        if kind == "drop":
            out.append(str(value))
            continue
        candidate = next(units if kind == "unit" else documents)
        texts = option_texts(candidate) if candidate is not None else None
        if texts is None:
            out.append(UNIT_UNAVAILABLE)
            continue
        ref = {k: v for k, v in candidate.items() if k != "details"}
        out.append(_Grounded(texts[0], texts[1], ref, ref_identity(candidate), candidate))
    return out


def _ground_period(ws, options: list[OfferedOption]) -> list[_Grounded | str]:
    stated = source_dates(ws)
    out: list[_Grounded | str] = []
    for o in options:
        period = parse_period(o.ref)
        if period is None:
            out.append(PERIOD_INVALID)
        elif not period_in_sources(period, stated):
            out.append(PERIOD_NOT_IN_SOURCES)
        else:
            ref = {"start": period.start, "end": period.end, "precision": period.precision}
            shown = dates.shown_period(period.start, period.precision, period.end if period.end != period.start else None)
            out.append(_Grounded(" ".join(o.label.split()), shown, ref, ref))
    return out


def _ground_phrases(ws, options: list[OfferedOption]) -> list[_Grounded | str]:
    out: list[_Grounded | str] = []
    for o in options:
        phrase = " ".join((o.ref or "").split())
        found = find_phrase(phrase, ws)
        if found is None:
            out.append(PHRASE_NOT_IN_SOURCES)
            continue
        where = f"{found.title} · {found.location}" if found.location else found.title
        out.append(_Grounded(" ".join(o.label.split()) or phrase, where[:DETAIL_CHARS], {"phrase": phrase}, {"phrase": phrase}))
    return out


def new_id() -> str:
    """The id of a clarification record: unique to the message that asks it (a clarification re-emitted after a help reply gets a new one)."""
    return uuid.uuid4().hex[:16]


def validate(ctx: TenantContext, ws, offer: ClarifyOffer | None, *, question: str, state: ClarificationState | None = None,
             pinned_unit: bool = False, extra_documents: Iterable[str] = (), parameter: bool = False, refuse: bool = False,
             record_id: str | None = None) -> Verdict:
    """The record of a clarification answer, validated and grounded (KTD2); the function the pending slot's writer calls.

    ``offer``: the answer's ``clarify`` (None: a text question, recorded with no options). ``question``: the text the user reads.
    ``ws``: the turn's workspace (what it showed, listed and read; its time decision). ``state``: the caller's knowledge of the
    clarifications already asked for the question (``ClarificationState``; None: none). ``pinned_unit``: the message or the
    conversation already pins the unit. ``extra_documents``: documents the turn was shown outside its tools. ``parameter``: the
    clarification asks for a calculation's missing parameter (never capped). ``refuse``: a violation (R10, or a clarification the question did not need) is final - there is no
    further re-prompt - so the options are dropped and the text question stays.

    The record always exists for a clarification. Its options are the valid ones, in the model's order and at most
    ``MAX_OPTIONS``; with fewer than ``MIN_OPTIONS``, or a clarification the guards refuse, it has none and ``check["fallback"]``
    says why."""
    check: dict = {"fallback": None, "dropped": [], "violation": None}
    kind = offer.kind if offer is not None else TEXT
    documents = known_documents(ws, extra_documents)
    options: list[dict] = []
    offered: list[dict] = []
    dropped_labels: list[str] = []

    def drop(o: OfferedOption, why: str) -> None:
        check["dropped"].append({"key": o.key, "reason": why})
        dropped_labels.append(o.label)

    if offer is None:
        check["fallback"] = NO_OFFER
    elif kind == PERIOD and getattr(ws.time, "mode", "none") != "none":
        check["fallback"] = TIME_DECIDES  # an explicit date or the "latest first" switch already decides which date (R3)
    elif kind == UNIT and pinned_unit:
        check["fallback"] = UNIT_PINNED  # the message or the conversation already names the unit
    else:
        grounded = (_ground_units(ctx, ws, offer.options, extra_documents) if kind == UNIT else
                    _ground_period(ws, offer.options) if kind == PERIOD else _ground_phrases(ws, offer.options))
        seen: set[tuple[str, str]] = set()
        seen_ids: set[str] = set()
        for o, g in zip(offer.options, grounded, strict=True):
            if (o.label.strip() or kind != UNIT) and (why := label_problem(kind, o.label)):
                drop(o, why)
            elif isinstance(g, str):
                drop(o, g)
            elif (why := label_problem(kind, g.label)) is not None:
                drop(o, why)
            elif (oid := option_id(kind, g.identity)) in seen_ids or (_flat(g.label), _flat(g.detail)) in seen:
                drop(o, DUPLICATE)
            elif len(options) >= MAX_OPTIONS:
                drop(o, OVER_LIMIT)
            else:
                seen_ids.add(oid)
                seen.add((_flat(g.label), _flat(g.detail)))
                options.append({"id": oid, "label": g.label, "detail": g.detail, "ref": g.ref})
                if g.candidate is not None:
                    offered.append(g.candidate)
                    documents.add(str(g.candidate["document_id"]))
        if len(options) < MIN_OPTIONS:
            check["fallback"] = TOO_FEW
    why = violation(kind, state, {o["id"] for o in options}, check_cap=not parameter)
    if why is None and check["fallback"] in (TIME_DECIDES, UNIT_PINNED):
        why = check["fallback"]
    elif why is None and kind in (AREA_BASIS, METHOD) and check["fallback"] == TOO_FEW and any(
            d["reason"] == PHRASE_NOT_IN_SOURCES for d in check["dropped"]):
        why = PHRASE_NOT_IN_SOURCES  # phrases no source writes: the two readings are the answer, not a question
    check["violation"] = why
    if why is not None and refuse:
        check["fallback"] = f"{why}_refused"
    if check["fallback"]:
        options, offered = [], []
    text = " ".join(question.split())
    override = None
    if any(len(f := _flat(label)) >= 3 and f in _flat(text) for label in dropped_labels):
        override = MSG_NEUTRAL.get(kind)  # the question must not list what was dropped (R6)
        text = override or text
    reason = (offer.reason.strip() if offer is not None else "")
    record = {"id": record_id or new_id(), "question": text if override else question.strip(),
              "reason": "" if _VALUE.search(reason) else reason, "kind": kind, "options": options,
              "allow_all": bool(offer is not None and offer.allow_all and len(options) >= MIN_OPTIONS)}
    return Verdict(record, check, offered, sorted(documents), why, offer, question, override)
