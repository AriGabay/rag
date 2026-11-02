"""Writes the synthetic appraisal documents of the clarification set (round 26, U1): invented places, streets,
block/parcel numbers and values, no real office data. Their facts are written to ``truth.yaml`` for grading.

**clarify_v1** (tests/fixtures/clarify_v1/), seven files for the ambiguity kinds of the clarification plan:
- ``bound``: one DOCX that binds three appraisals. The first has a title line; the second and third have none, and each
  states its own address, block/parcel and valuation date in its opening paragraph (untitled units are told apart by
  address and date, never shown as "ללא כותרת"). Each states a lot area, a built area and a value;
- ``two_dates``: one property with two separate appraisals of different valuation dates (2022 and 2025), different
  values and rents (a question about the value needs the date);
- ``comparables``: a table of six comparables whose simple mean of the price per m² differs from the area-weighted
  mean by more than 5% (neither average is written in the document: "average" needs an averaging method);
- ``gross_net``: gross and net built area of the same unit and a value per m² on one basis (the area, and a value per
  m², depend on the basis);
- ``conflict``: the same datum with two conflicting per-m² values in two tables (this must NOT trigger a
  clarification; the answer shows both places);
- ``same_address_a`` / ``same_address_b``: two appraisals of different properties with the same street and house
  number in different neighbourhoods (the address alone is ambiguous until the neighbourhood is given).

The file names are the documents' titles, as in the office. Run from backend/:
``uv run python scripts/generate_clarify_fixtures.py``. It rewrites the files and ``truth.yaml`` in place.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from docx import Document

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "clarify_v1"

VALUATION_DATE = "המועד הקובע"


def n(value: int) -> str:
    """A number as the reports write it: thousands separators."""
    return f"{value:,}"


def build(out: Path, name: str, parts: list) -> None:
    """Writes one DOCX. A part is ``("title", text)``, ``("h", text)``, ``("p", text)``, ``("page",)`` (a page
    break) or ``("table", caption, header, rows, notes)``."""
    doc = Document()
    for part in parts:
        kind = part[0]
        if kind == "title":
            doc.add_heading(part[1], level=0)
        elif kind == "h":
            doc.add_heading(part[1], level=1)
        elif kind == "p":
            doc.add_paragraph(part[1])
        elif kind == "page":
            doc.add_page_break()
        elif kind == "table":
            _, caption, header, rows, notes = part
            doc.add_paragraph(caption)
            t = doc.add_table(rows=1, cols=len(header))
            for c, h in zip(t.rows[0].cells, header, strict=True):
                c.text = h
            for r in rows:
                for c, v in zip(t.add_row().cells, r, strict=True):
                    c.text = v
            for note in notes:
                doc.add_paragraph(note)
    out.mkdir(parents=True, exist_ok=True)
    doc.save(out / name)


def fact(key: str, value, text: str) -> dict:
    """One fact the file states: its key, its value and the exact string that holds it in the document."""
    return {"key": key, "value": value, "text": text}


def bound() -> dict:
    name = "שומות כרוכות - שלוש שומות בקובץ אחד.docx"
    # (title or None, street/town line, block, parcel, date, lot, built, value, use)
    units = [
        ("שומת מקרקעין – בית מגורים, רחוב הסהר 7, קריית אלון", "רחוב הסהר 7, קריית אלון",
         7101, 24, "15.03.2022", 640, 210, 4_850_000, "בית מגורים"),
        (None, "רחוב הפעמון 3, גבעת הרימון", 7204, 58, "02.11.2023", 820, 305, 6_120_000, "בית מגורים"),
        (None, "שדרות האיריס 18, נווה צור", 7330, 91, "20.06.2024", 1_150, 480, 9_740_000, "בניין משרדים"),
    ]
    parts: list = []
    facts: list[dict] = []
    for i, (title, address, block, parcel, date, lot, built, value, use) in enumerate(units):
        if i:
            parts.append(("page",))
        opening = (f"הנכס: {address}, גוש {block} חלקה {parcel}. {VALUATION_DATE} לשומה: {date}. "
                   f"{use} בבעלות פרטית.")
        if title:
            parts.append(("title", title))
        parts.append(("p", opening))
        parts += [
            ("h", "1. תיאור הנכס"),
            ("p", f"{use} על מגרש בשטח {n(lot)} מ\"ר, ושטח בנוי של {n(built)} מ\"ר."),
            ("h", "2. השומה"),
            ("p", f"שווי הנכס במועד הקובע נקבע ל-{n(value)} ₪, ללא מע\"מ."),
        ]
        facts += [
            fact(f"unit{i + 1}.address", address, address),
            fact(f"unit{i + 1}.block_parcel", f"{block}/{parcel}", f"גוש {block} חלקה {parcel}"),
            fact(f"unit{i + 1}.valuation_date", date, date),
            fact(f"unit{i + 1}.lot_sqm", lot, f"{n(lot)} מ\"ר"),
            fact(f"unit{i + 1}.built_sqm", built, f"{n(built)} מ\"ר"),
            fact(f"unit{i + 1}.value_ils", value, f"{n(value)} ₪"),
        ]
        if title:
            facts.append(fact(f"unit{i + 1}.title", title, title))
    build(OUT, name, parts)
    return {
        "file": name, "kind": "bound",
        "note": "three appraisals in one file; unit 1 has a title line, units 2 and 3 have none",
        "units": 3, "titled_units": 1, "facts": facts,
        "absent": ["ללא כותרת"],
    }


def two_dates() -> list[dict]:
    street = "הרימון 5, גבעת התאנה"
    lot, built = 2_100, 1_300
    versions = [
        ("2022", "30.06.2022", 12_400_000, 48, 7_101, 18),
        ("2025", "31.03.2025", 14_950_000, 56, 7_101, 18),
    ]
    files = []
    for year, date, value, rent, block, parcel in versions:
        name = f"שומה - הרימון 5 גבעת התאנה {year}.docx"
        title = f"שומת מקרקעין – מבנה מסחרי, רחוב {street}"
        build(OUT, name, [
            ("title", title),
            ("h", "1. תיאור הנכס"),
            ("p", f"מבנה מסחרי בן שתי קומות, גוש {block} חלקה {parcel}, על מגרש בשטח {n(lot)} מ\"ר "
                  f"ושטח בנוי של {n(built)} מ\"ר."),
            ("p", f"{VALUATION_DATE} לשומה: {date}."),
            ("h", "2. השומה"),
            ("p", f"שווי הנכס במועד הקובע נקבע ל-{n(value)} ₪, ללא מע\"מ."),
            ("p", f"דמי שכירות ראויים: {rent} ₪ למ\"ר לחודש, ללא מע\"מ."),
        ])
        files.append({
            "file": name, "kind": "two_dates",
            "note": "same property as the other two_dates file; different valuation date, value and rent",
            "property": street,
            "facts": [
                fact("address", street, street),
                fact("valuation_date", date, date),
                fact("lot_sqm", lot, f"{n(lot)} מ\"ר"),
                fact("built_sqm", built, f"{n(built)} מ\"ר"),
                fact("value_ils", value, f"{n(value)} ₪"),
                fact("rent_per_sqm_month_ils", rent, f"{rent} ₪ למ\"ר לחודש"),
            ],
        })
    return files


def comparables() -> dict:
    name = "שומה - אולם תצוגה השקד 8 נווה צור.docx"
    # (address, area m2, price per m2); price = area * price per m2
    rows = [("הערבה 3", 60, 14_000), ("הערבה 11", 85, 13_500), ("התאנה 4", 110, 12_800),
            ("התאנה 20", 420, 9_200), ("הדס 6", 95, 13_900), ("הדס 15", 600, 8_800)]
    prices = [a * p for _, a, p in rows]
    simple = sum(p for _, _, p in rows) / len(rows)
    weighted = sum(prices) / sum(a for _, a, _ in rows)
    assert abs(simple - weighted) / weighted > 0.05  # the two means differ materially
    subject_ppsm = 10_500
    build(OUT, name, [
        ("title", "שומת מקרקעין – אולם תצוגה, רחוב השקד 8, נווה צור"),
        ("h", "1. תיאור הנכס"),
        ("p", "אולם תצוגה בן קומה אחת, בשטח בנוי של 540 מ\"ר, גוש 7455 חלקה 12."),
        ("p", f"{VALUATION_DATE} לשומה: 10.09.2024."),
        ("h", "2. עסקאות השוואה"),
        ("table", "להלן עסקאות מכר של נכסים דומים בסביבת הנכס:",
         ["כתובת", "שטח במ\"ר", "מחיר ב-₪", "מחיר למ\"ר ב-₪"],
         [[a, n(ar), n(ar * p), n(p)] for a, ar, p in rows],
         ["(*) המחירים אינם כוללים מע\"מ."]),
        ("h", "3. השומה"),
        ("p", f"בהתחשב בעסקאות ההשוואה, השווי למ\"ר בנוי לנכס נקבע ל-{n(subject_ppsm)} ₪, ללא מע\"מ."),
    ])
    facts = [fact("subject.address", "השקד 8, נווה צור", "השקד 8, נווה צור"),
             fact("subject.valuation_date", "10.09.2024", "10.09.2024"),
             fact("subject.value_per_sqm_ils", subject_ppsm, f"{n(subject_ppsm)} ₪")]
    for i, (a, ar, p) in enumerate(rows, 1):
        facts += [fact(f"comparable{i}.address", a, a), fact(f"comparable{i}.area_sqm", ar, n(ar)),
                  fact(f"comparable{i}.price_ils", ar * p, n(ar * p)),
                  fact(f"comparable{i}.price_per_sqm_ils", p, n(p))]
    return {
        "file": name, "kind": "comparables",
        "note": "an average price per m2 depends on the method; neither mean is written in the document",
        "comparables": len(rows),
        "derived_not_in_document": {
            "simple_mean_price_per_sqm": round(simple, 2),
            "area_weighted_mean_price_per_sqm": round(weighted, 2),
            "relative_gap": round((simple - weighted) / weighted, 4),
            "total_price_ils": sum(prices),
            "total_area_sqm": sum(a for _, a, _ in rows),
        },
        "facts": facts,
        "absent": [n(round(simple)), n(round(weighted)), f"{simple:,.1f}", f"{weighted:,.1f}", "ממוצע"],
    }


def gross_net() -> dict:
    name = "שומה - בניין משרדים התכלת 22 מעלה הדס.docx"
    gross, net, ppsm_net = 3_600, 3_150, 12_600
    total = net * ppsm_net  # 39,690,000
    build(OUT, name, [
        ("title", "שומת מקרקעין – בניין משרדים, רחוב התכלת 22, מעלה הדס"),
        ("h", "1. תיאור הנכס"),
        ("p", f"בניין משרדים בן ארבע קומות, גוש 7512 חלקה 33. {VALUATION_DATE} לשומה: 28.02.2025."),
        ("p", f"השטח הבנוי ברוטו הוא {n(gross)} מ\"ר. השטח נטו, בניכוי קירות ושטחים משותפים, הוא {n(net)} מ\"ר."),
        ("h", "2. השומה"),
        ("p", f"השווי למ\"ר נטו נקבע ל-{n(ppsm_net)} ₪, ללא מע\"מ."),
        ("p", f"השווי הכולל של הנכס הוא {n(total)} ₪, ללא מע\"מ."),
    ])
    return {
        "file": name, "kind": "gross_net",
        "note": "gross and net area of the same unit; the value per m2 is stated on the net basis only",
        "facts": [
            fact("address", "התכלת 22, מעלה הדס", "התכלת 22, מעלה הדס"),
            fact("valuation_date", "28.02.2025", "28.02.2025"),
            fact("gross_sqm", gross, f"{n(gross)} מ\"ר"),
            fact("net_sqm", net, f"{n(net)} מ\"ר"),
            fact("value_per_sqm_net_ils", ppsm_net, f"{n(ppsm_net)} ₪"),
            fact("total_value_ils", total, f"{n(total)} ₪"),
        ],
        "derived_not_in_document": {"value_per_sqm_gross_ils": round(total / gross, 2)},
        "absent": [n(round(total / gross))],
    }


def conflict() -> dict:
    name = "שומה - חנות הזמיר 4 קריית אלון.docx"
    area, rent_a, rent_b = 140, 210, 235
    caption = "דמי שכירות למ\"ר לחודש – חנות בקומת הקרקע"
    build(OUT, name, [
        ("title", "שומת מקרקעין – חנות, רחוב הזמיר 4, קריית אלון"),
        ("h", "1. תיאור הנכס"),
        ("p", f"חנות בקומת הקרקע בשטח של {area} מ\"ר, גוש 7620 חלקה 5. {VALUATION_DATE} לשומה: 12.12.2024."),
        ("h", "2. תחשיב ההכנסה"),
        ("table", "להלן נתוני התחשיב:", ["רכיב", "ערך"],
         [[caption, f"₪ {rent_a}"], ["שטח החנות במ\"ר", str(area)], ["שיעור היוון", "7.0%"]],
         ["(*) דמי השכירות בטבלה הם לחודש, ללא מע\"מ."]),
        ("h", "3. סיכום"),
        ("table", "להלן סיכום נתוני הנכס:", ["רכיב", "ערך"],
         [[caption, f"₪ {rent_b}"], ["שווי מעוגל", "₪ 4,300,000"]],
         ["(*) הסכומים ללא מע\"מ."]),
    ])
    return {
        "file": name, "kind": "conflict",
        "note": "the same datum has two conflicting per-m2 values in two tables; must NOT trigger a clarification",
        "conflict": {"datum": caption, "values": [rent_a, rent_b], "places": ["2. תחשיב ההכנסה", "3. סיכום"]},
        "facts": [
            fact("address", "הזמיר 4, קריית אלון", "הזמיר 4, קריית אלון"),
            fact("area_sqm", area, f"{area} מ\"ר"),
            fact("rent_per_sqm_month.table_calculation_ils", rent_a, f"₪ {rent_a}"),
            fact("rent_per_sqm_month.table_summary_ils", rent_b, f"₪ {rent_b}"),
            fact("rounded_value_ils", 4_300_000, "₪ 4,300,000"),
        ],
    }


def same_address() -> list[dict]:
    street = "הנרקיס 9"
    specs = [
        ("a", "שכונת הגבעה", 7801, 14, "18.05.2023", 530, 180, 5_300_000),
        ("b", "שכונת הנחל", 7842, 67, "09.01.2025", 910, 260, 8_100_000),
    ]
    files = []
    for tag, hood, block, parcel, date, lot, built, value in specs:
        name = f"שומה - הנרקיס 9 {hood} קריית אלון.docx"
        build(OUT, name, [
            ("title", f"שומת מקרקעין – בית מגורים, רחוב {street}, {hood}, קריית אלון"),
            ("h", "1. תיאור הנכס"),
            ("p", f"בית מגורים צמוד קרקע, גוש {block} חלקה {parcel}, על מגרש בשטח {n(lot)} מ\"ר "
                  f"ושטח בנוי של {n(built)} מ\"ר."),
            ("p", f"{VALUATION_DATE} לשומה: {date}."),
            ("h", "2. השומה"),
            ("p", f"שווי הנכס במועד הקובע נקבע ל-{n(value)} ₪, ללא מע\"מ."),
        ])
        files.append({
            "file": name, "kind": f"same_address_{tag}",
            "note": "same street and house number as the other same_address file, in another neighbourhood",
            "street_and_number": street, "neighbourhood": hood,
            "facts": [
                fact("address", street, street),
                fact("neighbourhood", hood, hood),
                fact("block_parcel", f"{block}/{parcel}", f"גוש {block} חלקה {parcel}"),
                fact("valuation_date", date, date),
                fact("lot_sqm", lot, f"{n(lot)} מ\"ר"),
                fact("built_sqm", built, f"{n(built)} מ\"ר"),
                fact("value_ils", value, f"{n(value)} ₪"),
            ],
        })
    return files


def main() -> None:
    entries = [bound(), *two_dates(), comparables(), gross_net(), conflict(), *same_address()]
    header = ("# Synthetic clarification fixtures (round 26, U1): every name, street, block/parcel, date and number is"
              " invented.\n# Generated by scripts/generate_clarify_fixtures.py. Each fact's `text` is the exact string"
              " the file holds;\n# `absent` strings must NOT appear; `derived_not_in_document` is for grading only.\n")
    body = yaml.safe_dump({"set": "clarify_v1", "files": entries}, allow_unicode=True, sort_keys=False, width=110)
    (OUT / "truth.yaml").write_text(header + body, encoding="utf-8")
    print(f"wrote {len(entries)} files and truth.yaml to {OUT}")


if __name__ == "__main__":
    main()
