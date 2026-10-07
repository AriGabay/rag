"""Writes the synthetic documents of the evaluation sets: small appraisal reports with invented places, addresses
and values (no real office data).

**v5** (tests/fixtures/eval_v5/), four reports.

They give the evaluation situations the real reports alone do not: three reports in one invented town (a
question about the town is about a set), a survey table with seven asking rents (an answer about the table must
keep its multiplicity), a sentence with a value per m² and a monthly rent per m² (the two must stay apart), a
value on an equivalent-area basis including VAT, a negative statement, and a land report in another town.

**v6** (tests/fixtures/eval_v6/), three reports in two other invented towns, for what v6 measures:
- area bases kept apart in one report (a gross built area and a net-of-walls area, a value per m² net of walls);
- corrections between a rent and a value, both per m²;
- an annual rent beside a monthly rent per m²;
- a datum a report does not hold (the plot area of the office building, while another report gives its own);
- a survey table of eight asking rents that must be given whole;
- an approximate annual income, and a parking facility with no value per m² at all.

**v8** (tests/fixtures/eval_v8/), four reports in two more invented towns, for the held-out set of round 4 (no test
fixture or regression case uses them):
- the same street address in two towns (a follow-up naming it is ambiguous until the town is given);
- one building with several units, each with its own value (a follow-up about another unit stays in the report);
- a capitalisation table whose rent per m² gets its area basis from another row and its period from the notes;
- two reports per town, for counts over a town.

The file names are the documents' titles (a title is taken from the uploaded file's name, as in the office).
Run from backend/: ``uv run python scripts/make_eval_docs.py [v5|v6|v8]`` (all when no argument is given).
"""

from __future__ import annotations

import sys
from pathlib import Path

from docx import Document

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
TOWN = "קריית אלון"


def report(out: Path, name: str, title: str, sections: list[tuple[str, list]]) -> None:
    doc = Document()
    doc.add_heading(title, level=0)
    for heading, body in sections:
        doc.add_heading(heading, level=1)
        for item in body:
            if isinstance(item, str):
                doc.add_paragraph(item)
            else:  # a table: (caption, header, rows, notes)
                caption, header, rows, notes = item
                doc.add_paragraph(caption)
                t = doc.add_table(rows=1, cols=len(header))
                for c, h in zip(t.rows[0].cells, header, strict=True):
                    c.text = h
                for r in rows:
                    cells = t.add_row().cells
                    for c, v in zip(cells, r, strict=True):
                        c.text = v
                for n in notes:
                    doc.add_paragraph(n)
    out.mkdir(parents=True, exist_ok=True)
    doc.save(out / name)


