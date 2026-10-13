"""A synthetic professional document (a standard or another professional file), built from blocks, chunks and stored
rows without a file, for the tests of the professional-source units (round 10 U2 onwards, KTD16). Every text is
invented and says so: the title and the first block of each document carry ``MARK``, and nothing here is a real
standard or taken from a customer's file.

``professional_file(office, group, blocks, ...)``: ``blocks`` are ``(kind, label, section_path, text, page)`` tuples in
reading order (``kind`` is ``heading``, ``paragraph`` or ``table``; ``label`` the numbering label of a section heading
or None; ``section_path`` the heading path as a list; ``page`` the page number). One chunk is made per block. The
document is stored through ``pipeline.persist_extraction``, as a reading is, with a ``reading_id`` of its own, its
version ready and current, and the chunks embedded; it has no units, measurements, records or transactions, as a
professional document ingested by the worker has none (round 10 U3)."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from app.db import tenant_tx
from app.extraction.base import Block, ChunkResult, ExtractionResult, PageResult
from app.platform import pipeline
from tests.factories import make_document

MARK = "תקן בדיקה סינתטי — אינו תקן אמיתי"
READING = "reading-professional-1"

Row = tuple[str, str | None, list[str], str, int]  # kind, label, section_path, text, page


def marked_title(title: str) -> str:
    """``title`` with the synthetic mark; a title that already carries it is returned as it is."""
    return title if MARK in title else f"{MARK} — {title}"


def professional_file(office, group, blocks: list[Row], title: str = "פרק א", source_kind: str = "standard",
                      sha: str | None = None, reading: str = READING) -> tuple[str, str]:
    """Create a document of ``source_kind`` with a current ready version holding ``blocks`` (the first block is the
    synthetic mark when the caller's first block does not already carry it). Returns ``(document_id, version_id)``."""
    rows = list(blocks)
    if not rows or MARK not in rows[0][3]:
        rows.insert(0, ("heading", None, [], MARK, 1))
    doc, ver = make_document(office, group, marked_title(title), sha=sha or hashlib.sha256(json.dumps([title, rows], ensure_ascii=False).encode()).hexdigest(),
                             source_kind=source_kind)
    result_blocks = [Block(i, kind, body, page=page, section=path[-1] if path else None,
                           section_path=list(path), label=label, status="read", table_index=None)
                     for i, (kind, label, path, body, page) in enumerate(rows)]
    pages_n = max(r[4] for r in rows)
    pages = [PageResult(n, "\n".join(b.text for b in result_blocks if b.page == n), "text_layer", 1.0, True)
             for n in range(1, pages_n + 1)]
    chunks = [ChunkResult(i, "table" if b.kind == "table" else "text", [b.page], b.section, b.text, block_start=i,
                          block_end=i) for i, b in enumerate(result_blocks)]
    result = ExtractionResult(pages_n, pages, [], chunks, blocks=result_blocks)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, info, result)
        conn.execute(text("UPDATE document_versions SET page_count = :n, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"n": pages_n, "i": json.dumps({"reading_id": reading}), "v": ver})
    pipeline.embed_stage(office.system, info, 1e18)
    return str(doc), str(ver)


# the names a synthetic standard's sections carry (round 10 U8): searched by words of its own text
APPROACH = "על השמאי לבסס את הערכתו על עסקאות השוואה בנכסים דומים."
EXCEPTION = "הוראה זו חלה על כל הנכסים למעט נכסים חריגים שאין להם נכסי השוואה."
RECOMMENDATION = "מומלץ להוון את ההכנסה לפי שיעור שוק."


def standard_blocks(number: str = "98.1", edition: str | None = "גרסה 2.0", effective: str | None = "05.10.2025",
                    approach: str = APPROACH) -> list[Row]:
    """A synthetic standard stating its number, edition and effective date, with an internal and an external
    reference, a section whose provision has an exception (2.1), a recommendation (2.2) and an appendix. ``approach``
    is the provision of section 2.1, which two editions may state differently."""
    path = ["2. שיטות", "2.1 גישת ההשוואה"]
    out: list[Row] = [("paragraph", None, [], MARK, 1), ("heading", None, [], f"תקן מס' {number} — מדידת שטחים סינתטית", 1)]
    if edition:
        out.append(("paragraph", None, [], edition, 1))
    if effective:
        out.append(("paragraph", None, [], f"תחילתו ביום {effective}.", 1))
    return out + [
        ("heading", "1", ["1. כללי"], "1. כללי", 1),
        ("paragraph", None, ["1. כללי"], "לפירוט שיטות ההערכה ראו סעיף 2. לעניין מקדמי ההמרה ראו תקן 97.", 1),
        ("heading", "2", ["2. שיטות"], "2. שיטות", 2),
        ("heading", "2.1", path, "2.1 גישת ההשוואה", 3),
        ("paragraph", None, path, approach, 3),
        ("paragraph", None, path, EXCEPTION, 4),
        ("heading", "2.2", ["2. שיטות", "2.2 גישת ההכנסות"], "2.2 גישת ההכנסות", 4),
        ("paragraph", None, ["2. שיטות", "2.2 גישת ההכנסות"], RECOMMENDATION, 4),
        ("heading", None, ["נספח א'"], "נספח א'", 5),
        ("paragraph", None, ["נספח א'"], "טבלת מקדמים סינתטית.", 5),
    ]
