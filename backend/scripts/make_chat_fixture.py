"""Writes tests/fixtures/chat/CHAT_synthetic_mixed_report.docx: a small synthetic appraisal report (invented names
and values) whose content tests the conversational engine and the DOCX reader:

- numbered section headings (a custom list style, as office reports use) and a heading-styled title;
- one sentence with two different statements: a value per m² without VAT, and a monthly rent per m²;
- a text box with the property's address;
- a rent survey table drawn as an EMF picture (text records and borders), introduced by a sentence;
- a Word table of building areas;
- a negative statement ("לא צפויה חבות בהיטל השבחה").

Run from backend/: ``uv run python scripts/make_chat_fixture.py``. Deterministic: same bytes on every run.
"""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

from tests.support.emf_builder import emf, grid, text  # noqa: E402

OUT = BACKEND / "tests" / "fixtures" / "chat" / "CHAT_synthetic_mixed_report.docx"


def placeholder_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, "PNG")
    return buf.getvalue()


def rent_table_emf() -> bytes:
    xs = [0, 120, 200, 300, 400]
    rows = [("הגפן 3", "120", "₪ 6,600", "₪ 55"), ("הזית 7", "80", "₪ 4,880", "₪ 61"),
            ("התאנה 1", "95", "₪ 5,510", "₪ 58"), ("הרימון 9", "150", "₪ 7,800", "₪ 52")]
    ys = [0, 30] + [30 + 30 * (i + 1) for i in range(len(rows))]
    recs = grid(xs, ys)
    recs += [text(330, 8, "כתובת"), text(215, 8, 'שטח במ"ר', 7), text(125, 8, 'שכ"ד חודשי', 7),
             text(15, 8, 'שכ"ד למ"ר', 7)]
    for i, (addr, area, monthly, per) in enumerate(rows):
        y = 38 + 30 * i
        recs += [text(320, y, addr), text(235, y, area), text(130, y, monthly), text(30, y, per)]
    return emf(*recs)


def numbered(doc, text_: str, level: int = 0):
    p = doc.add_paragraph(text_, style="List Number" if level == 0 else "List Number 2")
    return p


def build() -> bytes:
    doc = Document()
    doc.add_paragraph("שומת מקרקעין מלאה — מבנה מסחרי", style="Title")
    box_holder = doc.add_paragraph()
    box_holder.add_run(" ")
    doc.add_paragraph("לכבוד: בנק לדוגמה בע\"מ (סינתטי)")
    numbered(doc, "פרטי הנכס")
    doc.add_paragraph("הנכס הנדון הוא מבנה מסחרי בן 3 קומות ברחוב הגפן 12 בעיר לדוגמה, על חלקה 40 בגוש 9999. "
                      "במבנה 18 מקומות חניה.")
    numbered(doc, "סקר שוק")
    numbered(doc, "נתוני היצע לדמי שכירות", 1)
    doc.add_paragraph("להלן נתוני היצע לדמי שכירות לחנויות מהסביבה הקרובה:")
    doc.add_paragraph().add_run().add_picture(io.BytesIO(placeholder_png()))
    numbered(doc, "סיכום סקר שוק", 1)
    doc.add_paragraph("בהתאם לסקר, ראוי לקבוע את השווי למ\"ר בנוי ברוטו למסחר בגבולות של 9,500 ₪, ללא מע\"מ "
                      "ודמ\"ש ראויים למ\"ר בגבולות של 55 ₪ למ\"ר/חודש.")
    numbered(doc, "שטחי הבניין")
    t = doc.add_table(rows=4, cols=3)
    for r, cells in enumerate([("קומה", "שימוש", "שטח במ\"ר"), ("קרקע", "מסחר", "420"), ("א", "מסחר", "380"),
                               ("ב", "משרדים", "360")]):
        for c, value in enumerate(cells):
            t.cell(r, c).text = value
    numbered(doc, "היטל השבחה")
    doc.add_paragraph("לא צפויה חבות בהיטל השבחה בהתאם למצב התכנוני.")
    numbered(doc, "השומה")
    doc.add_paragraph("הננו שמים את שווי הנכס ב-11,300,000 ₪ ללא מע\"מ.")
    buf = io.BytesIO()
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    doc.core_properties.created = doc.core_properties.modified = fixed
    doc.core_properties.last_printed = fixed
    doc.save(buf)
    return _with_emf_and_textbox(buf.getvalue())


TEXTBOX = (
    '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml"><w:pict><v:shape style="width:200pt;height:40pt"><v:textbox>'
    '<w:txbxContent><w:p><w:r><w:t>רחוב הגפן 12, עיר לדוגמה</w:t></w:r></w:p></w:txbxContent>'
    '</v:textbox></v:shape></w:pict></w:r>'
)


def _with_emf_and_textbox(data: bytes) -> bytes:
    """Swap the placeholder PNG for the EMF table (python-docx cannot embed EMF) and put a text box in the
    paragraph after the title."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in sorted(src.infolist(), key=lambda i: i.filename):
            body = src.read(info.filename)
            name = info.filename
            if name.startswith("word/media/") and name.endswith(".png"):
                name = name[:-4] + ".emf"
                body = rent_table_emf()
            elif name == "word/_rels/document.xml.rels":
                body = body.replace(b".png\"", b".emf\"")
            elif name == "[Content_Types].xml":
                body = body.replace(b"</Types>", b'<Default Extension="emf" ContentType="image/x-emf"/></Types>')
            elif name == "word/document.xml":
                body = body.replace(b"<w:r><w:t xml:space=\"preserve\"> </w:t></w:r>", TEXTBOX.encode(), 1)
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, body)
    return out.getvalue()


if __name__ == "__main__":
    _ = qn  # keeps the namespace helper import explicit for readers of this script
    OUT.write_bytes(build())
    print(OUT, OUT.stat().st_size)
