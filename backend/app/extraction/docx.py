"""DOCX extraction via python-docx. A DOCX has no physical pages, so everything goes to one pseudo
page (``page_no`` 1, method ``docx``) and table rows / chunks carry ``page = None``; the UI cites the
section instead of a page (R5)."""

from __future__ import annotations

import io
import zipfile

from app.config import Settings
from app.extraction.base import ExtractionError, ExtractionResult, PageResult
from app.extraction.chunking import chunk_document, is_heading
from app.extraction.pdf import check_deadline
from app.extraction.tables import RawTable, assemble_tables, clean_cell

MSG_CORRUPT = "הקובץ פגום או שאינו DOCX תקין"
MSG_TOO_BIG = "קובץ ה-DOCX חורג ממגבלת הגודל המותרת לאחר פריסה"


def _check_zip(data: bytes, max_mb: int) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            total = sum(i.file_size for i in zf.infolist())
    except (zipfile.BadZipFile, ValueError, OSError):
        raise ExtractionError(MSG_CORRUPT, True) from None
    if "word/document.xml" not in names:
        raise ExtractionError(MSG_CORRUPT, True)
    if total > max_mb * 1024 * 1024:
        raise ExtractionError(MSG_TOO_BIG, True)


def extract_docx(data: bytes, deadline: float, settings: Settings) -> ExtractionResult:
    _check_zip(data, settings.max_docx_uncompressed_mb)
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(io.BytesIO(data))
    except ExtractionError:
        raise
    except Exception:  # noqa: BLE001
        raise ExtractionError(MSG_CORRUPT, True) from None

    lines: list[str] = []
    raws: list[RawTable] = []
    section: str | None = None
    for el in document.element.body.iterchildren():
        check_deadline(deadline)
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(el, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            if is_heading(text) or (style.lower().startswith("heading") and text[:1].isdigit()):
                section = text
            lines.extend(t.strip() for t in text.splitlines() if t.strip())
        elif tag == "tbl":
            table = Table(el, document)
            rows = [[clean_cell(c.text) for c in row.cells] for row in table.rows]
            if not rows or max(len(r) for r in rows) < 2:
                continue
            # Cells are already in logical (document) order; rows also go into the text (searchable).
            raws.append(RawTable(page=None, rows=rows, section=section, first_on_page=False))
            lines.extend(" ".join(c for c in r if c) for r in rows if any(r))

    text = "\n".join(lines)
    page = PageResult(page_no=1, text=text, method="docx", quality=1.0, ok=True)
    tables = assemble_tables(raws)
    chunks = chunk_document([(None, text)], tables)
    return ExtractionResult(page_count=None, pages=[page], tables=tables, chunks=chunks, is_docx=True)
