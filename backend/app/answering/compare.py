"""The two-sided compare tool (R10, R11; KTD10, KTD11).

The caller passes each side as an explicit handle: a version id (read even when superseded) or a document
id (its current version). Resolving "the second appraisal" from the conversation is the orchestrator's job.
Evidence is retrieved per side under the user's RLS, and every side needs at least one admitted passage;
otherwise the result says the comparison is incomplete and names the side that lacks evidence, never
comparing one-sidedly. Statements are labeled by document or version, and conflicts on the same datum are
reported with the sources of both sides.

The tool runs in three phases so no transaction is open across model calls (R26, KTD13): ``gather_sides``
reads under the caller's ``tenant_tx``, ``compose_comparison`` makes the answer and judge calls with no
connection, and the caller logs ``CompareOutcome.usage`` in a final short transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering.compose import Usage, answer_fields, compose_answer, source_json
from app.answering.content import evidence_from_hits
from app.answering.coverage import coverage
from app.platform.search import SearchScope, search_evidence
from app.providers.llm import LLMProvider
from app.providers.status import Mode, ProviderState

PER_SIDE_LIMIT = 4


@dataclass(frozen=True)
class CompareSide:
    """One side: exactly one of ``version_id`` (an explicit, possibly older version) or ``document_id``."""

    version_id: UUID | None = None
    document_id: UUID | None = None
    label: str | None = None


@dataclass
class _Resolved:
    document_id: UUID
    version_id: UUID
    title: str
    version_no: int
    explicit_version: bool
    label: str = ""


@dataclass
class CompareOutcome:
    answer: dict
    source_rows: list[dict] = field(default_factory=list)
    cacheable: bool = True
    usage: list[Usage] = field(default_factory=list)  # model calls made, logged by the caller afterwards


def _resolve(conn: Connection, side: CompareSide) -> _Resolved | None:
    """The side's version, if the user may see its document and it is not deleted (RLS decides visibility)."""
    if (side.version_id is None) == (side.document_id is None):
        raise ValueError("a compare side names exactly one version or document")
    where = "v.id = :h" if side.version_id is not None else "v.document_id = :h AND v.is_current"
    row = conn.execute(
        text("SELECT v.id, v.document_id, v.version_no, d.title FROM document_versions v JOIN documents d"
             f" ON d.id = v.document_id AND d.deleted_at IS NULL WHERE {where}"),
        {"h": side.version_id or side.document_id},
    ).first()
    if row is None:
        return None
    return _Resolved(row.document_id, row.id, row.title, row.version_no, side.version_id is not None)


def _label(r: _Resolved, side: CompareSide, sides: list[_Resolved | None]) -> str:
    if side.label:
        return side.label
    same_title = sum(1 for s in sides if s is not None and s.title == r.title) > 1
    return f"{r.title}, גרסה {r.version_no}" if (r.explicit_version or same_title) else r.title


@dataclass
class CompareGathered:
    """What the gather phase read for a comparison: labeled evidence per side and the coverage."""

    question: str
    evidence: list[dict]
    side_info: list[dict]
    missing: list[str]
    coverage: dict

    @property
    def source_rows(self) -> list[dict]:
        return [{"document_id": e["document_id"], "version_id": e["version_id"], "chunk_id": e["chunk_id"],
                 "page_list": e["page_list"]} for e in self.evidence]


def gather_sides(conn: Connection, question: str, sides: Sequence[CompareSide], *,
                 queries: Sequence[str] = ()) -> CompareGathered:
    """The gather phase: resolve each side and retrieve its evidence under the user's RLS (database reads
    only, no model call). ``queries`` are the search queries (the question when empty)."""
    if len(sides) < 2:
        raise ValueError("a comparison needs at least two sides")
    resolved = [_resolve(conn, s) for s in sides]
    queries = [q for q in queries if q and q.strip()] or [question]
    evidence: list[dict] = []
    missing: list[str] = []
    side_info = []
    for n, (side, r) in enumerate(zip(sides, resolved, strict=True), start=1):
        if r is None:  # not visible or deleted: named only by its position, never by anything it holds
            missing.append(side.label or f"צד {n}")
            side_info.append({"label": side.label or f"צד {n}", "document_id": None, "version_id": None,
                              "evidence_count": 0})
            continue
        r.label = _label(r, side, resolved)
        hits = search_evidence(conn, queries, PER_SIDE_LIMIT * 2, scope=SearchScope(
            version_ids=(r.version_id,), include_noncurrent=r.explicit_version)).hits
        admitted = [h for h in hits if h["lexical_support"]][:PER_SIDE_LIMIT]
        found = evidence_from_hits(admitted, len(evidence) + 1)
        for e in found:
            e["label"] = r.label
            if r.explicit_version:
                e["explicit_version"] = True
        evidence += found
        side_info.append({"label": r.label, "document_id": str(r.document_id), "version_id": str(r.version_id),
                          "evidence_count": len(found)})
        if not found:
            missing.append(r.label)
    return CompareGathered(question, evidence, side_info, missing, coverage(conn, None))


def compose_comparison(g: CompareGathered, provider: LLMProvider | None, state: ProviderState) -> CompareOutcome:
    """The compose phase: the answer and judge calls, with no database connection (R26). Every side needs
    evidence; otherwise the comparison is incomplete and no model is called. When the verified claims cite
    only some sides, the comparison is incomplete too and names the uncovered sides. The caller logs
    ``usage``."""
    if g.missing:
        body = (f"ההשוואה אינה שלמה: לא נמצאו ראיות רלוונטיות עבור {', '.join(g.missing)}"
                " במסמכים שאתם מורשים לראות. לא מוצגת השוואה חד-צדדית.")
        answer = {"kind": "abstain", "text": body, "provider": "template", "demo": False,
                  "sources": source_json(g.evidence), "coverage": g.coverage, "numeric": None,
                  "limitations": ["השוואה דורשת לפחות קטע ראיה אחד מכל צד."],
                  "claims": [], "abstention_kind": "not_found", "dropped_claims": 0, "mode": state.mode.value,
                  "compare": {"sides": g.side_info, "incomplete": True, "missing_sides": g.missing,
                              "conflicts": []}}
        return CompareOutcome(answer, g.source_rows, cacheable=False)

    comp = compose_answer(provider, g.question, g.evidence, compare=True)
    limitations = list(comp.limitations)
    if mode_note := state.limitation():
        limitations.append(mode_note)
    answer = {"kind": "content", "text": comp.text, "provider": comp.provider, "demo": comp.demo,
              "sources": source_json(g.evidence), "coverage": g.coverage, "limitations": limitations,
              "numeric": None, **answer_fields(comp, state.mode.value),
              "compare": {"sides": g.side_info, "incomplete": comp.incomplete, "missing_sides": comp.uncovered,
                          "conflicts": comp.conflicts}}
    return CompareOutcome(answer, g.source_rows, cacheable=comp.cacheable and state.mode != Mode.ERROR,
                          usage=comp.usage)
