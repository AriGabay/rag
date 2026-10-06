"""Extractor interface (KTD11). The worker depends only on these types, so Docling or a cloud
OCR adapter can replace the default extractor."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

MSG_DEADLINE = "חריגה מזמן העיבוד המותר למסמך"


class ExtractionError(Exception):
    """Processing failed. ``reason`` is a Hebrew message shown to users. Permanent errors
    (encrypted, corrupt, over limits) are not retried."""

    def __init__(self, reason: str, permanent: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


def check_deadline(deadline: float) -> None:
    """Fail the job permanently once its wall-clock budget is spent."""
    if time.monotonic() > deadline:
        raise ExtractionError(MSG_DEADLINE, True)


@dataclass
class PageResult:
    page_no: int  # physical page in the file, 1-based
    text: str  # logical-order text
    method: Literal["text_layer", "ocr", "docx", "failed"]
    quality: float
    ok: bool


@dataclass
class TableRow:
    page: int | None  # physical page of this row (None for DOCX)
    cells: list[str]


@dataclass
class TableResult:
    index: int
    headers: list[str]
    units: list[str | None]
    rows: list[TableRow]
    page_start: int | None
    page_end: int | None
    ocr: bool = False
    section: str | None = None


@dataclass
class ChunkResult:
    index: int
    kind: Literal["text", "table_row"]
    page_list: list[int] | None
    section: str | None
    text: str
    table_index: int | None = None  # table_row chunks: the source table and row (KTD10)
    row_index: int | None = None


@dataclass
class ExtractionResult:
    page_count: int | None
    pages: list[PageResult]
    tables: list[TableResult]
    chunks: list[ChunkResult]
    is_docx: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def pages_incomplete(self) -> int:
        return sum(1 for p in self.pages if not p.ok)


class Extractor(Protocol):
    def extract(self, data: bytes, mime_type: str, deadline: float) -> ExtractionResult: ...