def v5() -> None:
    out = FIXTURES / "eval_v5"
    report(out, "שומה - הדקל 14 קריית אלון.docx", f"שומת מקרקעין – בניין משרדים, רחוב הדקל 14, {TOWN}", [
        ("1. מטרת חוות הדעת", [f"חוות דעת זו נערכה לצורך הערכת שווי השוק של בניין משרדים ברחוב הדקל 14, {TOWN}, "
                               "לצורכי בטוחה."]),
        ("2. תיאור הנכס", ["הבניין בן 6 קומות משרדים מעל 2 קומות חניה תת-קרקעיות, ובו 48 מקומות חניה.",
                           "השטח הבנוי ברוטו למשרדים הוא 4,200 מ\"ר."]),
        ("3. סקר שוק", [("להלן נתוני היצע לשכירות משרדים בסביבת הנכס:",
                         ["כתובת", "קומה", "שטח במ\"ר", "דמ\"ש למ\"ר לחודש"],
                         [["הדקל 2", "3", "180", "₪ 55"], ["הדקל 9", "5", "240", "₪ 58"],
                          ["האורן 4", "2", "150", "₪ 60"], ["האורן 11", "7", "320", "₪ 63"],
                          ["הברוש 6", "4", "210", "₪ 61"], ["הברוש 15", "1", "130", "₪ 57"],
                          ["שדרות הנחל 20", "6", "400", "₪ 68"]],
                         ["(*) המחירים המבוקשים אינם כוללים מע\"מ ודמי ניהול."])]),
        ("4. השומה", ["בהתאם לסקר ולמיקום הנכס, ראוי לקבוע את השווי למ\"ר בנוי ברוטו למשרדים בגבולות של 11,500 ₪, "
                      "ללא מע\"מ, ודמי שכירות ראויים בגבולות של 62 ₪ למ\"ר לחודש.",
                      "השווי הכולל של הנכס הוא 48,300,000 ₪, ללא מע\"מ."]),
    ])
    report(out, "שומה - שדרות הנחל 8 קריית אלון.docx", f"שומת מקרקעין – בניין מגורים, שדרות הנחל 8, {TOWN}", [
        ("1. מטרת חוות הדעת", ["הערכת שווי דירות בבניין מגורים חדש לצורכי ליווי בנקאי."]),
        ("2. תיאור הנכס", ["בבניין 36 יחידות דיור, מהן 12 דירות נשואות חוות הדעת.",
                           "בבניין 40 מקומות חניה ומחסן לכל דירה."]),
        ("3. השומה", ["השווי למ\"ר אקוו' לדירות בבניין נקבע בכ-24,000 ₪ למ\"ר, כולל מע\"מ.",
                      "שווי 12 הדירות נשואות חוות הדעת הוא 18,600,000 ₪, כולל מע\"מ."]),
        ("4. מצב משפטי", ["לא רשומות הערות אזהרה על הנכס, ולא נמצאו שעבודים."]),
    ])
    report(out, "שומה - התמר 3 קריית אלון.docx", f"שומת מקרקעין – מבנה מסחרי, רחוב התמר 3, {TOWN}", [
        ("1. תיאור הנכס", ["מבנה מסחרי בן קומה אחת, בשטח בנוי של 860 מ\"ר, ובו 9 חנויות."]),
        ("2. השומה", ["השווי למ\"ר בנוי למסחר נקבע ל-9,800 ₪, ללא מע\"מ.",
                      "שיעור התפוסה במבנה הוא 89%."]),
        ("3. היטל השבחה", ["לא צפויה חבות בהיטל השבחה בגין הנכס."]),
    ])
    report(out, "חוות דעת - מתחם הגורן נווה צור.docx", "חוות דעת – מתחם הגורן, נווה צור", [
        ("1. תיאור המקרקעין", ["מתחם קרקע בשטח 14 דונם, בייעוד תעשייה ומלאכה."]),
        ("2. השומה", ["שווי דונם קרקע במתחם נקבע ל-1,200,000 ₪.",
                      "ערך הכינון של המבנים הקיימים במתחם הוא כ-7.5 מיליון ₪."]),
    ])


V6_TOWN = "גבעת הרימון"


def v6() -> None:
    out = FIXTURES / "eval_v6"
    report(out, "שומה - הצפצפה 11 גבעת הרימון.docx", f"שומת מקרקעין – בניין משרדים, רחוב הצפצפה 11, {V6_TOWN}", [
        ("1. מטרת חוות הדעת", [f"הערכת שווי השוק של בניין משרדים ברחוב הצפצפה 11, {V6_TOWN}, לצורכי דיווח כספי."]),
        ("2. תיאור הנכס", ["הבניין בן 8 קומות משרדים מעל קומת לובי, ובו 3 מעליות.",
                           "השטח הבנוי ברוטו של הבניין הוא 6,350 מ\"ר.",
                           "השטח המושכר נמדד פנים הקירות (פלדלת), והוא 5,120 מ\"ר."]),
        ("3. סקר שוק", [("להלן נתוני היצע לשכירות משרדים בסביבה:",
                         ["כתובת", "קומה", "שטח פלדלת במ\"ר", "דמ\"ש מבוקשים למ\"ר לחודש"],
                         [["הצפצפה 2", "4", "260", "₪ 66"], ["הצפצפה 17", "6", "310", "₪ 72"],
                          ["הערמון 5", "2", "180", "₪ 64"], ["הערמון 9", "8", "420", "₪ 79"],
                          ["דרך השקד 30", "3", "200", "₪ 69"], ["דרך השקד 44", "5", "350", "₪ 74"],
                          ["הלבנה 12", "7", "280", "₪ 76"], ["הלבנה 18", "1", "150", "₪ 67"]],
                         ["(*) המחירים המבוקשים אינם כוללים מע\"מ."])]),
        ("4. השומה", ["בהתאם לסקר, ראוי לקבוע את השווי למ\"ר פלדלת למשרדים ל-13,400 ₪, ללא מע\"מ, ואת דמי "
                      "השכירות הראויים ל-71 ₪ למ\"ר פלדלת לחודש.",
                      "השווי הכולל של הבניין, על בסיס השטח המושכר, הוא 68,608,000 ₪, ללא מע\"מ."]),
    ])
    report(out, "שומה - שדרות האלון 4 גבעת הרימון.docx", f"שומת מקרקעין – בניין מגורים ומסחר, שדרות האלון 4, {V6_TOWN}", [
        ("1. תיאור המקרקעין", ["שטח המגרש הרשום הוא 2,140 מ\"ר.",
                               "על המגרש בניין בן 14 קומות, ובו 52 דירות ושתי חנויות בקומת הקרקע."]),
        ("2. השומה", ["השווי למ\"ר אקוו' לדירות בבניין נקבע ל-27,500 ₪, כולל מע\"מ.",
                      "שווי הבניין כולו נקבע ל-96,250,000 ₪, כולל מע\"מ.",
                      "החנות המזרחית מושכרת בדמי שכירות של 186,000 ₪ לשנה; לפי שטחה, אלה כ-98 ₪ למ\"ר לחודש."]),
    ])
    report(out, "חוות דעת - חניון הגפנים מעלה הדס.docx", "חוות דעת – חניון הגפנים, מעלה הדס", [
        ("1. תיאור הנכס", ["חניון ציבורי בן שלוש קומות תת-קרקעיות, ובו 380 מקומות חניה."]),
        ("2. הערכת התקבולים", ["התקבולים השנתיים מהחניון נאמדו בכ-1,450,000 ₪, לפני הוצאות תפעול."]),
        ("3. השומה", ["שווי החניון בגישת היוון ההכנסות נקבע ל-17,800,000 ₪."]),
    ])


