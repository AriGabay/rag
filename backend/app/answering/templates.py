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
from app.appraisal.query import Stats

TEMPLATE_VERSION = "t1"


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


def abstain_text(c: QueryConditions, awaiting: int) -> str:
    base = "לא נמצאו רשומות מאומתות במאגר המשרד התואמות לתנאים"
    cond = _conditions_sentence(c)
    text = f"{base}{': ' + cond if cond else ''}. לא חושב מספר."
    if awaiting:
        text += f" קיימות {awaiting} רשומות תואמות שטרם אומתו; לאחר אישורן ניתן יהיה לחשב."
    return text
