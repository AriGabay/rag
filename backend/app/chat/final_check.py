"""The final check of the exact text that is saved (round 8 U6, KTD6, KTD7, KTD10; R3, R16–R19).

Everything the server writes after the model's answer is verified (``compose``), but the judge only ever saw the
model's draft: a sentence with a span cut out of it, a sentence under a heading that went, a sentence the server
marked or qualified, and the gap paragraph, the limit sentences and the coverage note are all new text in the saved
Markdown. ``finalize`` checks that text — the one that is composed, then saved, sent to the UI and shown after a refresh —
as a whole, within the verification allowance, and returns it with its digest. The loop (KTD6):

1. ``compose`` the draft with its report; split the composed Markdown into units.
2. A unit made entirely of server spans is not judged: it is a pure function of the final state (component statuses, the
   removal log, the visible cited documents, the limits hit), checked once, by the fixed-point assertion at the end — a
   last recomposition from that state reproduces the saved text and its registry exactly (``assert_fixed_point``).
3. A model-written unit — the server spans inside it excluded from its key and rendered to the judge as ``<server>`` —
   reuses the verdict of the draft sentence it came from when its key (text, the sources it cites, the context above it,
   the heading chain, the request, the judge's version) is that sentence's key: a partial mark, a qualifier, a citation the
   server attached, a removal elsewhere in the answer do not change it. A unit whose key changed — a span cut, a heading or a
   table header that went — is judged in ONE call (``verify_final``). When a unit that voted for a component was among
   them, the call carries the requirements block and the votes are replaced by its scores (KTD9).
4. A unit that fails goes to the ladder's last step (a rewrite needs a repair round, and there is none left): it is
   removed from the draft's report with the check that fired (``final``), what rests on it goes with it, and the answer is
   composed and checked again — the gap paragraph, the removal note and the coverage note follow from the new state — up
   to ``FINAL_PASSES`` times.
   A model unit that refers back ("לעיל", "כאמור", "שני המקדמים"; ``verify.BACK_REFERENCES``) after a unit before it in its
   section was removed is not that sentence any more (round 13 U11, KTD13 rule 3, R16, AE7): its key holds the removed
   predecessors, so it is judged again with the text that stays before it and the removed units beside it, and a verdict that
   names a removed unit in ``depends_on`` removes it as a ``dependency``.
5. After the last pass (or when the judge cannot be reached, or there is no time), what still fails or is unchecked is
   withheld, with no further judge call. Withholding cascades deterministically: a kept unit whose context changed because of
   a removal and has no verdict for its new key is withheld too (a sentence whose heading went; a table without its
   header goes with it, a result with its removed input). The server spans are recomputed from that state, the fixed
   point is asserted once more, and only that text, with the digest of its bytes, is returned.

A table row that fails in one cell is not removed whole when the rest of it stands (round 20 U2, KTD2, R5-R8; ``_Final.cells``): the judge
gives a row's claims per cell (rule 35); a cell that is independent — not the row's key cell, nothing else rests on it, no defect that changes
how the other cells read, no qualifier word in what is cut, a supported data cell left — is cut to the marker (``report.cuts``, an exact
claim record) when it is unsupported, and keeps the "partly verified" mark, written inside it, when it is partly supported. Any other row is
removed as before. A verdict is reused when the row, its sources, its header row, its headings and the scope its lead line names are the
same (``verify.key_context``): the removal of a line that carries none of them elsewhere in the answer does not judge a row again, and a cut
made here is accepted without a new judgment when what stays was judged supported (``cell_cut_stands``): no judge call is added per cell.

The check costs no judge call when ``compose`` changed only server spans. A technical failure of the check is never a
contradiction (R14): the unit is withheld with the cause ``check_failed``. A check with no time left to judge (round 9
R19, KTD10) keeps what the draft judged and the cut left standing — a cut sentence whose supported remainder is its
draft's words with the cut spans removed, the cut spans holding no negation, condition, status, date or caveat word
(``verify.QUALIFIER_LEXICON``) and the heading and table header above it unchanged (``remainder_stands``) — and
withholds the rest with the cause ``time_limit``: the claim was not found wrong, the check ran out of time."""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import logging
import re
from dataclasses import dataclass, field

from app.chat.compose import Composition, compose, text_digest
from app.chat.verify import (
    _IDS,
    _MISSING_TOKEN,
    DEFECT_FAILURES,
    DEPENDS_ON_REMOVED,
    FAILING_VERDICTS,
    GONE_ACTIONS,
    NOT_CLASSIFIED,
    STANDING_VERDICTS,
    CutGroup,
    JudgeRequirement,
    JudgeVerdict,
    Problem,
    TurnRequirements,
    Unit,
    VerificationUnavailable,
    VerifyReport,
    _apply_cut,
    _claim_cells,
    _Coverage,
    _cut_groups,
    _Evidence,
    _headings_above,
    _judge_all,
    _judged_failure,
    _leaves_words,
    _named_support,
    _non_claim_accepted,
    _norm,
    _primary_claim_id,
    _remove_dependents,
    _workspace_listing,
    cell_bounds,
    claim_records,
    clarification_question,
    defining_leads,
    exempt_without_verdict,
    has_back_reference,
    has_qualifier,
    ids_in,
    key_context,
    note_not_checked,
    rescued_expansions,
    row_cut_safe,
    scope_outcome,
    split_units,
    table_contexts,
    unit_claims,
    unit_key,
)
from app.providers.llm import has_time

