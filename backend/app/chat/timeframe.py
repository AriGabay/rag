"""The question's timeframe and the turn's time mode (round 11 U7: R3, R11, R16; KTD5).

``resolve(question, time_preference)`` decides, once per turn and from nothing but the question and the state the user
message was recorded with, which of three modes the turn runs in:

- ``explicit``: the question gives time — a point ("ב-2022", "נכון ל-15/04/2024", "השומה מיום D"), a range ("בין 2020
  ל-2023", "מ-2020 עד 2023"), an open range ("מאז 2021", "עד 2023", "לפני 2023", "אחרי 2021"), a relative period ("השנה",
  "בשנה שעברה", "בשנתיים האחרונות", "ב-3 השנים האחרונות") or a comparison of two points. It overrides the switch, either
  way (R11); its ``source`` is ``question``;
- ``recent``: no time in the question and the switch on; ``none``: no time and the switch off (R3: no preference, the
  dates the sources give are still shown). Their ``source`` is ``switch``.

The requested time is exact, never widened: "לפני X" and "אחרי X" are exclusive, so the open range ends or starts at the
period next to X in X's own precision ("לפני 2023" ends 2022, "אחרי מרץ 2024" starts אפריל 2024) and every consumer can
read ``start``/``end`` as inclusive. A point has ``start == end``; a comparison holds its two points in date order
(``start`` the earlier) and the coarser precision of the two; a relative period is resolved against the user message's
recorded ``anchor_date`` (Asia/Jerusalem), so a retry a year later gives the same range (R14).

What is not time stays out. The numbers that ``intent`` reads as a block, a parcel, a standard or a section are masked
with the very patterns ``intent`` masks them with; prices, areas, percentages, quarters and street numbers are refused by
the shared parser (``app.platform.dates``), which also refuses a bare year that no date word introduces.

The result is the turn's own: it never writes ``ws.intent``, ``ws.named_documents``, the unit focus or the slots (R16), and
nothing here is carried to the next turn — the next message resolves its own time from its own recorded state."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.chat.intent import _BLOCK, _PARCEL, _PROFESSIONAL_REF, _mask
from app.platform import dates
from app.providers.llm import prompt_text

JERUSALEM = ZoneInfo("Asia/Jerusalem")


def today() -> date:
    """Today's date in the office's calendar (Asia/Jerusalem)."""
    return datetime.now(JERUSALEM).date()


@dataclass(frozen=True)
class Requested:
    """The time a question asks about. ``start``/``end``: inclusive ISO dates in ``precision`` (YYYY, YYYY-MM or
    YYYY-MM-DD); None for an open end. A point has ``start == end``; a comparison's two points are ``start`` (the earlier)
    and ``end``. ``text``: the words of the question that gave it."""
    kind: str
    start: str | None
    end: str | None
    precision: str
    text: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "start": self.start, "end": self.end, "precision": self.precision, "text": self.text}


@dataclass(frozen=True)
class TimeMode:
    """One turn's time decision. ``anchor``: the calendar date the message was written on (relative periods are resolved
    against it); ``prefer_recent``: the switch as the message was recorded with it."""
    mode: str = "none"
    source: str = "switch"
    anchor: date = field(default_factory=today)
    requested: Requested | None = None
    prefer_recent: bool = False

    def as_dict(self) -> dict:
        """The diagnostics record (``resolution["time"]``): mode, source, switch, anchor, requested time."""
        return {"mode": self.mode, "source": self.source, "prefer_recent": self.prefer_recent,
                "anchor": self.anchor.isoformat(), "requested": self.requested.as_dict() if self.requested else None}


# --- reading the question ----------------------------------------------------------------------------------------------

_HY = "-־‐‑–—"
_WORD_N = {"שלוש": 3, "ארבע": 4, "חמש": 5, "שש": 6, "שבע": 7, "שמונה": 8, "תשע": 9, "עשר": 10}
_TRAILING_N = re.compile(
    rf"(?<![א-ת])ב?(?:(?P<n>\d{{1,2}})\s*|(?P<w>{'|'.join(_WORD_N)})\s+)ה?שנים\s+האחרונות(?![א-ת])")
_TRAILING_TWO = re.compile(r"(?<![א-ת])ב?שנתיים\s+ה?אחרונות(?![א-ת])")
_TRAILING_ONE = re.compile(r"(?<![א-ת])[בה]?שנה\s+האחרונה(?![א-ת])")
_LAST_YEAR = re.compile(r"(?<![א-ת])(?:[בכ]?שנה\s+(?:שעברה|הקודמת)|אשתקד)(?![א-ת])")
# "השנה" is the year we are in; "השנה שבה …" is a relative clause about some year, not time
_THIS_YEAR = re.compile(r"(?<![א-ת])(?:השנה(?:\s+(?:הזו|הזאת|הנוכחית))?|בשנה\s+(?:הזו|הזאת|הנוכחית))(?![א-ת])(?!\s+ש)")

_SINCE = re.compile(r"(?<![א-ת])מאז\s*$")
_UNTIL = re.compile(rf"(?<![א-ת])ו?עד\s*(?:ל[{_HY}]?\s*)?$")
_BEFORE = re.compile(rf"(?<![א-ת])לפני\s*[{_HY}]?\s*$")
_AFTER = re.compile(rf"(?<![א-ת])אחרי\s*[{_HY}]?\s*$")
_FROM = re.compile(rf"(?<![א-ת])מ[{_HY}]?\s*$")
# the parser knows a bare year after a date word; "לפני" and "אחרי" are date words here (same length: the offsets hold)
_NEAR_WORD = re.compile(r"(?<![א-ת])(?:לפני|אחרי)(?=\s)")
# a conjunction glued to a date word ("ובשנת 2024", "וב-2024") would hide the word from the parser
_CONJUNCTION = re.compile(rf"(?<![א-ת])ו(?=(?:בשנת|לשנת|משנת|שנת|עד|בין|מאז|[בלמ][{_HY}]))")


# A year that follows a building, purchase or legislation phrase ("נבנה ב-1985", "נרכשה בשנת 2010", "חוק התכנון והבנייה
# 1965") describes the property or the law, not the valuation time asked about (review #2): masked like an identifier, so
# it never makes the turn explicit. Within 25 characters, no digit, no sentence break and no valuation word between the
# phrase and the year: "נרכש לפני שנים, מה השווי ב-2022" keeps its year.
_NON_VALUATION_YEAR = re.compile(
    r"(?<![א-ת])[והבלמכש]{0,3}(?:נבנ(?:ה|תה|ו)|בנייה|בניה|הקמה|נרכש(?:ה|ו)?|רכישה|חוקי|חוק|תקנות|תיקון|היתרי|היתר|צו)"
    r"(?![א-ת])(?:(?!שווי|נכון|שומ|מחיר|הערכ)[^\d?.!,;:]){0,25}?(?:\d{1,2}[./]){0,2}(?:19|20)\d{2}(?!\d)")


def _blank(text: str, span: tuple[int, int]) -> str:
    return text[:span[0]] + " " * (span[1] - span[0]) + text[span[1]:]


def _step(iso: str, precision: str, delta: int) -> str:
    """The period next to ``iso`` in its own precision (never finer, never coarser)."""
    if precision == "year":
        return f"{int(iso) + delta:04d}"
    if precision == "month":
        n = int(iso[:4]) * 12 + int(iso[5:7]) - 1 + delta
        return f"{n // 12:04d}-{n % 12 + 1:02d}"
    return (date.fromisoformat(iso) + timedelta(days=delta)).isoformat()


def _years_back(day: date, n: int) -> date:
    try:
        return day.replace(year=day.year - n)
    except ValueError:  # a leap day: the nearest valid day before it
        return day.replace(year=day.year - n, day=28)


def _relative(masked: str, anchor: date) -> tuple[list[tuple[int, Requested]], str]:
    """The relative periods of the text, resolved against ``anchor``, with the text left once their words are blanked (so
    "השנים האחרונות" is not also read as something else). Longest forms first."""
    found: list[tuple[int, Requested]] = []
    rest = masked

    def take(pattern: re.Pattern, build) -> None:
        nonlocal rest
        for m in list(pattern.finditer(rest)):
            requested = build(m)
            if requested is not None:
                found.append((m.start(), requested))
                rest = _blank(rest, m.span())

    def trailing(n: int, m: re.Match) -> Requested | None:
        if not 1 <= n <= 50:
            return None
        return Requested("relative", _years_back(anchor, n).isoformat(), anchor.isoformat(), "day", m.group().strip())

    take(_TRAILING_N, lambda m: trailing(int(m["n"]) if m["n"] else _WORD_N[m["w"]], m))
    take(_TRAILING_TWO, lambda m: trailing(2, m))
    take(_TRAILING_ONE, lambda m: trailing(1, m))
    take(_LAST_YEAR, lambda m: Requested("relative", str(anchor.year - 1), str(anchor.year - 1), "year", m.group().strip()))
    take(_THIS_YEAR, lambda m: Requested("relative", str(anchor.year), str(anchor.year), "year", m.group().strip()))
    return found, rest


def _absolute(masked: str, rest: str) -> list[tuple[int, Requested]]:
    """The dates the question states, each classified by the word in front of it. ``masked``: the text with the
    identifier numbers masked (the words are read there); ``rest``: the same with the relative periods blanked."""
    near = _NEAR_WORD.sub("בשנת", rest)  # same length ("לפני"/"אחרי" and "בשנת" are 4 letters)
    near = _CONJUNCTION.sub(" ", near)
    out: list[tuple[int, Requested]] = []
    for p in dates.find_all(near):
        start, end = p.span
        if p.end is not None:
            out.append((start, Requested("range", p.iso, p.end, p.precision, p.written)))
            continue
        before = masked[:start]
        text = p.written
        if _SINCE.search(before) or _FROM.search(before):
            requested = Requested("open", p.iso, None, p.precision, text)
        elif _BEFORE.search(before):
            requested = Requested("open", None, _step(p.iso, p.precision, -1), p.precision, f"לפני {text}")
        elif _AFTER.search(before):
            requested = Requested("open", _step(p.iso, p.precision, 1), None, p.precision, f"אחרי {text}")
        elif _UNTIL.search(before):
            requested = Requested("open", None, p.iso, p.precision, text)
        else:
            requested = Requested("point", p.iso, p.iso, p.precision, text)
        out.append((start, requested))
    return out


def _compare(a: Requested, b: Requested) -> Requested:
    precision = dates.coarser(a.precision, b.precision)
    first, second = sorted((dates.cut(a.start, precision), dates.cut(b.start, precision)))  # type: ignore[arg-type]
    return Requested("compare", first, second, precision, f"{a.text} / {b.text}")


def _requested(question: str, anchor: date) -> Requested | None:
    masked = _mask(question or "", _BLOCK, _PARCEL, _PROFESSIONAL_REF, _NON_VALUATION_YEAR)
    relative, rest = _relative(masked, anchor)
    items = sorted(relative + _absolute(masked, rest), key=lambda item: item[0])
    if not items:
        return None
    first = items[0][1]
    points = [r for _, r in items if r.kind == "point"]
    if first.kind == "point":
        distinct = list({(r.start, r.precision): r for r in points}.values())
        if len(distinct) >= 2:
            return _compare(distinct[0], distinct[1])  # a third point is not part of a comparison of two
    return first


def _anchor_of(time_preference: dict | None) -> date:
    try:
        return date.fromisoformat(str((time_preference or {}).get("anchor_date")))
    except ValueError:
        return today()


def resolve(question: str, time_preference: dict | None) -> TimeMode:
    """The turn's time mode (see the module docstring). ``time_preference``: the state the user message was recorded
    with (``{"version", "prefer_recent", "anchor_date"}``); None (a caller that gives none): the switch off, today's
    Jerusalem date as the anchor."""
    prefer = bool((time_preference or {}).get("prefer_recent"))
    anchor = _anchor_of(time_preference)
    requested = _requested(question, anchor)
    if requested is not None:
        return TimeMode("explicit", "question", anchor, requested, prefer)
    return TimeMode("recent" if prefer else "none", "switch", anchor, None, prefer)


# --- the model-facing block ---------------------------------------------------------------------------------------------

# the one phrase the answer uses for a datum when not every appraisal of the property was checked (round 12 U8, R12, KTD8;
# session-settled): the latest datum in the sources checked, never the latest in the repository
QUALIFIED = "במקורות שנבדקו"   # the words that qualify a "latest" claim (`verify` checks for them)
LATEST_CHECKED = f"הנתון המאוחר ביותר {QUALIFIED}"
PHRASE_RULE = (
    f"כשתוצאת החיפוש מציינת שכיסוי השומות חלקי, או שהנכס לא זוהה, אל תכתוב על נתון שהוא «העדכני ביותר» או «האחרון»: "
    f"כתוב «{LATEST_CHECKED}» וציין מה לא נבדק. גם כשהכיסוי מלא, אל תכתוב שזה הנתון האחרון במאגר — רק של השומות "
    f"שנבדקו. השרת מוסיף לתשובה הערת כיסוי משלו, ואינו תלוי בניסוח שלך.")

_GENERAL_RULES = (
    "- אל תשמיט נתון ישן שרלוונטי: העדפת הזמן קובעת סדר, לא סינון.\n"
    "- תאריך שהמקורות אינם נותנים נשאר \"מועד לא ידוע\" ומוצג אחרי הנתונים המתוארכים; אל תנחש אותו, ואל תתארך נתון "
    "לפי תאריך כתיבת הדוח, ההעלאה או העיבוד.\n"
    "- תחזית או אומדן לעתיד אינם נתון שנמדד: אל תציג אותם כך, ואל תציג אותם כנתון העדכני.\n"
    "- נתונים של נכסים שונים, או של מדד, יחידה, תקופתיות, בסיס שטח או מע\"מ שונים, אינם היסטוריה אחת: הצג אותם "
    "כקבוצות נפרדות (למשל שכירות לחודש מול שכירות לשנה, כולל מע\"מ מול לא כולל), כל קבוצה מהחדש לישן.\n"
    "- רשום ב-take_value כל נתון מתוארך שאתה מציג, גם את הקודמים, וצטט אותו (V#): השרת בונה מהערכים הרשומים ציר זמן "
    "מאומת עם המועד והמקור של כל אחד, ונתון שצוטט רק כקטע (S#) אינו נכנס אליו.\n"
    f"- {PHRASE_RULE}")

BLOCK_RECENT = (
    "הנחיית זמן (המשתמש ביקש להעדיף נתונים עדכניים):\n"
    "- פתח בנתון הרלוונטי העדכני ביותר, לפי התאריך שאליו הנתון מתייחס, ואחריו נתונים קודמים רלוונטיים, מהחדש לישן, "
    "כל אחד עם התאריך והמקור שלו.\n" + _GENERAL_RULES)

_EXPLICIT_HEAD = "הנחיית זמן (השאלה מבקשת זמן מסוים; הוא גובר על העדפת הנתונים העדכניים):\n"
_EXPLICIT_RULES = (
    "- ענה תחילה על הזמן המבוקש. נתונים מחוץ לו יופיעו רק אחר כך, מסומנים כמחוץ לזמן שביקש המשתמש.\n"
    "- אם אין נתון בתוך הזמן המבוקש, אמור זאת במפורש והצג את הנתון הקרוב ביותר אליו.\n" + _GENERAL_RULES)


def _describe(r: Requested) -> str:
    if r.kind == "compare":
        return f"השוואה בין שתי נקודות זמן: {r.start} מול {r.end}"
    if r.kind == "point":
        return f"נקודת זמן: {r.start}"
    if r.start and r.end:
        return f"הטווח {r.start} עד {r.end} (כולל)"
    if r.start:
        return f"מ-{r.start} ואילך (כולל)"
    return f"עד {r.end} (כולל)"


def block(time: TimeMode) -> str | None:
    """The short Hebrew time block of the turn's first message (``engine._context_message``): None when the turn has no
    time preference (mode ``none``: no block, dates are still given as written)."""
    if time.mode == "recent":
        return BLOCK_RECENT
    if time.mode == "explicit" and time.requested is not None:
        r = time.requested
        lines = [f"- הזמן המבוקש: {_describe(r)}; כפי שנכתב בשאלה: «{prompt_text(r.text)}»."]
        if r.kind == "relative":
            lines.append(f"- תאריך העוגן של התקופה היחסית: {time.anchor.isoformat()}.")
        return _EXPLICIT_HEAD + "\n".join(lines) + "\n" + _EXPLICIT_RULES
    return None


# --- the coverage note (round 12 U8, R12, R13, KTD7, KTD8) ----------------------------------------------------------------
#
# The server words the coverage record (``tools.coverage_record``; persisted as ``answer.time.coverage``) itself, from the
# record alone: the same note is in the payload and the copy text whether or not the answer has timeline rows, and the model
# never decides it. A message without a record reads as "not recorded": no note, never "complete". Statuses and blocking
# reasons are the record's own strings, defined here once (this module is a leaf: ``tools`` imports them).

# ``LOCATED``: a passage of the unit was returned by the search (round 13 U8, KTD16: the status ``hit`` of a round-12 record,
# which a stored answer may still carry, is the same thing). It is not ``READ``: a unit is read when the turn registered a
# datum of the cited datum's base key from it, or read its section or table.
LOCATED, NO_MATCH, NOT_SEARCHED_CAP = "located", "no_match", "not_searched_cap"
# a record built at answer time for a turn that ran no time-ranked search (``read_coverage``, round 12 U12b): a unit whose
# source the answer cites was ``READ``; every other unit of the property is ``NOT_SEARCHED`` (no search ran at all)
READ, NOT_SEARCHED = "read", "not_searched"
PROPERTY, UNIT, WINDOW = "property", "unit", "window"  # what a record covers: a gathered property, one unit, a relevance window
# why a record is not complete (``tools.assess_coverage``); a unit that causes one carries ``blocks``
B_NEWER, B_NO_MATCH, B_RANGE = "newer_not_searched", "newer_no_match", "range_not_searched"
B_UNDATED, B_CANDIDATES, B_UNDETERMINED = "undated", "candidates", "undetermined"  # ``undetermined``: a v1 record only
B_NOT_IDENTIFIED, B_WINDOW, B_SEVERAL = "not_identified", "window", "several_scopes"
B_NOT_SEARCHED = "not_searched"
# round 13 U8 (R18-R20, KTD15, KTD16): a newer unit only located; a possible candidate (an unidentified unit that mentions the
# property or was read in part); a newer datum of an undecided class (U4, KTD4)
B_LOCATED, B_POSSIBLE, B_LATER_UNDECIDED = "newer_located", "possible", "later_undecided"

MSG_NOTE_RECENT = f"{LATEST_CHECKED}."
MSG_NOTE_EXPLICIT = f"הנתונים לתקופה שביקשת נמצאו רק {QUALIFIED}."
MSG_NOTE_NOT_IDENTIFIED = "הנכס לא זוהה; נבדקה רק השומה שבמוקד."
MSG_NOTE_COMPLETE = "נבדקו כל השומות של הנכס הזמינות לך ({n})."  # relative to the appraisals visible to the user
MSG_GROUP_CAP = "לא נבדקו (מגבלת מספר השומות בחיפוש)"
MSG_GROUP_NO_MATCH = "נבדקו ללא התאמה לחיפוש"
MSG_GROUP_READ_ONLY = "נבדקו רק המקורות שנקראו; לא נבדקו"  # a turn with no time-ranked search (round 12 U12b)
MSG_GROUP_UNDATED = "ללא מועד קובע ברור"
MSG_GROUP_CANDIDATES = "שומות שלא קושרו לנכס (אין הוכחה שהן שלו)"
MSG_GROUP_UNDETERMINED = "שומות שלא ניתן לקבוע אם הן של הנכס"  # a v1 record's count; a v2 record has the two lines below
MSG_GROUP_POSSIBLE = "שומות בלי פרטי זיהוי שייתכן שהן של הנכס (הנכס נזכר בהן, או שנקראו רק בחלקן)"
MSG_GROUP_UNLINKABLE = "שומות במשרד בלי פרטי זיהוי ובלי אזכור של הנכס, שלא נכללו"
MSG_LOCATED = "נמצא קטע בשומה מ-{date} אך הנתון בה לא נקרא."
MSG_GROUP_LATER_UNDECIDED = "נמצא נתון מאוחר יותר שלא ניתן להשוות לנתון שצוין: {title} ({when})"
MSG_GROUP_WINDOW = "נבדקו רק השומות שעלו בחיפוש, בלי רשימה של כל שומות הנכס."
MSG_GROUP_SEVERAL = "הנתונים הם מכמה חיפושים או נכסים, ולכן הכיסוי אינו אחיד."
MSG_UNTITLED = "שומה ללא כותרת"
MSG_MORE = "ועוד {n}"
NAMED_PER_GROUP = 3  # units a reason group names; the rest are counted
_SUMMARY_HEAD = "כיסוי השומות בחיפוש הזה (השרת מוסיף אותו לתשובה):\n"


def _date_of(item: dict) -> str:
    return item.get("date") or ""


def _named(item: dict, dated: bool = True) -> str:
    """A unit as the note names it: its title, and its valuation date (as the reader sees it) unless it has none."""
    title = (item.get("title") or "").strip() or MSG_UNTITLED
    if not dated or not item.get("date"):
        return title
    return f"{title} ({dates.shown_period(item['date'], item.get('precision') or 'day', None)})"


def _group(label: str, items: list[dict], total: int | None = None, *, dated: bool = True) -> str | None:
    """One reason line: the label, then at most ``NAMED_PER_GROUP`` units newest first (the dated before the undated, ties in
    the record's order), then "ועוד N" for the rest of ``total`` (the record's own count of a group it only names in part)."""
    if not items:
        return None
    ordered = sorted(items, key=_date_of, reverse=True)  # stable: undated ("") last, equal dates keep the record's order
    shown = ordered[:NAMED_PER_GROUP]
    rest = max((total if total is not None else len(items)) - len(shown), 0)
    names = "; ".join(_named(i, dated) for i in shown)
    return f"{label}: {names}" + (f" {MSG_MORE.format(n=rest)}" if rest else "") + "."


def _located(items: list[dict]) -> list[str]:
    """One line per newer unit that was only located (round 13 U8, R19), newest first, at most ``NAMED_PER_GROUP``, then
    "ועוד N": the search returned a passage of it but no datum of the cited kind was registered from it or read in it."""
    ordered = sorted(items, key=_date_of, reverse=True)
    lines = [MSG_LOCATED.format(date=dates.shown_period(i["date"], i.get("precision") or "day", None))
             for i in ordered[:NAMED_PER_GROUP] if i.get("date")]
    if len(ordered) > NAMED_PER_GROUP:
        lines.append(MSG_MORE.format(n=len(ordered) - NAMED_PER_GROUP) + ".")
    return lines


def _later_undecided(record: dict) -> str | None:
    """The newer datum of a class that could not be compared with the cited one (round 13 U4, KTD4; U8): named by its title
    and date, so the answer keeps the qualified wording and says which datum it could not place."""
    found = record.get("later_undecided") or []
    if not found:
        return None
    return "; ".join(MSG_GROUP_LATER_UNDECIDED.format(title=(i.get("title") or "").strip() or MSG_UNTITLED,
                                                      when=i.get("when") or "") for i in found[:NAMED_PER_GROUP]) + "."


def _unlinkable(record: dict) -> str | None:
    n = int(record.get("unlinkable") or 0)
    return f"{MSG_GROUP_UNLINKABLE}: {n}." if n else None


def coverage_note(record: dict | None) -> str | None:
    """The visible coverage note of a record (KTD8's state table), or None for a message with no record (an old message,
    or a turn with no time mode: nothing is claimed). Complete: the count of the appraisals checked, relative to those the
    user can see, and — round 13 U8 (R18) — the count of unlinkable units, which are no part of the property's record and
    block nothing. Not identified: the property was not identified, only the focus appraisal was searched. Partial: the
    mode's sentence, then one line per reason group — the units that keep the datum from being called the latest (not
    searched for the cap or searched without a match, only those that block), the units nothing searched when the answer
    rests on reads alone ("נבדקו רק המקורות שנקראו; לא נבדקו", round 12 U12b), a newer unit only located (R19), the undated
    ones, the unlinked candidates, the possible candidates among the unidentified units, a newer datum that could not be
    compared (KTD4) and the count of unlinkable units. At most three units per group, newest first, then "ועוד N". A v1
    record (its single ``undetermined`` count) renders as it did."""
    if not record or record.get("mode") not in ("recent", "explicit"):
        return None
    units = record.get("units") or []
    blocking = record.get("blocking") or []
    if record.get("complete"):
        return "\n".join(ln for ln in (MSG_NOTE_COMPLETE.format(n=len(units)), _unlinkable(record)) if ln)
    if B_NOT_IDENTIFIED in blocking:
        return MSG_NOTE_NOT_IDENTIFIED
    lines = [MSG_NOTE_EXPLICIT if record["mode"] == "explicit" else MSG_NOTE_RECENT]
    dated = [u for u in units if not u.get("undated")]
    candidates = record.get("candidates") or {}
    possible = record.get("possible") or {}
    undetermined = int(record.get("undetermined") or 0)
    lines += [ln for ln in (
        _group(MSG_GROUP_CAP, [u for u in dated if u["status"] == NOT_SEARCHED_CAP and u.get("blocks")]),
        _group(MSG_GROUP_NO_MATCH, [u for u in dated if u["status"] == NO_MATCH and u.get("blocks")]),
        _group(MSG_GROUP_READ_ONLY, [u for u in dated if u["status"] == NOT_SEARCHED and u.get("blocks")]),
        *_located([u for u in dated if u["status"] == LOCATED and u.get("blocks")]),
        _group(MSG_GROUP_UNDATED, [u for u in units if u.get("undated")], dated=False),
        _group(MSG_GROUP_CANDIDATES, list(candidates.get("named") or []), int(candidates.get("count") or 0)),
        _group(MSG_GROUP_POSSIBLE, list(possible.get("named") or []), int(possible.get("count") or 0)),
        _later_undecided(record),
        _unlinkable(record),
        f"{MSG_GROUP_UNDETERMINED}: {undetermined}." if undetermined else None,
        MSG_GROUP_WINDOW if B_WINDOW in blocking else None,
        MSG_GROUP_SEVERAL if B_SEVERAL in blocking else None) if ln]
    return "\n".join(lines)


def coverage_summary(record: dict | None) -> str | None:
    """What the model reads after a time-ranked search (KTD8): the note the answer will carry, and — when the coverage is
    partial or the property not identified — the rule for the phrase. None without a record."""
    note = coverage_note(record)
    if note is None:
        return None
    if record.get("complete"):
        return _SUMMARY_HEAD + note
    return f"{_SUMMARY_HEAD}{note}\n{PHRASE_RULE}"
