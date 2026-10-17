"""Rules-first Hebrew question parser (KTD1, KTD2, R2, R3, R17, R18).

Turns a question into ``QueryConditions`` plus an ordered list of missing conditions that change
the result. Place names are matched only against the office's own data (the gazetteer), never
against outside knowledge. The question text is data: nothing in it can add fields or SQL.

It also reports the content tokens it could not explain. The vocabulary below is grammar, places,
years, money, aggregation, date fields, area bases, property types and follow-up forms only; it never
names the topic of a question. The fast path runs only when every content token is explained and the
request is monetary, so a question about anything else leaves the money path in every mode."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.answering.conditions import QueryConditions
from app.answering.places import KNOWN_CITIES
from app.extraction.normalize_text import STOPWORDS, base_normalize, prefix_variants

_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20\d{2})(?!\d)")
_RANGE = re.compile(r"(?<!\d)(19[5-9]\d|20\d{2})\s*(?:-|–|עד|ל-?|ול-?)\s*(19[5-9]\d|20\d{2})(?!\d)")
_FOLLOWUP = re.compile(r"^\s*(ו?מה\s+(לגבי|עם|ב)|ו?ב-?\s*(19|20)\d{2}|ואם|ו?באותם תנאים|ו?אותו דבר)")
_NEIGHBORHOOD_PHRASE = re.compile(r"(?:ב|ל|מ)?שכונת\s+((?:[א-ת״׳\"'-]+\s?){1,3})")
_CITY_PHRASE = re.compile(r"(?:ב|ל|מ)?עיר\s+((?:[א-ת״׳\"'-]+\s?){1,3})")

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

# Explicit monetary terms (price, value, transaction, per square meter, currency). Aggregation words
# such as "ממוצע" are not among them: an average of something else is not a price.
MONETARY_WORDS = ("מחיר", "מחירי", "מחירים", "שווי", "עסקה", "עסקאות", "עסקת", "נמכר", "נמכרה", "נמכרו", "מכירה",
                  "מכירות", "מבוקש", "מבוקשים", "מתואם", "מתואמים", "הוערך", "הוערכו", "הערכת", "למ״ר", "למטר",
                  "₪", "ש״ח", "שקל", "שקלים")
AGGREGATION_WORDS = ("ממוצע", "ממוצעת", "ממוצעים", "חציון", "משוקלל", "משוקללת", "מספר", "סך")
UNIT_WORDS = ("מ״ר", "מטר")
CONNECTOR_PHRASES = ("לפי", "שכונת", "עיר", "שנת", "כולל מע״מ", "מע״מ", "באותם תנאים", "אותו דבר",
                     "השוואה מתואם", "השוואה מתואמים", "נתוני השוואה")
RELATIVE_YEAR_PHRASES = {
    -1: ("שנה הקודמת", "השנה הקודמת", "שנה קודמת", "שנה שלפני", "השנה שלפני"),
    1: ("שנה שאחרי", "השנה שאחרי", "שנה הבאה", "השנה הבאה"),
}
# A clause introduced by one of these, after the monetary part, may hold the words the rules cannot explain.
CLAUSE_STARTERS = ("ומה", "ולמה", "למה", "מדוע", "ומדוע", "הסבר", "והסבר")
CLAUSE_PHRASES = ("מה נכתב", "מה נאמר", "מה כתוב")
_TOKEN = re.compile(r"[א-ת״׳\w./-]+")
_TOKEN_EDGES = "./-׳״"
_YEAR_TOKEN = re.compile(r"(19[5-9]\d|20\d{2})")


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
    explicit: frozenset[str] = frozenset()  # keys a follow-up turn stated itself
    unexplained: tuple[str, ...] = ()  # content tokens the rules could not account for (KTD2)
    monetary: bool = False  # explicit monetary term, or a follow-up form after a monetary computation
    fast_path: bool = False  # every unexplained token (if any) sits in a separable explanation clause
    followup: bool = False  # the question has a follow-up form ("ומה לגבי ...")
    relative_year_offset: int | None = None  # "השנה הקודמת" = -1, resolved against stored conditions
    numeric_question: str | None = None  # the monetary part when an explanation clause was split off
    explanation_clause: str | None = None  # the clause answered by content search


def _tokens(text: str) -> list[str]:
    return re.findall(r"[א-ת״׳\w./-]+", text)


def _contains_phrase(tokens: list[str], phrase: str) -> bool:
    words = phrase.split()
    for i in range(len(tokens) - len(words) + 1):
        first = tokens[i]
        short_prefix = len(first) > 1 and first[0] in "בלמהושכ" and first[1:] == words[0]
        if first != words[0] and not short_prefix and words[0] not in prefix_variants(first):
            continue
        if tokens[i + 1:i + len(words)] == words[1:]:
            return True
    return False


def _word_matches(token: str, word: str) -> bool:
    """Exact match, or the word behind one or two Hebrew prefix letters ("לעסקאות" -> "עסקאות")."""
    token = token.strip(_TOKEN_EDGES) or token
    if token == word or word in prefix_variants(token):
        return True
    return len(token) > 1 and token[0] in "בלמהושכ" and token[1:] == word


def _spans(words: list[str], phrase: str) -> list[range]:
    parts = phrase.split()
    return [range(i, i + len(parts)) for i in range(len(words) - len(parts) + 1)
            if all(_word_matches(words[i + k], p) for k, p in enumerate(parts))]


def _vocabulary() -> tuple[str, ...]:
    phrases = [*TRANSACTION_WORDS, *VALUE_WORDS, *ASKING_WORDS, *ADJUSTED_WORDS, *AGGREGATION_WORDS, *UNIT_WORDS,
               *CONNECTOR_PHRASES, *(w for words in DATE_FIELD_WORDS.values() for w in words),
               *(w for words in AREA_WORDS.values() for w in words), *(f"שטח {w}" for words in AREA_WORDS.values()
                                                                       for w in words),
               *(w for _, words in PROPERTY_WORDS for w in words),
               *(w for words in RELATIVE_YEAR_PHRASES.values() for w in words)]
    return tuple(dict.fromkeys(phrases))


_VOCABULARY = _vocabulary()


def _is_noise(core: str) -> bool:
    """Stopwords, one-letter particles and years carry no content of their own."""
    letters = sum(ch.isalpha() for ch in core)
    if not core or (letters < 2 and not any(ch.isdigit() for ch in core)) or _YEAR_TOKEN.fullmatch(core):
        return True
    return core in STOPWORDS or any(v in STOPWORDS for v in prefix_variants(core))


@dataclass
class _Scan:
    unexplained: tuple[str, ...]
    monetary_term: bool  # an explicit monetary term in the (numeric part of the) question
    clause_at: int | None  # character offset of a separable explanation clause, when one was split off


def _scan(text: str, gaz: Gazetteer, unknown: str | None) -> _Scan:
    """Which content tokens the rules account for, and whether an explanation clause holds the rest."""
    found = [(m.group(0), m.start()) for m in _TOKEN.finditer(text)]
    words = [w for w, _ in found]
    places = [*gaz.cities, *(n for _, n in gaz.neighborhoods), *([unknown] if unknown else [])]
    covered = {i for phrase in (*_VOCABULARY, *places) for span in _spans(words, phrase) for i in span}
    money = sorted({i for phrase in MONETARY_WORDS for span in _spans(words, phrase) for i in span})
    covered |= set(money)
    unexplained = [i for i, w in enumerate(words) if i not in covered and not _is_noise(w.strip(_TOKEN_EDGES))]
    start = next((i for i in range(1, len(words)) if words[i] in CLAUSE_STARTERS
                  or any(_spans(words[i:i + 2], p) for p in CLAUSE_PHRASES)), None)
    if unexplained and start is not None and min(unexplained) >= start and money and money[0] < start:
        return _Scan(tuple(words[i].strip(_TOKEN_EDGES) for i in unexplained), True, found[start][1])
    return _Scan(tuple(words[i].strip(_TOKEN_EDGES) for i in unexplained), bool(money), None)


def _relative_year(text: str) -> int | None:
    words = _TOKEN.findall(text)
    return next((offset for offset, phrases in RELATIVE_YEAR_PHRASES.items()
                 if any(_spans(words, p) for p in phrases)), None)


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


def _unknown_place(text: str, tokens: list[str], gaz: Gazetteer, city: str | None, hood: str | None) -> str | None:
    unknown = None
    if city is None:
        unknown = next((c for c in sorted(KNOWN_CITIES, key=len, reverse=True)
                        if c not in gaz.cities and _contains_phrase(tokens, c)), None)
        m_city = _CITY_PHRASE.search(text)
        if unknown is None and m_city:
            unknown = re.sub(r"\s+(ב|ל|מ)?(-?\d{4}|שנת|שכונת).*$", "", m_city.group(1).strip(" ?.!,")).strip() or None
    m = _NEIGHBORHOOD_PHRASE.search(text)
    if m and hood is None and unknown is None:
        unknown = m.group(1).strip(" ?.!,")
        unknown = re.sub(r"\s+(ב-?|בשנת|לשנת|ל-?)?\s*\d{4}.*$", "", unknown).strip()
        unknown = re.sub(r"\s+(ב|ל|מ)(שנת|-).*$", "", unknown).strip()
        unknown = re.sub(r"\s+[בלמ]$", "", unknown).strip() or None  # "ב 2024" after hyphen splitting
    return unknown


def parse_question(question: str, gaz: Gazetteer, previous: QueryConditions | None = None) -> ParseResult:
    text = base_normalize(question)
    tokens = _tokens(text)
    city, hood = _place(tokens, gaz)
    year_from, year_to = _years(text)
    unknown = _unknown_place(text, tokens, gaz, city, hood)
    relative = _relative_year(text) if year_from is None else None  # an explicit year wins
    is_followup = bool(_FOLLOWUP.search(question))
    scan = _scan(text, gaz, unknown)
    # A follow-up form is a money question only when the conversation holds a monetary computation.
    followup_monetary = (is_followup and previous is not None and previous.data_kind is not None
                         and previous.intent in ("calculation", "combined"))
    monetary = scan.monetary_term or followup_monetary
    report = {"unexplained": scan.unexplained, "monetary": monetary, "followup": is_followup,
              "fast_path": monetary and (not scan.unexplained or scan.clause_at is not None),
              "relative_year_offset": relative}
    if scan.clause_at is not None:
        report |= {"numeric_question": text[:scan.clause_at].strip(" ,"),
                   "explanation_clause": text[scan.clause_at:].strip(" ?.!")}

    if previous is not None and is_followup:
        update: dict = {}
        if year_from is not None:
            update |= {"year_from": year_from, "year_to": year_to}
        elif relative is not None and previous.year_from is not None:
            update |= {"year_from": previous.year_from + relative,
                       "year_to": previous.year_to + relative if previous.year_to is not None else None}
        if hood is not None:
            update |= {"neighborhood": hood, "city": city or previous.city}
        elif city is not None:
            update |= {"city": city, "neighborhood": None}
        area = next((k for k, w in AREA_WORDS.items() if _has(text, w)), None)
        if area:
            update["area_type"] = area
        if update:
            conds = previous.model_copy(update=update)
            return ParseResult(conds, missing_conditions(conds), "followup", unknown, frozenset(update), **report)

    # With a separable explanation clause, the conditions come from the monetary part only.
    ctext = text[:scan.clause_at] if scan.clause_at is not None else text
    if scan.clause_at is not None:
        city, hood = _place(_tokens(ctext), gaz)
        year_from, year_to = _years(ctext)
        intent = "combined"
    else:
        intent = "calculation" if is_followup and not _has(text, EXPLAIN_WORDS) else _intent(text)
    conds = QueryConditions(
        intent=intent,
        data_kind=_data_kind(ctext) if scan.monetary_term else None,
        date_field=_date_field(ctext),
        year_from=year_from,
        year_to=year_to,
        city=city,
        neighborhood=hood,
        property_type=next((code for code, words in PROPERTY_WORDS if _has(ctext, words)), None),
        area_type=next((k for k, w in AREA_WORDS.items() if _has(ctext, w)), None),
        vat_basis="excluded" if "לא כולל מע" in ctext else ("included" if "כולל מע" in ctext else None),
        aggregation="weighted" if "משוקלל" in ctext else ("median" if "חציון" in ctext else "both"),
    )
    return ParseResult(conds, missing_conditions(conds), "rules", unknown, **report)
