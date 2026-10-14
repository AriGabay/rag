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

_GENERAL_RULES = (
    "- אל תשמיט נתון ישן שרלוונטי: העדפת הזמן קובעת סדר, לא סינון.\n"
    "- תאריך שהמקורות אינם נותנים נשאר \"מועד לא ידוע\" ומוצג אחרי הנתונים המתוארכים; אל תנחש אותו, ואל תתארך נתון "
    "לפי תאריך כתיבת הדוח, ההעלאה או העיבוד.\n"
    "- תחזית או אומדן לעתיד אינם נתון שנמדד: אל תציג אותם כך, ואל תציג אותם כנתון העדכני.\n"
    "- נתונים של נכסים שונים, או של מדד, יחידה, תקופתיות, בסיס שטח או מע\"מ שונים, אינם היסטוריה אחת: הצג אותם "
    "כקבוצות נפרדות (למשל שכירות לחודש מול שכירות לשנה, כולל מע\"מ מול לא כולל), כל קבוצה מהחדש לישן.\n"
    "- רשום ב-take_value כל נתון מתוארך שאתה מציג, גם את הקודמים, וצטט אותו (V#): השרת בונה מהערכים הרשומים ציר זמן "
    "מאומת עם המועד והמקור של כל אחד, ונתון שצוטט רק כקטע (S#) אינו נכנס אליו.")

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
