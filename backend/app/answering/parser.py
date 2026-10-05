"""Rules-first Hebrew question parser (KTD1, R17, R18).

Turns a question into ``QueryConditions`` plus an ordered list of missing conditions that change
the result. Place names are matched only against the office's own data (the gazetteer), never
against outside knowledge. The question text is data: nothing in it can add fields or SQL."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.answering.conditions import QueryConditions
from app.extraction.normalize_text import base_normalize, prefix_variants

_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20\d{2})(?!\d)")
_RANGE = re.compile(r"(?<!\d)(19[5-9]\d|20\d{2})\s*(?:-|–|עד|ל-?|ול-?)\s*(19[5-9]\d|20\d{2})(?!\d)")
_FOLLOWUP = re.compile(r"^\s*(ו?מה\s+(לגבי|עם|ב)|ו?ב-?\s*(19|20)\d{2}|ואם|ו?באותם תנאים|ו?אותו דבר)")
_NEIGHBORHOOD_PHRASE = re.compile(r"(?:ב|ל|מ)?שכונת\s+((?:[א-ת״׳\"'-]+\s?){1,3})")

TRANSACTION_WORDS = ("עסקה", "עסקאות", "עסקת", "נמכר", "נמכרה", "נמכרו", "מכירה", "מכירות", "מחירי עסקאות")
VALUE_WORDS = ("שווי", "שומה", "שומות", "הוערך", "הוערכו", "הערכת")
ASKING_WORDS = ("מבוקש", "מחיר מבוקש", "מחירים מבוקשים")
ADJUSTED_WORDS = ("מתואם", "מתואמים", "לאחר התאמות")
CALC_WORDS = ("למ״ר", "למטר", "ממוצע", "חציון", "משוקלל", "כמה עסקאות", "מספר העסקאות", "מחיר", "שווי")
EXPLAIN_WORDS = ("למה", "מדוע", "שיקול", "שיקולי", "השיקולים", "הסבר", "נימוק", "נימוקי", "מה נכתב", "מה נאמר",
                 "איך השמאי", "מה השמאי", "הוזכר", "הוזכרו", "התייחס")
LOOKUP_WORDS = ("באיזה מסמך", "באיזו שומה", "באילו שומות", "באילו מסמכים", "איפה מופיע", "מצא את", "איזה מסמך",
                "אילו מסמכים", "איזו שומה")
DATE_FIELD_WORDS = {
    "valuation_date": ("מועד קובע", "המועד הקובע", "למועד הקובע", "במועד הקובע"),
    "report_date": ("תאריך עריכת", "נערכה", "נערכו", "נכתבה", "נכתבו", "תאריך השומה", "תאריך הדוח"),
    "transaction_date": ("תאריך עסקה", "תאריך העסקה", "לפי תאריך עסקה", "נחתמו", "נחתמה", "בוצעו", "בוצעה",
                         "שנחתמו", "שבוצעו", "נמכרו ב", "נמכר ב"),
}
AREA_WORDS = {"net": ("נטו",), "gross": ("ברוטו",), "registered": ("רשום", "רשומה"),
              "equivalent": ("אקוויוולנטי", "אקויוולנטי")}
PROPERTY_WORDS = [
    ("garden_apartment", ("דירת גן", "דירות גן")), ("penthouse", ("פנטהאוז", "פנטהאוס")),
    ("duplex", ("דופלקס",)), ("cottage", ("קוטג׳", "קוטג'")), ("house", ("בית פרטי", "בתים פרטיים")),
    ("apartment", ("דירה", "דירות")), ("office", ("משרדים",)), ("retail", ("חנות", "חנויות")),
    ("land", ("מגרש", "מגרשים")),
]


@dataclass
class Gazetteer:
    cities: list[str] = field(default_factory=list)
    neighborhoods: list[tuple[str | None, str]] = field(default_factory=list)


@dataclass
class ParseResult:
    conditions: QueryConditions
    missing: list[str]
    route: str  # rules | followup | model
    unknown_place: str | None = None


def _tokens(text: str) -> list[str]:
    return re.findall(r"[א-ת״׳\w./-]+", text)


def _contains_phrase(tokens: list[str], phrase: str) -> bool:
    words = phrase.split()
    for i in range(len(tokens) - len(words) + 1):
        first = tokens[i]
        if first != words[0] and words[0] not in prefix_variants(first):
            continue
        if tokens[i + 1:i + len(words)] == words[1:]:
            return True
    return False


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def _place(tokens: list[str], gaz: Gazetteer) -> tuple[str | None, str | None]:
    city = next((c for c in sorted(gaz.cities, key=len, reverse=True) if _contains_phrase(tokens, c)), None)
    hood_city, hood = None, None
    for c, n in sorted(gaz.neighborhoods, key=lambda x: len(x[1]), reverse=True):
        if (city is None or c is None or c == city) and _contains_phrase(tokens, n):
            hood_city, hood = c, n
            break
    if city is None and hood is not None:
        cities = {c for c, n in gaz.neighborhoods if n == hood and c}
        if len(cities) == 1:
            city = hood_city
    return city, hood


def _years(text: str) -> tuple[int | None, int | None]:
    m = _RANGE.search(text)
    if m:
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        return a, b
    years = [int(y) for y in _YEAR.findall(text)]
    return (years[0], None) if years else (None, None)


def _intent(text: str) -> str:
    calc = _has(text, CALC_WORDS)
    explain = _has(text, EXPLAIN_WORDS)
    lookup = _has(text, LOOKUP_WORDS)
    if lookup and not calc:
        return "document_lookup"
    if explain and calc and ("למ״ר" in text or "ממוצע" in text or "משוקלל" in text):
        return "combined"
    if explain:
        return "explanation"
    if lookup:
        return "document_lookup"
    return "calculation" if calc else "explanation"


def _data_kind(text: str) -> str | None:
    kinds = set()
    if _has(text, ADJUSTED_WORDS):
        kinds.add("adjusted_comparable")
    elif _has(text, ASKING_WORDS):
        kinds.add("asking_price")
    else:
        if _has(text, TRANSACTION_WORDS):
            kinds.add("transaction_price")
        if _has(text, VALUE_WORDS):
            kinds.add("appraised_value")
            if "מחיר" in text and "transaction_price" not in kinds:
                return None  # "price" and "value" together: which one is meant is the user's call
    return kinds.pop() if len(kinds) == 1 else None


def _date_field(text: str) -> str | None:
    found = [f for f, words in DATE_FIELD_WORDS.items() if _has(text, words)]
    return found[0] if len(found) == 1 else None


def missing_conditions(c: QueryConditions) -> list[str]:
    """Conditions that change the result and must be clarified before computing (R18, R19)."""
    if c.intent not in ("calculation", "combined"):
        return []
    missing = []
    if c.data_kind is None:
        missing.append("data_kind")
    if c.year_from is not None and c.date_field is None:
        missing.append("date_field")
    return missing


def parse_question(question: str, gaz: Gazetteer, previous: QueryConditions | None = None) -> ParseResult:
    text = base_normalize(question)
    tokens = _tokens(text)
    city, hood = _place(tokens, gaz)
    year_from, year_to = _years(text)

    unknown = None
    m = _NEIGHBORHOOD_PHRASE.search(text)
    if m and hood is None:
        unknown = m.group(1).strip(" ?.!,")
        unknown = re.sub(r"\s+(ב-?|בשנת|לשנת|ל-?)?\s*\d{4}.*$", "", unknown).strip()
        unknown = re.sub(r"\s+(ב|ל|מ)(שנת|-).*$", "", unknown).strip() or None

    if previous is not None and _FOLLOWUP.search(question):
        update: dict = {}
        if year_from is not None:
            update |= {"year_from": year_from, "year_to": year_to}
        if hood is not None:
            update |= {"neighborhood": hood, "city": city or previous.city}
        elif city is not None:
            update |= {"city": city, "neighborhood": None}
        area = next((k for k, w in AREA_WORDS.items() if _has(text, w)), None)
        if area:
            update["area_type"] = area
        if update:
            conds = previous.model_copy(update=update)
            return ParseResult(conds, missing_conditions(conds), "followup", unknown)

    is_followup = bool(_FOLLOWUP.search(question))
    conds = QueryConditions(
        intent="calculation" if is_followup and not _has(text, EXPLAIN_WORDS) else _intent(text),
        data_kind=_data_kind(text),
        date_field=_date_field(text),
        year_from=year_from,
        year_to=year_to,
        city=city,
        neighborhood=hood,
        property_type=next((code for code, words in PROPERTY_WORDS if _has(text, words)), None),
        area_type=next((k for k, w in AREA_WORDS.items() if _has(text, w)), None),
        vat_basis="excluded" if "לא כולל מע" in text else ("included" if "כולל מע" in text else None),
        aggregation="weighted" if "משוקלל" in text else ("median" if "חציון" in text else "both"),
    )
    return ParseResult(conds, missing_conditions(conds), "rules", unknown)
