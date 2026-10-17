"""Generate the synthetic broken-font-map fixtures (U5, KTD6) under ``tests/fixtures/fontmap``.

Every document is synthetic ("מסמך סינתטי לדמו" appears in it, and every filename contains ``synthetic``). The
text is written with fpdf2 + DejaVu Sans, and a font's ToUnicode CMap is then edited with pypdf, the way a broken
producer writes it: the glyphs stay the right Hebrew letters on the page, but the text layer reads another
character. Files:

- ``F1_synthetic_fontmap_clean.pdf``: the source document, maps intact (the expected text after a repair);
- ``F1_synthetic_fontmap_broken.pdf``: the same document; the regular font maps נ to ``ð`` and the bold font maps
  ר to ``ð`` (one suspect character, a different letter per font);
- ``F2_synthetic_fontmap_inconsistent.pdf``: the regular font maps both נ and ה to ``ð``, so the glyphs behind
  ``ð`` disagree and no single repair is valid;
- ``F3_synthetic_fontmap_english.pdf``: an English text that legitimately contains ``ð`` (place names), maps
  intact.

Output is deterministic (fixed dates and PDF ``/ID``). Run inside the backend image (it has the DejaVu fonts):

    docker compose run --rm --no-deps -v "$PWD/backend:/app" worker python scripts/make_fontmap_fixtures.py
"""

from __future__ import annotations

import argparse
import io
import re
from datetime import UTC, datetime
from pathlib import Path

SYNTHETIC_MARKER = "מסמך סינתטי לדמו"
FIXED_TS = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
DEFAULT_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fontmap"
BROKEN = "ð"  # U+00F0

# (bold, size, text) in logical order. Numbers, %, ₪, '/', and Latin words sit inside the Hebrew lines; a repair
# must leave every one of them byte-identical.
HEBREW_DOC = [
    (True, 15, "חוות דעת שמאית — מגדל הנחל, רמת נוף"),
    (False, 10, SYNTHETIC_MARKER),
    (True, 13, "1. תיאור הנכס והסביבה"),
    (False, 11, "הנכס נמצא בשכונה שקטה בצפון העיר, בקרבת גן ציבורי ומוסדות חינוך."),
    (False, 11, "הבניין נבנה בשנת 2015 וכולל 12 קומות מגורים מעל קומת כניסה."),
    (False, 11, "שטח הדירה הנישום 118.5 מ״ר נטו, ובנוסף מרפסת שמש בשטח 14 מ״ר."),
    (False, 11, "הנכס רשום בגוש 6123 חלקה 45 תת חלקה 17, Block A, Tower B."),
    (True, 13, "2. גורמים ושיקולים"),
    (False, 11, "בהערכת השווי נלקחו בחשבון מיקום הנכס, מצבו הפיזי ונתוני השוק העדכניים."),
    (False, 11, "נמצאו עסקאות השוואה בבניינים סמוכים, בעלות מאפיינים דומים לנכס הנדון."),
    (False, 11, "ניתנה התאמה של 5% בגין קומה גבוהה ושל 3.5% בגין חניה נוספת."),
    (False, 11, "נתוני GIS ותכנית המתאר נבדקו באתר הרשות המקומית בתאריך 15/03/2024."),
    (True, 13, "3. הערכת השווי"),
    (False, 11, "שווי הנכס נאמד בסך 2,450,000 ₪ נכון ליום 01/04/2024, כולל מע״מ."),
    (False, 11, "השווי למ״ר בנוי נאמד בכ־20,700 ₪, ושיעור התשואה הנובע הוא 3.2%."),
    (False, 11, "הנתונים נבחנו ונמצאו תואמים לממוצע העסקאות באזור, בסטייה של 1/20 לכל היותר."),
    (True, 13, "4. ריכוז נתונים"),
    (False, 11, "מספר חניות: 2. מחסן: כן. מעלית: כן. כיווני אוויר: צפון, מערב ודרום."),
    (False, 11, "הנכס נמסר לבעלים בשנת 2016 ולא נערכו בו שינויים מהותיים מאז."),
    (True, 13, "5. מקורות המידע"),
    (False, 11, "נסח רישום מקוון, היתרי בנייה, נתוני רשות המסים ונציגי הבעלים."),
    (False, 11, "השומה נערכה בהתאם לתקנים המקצועיים ואינה כוללת בדיקה הנדסית."),
]

