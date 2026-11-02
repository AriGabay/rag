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
        content (``app.extraction.regions.ReadingCache``); PDF region and DOCX picture reading consult and fill it."""
        settings = get_settings()
        if mime_type == PDF_MIME:
            from app.extraction.pdf import extract_pdf

            return extract_pdf(data, deadline, settings, vision, readings)
        if mime_type == DOCX_MIME:
            from app.extraction.docx import extract_docx

            return extract_docx(data, deadline, settings, vision, readings)
        raise ExtractionError(MSG_UNSUPPORTED, True)

    def extract_staged(self, data: bytes, mime_type: str, deadline: float, vision, readings, stage,
                       progress=None) -> ExtractionResult:
        """A PDF read in page windows through ``stage`` (KTD2): what an earlier attempt of the same reading finished is
        kept, ``progress`` hears (pages read, pages in the file, stage), and the time running out stops the call at a
        window or batch boundary (``ExtractionPaused``) instead of failing it. Only PDFs have windows."""
        if mime_type != PDF_MIME:
            raise ExtractionError(MSG_UNSUPPORTED, True)
        from app.extraction.pdf_windows import read_pdf

        return read_pdf(data, deadline, get_settings(), vision, readings, stage, progress, soft=True)