logger = logging.getLogger(__name__)

FINAL_PASSES = 2  # compose-and-check rounds that may judge; what still fails after them is withheld (KTD6)
FINAL_MIN_SECONDS = 10  # the final check does not call the judge with less of the verification allowance left
PRECEDING_UNITS = 4  # the units that stay before a unit that refers back, shown to the judge with it (round 13 U11)
PRECEDING_CHARS = 1_200
FINAL_LABEL = "verify_final"  # the usage purpose of its judge calls (KTD10)
# why a sentence of the saved text was removed or withheld by the final check (diagnostics; the user's path has the
# server's fixed sentences, ``verify.REMOVAL_SENTENCES`` and ``CHECK_CAUSE_SENTENCES``)
FINAL_UNSUPPORTED = "אחרי העריכה לא נתמך במקורות: {reason}"
FINAL_UNCHECKED = "הנוסח הסופי של הטענה לא נבדק מול המקורות: הבדיקה לא הושלמה ({status})"
FINAL_PARTIAL = "נתמך חלקית בנוסחו הסופי"
FINAL_ATTACH = "נתמך ב-{source}, שהתשובה לא ציטטה"
_BULLET = re.compile(r"^[-*+•]\s+")  # a list marker: markup, not content (a sentence moved to the front of its line)


class FinalCheckError(Exception):
    """The final text could not be made a fixed point of its own state: nothing unchecked is ever returned (R19). ``stats``: the
    check's counts when it failed (no content), kept with the failure so its cause is not lost (round 18 U3, KTD7, R13)."""

    def __init__(self, message: str, stats: dict | None = None):
        super().__init__(message)
        self.stats = dict(stats or {})


@dataclass
class FinalUnit:
    """A unit of the composed text: ``model`` — it holds the model's own words (else it is only the server's);
    ``drafts`` — the indexes of the draft's units those words come from; ``key`` — its reuse key, the model-written text
    only (server spans excluded); ``protected`` — it holds a clarification's question line."""

    unit: Unit
    model: bool
    drafts: list[int] = field(default_factory=list)
    key: str = ""
    protected: bool = False
    exempt: bool = False  # neutral navigation text, which needs no verdict (``exempt_without_verdict``)
    text: str = ""  # the model-written text the key stands for: no citations, no server span, no list marker
    # a unit that refers back after a removal before it in its section (round 13 U11, KTD13 rule 3): the draft indexes of the
    # removed units, and the model text that stays before it in its section (both in its key)
    removed_before: list[int] = field(default_factory=list)
    preceding: str = ""


@dataclass
class Failure:
    """A unit of the saved text that failed the check, or could not be checked: the draft units to remove, the failure kind
    and its cause (``not_checked``: ``check_failed``, ``omitted`` or ``not_classified``) and the verdict recorded."""

    drafts: list[int]
    kind: str
    reason: str
    cause: str = ""
    record: str = "unsupported"
    check: str = "final"  # ``dependency``: the unit rests on a unit removed before it (``basis``: the claim of that unit)
    basis: str = ""


@dataclass
class Checked:
    failures: list[Failure] = field(default_factory=list)
    mutated: bool = False  # a partial mark or an attached citation was added: the text is composed again
    unavailable: bool = False  # the judge could not be reached

    @property
    def stable(self) -> bool:
        return not self.failures and not self.mutated


@dataclass(frozen=True)
class CellCut:
    """A table row the final check cut cell by cell (round 20 U2, KTD2): the heading chain and the reuse context (``verify.key_context``)
    of the row as the judge read it, and the cuts made in it (the report's, in draft offsets). The cut row is the judged row with
    the cells that failed taken out, so it is accepted without a new judgment while those are what it is (``cell_cut_stands``)."""

    heading: str
    context: str
    groups: tuple[CutGroup, ...]


@dataclass
class Finalized:
    """The checked text with the registry of the server's spans in it (``composition``), the digest of its bytes
    (``digest``) and the check's counts (``stats``: no content)."""

    composition: Composition
    digest: str
    stats: dict


def assert_fixed_point(saved: Composition, again: Composition) -> None:
    """The server's spans are checked by construction (KTD6): recomputing them from the final state reproduces the saved
    text and its registry exactly. Raises ``FinalCheckError`` when they do not."""
    same = (saved.answer.answer_markdown == again.answer.answer_markdown and saved.spans == again.spans
            and saved.answer.status == again.answer.status)
    if not same:
        raise FinalCheckError("the server's spans are not a fixed point of the final state")


def _keyed(unit: Unit, text: str, ids: list[str]) -> Unit:
    """The unit as its reuse key sees it: the model's text without its citations (and without a list marker), and the
    ids the model cited — a citation the server attached is not part of what was judged."""
    return dataclasses.replace(unit, text=_BULLET.sub("", text), ids=ids)


