"""Extractor selection. The default is pdfplumber + pypdfium2 + Tesseract (KTD11); an alternative
(Docling, cloud OCR) plugs in here behind the same ``Extractor`` interface."""

from __future__ import annotations

from app.extraction.base import Extractor


def get_extractor() -> Extractor:
    from app.extraction.default import DefaultExtractor

    return DefaultExtractor()