V8_A, V8_B = "נווה אשחר", "כרם הזית"


def v8() -> None:
    out = FIXTURES / "eval_v8"
    report(out, "שומה - הכלנית 6 נווה אשחר.docx", f"שומת מקרקעין – בניין מגורים, רחוב הכלנית 6, {V8_A}", [
        ("1. תיאור המקרקעין", ["שטח המגרש הרשום הוא 1,860 מ\"ר.",
                               "על המגרש בניין מגורים בן 9 קומות, ובו 27 דירות."]),
        ("2. השומה", ["השווי למ\"ר אקוו' לדירות בבניין נקבע ל-19,800 ₪, כולל מע\"מ.",
                      "שווי הבניין כולו נקבע ל-41,580,000 ₪, כולל מע\"מ."]),
    ])
    report(out, "שומה - הכלנית 6 כרם הזית.docx", f"שומת מקרקעין – מבנה מסחרי, רחוב הכלנית 6, {V8_B}", [
        ("1. תיאור הנכס", ["מבנה מסחרי בן שתי קומות, בשטח בנוי של 1,150 מ\"ר, ובו 7 חנויות."]),
        ("2. השומה", ["השווי למ\"ר בנוי למסחר נקבע ל-8,700 ₪, ללא מע\"מ.",
                      "השווי הכולל של המבנה הוא 10,005,000 ₪, ללא מע\"מ."]),
    ])
    report(out, "שומה - הצבעוני 21 נווה אשחר.docx", f"שומת מקרקעין – דירות בבניין, רחוב הצבעוני 21, {V8_A}", [
        ("1. מטרת חוות הדעת", ["הערכת שווי ארבע דירות בבניין לצורך מכירה."]),
        ("2. הדירות", [("פירוט הדירות נשואות חוות הדעת:",
                        ["דירה", "קומה", "שטח במ\"ר", "שווי"],
                        [["D1", "2", "96", "₪ 2,350,000"], ["D2", "3", "104", "₪ 2,520,000"],
                         ["D3", "5", "88", "₪ 2,190,000"], ["D4", "7", "121", "₪ 3,080,000"]],
                        ["(*) השווי כולל מע\"מ, ומתייחס לדירה עם חניה ומחסן."])]),
    ])
    report(out, "שומה - דרך הנחשול 40 כרם הזית.docx", f"שומת מקרקעין – מבנה תעשייה, דרך הנחשול 40, {V8_B}", [
        ("1. תיאור הנכס", ["מבנה תעשייה בן קומה אחת וגלריה, על מגרש בשטח 4,300 מ\"ר."]),
        ("2. תחשיב השווי", [("תחשיב שווי הנכס בגישת היוון ההכנסות:",
                             ["רכיב", "ערך"],
                             [["סה\"כ מ\"ר אקווי'", "2,480"], ["דמ\"ש למ\"ר", "₪ 52"],
                              ["הכנסה שנתית", "₪ 1,547,520"], ["שיעור היוון", "7.5%"],
                              ["שווי מעוגל", "₪ 20,630,000"]],
                             ["(*) דמי השכירות בטבלה הם לחודש, ללא מע\"מ."])]),
    ])


if __name__ == "__main__":
    which = sys.argv[1:] or ["v5", "v6", "v8"]
    for name in which:
        {"v5": v5, "v6": v6, "v8": v8}[name]()
