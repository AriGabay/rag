"""One failure-kind vocabulary (round 7 KTD5, R12, R13): every backend table that describes a failure kind, and the
UI's label tables, cover exactly the kinds the server produces — a new kind, or a dropped one, fails here instead
of reaching a ``KeyError`` at removal time or a generic fallback text. The UI tables are read from
``frontend/lib/format.ts`` with a simple pattern over their object keys. No database."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.chat import coverage, verify

FORMAT_TS = Path(__file__).resolve().parents[3] / "frontend" / "lib" / "format.ts"


def _ts_keys(name: str) -> set[str]:
    """The keys of ``export const <name>: Record<string, string> = { ... };`` in format.ts."""
    source = FORMAT_TS.read_text(encoding="utf-8")
    m = re.search(rf"export const {name}\b[^=]*=\s*\{{(.*?)\n\}};", source, re.S)
    assert m, f"{name} not found in {FORMAT_TS}"
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1), re.M))


def test_every_backend_failure_kind_table_covers_exactly_the_failure_kinds():
    kinds = set(verify.FAILURE_KINDS)
    assert kinds and len(kinds) == len(verify.FAILURE_KINDS)
    assert set(verify.FAILURE_LABELS) == kinds
    assert set(verify.REMOVAL_SENTENCES) == kinds
    # a unit the server could not check is never sent to the model to repair: the one kind with no repair hint
    assert set(verify.REPAIR_HINTS) == kinds - {"not_checked"}
    # "" is the gap paragraph's fallback for a removal recorded without a kind
    assert set(coverage.REMOVED_BECAUSE) == kinds | {""}
    # a problem kind that is no failure kind maps onto one
    assert set(verify.KIND_FAILURES.values()) <= kinds and set(verify.DEFECT_FAILURES.values()) <= kinds


@pytest.mark.skipif(not FORMAT_TS.exists(), reason="the frontend is not checked out next to the backend")
def test_the_ui_label_tables_cover_exactly_the_backends_failure_kinds_and_gap_reasons():
    assert _ts_keys("FAILURE_KIND_LABEL") == set(verify.FAILURE_KINDS)
    assert _ts_keys("GAP_REASON_LABEL") == set(coverage.REASONS)
