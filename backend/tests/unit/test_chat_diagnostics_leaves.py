"""Round 21 review: the documents behind a draft's calculation are behind the diagnostics (``api._diagnostics``). A computation's leaves
are V#, M# and A# ids (``tools._leaves``), not S# ids, so the document of each registered value or measurement is looked up in the
workspace's values and measurements; an assumption has no document. Stub workspace and invented ids only."""

from __future__ import annotations

from types import SimpleNamespace as NS

from app.chat import api

DOC_V, DOC_M, DOC_S = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"


def _outcome(drafts: list[dict]) -> NS:
    ws = NS(sources={"S9": NS(document_id=DOC_S)}, values={"V1": NS(document_id=DOC_V)}, measurements={"M1": NS(document_id=DOC_M)},
            clock=NS(summary=lambda: {}), registration_log=[], limits_hit=[])
    return NS(resolution=None, workspace=ws, report=None, requirements_trace=[], item_ledger={}, drafts=drafts, rounds=[], timing={})


def _diagnose(drafts: list[dict]) -> dict:
    return api._diagnostics(_outcome(drafts), {}, api._Removals([], [], {}, {}))


def test_the_document_of_a_values_and_a_measurements_leaf_of_a_draft_calculation_is_behind_the_diagnostics():
    draft = {"round": 1, "version": 1, "markdown": "הערך הוא 5 ₪ [C1].", "sources": {"C1": {"type": "computation", "leaves": ["V1", "M1", "A1"]}}}
    assert _diagnose([draft])["document_ids"] == {DOC_V, DOC_M}  # A1 is an assumption: it names no document


def test_a_leaf_that_is_a_source_id_is_still_looked_up_as_a_source():
    draft = {"round": 1, "version": 1, "markdown": "הערך הוא 5 ₪ [C1].", "sources": {"C1": {"type": "computation", "leaves": ["S9"]}}}
    assert _diagnose([draft])["document_ids"] == {DOC_S}


def test_a_draft_without_a_calculation_adds_no_leaf_document():
    draft = {"round": 1, "version": 1, "markdown": "הערך הוא 5 ₪ [S9].", "sources": {"S9": {"type": "source", "document_id": DOC_S}}}
    assert _diagnose([draft])["document_ids"] == {DOC_S}
