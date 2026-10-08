"""Default extractor (KTD11): pdfplumber + Hebrew order fix-up + pypdfium2 rendering + Tesseract OCR
for PDF, python-docx for DOCX. Selected by ``app.extraction.pipeline.get_extractor``."""

from __future__ import annotations

from app.config import get_settings
from app.extraction.base import ExtractionError, ExtractionResult

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MSG_UNSUPPORTED = "סוג הקובץ אינו נתמך. ניתן לעבד PDF או DOCX"


class DefaultExtractor:
    def extract(self, data: bytes, mime_type: str, deadline: float, vision=None, readings=None) -> ExtractionResult:
        """``vision``: the office's vision reader, or None. ``readings``: the office's cache of picture readings by
        content (``app.extraction.regions.ReadingCache``); PDF region reading consults and fills it."""
        settings = get_settings()
        if mime_type == PDF_MIME:
            from app.extraction.pdf import extract_pdf

            return extract_pdf(data, deadline, settings, vision, readings)
        if mime_type == DOCX_MIME:
            from app.extraction.docx import extract_docx

            return extract_docx(data, deadline, settings, vision)
        raise ExtractionError(MSG_UNSUPPORTED, True)
