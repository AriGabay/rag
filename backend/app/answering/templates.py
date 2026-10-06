"""Templated Hebrew answers for structured questions (R21, R22): no model call."""

from __future__ import annotations

from decimal import Decimal

from app.answering.conditions import (
    AREA_TYPE_LABELS,
    DATA_KIND_LABELS,
    DATE_FIELD_LABELS,
    PROPERTY_TYPE_LABELS,
    VAT_LABELS,
    QueryConditions,
)
from app.appraisal.query import RecordResult, Stats

# The answer logic and wording the server applies after the model: bump it whenever a server rule changes what an
# answer says (completeness, validation, locate rules), so answers cached by earlier logic are not served after a
# deploy. t2: per-document audit and completeness, meaning-bound fact validation, structured absence claims. t3: final-review rules (ordinals, qualified zeros, rival nouns, place spelling, locate existence). t4: only the user's own words count as absent in locate. t5: the question decides locate polarity.
TEMPLATE_VERSION = "t5"


def money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    q = value.quantize(Decimal("0.01"))
    return f"{q:,.0f}" if q == q.to_integral_value() else f"{q:,.2f}"


def _conditions_sentence(c: QueryConditions) -> str:
    bits = []
    if c.city:
        bits.append(c.city)
    if c.neighborhood:
        bits.append(f"שכונת {c.neighborhood}")
    if c.year_from is not None and c.date_field:
        years = str(c.year_from) if c.year_to in (None, c.year_from) else f"{c.year_from}–{c.year_to}"
        bits.append(f"{DATE_FIELD_LABELS[c.date_field]} בשנת {years}" if c.year_to in (None, c.year_from)
                    else f"{DATE_FIELD_LABELS[c.date_field]} בשנים {years}")
    if c.property_type:
        bits.append(PROPERTY_TYPE_LABELS.get(c.property_type, c.property_type))
    if c.area_type:
        bits.append(f"שטח {AREA_TYPE_LABELS[c.area_type]}")
    if c.vat_basis:
        bits.append(VAT_LABELS[c.vat_basis])
    return ", ".join(bits)


def numeric_text(c: QueryConditions, s: Stats, uncertain: int, conflict_count: int) -> tuple[str, list[str]]:
    kind = DATA_KIND_LABELS[c.data_kind]
    noun = "עסקאות ייחודיות" if c.data_kind == "transaction_price" else "רשומות ייחודיות"
    lines = [
        f"מתוך הרשומות המאומתות שנקלטו במאגר המשרד ({kind}): נמצאו {s.count} {noun} — {_conditions_sentence(c)}.",
        f"ממוצע מחירי המ״ר של הרשומות: {money(s.mean)} ₪ למ״ר.",
        f"מחיר משוקלל (סך המחירים חלקי סך השטחים): {money(s.weighted)} ₪ למ״ר.",
    ]
    if s.count >= 3:
        lines.append(f"חציון: {money(s.median)} ₪ למ״ר; טווח: {money(s.minimum)}–{money(s.maximum)} ₪ למ״ר.")
    limitations = [
        "הנתון מבוסס רק על רשומות מאגר המשרד שאושרו, ואינו אומדן של כלל השוק.",
        "לא בוצעו התאמות לשווי, למדד, לקומה או למאפייני נכס.",
    ]
    if uncertain:
        limitations.append(f"{uncertain} זוגות רשומות חשודים ככפילות שלא אושרה; הם נספרו כרשומות נפרדות.")
    if conflict_count:
        limitations.append(f"ב-{conflict_count} רשומות המחיר למ״ר שהופיע במסמך שונה מהמחושב; החישוב משתמש במחיר ובשטח.")
    return "\n".join(lines), limitations


OPERATION_LABELS = {
    "count": "מספר העסקאות", "sum": "סכום", "mean": "ממוצע", "weighted_mean": "ממוצע משוקלל",
    "median": "חציון", "min": "ערך מינימלי", "max": "ערך מקסימלי", "range": "טווח", "values": "ערכים",
}
UNIT_LABELS = {"sqm": "מ״ר", "ILS": "₪", "ILS/sqm": "₪ למ״ר", "room": "חדרים", "m": "מ׳", "m3": "מ״ק",
               "percent": "%", "month": "חודשים"}
VALUES_SHOWN = 5


def number(value: Decimal | int | None) -> str:
    """Thousands separators, at most two decimals, no trailing zeros (3.5 rather than 3.50)."""
    if value is None:
        return "—"
    q = Decimal(value).quantize(Decimal("0.01"))
    if q == q.to_integral_value():
        return f"{q:,.0f}"
    return f"{q:,.2f}".rstrip("0")


def _with_unit(value: str, unit: str | None) -> str:
    label = UNIT_LABELS.get(unit or "", "")
    return f"{value} {label}" if label else value


def structured_text(c: QueryConditions, label: str, operation: str, r: RecordResult, canonical_unit: str | None,
                    uncertain: int = 0) -> tuple[str, list[str]]:
    """A structured attribute (area, rooms, price, ...) computed over unique verified records."""
    kind = DATA_KIND_LABELS[c.data_kind] if c.data_kind else "כל סוגי הנתונים"
    noun = "עסקאות ייחודיות" if c.data_kind == "transaction_price" else "רשומות ייחודיות"
    cond = _conditions_sentence(c)
    lines = [f"מתוך הרשומות המאומתות שנקלטו במאגר המשרד ({kind}): נמצאו {r.count} {noun} עם ערך {label}"
             f"{' — ' + cond if cond else ''}."]
    if operation == "count":
        lines.append(f"{OPERATION_LABELS['count']} שבהן מופיע {label}: {r.count}.")
    elif operation == "range":
        lines.append(f"טווח {label}: {_with_unit(f'{number(r.minimum)}–{number(r.maximum)}', canonical_unit)}"
                     f" (הפרש {_with_unit(number(r.value), canonical_unit)}).")
    elif operation != "values":
        lines.append(f"{OPERATION_LABELS[operation]} {label}: {_with_unit(number(r.value), canonical_unit)}.")
    if r.values and (r.count <= VALUES_SHOWN or operation == "values"):
        shown = ", ".join(number(v) for v in r.values)
        lines.append(f"הערכים: {_with_unit(shown, canonical_unit)}.")
    limitations = ["הנתון מבוסס רק על רשומות מאגר המשרד שאושרו, ואינו אומדן של כלל השוק."]
    if uncertain:
        limitations.append(f"{uncertain} זוגות רשומות חשודים ככפילות שלא אושרה; הם נספרו כרשומות נפרדות.")
    return "\n".join(lines), limitations


def abstain_text(c: QueryConditions, awaiting: int) -> str:
    base = "לא נמצאו רשומות מאומתות במאגר המשרד התואמות לתנאים"
    cond = _conditions_sentence(c)
    text = f"{base}{': ' + cond if cond else ''}. לא חושב מספר."
    if awaiting:
        text += f" קיימות {awaiting} רשומות תואמות שטרם אומתו; לאחר אישורן ניתן יהיה לחשב."
    return text