class _Final:
    """One final check of one answer: the draft and its report (which the loop edits: a unit the check removes is a problem
    of the report, so every derived text — the removal note, the components, the gap paragraph — follows)."""

    def __init__(self, provider, ws, answer, report: VerifyReport, turn: TurnRequirements, *, question: str,
                 request: str | None, usage: list[dict], deadline: float | None, limit_sentences: list[str] | None):
        self.provider, self.ws, self.answer, self.report, self.turn = provider, ws, answer, report, turn
        self.question, self.request, self.usage, self.deadline = question, request, usage, deadline
        self.limit_sentences = limit_sentences
        self.digests: dict[str, str] = {}
        self.passed: set[str] = set()  # the reuse keys that have a verdict: the draft's, and the final check's own
        self.cell_cuts: dict[int, CellCut] = {}  # the rows this check cut cell by cell, by draft unit (round 20 U2)
        self.stats = {"passes": 0, "units": 0, "reused": 0, "judged": 0, "removed": 0, "withheld": 0,
                      "unjudged_kept": 0, "unavailable": False, "cell_cuts": 0, "cell_marks": 0}
        self.drafts = {d.index: d for d in report.units}
        self.draft_starts = [d.start for d in report.units]
        self._evidence: _Evidence | None = None
        self._voters: set[int] | None = None  # ``voters`` of the check in progress: the votes do not change until it rescores

    # --- composing and splitting ------------------------------------------------------------------------------------

    def compose(self) -> Composition:
        return compose(self.ws, self.answer, self.report, self.turn, question=self.question,
                       limit_sentences=self.limit_sentences)

    def key_of(self, unit: Unit) -> str:
        return unit_key(unit, self.ws, self.request or self.question, unit.heading, self.digests)

    def draft_key(self, d: Unit) -> str:
        return self.key_of(_keyed(d, d.text, ids_in(d.raw)))

    def split(self, comp: Composition) -> list[FinalUnit]:
        """The composed text's units, each told apart as the server's or the model's, with the draft units it comes from,
        the server spans inside it (for the judge: ``<server>``) and, for the model's, its reuse key."""
        md = comp.answer.answer_markdown
        if len(comp.origin) != len(md):
            raise FinalCheckError("the composition does not index its text")
        server = [o < 0 for o in comp.origin]
        units = split_units(md)
        # the heading chain is the model's own words: a mark or a marker on a heading is not a new heading
        blank = "".join(" " if s and c != "\n" else c for c, s in zip(md, server, strict=True))
        chains = _headings_above(blank, units)
        # likewise the line that opens a list and the one before a table: a server span written in it is not a new lead (round 21 U6)
        leads, contexts = defining_leads(blank, units), table_contexts(blank, units)
        question = clarification_question(self.answer)
        out: list[FinalUnit] = []
        for u in units:
            u.heading = chains[u.index]
            u.lead = leads.get(u.index, "")
            u.context = contexts.get(u.index, u.context)
            model_at = [i for i in range(u.start, u.end) if not server[i]]
            model_raw = "".join(md[i] for i in model_at)
            # the server spans inside the unit as its text holds them (citations are not in a unit's text: the judge is
            # shown an inserted one after it)
            u.server = []
            for s in comp.spans:
                if s.start < u.end and u.start < s.end:
                    piece = md[max(s.start, u.start):min(s.end, u.end)]
                    piece = piece if s.kind == "citation" else _IDS.sub("", piece)
                    if piece.strip():
                        u.server.append(piece)
            if not re.search(r"\w", model_raw):
                out.append(FinalUnit(u, False))
                continue
            # the draft units its words come from — letters and digits only: a list marker or a full stop a cut left
            # behind belongs to the sentence that was cut, not to the one that stays
            drafts = sorted({self.draft_at(comp.origin[i]) for i in model_at if md[i].isalnum()} - {-1})
            keyed = _keyed(u, _IDS.sub("", model_raw).strip(), ids_in(model_raw))
            f = FinalUnit(u, True, drafts, self.key_of(keyed), exempt=exempt_without_verdict(keyed), text=keyed.text)
            f.protected = bool(question) and (question in _norm(u.text) or any(
                d in self.report.protected for d in drafts))
            out.append(f)
        self.back_references(out)
        return out

    def back_references(self, units: list[FinalUnit]) -> None:
        """A model unit that refers back (``verify.has_back_reference``) with a unit removed before it in its section is judged
        again (round 13 U11, KTD13 rule 3): its key takes in the removed predecessors, and it is shown the model text that
        stays before it in its section. A function of the composed text and the report's removals; a removal elsewhere (another
        section, after the unit) leaves the key as the draft's."""
        removed = self.report.removed_units()
        for n, f in enumerate(units):
            if not f.model or f.exempt or not f.drafts or not has_back_reference(f.text):
                continue
            first = min(f.drafts)
            heading = self.drafts[first].heading if first in self.drafts else f.unit.heading
            before = sorted(i for i in removed if i < first and i in self.drafts and self.drafts[i].heading == heading)
            if not before:
                continue
            f.removed_before = before
            stays = [x.text for x in units[:n] if x.model and x.unit.heading == f.unit.heading and x.text]
            f.preceding = " ".join(stays[-PRECEDING_UNITS:])[-PRECEDING_CHARS:]
            salt = "\x1f".join(f"{i}\x1e{self.drafts[i].id}" for i in before)
            f.key = hashlib.sha256(f"{f.key}\x1d{salt}".encode()).hexdigest()

    def draft_at(self, offset: int) -> int:
        """The index of the draft unit holding ``offset`` of the draft, -1 for none (whitespace, markup)."""
        n = bisect.bisect_right(self.draft_starts, offset) - 1
        if n >= 0 and offset < self.report.units[n].end:
            return self.report.units[n].index
        return -1

    # --- the check ---------------------------------------------------------------------------------------------------

    def time_left(self) -> bool:
        return has_time(self.deadline, FINAL_MIN_SECONDS)

    def check(self, comp: Composition, may_judge: bool) -> Checked:
        """The model's units of ``comp`` against what is known: a unit whose key has a verdict passes; the rest are judged
        in one call (``may_judge``), or, without it, fail as not checked (the withholding of the last step)."""
        res = Checked()
        self._voters = None
        units = [f for f in self.split(comp) if f.model]
        self.stats["units"] = len(units)
        stale = self.stale_units() if may_judge else set()  # without a call, the draft's verdicts stand
        todo = [f for f in units if not f.protected and not f.exempt and (f.key not in self.passed
                                                                           or set(f.drafts) & stale)]
        self.stats["reused"] += len(units) - len(todo)
        stands = [f for f in todo if f.key not in self.passed and self.cell_cut_stands(f, may_judge)]
        for f in stands:
            # a row this check cut cell by cell, as it was judged without the cells that failed: nothing new to judge (R8)
            self.passed.add(f.key)
            self.stats["unjudged_kept"] += 1
        if stands:
            gone = {id(f) for f in stands}
            todo = [f for f in todo if id(f) not in gone]
        if not todo:
            return res
        if not may_judge:
            # no judge call: a cut sentence whose remainder the draft judged supported stays; the rest is not checked
            # (``time_limit`` when the time ran out; ``check_failed`` when the judge was down or the passes were used)
            cause = "check_failed" if self.stats["unavailable"] or self.time_left() else "time_limit"
            status = "unavailable" if cause == "check_failed" else "time_limit"
            for f in todo:
                if self.remainder_stands(f):
                    self.passed.add(f.key)
                    self.stats["unjudged_kept"] += 1
                else:
                    res.failures.append(self.unchecked(f, cause, status))
            return res
        changed = todo
        todo = self.with_votes(todo, units)
        batch, index_of, coverage = self.batch(todo)
        try:
            # the context a re-read read for a claim of these sentences is shown again in the source it expanded
            shown = rescued_expansions(self.report, self.ws, sorted({d for f in todo for d in f.drafts}))
            verdicts, scored, unchecked = _judge_all(self.provider, batch, self.ws, self.usage, self.deadline, coverage,
                                                     self.removed_units(todo), FINAL_LABEL, shown)
        except VerificationUnavailable as exc:
            logger.warning("final check: the judge could not be reached (%s)", exc.status)
            self.stats["unavailable"], res.unavailable = True, True
            res.failures = [self.unchecked(f, "check_failed", exc.status) for f in changed]
            return res
        self.stats["judged"] += len(todo)
        for bi, f in index_of.items():
            v = verdicts.get(bi)
            if v is None:
                if not any(f is c for c in changed):
                    continue  # judged again only to be scored with the unit it voted with: its own verdict stands
                status = unchecked.get(bi)
                res.failures.append(self.unchecked(f, "check_failed" if status else "omitted", status or "omitted"))
                continue
            self.decide(f, v, res)
        if coverage is not None:
            # the components now given by what the units say as saved: the gap paragraph follows from the new votes
            res.mutated |= self.rescore(scored, {bi: (f.drafts[0] if f.drafts else -1) for bi, f in index_of.items()},
                                        {d for f in todo for d in f.drafts})
        return res

    def removed_units(self, todo: list[FinalUnit]) -> list[Unit]:
        """The draft units removed from before the units of a call that refer back (``<removed_units>``, rule 31): shown so
        the judge can name them in ``depends_on``."""
        wanted = sorted({i for f in todo for i in f.removed_before})
        return [self.drafts[i] for i in wanted if i in self.drafts]

    def remainder_stands(self, f: FinalUnit) -> bool:
        """Whether a changed unit needs no judge call (round 9 R19, KTD10, AE6): it is a draft unit whose edit was a cut
        (``report.cuts``), its words are the draft's with the cut spans removed, every claim that stands was judged
        supported, the cut spans hold no word that qualifies what stays (``has_qualifier``), and the heading chain and
        the table header and context above it are the draft's — a heading or a header that went changes what the
        sentence says ("proposed" read as approved), and that needs a judge."""
        if len(f.drafts) != 1 or not f.text:
            return False
        d, groups = self.drafts.get(f.drafts[0]), self.report.cuts.get(f.drafts[0])
        if d is None or not groups:
            return False
        cut = {cid for g in groups for cid in g.claims}
        standing = [r for r in self.report.claims.get(d.index, []) if r.id not in cut]
        if not standing or any(r.verdict != "supported" for r in standing):
            return False
        u = f.unit
        if u.heading != d.heading or u.table_header != d.table_header or _norm(key_context(u)) != _norm(key_context(d)):
            return False
        spans = [(g.start - d.start, g.end - d.start) for g in groups]
        if any(has_qualifier(d.raw[a:b]) for a, b in spans):
            return False
        return self.leaves_the_text(d, groups, f)

    @staticmethod
    def leaves_the_text(d: Unit, groups, f: FinalUnit) -> bool:
        """Whether the draft unit ``d`` with the cut ``groups`` taken out has the words of the saved unit ``f`` and no others."""
        left, at = [], 0
        for g in sorted(groups, key=lambda g: g.start):
            left.append(d.raw[at:g.start - d.start])
            at = g.end - d.start
        left.append(d.raw[at:])

        def words(text: str) -> list[str]:
            return _MISSING_TOKEN.findall(_IDS.sub("", text))

        return words("".join(left)) == words(f.text)

    def cell_cut_stands(self, f: FinalUnit, may_judge: bool = False) -> bool:
        """Whether a saved row is a row this check cut cell by cell (``cells``) and nothing else changed (round 20 U2, KTD2, R8): its
        words are the draft's with the cut cells taken out, and its heading chain, its table header and the context its verdict is reused
        under are what the judge read. The cells that stay were judged supported (or partly, and marked) in that verdict. A row that voted
        for a component is judged again while there is a call to make (``may_judge``), as every changed voter is (KTD9): the component is
        scored again over the row without the cell; with no call left it stands as cut."""
        if len(f.drafts) != 1 or not f.text or f.drafts[0] not in self.cell_cuts:
            return False
        if may_judge and self.turn.derived and self.report.requirements and f.drafts[0] in self.voters():
            return False
        cut, d, groups = self.cell_cuts[f.drafts[0]], self.drafts.get(f.drafts[0]), self.report.cuts.get(f.drafts[0])
        if d is None or not groups or tuple(groups) != cut.groups:
            return False
        u = f.unit
        if u.heading != cut.heading or u.table_header != d.table_header or _norm(key_context(u)) != _norm(cut.context):
            return False
        return self.leaves_the_text(d, groups, f)

    def evidence(self) -> _Evidence:
        """What the requirements calls could see (``_Evidence``), built once for the whole check: the workspace's sources, the
        components and the answer's ids do not change while the check runs, so the passages and the signatures stay what they were."""
        if self._evidence is None:
            self._evidence = _Evidence(self.ws, self.report.requirements, self.report.answer_ids)
        return self._evidence

    def stale_units(self) -> set[int]:
        """The draft units that gave a score whose component's evidence changed since it was cast (round 17 U1, KTD1): the same
        admission rule the verification applies to a carried score. The final check compares the read passages (the ledger is
        what the verification closed). Such a unit is judged again with the requirements block, and its component is scored
        again over its own units, as in ``VerdictCache.reuse``."""
        if not (self.turn.derived and self.report.requirements and self.report.requirement_votes):
            return set()
        evidence = self.evidence()
        return {i for votes in self.report.requirement_votes.values() for v in votes
                if evidence.refusal(v, items=False) for i in (*v.units, *v.absent)}

    def with_votes(self, todo: list[FinalUnit], units: list[FinalUnit]) -> list[FinalUnit]:
        """A unit that voted for a component and changed is judged with the units it voted with (KTD9, as
        ``VerdictCache.reuse`` does): the requirement is scored on them together."""
        voters = self.voters()
        if not (self.turn.derived and self.report.requirements) or not any(set(f.drafts) & voters for f in todo):
            return todo
        wanted = {d for f in todo for d in f.drafts}
        extra: set[int] = set()
        for votes in self.report.requirement_votes.values():
            for v in votes:
                if v.status in ("full", "partial") and set(v.units) & wanted:
                    extra |= set(v.units) - wanted
        more = [f for f in units if f not in todo and not f.protected and not f.exempt and len(f.drafts) == 1
                and f.drafts[0] in extra]
        return todo + more

    def voters(self) -> set[int]:
        """The draft units that voted for a component (a full or partial vote), as of the check in progress."""
        if self._voters is None:
            self._voters = {i for votes in self.report.requirement_votes.values() for v in votes
                            if v.status in ("full", "partial") for i in v.units}
        return self._voters

    def batch(self, todo: list[FinalUnit]) -> tuple[list[Unit], dict[int, FinalUnit], _Coverage | None]:
        """The units of one judge call, each under the index of the draft unit it comes from (a requirement vote names
        draft indexes; a second unit of the same draft unit takes a fresh one), and the requirements block when a unit
        that voted for a component is among them."""
        fresh = len(self.report.units) + 1000
        used: set[int] = set()
        index_of: dict[int, FinalUnit] = {}
        batch: list[Unit] = []
        for f in todo:
            bi = f.drafts[0] if len(f.drafts) == 1 and f.drafts[0] not in used else fresh + len(index_of)
            used.add(bi)
            index_of[bi] = f
            batch.append(dataclasses.replace(f.unit, index=bi, removed_before=list(f.removed_before),
                                             preceding=f.preceding))
        coverage = None
        if self.turn.derived and self.report.requirements and any(set(f.drafts) & self.voters() for f in todo):
            listing, known = _workspace_listing(self.ws, self.turn)
            coverage = _Coverage(self.turn, [p.ask for p in getattr(self.answer, "parts", None) or []],
                                 self.request or self.question, listing, known, items=self.report.item_listing(ask=False),
                                 question=self.question)
        return batch, index_of, coverage

    def rescore(self, scored: list[JudgeRequirement], to_draft: dict[int, int], judged: set[int]) -> bool:
        """The votes of the requirements the judged units voted for, replaced by this call's scores of them (KTD9).
        Whether any vote changed (the answer is then composed again: a component's status is part of its text)."""
        mapped = [v.model_copy(update={"units": [to_draft[i] for i in v.units if to_draft.get(i, -1) >= 0],
                                       "absent": [to_draft[i] for i in v.absent if to_draft.get(i, -1) >= 0]})
                  for v in scored]
        evidence = self.evidence()
        for v in mapped:  # cast on the saved text of this version: stamped as the verification stamps its own
            stamp = v._stamp or {}
            evidence.stamp(v, self.report.version, self.report.digest)
            v._stamp |= {"call_units": [to_draft[i] for i in stamp.get("call_units", []) if to_draft.get(i, -1) >= 0],
                         "final": True}
        votes = self.report.requirement_votes
        before = {rid: [v.model_dump_json() for v in old] for rid, old in votes.items()}
        for rid, old in list(votes.items()):
            if any((set(v.units) | set(v.absent)) & judged for v in old):
                votes[rid] = [v for v in old if not (set(v.units) | set(v.absent)) & judged] + [
                    v for v in mapped if v.id == rid]
        self.report.settle_parameters(self.ws)
        self._voters = None
        return before != {rid: [v.model_dump_json() for v in new] for rid, new in votes.items()}

    def decide(self, f: FinalUnit, v: JudgeVerdict, res: Checked) -> None:
        """What the judge's verdict on a unit of the saved text means (the unit is already shown the server's spans as
        such): supported — it passes; partly supported with no concrete defect — it passes, marked; supported by a source it
        does not cite — the citation is attached (the draft's rule, ``_named_support``); anything else fails."""
        u = f.unit
        if v.verdict in ("not_factual", "navigation"):
            if _non_claim_accepted(u, v.verdict):
                return self.accept(f, v.verdict)
            res.failures.append(Failure(f.drafts, "not_checked", NOT_CLASSIFIED, "not_classified", "unchecked"))
            return
        if v.verdict in ("unsupported", "contradicts"):
            res.failures.append(Failure(f.drafts, _judged_failure(u, v, self.ws),
                                        FINAL_UNSUPPORTED.format(reason=v.reason), record=v.verdict))
            return
        if (dependency := self.dependency(f, v)) is not None:
            res.failures.append(dependency)
            return
        claims = unit_claims(u, v, self.ws)
        failing = [c for c in claims if not c.whole_unit and c.verdict in ("unsupported", "contradicts", "partial")]
        if v.defect == "scope" and v.verdict in STANDING_VERDICTS and not failing and self.scope(f, v, res):
            return
        if v.verdict == "partial" and (failing or v.defect != "none") and not self.attribute_only(f, v, failing) or (
                v.verdict == "supported" and any(c.verdict in ("unsupported", "contradicts") for c in claims)):
            if self.cells(f, v, claims, res):  # only the failing cell goes, or is marked, when the rest of the row stands (R5, R6)
                return
            res.failures.append(Failure(f.drafts, DEFECT_FAILURES.get(v.defect) or "absent_from_source",
                                        FINAL_UNSUPPORTED.format(reason=v.reason), record="partial"))
            return
        if v.verdict == "partial":
            self.accept(f, v.verdict)
            res.mutated |= self.mark(Problem(self.drafts[f.drafts[0]], FINAL_PARTIAL, "partial", defect="none",
                                             check="final")) if len(f.drafts) == 1 else False
            return
        named = [s for s in dict.fromkeys(v.supported_by) if s not in u.ids]
        if named:
            problem = _named_support(u, named, self.ws, self.question)
            if problem is not None:
                res.failures.append(Failure(f.drafts, problem.failure_kind or "absent_from_source",
                                            FINAL_UNSUPPORTED.format(reason=problem.reason)))
                return
            if len(f.drafts) == 1:
                d = self.drafts[f.drafts[0]]
                for s in named:
                    res.mutated |= self.mark(Problem(d, FINAL_ATTACH.format(source=s), kind="needs_citation",
                                                     cite=s, check="final"))
        self.accept(f, v.verdict)

    def cells(self, f: FinalUnit, v: JudgeVerdict, claims: list, res: Checked) -> bool:
        """A table row that fails in some of its cells keeps the others (round 20 U2, KTD2, R5-R8, AE4, AE5): the judge's claims of the
        row are its cells (rule 35), each found once in the draft row and placed in one cell. When every cell that fails is independent
        (``verify.row_cut_safe``: not the key cell, nothing else rests on it, no defect that changes how the other cells read, no
        qualifier word in what is cut, a supported data cell left), an unsupported cell is cut to the marker — a cut group of the
        report with an exact claim record, as ``plan_edits`` makes it — and a partly supported one keeps the "partly verified" mark,
        written inside it. The judge's claims replace the draft's records of the row, so the log, the markers and the coverage
        decision read the cells. False — and nothing changed — for anything else: the caller removes the row, as before. No judge call."""
        u = f.unit
        if len(f.drafts) != 1 or u.table_span is None or u.table_header:
            return False
        d = self.drafts.get(f.drafts[0])
        report = self.report
        if d is None or d.table_span is None or d.table_header or d.index in report.protected or report.cuts.get(d.index):
            return False
        spans = [c.span for c in claims]
        if any(c.whole_unit or c.fallback for c in claims) or len(set(spans)) != len(spans) \
                or any(d.text.count(x) != 1 for x in spans):
            return False
        recs = claim_records(d, claims)
        if not row_cut_safe(d, recs, v.defect):
            return False
        failing = [r for r in recs if r.verdict in FAILING_VERDICTS]
        partial = [r for r in recs if r.verdict == "partial"]
        groups = _cut_groups(d, recs) if failing else []
        if (not failing and not partial) or groups is None or (groups and not _leaves_words(d, groups)):
            return False
        bounds = cell_bounds(d.raw)
        for r in recs:
            r.post_check, r.citation = r.verdict, "cited" if d.ids else "not_needed"
            r.action = "qualified" if r in partial else "kept"  # the failing claims' action is the cut's (``_apply_cut``)
        old = {r.id for r in report.claims.get(d.index, [])}
        report.claims[d.index] = recs
        primary = _primary_claim_id(report, d.index)
        for p in report.problems:  # a problem of the draft names a record of it that the cells replace
            if p.unit.index == d.index and p.claim_id in old:
                p.claim_id = primary
        if groups:
            _apply_cut(report, d.index, groups, failing)
            for r in failing:
                report.problems.append(Problem(d, FINAL_UNSUPPORTED.format(reason=v.reason), "partial", failure_kind=r.failure_kind(),
                                               check="final", claim_id=r.id, resolution="cut"))
            self.cell_cuts[d.index] = CellCut(f.unit.heading, key_context(f.unit), tuple(groups))
            self.stats["cell_cuts"] += len(failing)
        for r in partial:  # every partly supported cell has its own mark, at the end of its own content
            cell = min(_claim_cells(d, r, bounds))
            end = d.start + bounds[cell][0] + len(d.raw[bounds[cell][0]:bounds[cell][1]].rstrip())
            self.stats["cell_marks"] += self.mark(Problem(d, FINAL_PARTIAL, "partial", defect="none", check="final",
                                                          claim_id=r.id, cell_end=end), keep_claim=True)
        self.accept(f, "partial" if partial else "supported", records=False)
        report.assign_components()
        res.mutated = True
        return True

    def scope(self, f: FinalUnit, v: JudgeVerdict, res: Checked) -> bool:
        """A sentence of the saved text shown wider than the scope its source states (defect ``scope``, round 17 U5, KTD5, R10, R11): it
        is qualified, never removed. The scope phrase the judge named, when it stands in the context of the value's own table or
        section, is written next to the value with the citation; else the value stays with the note that its scope was not
        verified and where it was read. Both are problems of the draft's unit, which the composer turns into server spans, so the
        saved text is a function of the final state. False: the unit cites no source (the caller marks it partly verified)."""
        if len(f.drafts) != 1:
            return False
        d = self.drafts[f.drafts[0]]
        unbound = len(self.report.unbound)
        handled, problem = scope_outcome(d, v.scope, self.ws, f.text, claims=self.report.claims.get(d.index, []), report=self.report)
        if not handled:
            return False
        if len(self.report.unbound) > unbound:  # no unique place for the scope: the uncertainty line states it (round 21 U2)
            res.mutated = True
        if problem is not None and not any(p.unit is d and p.kind == problem.kind and p.annotation == problem.annotation
                                           for p in self.report.problems):
            problem.claim_id = problem.claim_id or _primary_claim_id(self.report, d.index)
            self.report.problems.append(problem)
            res.mutated = True
        self.accept(f, "partial")
        return True

    def attribute_only(self, f: FinalUnit, v: JudgeVerdict, failing: list) -> bool:
        """Whether a partly supported verdict on a sentence verification already qualifier-edited (a VAT, basis, period or
        number edit cut the attribute's words) names only that kind of defect: no defect, or a unit/period/VAT defect, and
        no claim of it unsupported or contradicted for any other reason. A grounded number is never removed because of an
        attribute (round 13, settled decision: the supported parts stay, the sentence is marked partly verified); the
        server's own edit already said the attribute is not stated. Any other defect, or a claim found absent or
        contradicted, keeps the removal. A scope the answer omits (defect ``scope``, round 17 U5) is never a reason to remove
        either: the sentence stays, marked partly verified, when the server could not write the scope or its limitation
        (``scope``), unless a claim of it is found absent or contradicted for another reason."""
        if v.defect == "scope" and len(f.drafts) == 1:
            return all(c.verdict == "partial" for c in failing)
        if len(f.drafts) != 1 or self.drafts[f.drafts[0]].index not in self.report.qualifier_edits:
            return False
        if v.defect not in ("none", "units"):
            return False
        return all(c.verdict == "partial" or (c.verdict == "unsupported" and c.failure == "wrong_unit") for c in failing)

    def dependency(self, f: FinalUnit, v: JudgeVerdict) -> Failure | None:
        """A unit that refers back and whose verdict rests (``depends_on``) on a unit removed from before it is removed as a
        dependency on that unit, with its failure kind and cause (round 13 U11, KTD13 rule 3, AE7; the same as
        ``verify._remove_dependents``). A repair would be a rewrite, and there is no round left for one."""
        names = [d for d in v.depends_on if d in f.removed_before]
        base = next((p for d in names for p in self.report.problems if p.unit.index == d and p.removes_unit), None)
        if base is None:
            return None
        return Failure(f.drafts, base.failure_kind or "absent_from_source",
                       DEPENDS_ON_REMOVED.format(text=base.unit.text[:120]), base.cause, check="dependency",
                       basis=base.claim_id)

    def mark(self, problem: Problem, keep_claim: bool = False) -> bool:
        """Add a problem the composer turns into a server span (a partial mark, an attached citation), once. ``keep_claim``: the problem
        already names the claim it is about (a cell of a row, round 20 U2): a unit has one such problem for each claim and cell."""
        problems = self.report.problems
        if any(p.unit is problem.unit and p.check == "final" and p.kind == problem.kind and p.cite == problem.cite
               and p.severity == problem.severity and p.resolution == problem.resolution and p.cell_end == problem.cell_end
               and (problem.cell_end is None or p.claim_id == problem.claim_id) for p in problems):
            return False
        if not keep_claim:
            problem.claim_id = _primary_claim_id(self.report, problem.unit.index)
        problems.append(problem)
        return True

    def accept(self, f: FinalUnit, post: str, records: bool = True) -> None:
        """The unit passes under its key. ``records``: its claims record the verdict the check gave (``post_check``); a row cut by cells
        has recorded each cell's own."""
        self.passed.add(f.key)
        for d in f.drafts if records and len(f.drafts) == 1 else ():
            for r in self.report.claims.get(d, []):
                r.post_check = post

    def unchecked(self, f: FinalUnit, cause: str, status: str) -> Failure:
        note_not_checked(getattr(self, "ws", None), status)  # round 22 U0: by the judge call's status, ``omitted``, ``time_limit`` or ``unavailable``
        return Failure(f.drafts, "not_checked", FINAL_UNCHECKED.format(status=status), cause, "unchecked")

    # --- the ladder's last step --------------------------------------------------------------------------------------

    def apply(self, failures: list[Failure], action: str) -> int:
        """Remove the draft units of the failures from the report (``action``: ``removed`` — the logged last resort —
        or ``withheld``), and with them what rests on them (R11). Returns how many units went."""
        report = self.report
        before = report.removed_units()
        gone = set(before)
        for fail in failures:
            def mark_claims(i: int, record: str = fail.record) -> None:
                for r in report.claims.get(i, []):
                    r.action, r.post_check = action, record

            for di in fail.drafts:
                d = self.drafts.get(di)
                if d is None or di in gone or di in report.protected:
                    continue
                gone.add(di)
                head = Problem(d, fail.reason, failure_kind=fail.kind, check=fail.check, cause=fail.cause,
                               claim_id=_primary_claim_id(report, di), basis=fail.basis)
                report.problems.append(head)
                mark_claims(di)
                if d.table_header and d.table_span is not None:
                    # a table left without its header goes with it: its rows are removed on the same ground (R12)
                    for row in report.units:
                        if row.table_span == d.table_span and not row.table_header and row.index not in gone:
                            gone.add(row.index)
                            report.problems.append(Problem(
                                row, "השורה שייכת לטבלה שכותרתה הוסרה בבדיקה הסופית", failure_kind=fail.kind,
                                check="dependency", cause=fail.cause, claim_id=_primary_claim_id(report, row.index),
                                basis=head.claim_id))
                            mark_claims(row.index)
        if gone != before:
            _remove_dependents(report, report.unit_verdicts, self.ws)
            report.assign_components()
        after = report.removed_units()
        for di in after - before:
            for r in report.claims.get(di, []):
                r.action = r.action if r.action in GONE_ACTIONS else action
        went = len(after - before)
        self.stats["withheld" if action == "withheld" else "removed"] += went
        return went

    # --- the loop ---------------------------------------------------------------------------------------------------

    def run(self) -> Finalized:
        try:
            return self._run()
        except FinalCheckError as exc:
            exc.stats = exc.stats or dict(self.stats)
            logger.error("final check failed: %s; stats=%s", exc, exc.stats)
            raise

    def _run(self) -> Finalized:
        comp = self.compose()
        removed = self.report.removed_units()
        self.passed = {self.draft_key(d) for d in self.report.units if d.index not in removed}
        judging = True
        stable = False
        for n in range(1, FINAL_PASSES + 1):
            self.stats["passes"] = n
            if n > 1:
                comp = self.compose()
            may_judge = judging and self.time_left()
            res = self.check(comp, may_judge)
            if res.stable:
                stable = True
                break
            withhold = n == FINAL_PASSES or res.unavailable or not may_judge
            if res.failures and not self.apply(res.failures, "withheld" if withhold else "removed"):
                logger.error("final check: %d failing units could not be removed", len(res.failures))
                raise FinalCheckError("a unit that failed could not be removed")
            if res.unavailable:
                judging = False
            if withhold:
                break
        if not stable:
            comp = self.withhold()
        again = self.compose()
        assert_fixed_point(comp, again)
        self.stats["judge_calls"] = sum(1 for u in self.usage if u.get("purpose") == FINAL_LABEL)
        return Finalized(comp, text_digest(comp.answer.answer_markdown), self.stats)

    def withhold(self) -> Composition:
        """After the last pass (KTD6): what still fails or is unchecked is withheld, and the cascade — a kept unit whose
        context changed because of a removal, with no verdict for its new key — follows with no judge call, until the
        composed text is stable."""
        for _ in range(len(self.report.units) + 2):
            comp = self.compose()
            res = self.check(comp, may_judge=False)
            if res.stable:
                return comp
            if res.failures and not self.apply(res.failures, "withheld"):
                raise FinalCheckError("a unit that could not be checked could not be withheld")
        raise FinalCheckError("the withholding did not settle")


def finalize(provider, ws, answer, report: VerifyReport, turn: TurnRequirements, *, question: str,
             request: str | None = None, usage: list[dict], deadline: float | None = None,
             limit_sentences: list[str] | None = None) -> Finalized:
    """Compose the answer and check the composed text (the module docstring): the checked text, the registry of the server's
    spans in it, the digest of its bytes, and the check's counts. ``report`` is edited — a unit the final check removes
    or withholds is a problem of it, with the claim records' ``action`` and ``post_check`` — so the diagnostics and the
    counts describe the saved text. ``request``: the request as resolved in context (the key's ``request``, as in
    ``verify_answer``); ``deadline``: the verification allowance's end, shared with the verification itself; ``usage``
    receives the final check's judge calls (``verify_final``). Raises ``FinalCheckError`` when the text cannot be made
    a fixed point (never returns unchecked text)."""
    return _Final(provider, ws, answer, report, turn, question=question, request=request, usage=usage,
                  deadline=deadline, limit_sentences=limit_sentences).run()