ENGLISH_DOC = [
    (True, 15, "Field notes — northern survey"),
    (False, 10, f"Synthetic demo document ({SYNTHETIC_MARKER})"),
    (False, 11, "The survey team visited Garðabær and the harbour of Hafnarfjörður in May."),
    (False, 11, "Reference 12/2024: site value 1,250,000 ISK, yield 6.5%, plot 45/17."),
    (False, 11, "Local names keep their letters: Þórður, Guðrún and the bay of Reyðarfjörður."),
    (False, 11, "Old English spellings such as ðæt and wið appear in the archive copy."),
    (False, 11, "שם הפרויקט: מגדל הנחל"),
]


def render(lines: list[tuple[bool, float, str]], font_dir: Path, rtl: bool) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_creation_date(FIXED_TS)
    pdf.set_title("synthetic font map fixture")
    pdf.set_author("synthetic fixture generator")
    pdf.set_creator("make_fontmap_fixtures.py")
    pdf.set_producer("fpdf2")
    pdf.set_margins(15, 15, 15)
    pdf.add_font("DejaVu", "", str(font_dir / "DejaVuSans.ttf"))
    pdf.add_font("DejaVu", "B", str(font_dir / "DejaVuSans-Bold.ttf"))
    if rtl:
        pdf.set_text_shaping(use_shaping_engine=True, direction="rtl", script="hebr", language="heb")
    pdf.add_page()
    for bold, size, text in lines:
        pdf.set_font("DejaVu", "B" if bold else "", size)
        pdf.cell(0, size * 0.75, text, align="R" if rtl else "L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if bold:
            pdf.ln(1.5)
    return bytes(pdf.output())


def break_tounicode(data: bytes, remap: dict[str, dict[str, str]]) -> bytes:
    """Rewrite the ToUnicode CMap of each font named in ``remap`` (a substring of its BaseFont, "Book" / "Bold"):
    every code mapped to a key letter is mapped to the value character instead. The glyphs are untouched."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, ByteStringObject, NameObject, StreamObject

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    done: set[str] = set()
    for page in writer.pages:
        for font in page["/Resources"]["/Font"].values():
            font = font.get_object()
            base = str(font["/BaseFont"])
            key = next((k for k in remap if k in base), None)
            if key is None or base in done:
                continue
            cmap = font["/ToUnicode"].get_object().get_data().decode("ascii")
            for letter, wrong in remap[key].items():
                src, dst = f"<{ord(letter):04X}>", f"<{ord(wrong):04X}>"
                cmap, n = re.subn(rf"(<[0-9A-F]{{4}}> ){src}", rf"\g<1>{dst}", cmap)
                if n == 0:
                    raise SystemExit(f"{letter!r} is not in the ToUnicode map of {base}")
            stream = StreamObject()
            stream.set_data(cmap.encode("ascii"))
            font[NameObject("/ToUnicode")] = writer._add_object(stream)
            done.add(base)
    missing = [k for k in remap if not any(k in b for b in done)]
    if missing:
        raise SystemExit(f"fonts not found: {missing}")
    fixed_id = ByteStringObject(b"synthetic-fontmap-fixture")
    writer._ID = ArrayObject([fixed_id, fixed_id])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--font-dir", default=DEFAULT_FONT_DIR)
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()
    font_dir, out = Path(args.font_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    clean = render(HEBREW_DOC, font_dir, rtl=True)
    files = {
        "F1_synthetic_fontmap_clean.pdf": clean,
        "F1_synthetic_fontmap_broken.pdf": break_tounicode(clean, {"Book": {"נ": BROKEN}, "Bold": {"ר": BROKEN}}),
        "F2_synthetic_fontmap_inconsistent.pdf": break_tounicode(clean, {"Book": {"נ": BROKEN, "ה": BROKEN}}),
        "F3_synthetic_fontmap_english.pdf": render(ENGLISH_DOC, font_dir, rtl=False),
    }
    for name, data in files.items():
        (out / name).write_bytes(data)
        print(f"wrote {out / name} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
