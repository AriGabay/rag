"""A synthetic file of several units, built from blocks, chunks and stored units, for tests that need control the
ingestion of a PDF does not give (round 9 U9): where a chunk runs across a boundary, a boundary that is only probable,
a title the document does not state, a comparable named only in a table. Every text is invented.

``unit_file(office, group, title, units)``: ``units`` are dicts ``{"title", "aliases", "kind", "certainty", "blocks":
[text...], "identifiers": [shown...]}`` in reading order. Each unit's blocks sit on a page of their own (the unit's
number); a text starting with ``"|"`` is a table cell block (its chunk a ``table_row``). Chunks are one per block, plus
the ``chunks`` given as ``(first block, last block)`` over the whole file, which replace the per-block chunk of the
blocks they cover (a chunk that runs across a unit boundary)."""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text

from app.chat import units as unit_store
from app.db import tenant_tx
from app.extraction.base import Block, ChunkResult, ExtractionResult, PageResult
from app.platform import pipeline
from tests.factories import make_document

READING = "reading-units-1"


def unit_file(office, group, title: str, units: list[dict], chunks: list[tuple[int, int]] | None = None,
              sha: str = "u" * 64) -> tuple[str, str]:
    doc, ver = make_document(office, group, title, sha=sha)
    blocks: list[Block] = []
    spans: list[tuple[int, int]] = []
    for n, u in enumerate(units, 1):
        first = len(blocks)
        for body in u["blocks"]:
            table = body.startswith("|")
            blocks.append(Block(len(blocks), "table" if table else "paragraph", body.lstrip("|"), page=n,
                                section=None, section_path=[], status="read",
                                table_index=None))
        spans.append((first, len(blocks) - 1))
    covered = set()
    for a, b in chunks or []:
        covered |= set(range(a, b + 1))
    result_chunks = []
    for a, b in chunks or []:
        result_chunks.append(("text", a, b))
    for b in blocks:
        if b.index not in covered:
            result_chunks.append(("table" if units_table(units, spans, b.index) else "text", b.index, b.index))
    result_chunks.sort(key=lambda c: (c[1], c[2]))
    pages = [PageResult(n, "\n".join(b.text for b in blocks if b.page == n), "text_layer", 1.0, True)
             for n in range(1, len(units) + 1)]
    chunk_rows = [ChunkResult(i, kind, sorted({blocks[x].page for x in range(a, b + 1)}), None,
                              "\n".join(blocks[x].text for x in range(a, b + 1)), block_start=a, block_end=b)
                  for i, (kind, a, b) in enumerate(result_chunks)]
    result = ExtractionResult(len(units), pages, [], chunk_rows, blocks=blocks)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, info, result)
        ingestion = {"reading_id": READING}
        conn.execute(text("UPDATE document_versions SET page_count = :n, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"n": len(units), "i": json.dumps(ingestion), "v": ver})
        reading = unit_store.Reading(UUID(str(doc)), READING, unit_store.derivation_key(ingestion), ())
        rows = []
        for n, (u, (a, b)) in enumerate(zip(units, spans, strict=True), 1):
            kind = u.get("kind", "appraisal")
            rows.append(unit_store.Unit(
                n, kind, u.get("title"), tuple(u.get("aliases") or ()),
                {"shown": list(u.get("identifiers") or ())}, n, n, a, b,
                u.get("certainty", "certain" if n == 1 else "certain"), {}))
        unit_store._insert(conn, ver, reading, rows)
    pipeline.embed_stage(office.system, info, 1e18)
    return str(doc), str(ver)


def units_table(units: list[dict], spans: list[tuple[int, int]], index: int) -> bool:
    """Whether block ``index`` is a table cell block (its text started with ``|``)."""
    for u, (a, b) in zip(units, spans, strict=True):
        if a <= index <= b:
            return u["blocks"][index - a].startswith("|")
    return False


def reread(office, ver: str, reading: str, units: list[dict]) -> None:
    """The file read again (round 9 U10): ``ver`` gets the new reading id ``reading``, and the units of that reading are
    the given ones (``{"title", "aliases", "kind", "identifiers", "blocks": (first, last), "pages": (first, last)}``, in
    order, numbered from 1) over the version's unchanged blocks. The units of the old reading stay stored, as they do
    when a version is read again."""
    with tenant_tx(office.system) as conn:
        doc = conn.execute(text("SELECT document_id FROM document_versions WHERE id = :v"), {"v": ver}).scalar_one()
        ingestion = {"reading_id": reading}
        conn.execute(text("UPDATE document_versions SET ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"i": json.dumps(ingestion), "v": ver})
        stored = unit_store.Reading(UUID(str(doc)), reading, unit_store.derivation_key(ingestion), ())
        rows = [unit_store.Unit(n, u.get("kind", "appraisal"), u.get("title"), tuple(u.get("aliases") or ()),
                                {"shown": list(u.get("identifiers") or ())}, u["pages"][0], u["pages"][1],
                                u["blocks"][0], u["blocks"][1], "certain", {})
                for n, u in enumerate(units, 1)]
        unit_store._insert(conn, ver, stored, rows)
