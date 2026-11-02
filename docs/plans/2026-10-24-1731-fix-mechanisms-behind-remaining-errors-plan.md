---
title: The mechanisms behind the remaining errors - Plan
type: fix
date: 2026-10-24
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# The mechanisms behind the remaining errors - Plan

## Goal Capsule

- **Objective.** When a user asks about an appraisal, the answer contains fewer wrong statements the system adds on its own, fewer correct data
  removed and fewer required data lost, on the same conversations. M1-M5 and M8 address the false system text and a minority of the
  required-data loss; the larger share of that loss (M6/M7) is decided by a measured gate (U8).
- **Means.**
  - Fix the mechanisms the round-23 diagnosis proved responsible for most remaining errors, in order of user-visible harm (KTD1).
  - Every server sentence that says something about the source needs evidence for its wording and for its datum, on every path (KTD3).
  - A general replacement removes the narrow rule it supersedes after a replay on the same real drafts (KTD2).
  - A new evaluation (U0, U10) with script-enumerated units, paired by conversation, with stages that show each fix fired on real failures.
- **Authority.**
  1. The user's round-23 request and the settled decisions in Key Decisions and KTDs.
  2. This plan's R-IDs.
  3. KTDs on mechanism.
  4. Units.
  - Project rules in `CLAUDE.md` (data safety, host-only pytest, model split, public repository) bind every unit.
- **Stop conditions.** Stop and report rather than proceed when:
  - a change would delete or rewrite stored data, or re-read stored documents (a `spans` backfill is a write to stored data: approval and a
    backup first);
  - a replacement cannot be shown, on stored real drafts, to cover the family its narrow rule served;
  - an extra model call would exceed the pre-set budget, or a more expensive model would become the default.
- **Execution profile.** One mechanism at a time: a failing synthetic reproduction, the fix, property tests with negative controls, a
  replay on the stored real drafts of rounds 21-22 (fixed/new counts), then the next. Opus diagnoses and decides; Sonnet implements and
  reviews; Haiku runs simple searches and checks.
- **Finishes and ships.** The orchestrating session commits each unit, updates PR AriGabay/rag#2 and reports. No merge, no deploy.

---

## Product Contract

### Summary
Round 22 fixed real defects, but end to end it did not move the counts the user feels: required items correct stayed equal, wrong server
additions, wrong removals and wrong gap lines did not fall. This round starts from the round-22 traces, maps every remaining failure to a
root mechanism (done: `real_documents/tests/round23/diag/`, private), fixes the mechanisms that explain most of the harm, and measures with an
evaluation that can decide.

### Problem Frame
The diagnosis (four tracers, every failure verified against the stored source, plus a live re-judging experiment) found:
- **Gap and coverage lines are decided on form and never checked against the final answer (M1).** A table row without its own citation, a
  shared value written in a lead-in sentence, a date range, a judge "missing part" that is a caption or a footnote mark: each produces a
  false "read and not used" line. Removal reasons default to "no support was found". This is 30 of the final arm's wrong gap lines and most
  of its "wrong server additions"; the round-22 base code writes the same lines on the same drafts.
- **Server notes that state what the source says are written from a flag or a vocabulary gap (M2).** "The kind/unit/period is not stated in
  the source" comes from a provenance flag with no read of the value's own row, header and caption; small vocabulary and scale holes cut true
  qualifiers.
- **Rows whose text layer lists cells in reverse of the header order (M3)** make the model and the judge bind values to the wrong column:
  correct cells are cut as "swapped", a table is lost by dependency removal, and the one wrong final answer.
- **Destructive judge removals (M4)** rest on one call that is unstable on table rows (three calls agree on 73.5% of units) and that sees only
  the claim's cited sources.
- **Context identity (M5)** reads an unparsed parcel number as another property and removes the right data.
- **The largest loss of required items (M6, M7)** is multi-part needs answered in part after the evidence was delivered, and evidence one block
  away from what was read; both share one turn time budget, and the repair gate is closed for most long turns.
- **The round-22 gate could not decide:** its noise bands came from different graders' counting conventions and two runs per set, and the pass
  rule required falls larger than the control counts.

### Key Decisions
- **No change of product models, libraries, database or OCR; no more expensive default model.** (session-settled: user-directed — chosen over
  switching or strengthening the judge or agent model: the mechanisms are fixed instead.) Governs R10, R11.
- **Narrow fixes superseded by a tested general mechanism leave the active path; sound deterministic rules for numbers, units, dates, structure
  and permissions stay deterministic.** (session-settled: user-directed — chosen over kept fallbacks or model guesses: one active decision per
  question, deterministic where structure decides.) Governs R20, R4.
- **Round 22 is not re-scored; round 23 uses a new evaluation: consistent units, paired by conversation, pre-set practical minimum, guards,
  repetitions and budget; insufficient evidence is "not decided"; an unexercised path is "path not exercised".** (session-settled: user-directed
  — chosen over recomputing round 22 with new bands or a pass from an unexercised check.) Governs R12, R13, R14.
- **Staged measurement from existing traces and focused checks before any broad run; every fix reports eligible, fired, fixed, new errors,
  time and cost.** (session-settled: user-directed — chosen over a broad end-to-end run first.) Governs R15.
- **No deletion of stored data, no re-read of the 16 stored multi-page tables, no mandatory manual approval; private data stays out of git,
  and result or session files are never deleted to handle private data.** (session-settled: user-directed — chosen over re-reading or
  rewriting stored documents to make a fix visible.) Governs R16, R17.
- **All exposed material is regression; no generalisation claim without new material.** (session-settled: user-directed — chosen over
  reporting exposed-set gains as general.) Governs R14.

### Requirements
- R1. A gap or coverage line ("read and not used", "not in the pages read", "not found in the search", a removal reason) stands only when the
  final answer does not state that datum; it is decided by datum (value tokens, row label or quote words, condition), not by citation form.
- R2. A removal or gap wording says what actually happened: removed by verification (with the reason when known), given in part, read and
  not used, not read, not checked. "No support was found" and absence claims appear only when a check established them.
- R3. A server sentence that states what the source says or does not say (kind, unit, period, basis, VAT, scale, scope) is written only when a
  complete read of the datum's own parts (cell, row label, all header rows, caption, table notes, the governing note line, the sentence)
  establishes it; otherwise the wording is "could not be confirmed" or nothing, never "not stated in the source".
- R4. The deterministic readers read the forms the diagnosis found missing (area basis "built", English period column headers, quarterly
  columns, the abbreviation "no." beside a kind word, a unit header spread over header rows, amounts restated in another scale, a negated
  period word, a document-level VAT note, a plot or block number inside a clause), with a negative control for each.
- R5. Page-text rows whose stored order differs from the drawn order are served with each cell bound to its column, the same way to the agent
  and to the judge, decided from stored word positions; with no positions the binding is "unknown" and nothing is removed for it.
- R6. A value-bearing claim is not removed on a single judge verdict when the turn's read sources hold it under the claimed scope, or when a
  bounded confirming call disagrees; the grounded part is kept and the gap stated precisely; the confirmed verdict, not the first, is reused.
- R7. An identity the parser cannot read is unknown, never "another property"; a datum is never moved or removed because of an equal number, an
  overlapping quote or the same appraisal file alone.
- R8. Grounding, calculation correctness and completeness stay separate decisions on the same claim and evidence ids; a calculation is checked by
  its inputs, assumptions, formula and units; a technical failure is never evidence of absence.
- R9. Decisions and dates survive repair rounds and follow-up turns: a claim's identity is its datum and source place, not its position or
  number in one round.
- R10. Product models, libraries, database and OCR are unchanged; no more expensive model by default.
- R11. An extra model call introduced by this round runs only within a measured, pre-set budget.
- R12. The evaluation counts script-enumerated units (server spans by kind, removal entries, reference needs) that graders label; no unit is
  counted in two metrics.
- R13. Comparisons are paired on the same conversations with a cluster bootstrap over conversations; grader agreement is measured; the
  minimal practical improvement, guards, repetitions and budget are fixed before the end-to-end run.
- R14. Insufficient evidence is reported as "not decided"; a check whose path the model did not produce is "path not exercised"; all
  exposed material is regression.
- R15. Every fix reports, on the stored real drafts and on the measured runs: eligible cases, fired, fixed, new errors, time and cost; a fix
  added after a measurement is verified on the final code including the combined path.
- R16. No stored data is deleted or rewritten; the 16 stored multi-page tables are not re-read; a write to stored positions needs approval and
  a backup; fixed code is reported separately from stored data not yet updated.
- R17. Customer data and keys stay out of code, public tests, public docs and git; result and session files are never deleted.
- R18. Permissions, office isolation, source-version checks, Decimal arithmetic, exact citations, the standards store, conversation history and
  the time selector keep working; no mandatory manual approval is added.
- R19. Generalisation is shown by varying names, values, wording, order, position and counts, with negative controls; unrelated added text or
  reordered results do not change attribution; the original questions stay regression only.
- R20. Each unit removes the narrow rules its mechanism supersedes, after a replay on the same real drafts shows the family is covered.

### Acceptance Examples
- AE1. **Covers R1.** **Given** an answer table cited once on its lead-in line, whose rows state an item's value and label, **when** the answer
  is composed, **then** no "read and not used" line names that item; a table that cites plan B on a row still does not cover a plan-A item.
- AE2. **Covers R2.** **Given** a sentence removed by the VAT check, **when** the gap lines are written, **then** the line says it was removed
  in verification, never "no support was found" and never "read and not used".
- AE3. **Covers R3.** **Given** a value whose column header reads "in thousands of shekels" and whose model registration said unit "other",
  **when** a calculation uses it, **then** no note says the unit is not stated in the source.
- AE4. **Covers R5.** **Given** a Latin-labelled numeric row on a Hebrew page whose stored order is reversed against the drawn order, **when**
  the agent and the judge read it, **then** both see each number under its drawn column; with no word positions the row is served as stored
  and no cell is cut for "swapped".
- AE5. **Covers R6.** **Given** a value-bearing table row the judge marks unsupported while another source read in the turn states it under the
  same scope, **when** verification runs, **then** the row stays with a "partly verified" mark and a citation of the holding source.
- AE6. **Covers R7.** **Given** a unit labelled with a parcel number in a notation the parser does not read, **when** a claim about the asked
  property cites it, **then** the claim is not removed as another property; a unit with a different readable parcel still is.
- AE7. **Covers R12, R13.** **Given** two arms run on the same conversations, **when** the results are scored, **then** each wrong gap line is one
  enumerated span counted once, and the difference is reported with a paired interval over conversations.

### Scope Boundaries
- OCR, extraction of new fields and stored-data re-reads are out of scope.
- Reference breadth (data a question did not ask for counted as required) and grader conventions are evaluation decisions, recorded, not code.
- The turn time budget (`chat_turn_seconds`, repair factor, verify reserve) changes only if U8's measurement proves the gate is the blocker and
  a focused run shows no new timeouts.

#### Deferred to Follow-Up Work
- A `spans` backfill for documents read before word positions existed, if U4's probe finds the affected blocks without positions (approval
  and backup first).
- New, unused evaluation material for a generalisation claim.

---

## Planning Contract

### Key Technical Decisions
- KTD1. **Order of work by user-visible harm and safety.** M1 gap/coverage lines and M2 source-statement notes first (deterministic, largest
  share of false server text, replay-provable), then M5 identity and M8 small causes, then M4 removals, M3 page-text binding (data
  precondition), and M6/M7 last behind a measurement gate. (Governs R15.)
- KTD2. **Remove a narrow rule only after a replay shows coverage.** For each superseded rule, diff its decisions against the replacement over
  the stored real drafts of rounds 21-22 and over inflected and varied forms; keep a "still removes / still writes" test for every protection
  the change touches. (session-settled: user-directed — chosen over keeping parallel fallbacks: one active decision.) (Governs R20.)
- KTD3. **One evidence rule for every server sentence about the source.** A sentence that states what the source says (or does not say) is a
  meaning-changing addition: it needs evidence for its wording (a complete read of the datum's own parts reports the attestation or its
  absence) and for its datum (the existing applies/structure binding). The qualifier-edit notes, the attribute clauses of conditional notes,
  removal reasons and absence sentences all go through it. (Governs R3, R2.)
- KTD4. **The gap/coverage audit lives inside the one coverage decision, not in `Doc.audit`.** `VerifyReport.item_states` already flips a
  judge-support item by presence in the final answer; the datum audit is a second pure test there, so every line, cap and completeness verdict
  read the same decision and `final_check.assert_fixed_point` still holds. `Doc.audit` keeps its "drop an addition whose datum is gone"
  meaning. (Governs R1.)
- KTD5. **A table's citation counts for its rows only for coverage attribution, never for the row's verification.** Ids parsed from the table's
  lead-in or header row apply to body rows that carry no ids of their own; a row that cites another source keeps that attribution.
  (Governs R1, R7.)
- KTD6. **Judge-support items come only from source data.** A judge "missing part" becomes an item only when it coincides with a stored
  structural span (a table row or cell, a list member, a sentence with a figure); reader-written captions and titles, footnote marks, row
  ordinals and clipped numbers are not required values. (Governs R1.)
- KTD7. **Vocabulary feeds the readers; it never decides silence alone.** Missing forms are added to the shared readers (`meaning`, number and
  scale readers), each with a negative control; the decision "the source is silent" requires KTD3's complete read. (session-settled:
  user-directed — chosen over model guesses for numbers, units, dates and structure.) (Governs R4, R3.)
- KTD8. **Page-text column binding is a serve-time reading of stored word positions** (`document_blocks.spans`), applied in the one function that
  serves rows to both the agent and the judge; nothing stored is rewritten; no positions means "unknown", never a cut. The judge contract
  version is bumped. (Governs R5, R16.)
- KTD9. **Before a destructive removal of a value-bearing claim: a deterministic rescue over the turn's read sources, then a bounded confirming
  call.** The rescue widens the existing `rescue_judged` sources from "cited in the answer" to "read in the turn, same document, unit and
  scope" and includes scope-check failures when a holder exists. The confirming call reuses the `_reread` pattern on the same verify model,
  capped by a new setting whose default is set in U5 from its judge_live flip-rate and cost measurement and frozen before U9 (0, off, if the
  gain does not justify the cost; the measurement draws on KTD13's budget); remove only when both verdicts fail; a disagreement keeps the grounded part
  with the mark. The confirmed verdict is the one cached. (Governs R6, R9, R11.)
- KTD10. **Unknown identity never removes.** `contexts.identifiers` reads parcel and block numbers in the notations the units use, and an
  identity quote that parses to nothing is noise, neither naming nor conflicting. (Governs R7.)
- KTD11. **M6/M7 behind a measurement gate.** First a zero-behaviour record of the repair gate and of unfollowed continuations (fixing the
  `repair_attempted` serialisation order); then, only if the record shows the gate or the cursor is the blocker on real turns: candidates from
  read (not only cited) sources in asked units, tools-off completion rounds measured against the verification allowance, and a notice of
  unfollowed cursors before the last reading step. Each is enabled only after a focused oracle run shows a gain with no new timeouts;
  otherwise deferred with the evidence. (Governs R15.)
- KTD12. **Evaluation units are enumerated by a script and labelled by graders** (server spans by meaning-changing kind, each item-gap line one
  unit; removal entries; reference needs); both arms of a conversation are graded by one grader in a blind pair; 20% of conversations are
  graded twice for agreement. (session-settled: user-directed — chosen over grader-defined counts.) (Governs R12, R13.)
- KTD13. **Pre-registered decision rule for the end-to-end stage** (written in U10 before the run, from stage 1-3 counts): primary = the
  enumerated target classes of the fixed mechanisms; minimal practical improvement = at least 25% of the control count of the primary class with
  a 95% paired cluster-bootstrap interval excluding zero; guards = required needs correct, wrong answers, empty or abstaining answers, failed
  turns and timeouts, wrong removals, cost and wall time per turn (+20% at most), none worse beyond its paired interval; two further guards against keeping more: unsupported or wrong statements that survive
  in the answer (graded on the surviving server and model spans) and true omissions the answer discloses in a gap line, both not worse beyond
  their paired interval; the 25% minimum and the 95% interval apply to each target class separately (gap lines, source-statement additions,
  wrong removals), the pooled count is supporting information only; the number of conversations and repetitions is fixed before stage 4, and the
  smallest paired difference that design can exclude from zero is computed from the round-22 per-conversation counts first — if it cannot decide a
  25% effect, conversations or repetitions are raised or the class is reported as undecidable before the run; product-model budget USD 15 for
  all round-23 measurement, 8 hours of run time. (Governs R13, R14.)

### High-Level Technical Design

*Directional; not an implementation specification.*

One coverage decision, now with a datum audit:

```text
draft -> verify (judge verdicts, problems) -> report.decision(final text)
            item_states: ledger outcome -> judge-support presence flip (existing)
                                        -> datum audit vs final model blocks (new, pure)
         -> component caps, gap_paragraph, item_gap lines, completeness   (all read the same states)
```

Evidence rule for a sentence about the source (KTD3):

```text
note wants to say "attribute A of datum D is <stated X | not stated | different>"
  read D's own parts: cell, row label, header rows, caption, table notes, governing note, sentence
  complete read?  no  -> "could not be confirmed" (or nothing)
                  yes -> attestation found -> say it ; none -> "not stated" allowed
  then: applies to D? (existing structure/judge binding) -> write inline, else uncertainty line
```

Removal path with rescue and confirmation (KTD9):

```text
judge verdict fails a value-bearing claim
  -> deterministic rescue: a source read in the turn (same doc, unit, scope) holds the numbers + label words in one sentence/row?
       yes -> keep, cite holder, "partly verified"
       no  -> confirming call (bounded, same model, widened evidence)
               both fail -> remove (smallest failing span when exact) ; disagree -> keep grounded part + mark
  cache the confirmed verdict
```

Measurement stages (KTD12, KTD13):

```text
stage 1  fixed draft + evidence + stored verdicts  (replay)          -> server edits: eligible/fired/fixed/new
stage 2  fixed draft + evidence, NEW judging       (judge_live, N=3) -> judge stability, what it was shown
stage 3  fixed sources, free drafting              (oracle runs)     -> omissions
stage 4  end to end, same conversations, paired    (only if 1-3 show the mechanisms fire and help)
```

### Assumptions
- The diagnosis records are representative of the remaining harm on the exposed sets; they are regression material only.
- Page-text word positions exist for most affected text-layer blocks; U4 verifies this read-only before relying on it.
- The verify model's latency stays near round 22's measurement (healthy calls p99 40 s); U5's confirming call is budgeted on it.

### Sequencing
U0 → (U8 measurement step, in parallel with U1-U3) → U1 → U2 → U3 → U6 → U5 → U4 → U7 → (U8 implementation step, only if enabled) → U9 →
U10 → U11 → U12. U1 and U2 may overlap after U0; U4 depends on its data probe; U8's measurement needs only U0 and decides enable/defer before
U7; U9 is the regression check before measurement.

---

## Implementation Units

### U0. Evaluation instrumentation and gate record, no behaviour change
- **Goal:** Make the round-23 evaluation able to decide, and record what the repair and reading gates did. The enumeration includes the two
  "keeping more" guard units of KTD13 (surviving unsupported statements, disclosed true omissions).
- **Requirements:** R12, R13, R14, R15. KTD11, KTD12.
- **Files:**
  - `backend/app/chat/engine.py` (record per round: attempt, report ok/settled, steps, time to the repair gate, answer step seconds,
    repairable problem kinds; serialise rounds after repairs are marked)
  - `backend/app/chat/verify.py` (`mark_repairs` also stamps requirement problems)
  - `backend/app/chat/tools.py` (record unfollowed continuation cursors per source at turn end)
  - `backend/tests/unit/test_timing.py`, `backend/tests/integration/test_chat_limits.py`
  - private tooling under `real_documents/tests/round23/` (span/removal/need enumeration, pair builder with enumerated checklists, paired
    cluster bootstrap, grader agreement) — never committed
- **Approach:** diagnostics fields only; the private scripts read run files and write per-run results with run id, code version, material and
  settings, never overwriting.
- **Test scenarios:**
  - A turn whose repair round is refused by the time gate records the gate inputs and the refusal; a turn that repairs records the attempt.
  - A requirement problem that a repair round addressed is marked repaired in the stored rounds.
  - An unfollowed read cursor appears in the turn's diagnostics; a followed one does not.
  - Integration: the final answer text, spans and status of a scripted turn are byte-identical before and after the unit.
- **Verification:** replay of the stored real drafts is byte-identical; the enumeration script counts the round-22 units and reports them per
  kind with no unit in two metrics.

### U1. Gap and coverage lines decided by datum (M1)
- **Goal:** No "read and not used", absence or removal line for a datum the final answer states, and honest wording for what happened.
- **Requirements:** R1, R2, R8, R19, R20. KTD3, KTD4, KTD5, KTD6.
- **Dependencies:** U0.
- **Files:**
  - `backend/app/chat/verify.py` (`item_states` datum audit; `validated_support` source-data test; removal kinds for non-judge removals)
  - `backend/app/chat/items.py` (`_attributed` table-citation inheritance for coverage; `_tells_apart` reads the lead-in; date-range values; label
    vs quote order; footnote marks and ordinals in `_not_headline`)
  - `backend/app/chat/coverage.py` (`reason_of`, `item_line`, `REMOVED_BECAUSE`, a "removed by verification" state, no absence claim from a part
    without its own words, uncertain-value vs limit order)
  - `backend/tests/unit/test_items.py`, `test_coverage.py`, `test_coverage_note.py`, new `backend/tests/unit/test_gap_audit.py`
- **Execution note:** start each sub-fix from a failing synthetic reproduction of its shape (a table cited only on its lead-in; a shared value in
  a lead-in; a date range; a caption as a judge part; a non-judge removal; a removed statement).
- **Approach:**
  1. Datum audit in `item_states`: an omitted or partial item flips to covered when one model block of the final text states all its headline
     tokens together with its row label or quote words; covered items keep their condition check.
  2. Coverage attribution inherits the table's lead-in/header citation for body rows with no ids (KTD5).
  3. Judge-support items only from structural spans (KTD6).
  4. Wording: removal with no kind → neutral "removed in verification"; an item whose giving unit was removed → that state, not "read and not
     used"; a judge part made only of the component's words → no absence sentence.
  5. Remove superseded narrow rules (KTD2): the inventory in the research dossier (`_SUM_DIGITS`/`_holds_value`, digit-class special cases,
     label heuristics, the default removal wording) — each only after the replay shows coverage.
- **Test scenarios:**
  - Happy: a table cited once on its lead-in states five rows → no item line; the same table with one row missing → exactly one line, for it.
  - Negative control: a row that cites plan B does not cover the plan-A item of the same value.
  - Twin items with a shared value in the lead-in and distinguishing words in the block below → covered; a block that names only the twin → not.
  - A value "from 1.1 to 31.12" stated as the range → covered; only the start date → partial.
  - A judge part that is a reader-written caption or carries only a footnote mark → no item; a part that is a source row → an item.
  - A sentence removed by the VAT check → "removed in verification" wording; a judge removal keeps its kind-specific reason.
  - A component part with no words of its own → no "not in the pages read" sentence.
  - Property: rename labels, reorder rows, add an unrelated paragraph, split one sentence into two → the same coverage outcome.
  - Integration: the fixed-point check holds on a scripted turn with a datum-audit flip.
- **Verification:** replay on the stored real drafts: of the traced wrong gap lines (eligible), how many no longer appear (fixed), and no new
  line or lost true line (new = 0 on the traced true omissions).

### U2. Server sentences about the source need evidence (M2)
- **Goal:** No server note claims the source is silent or different unless a complete read of the datum's own parts shows it.
- **Requirements:** R3, R4, R8, R19, R20. KTD3, KTD7.
- **Dependencies:** U0.
- **Files:**
  - `backend/app/chat/tools.py` (`attribute_clauses`, `_settle_meaning`, `_kind_state`, `_take_cell` header rows)
  - `backend/app/chat/verify.py` (`conditional_qualifier`, `plan_qualifier_edits`/`_qualifier_edits`, `QUALIFIER_UNSTATED`, `MSG_QUALIFIER_EDITED`,
    `_wrong_vat` source scale, VAT document note binding)
  - `backend/app/chat/meaning.py` (basis, period and kind vocabularies; negated period word; plot/block number not an amount)
  - `backend/app/chat/calc.py` (`display_matches` source scale use; `Value.unconfirmed`)
  - `backend/app/chat/compose.py` (register the source-statement kind as a meaning-changing addition if a new span kind is added)
  - `backend/tests/unit/test_meaning.py`, `test_value_certainty.py`, `test_calc.py`, `test_verify_edit_ladder.py`, `test_applies_to_datum.py`, new
    `backend/tests/unit/test_source_statements.py`
- **Execution note:** each vocabulary or scale fix starts from a failing reproduction and carries a negative control.
- **Approach:**
  1. A single "datum's own parts" reader (cell, row label, all header rows, caption, table notes, governing note, sentence) with a completeness
     flag; `attribute_clauses` and the qualifier-edit notes ask it before saying "not stated" or "different"; incomplete → "could not be
     confirmed" or nothing.
  2. Vocabulary/scale holes into the shared readers (R4), and the round-22 regression where a unit word reads as a stated kind.
  3. Calculation notes: a calculation is conditional only on an attribute its operation consumed that the read did not confirm (round 22),
     with wording from step 1; the result need not appear in the document.
  4. Remove superseded templates and word-list hacks per KTD2.
- **Test scenarios:**
  - A header "in thousands" bound across two header rows → the cell's unit is attested; no "unit not stated" note.
  - A clause with a plot number and one amount → the kind is confirmed; with two amounts → not.
  - "No." beside a number → not the levy kind; the word "tax" as a kind → still the levy kind.
  - An English "for the years ending" header → a period for its columns; "quarterly" → its own period; a negated period word → no period claim.
  - A thousands table restated in millions in the answer → the VAT words are backed; a different value → not.
  - A model-asserted kind with an incomplete read → "could not be confirmed", never "not stated in the source".
  - Property: the same value under varied header layouts and label wordings → the same attestation.
- **Verification:** replay: the traced false source-statement notes and the deterministic-reader cuts (eligible) no longer appear (fixed); no
  true qualifier is newly cut and no true note disappears (new = 0).

### U3. Context identity: unknown never removes (M5)
- **Goal:** No claim about the asked property is removed because its unit's identity could not be parsed.
- **Requirements:** R7, R9, R19, R20. KTD2, KTD10.
- **Dependencies:** U0.
- **Files:** `backend/app/chat/contexts.py` (`identifiers`, `_card_identity`, `noise_heading` for identity quotes),
  `backend/app/answering/entities.py` (reuse of the parcel notation readers), `backend/tests/unit/test_appraisal_context.py`,
  `backend/tests/integration/test_contexts_units.py`.
- **Approach:** read the parcel/block notations the units use; an identity quote that parses to nothing is noise; `_conflicts` sees only parsed
  identities. Remove the Hebrew-only asymmetry and ad-hoc noise cases (KTD2). Follow-up turns (R9, traced: a follow-up whose requirement text
  carried another property's label drove a removal): the identity a requirement names is the conversation's resolved property, not a label
  copied from the previous answer; an equal number or the same file alone never sets identity (R7).
- **Test scenarios:**
  - A unit labelled with a parcel number in a slash or foreign notation and the question naming that parcel → same; a different readable parcel →
    different (removal still happens).
  - A digit-only or garbled unit label → unknown: a neutral fact line, no removal.
  - Property: the same unit under varied label notations and positions → the same decision.
  - A follow-up turn whose previous answer named another property in a label → the follow-up's claims about the resolved property are not
    removed as "another property"; a claim that really cites the other property still is.
  - Two units of one file with an equal value and different readable parcels → the value is not moved between them.
- **Verification:** replay: the traced context removals (eligible) are kept (fixed); no claim about another property newly passes (new = 0 on
  the traced true removals).

### U4. Page-text column binding by stored word positions (M3)
- **Goal:** The agent and the judge read each number of a page-text row under its drawn column.
- **Requirements:** R5, R16, R19. KTD8.
- **Dependencies:** U0. A read-only data probe first.
- **Execution note:** before any code, probe read-only (through the replay harness's read-only connection), for the traced reversed-row
  cases: how many blocks carry `spans`; whether the header line and the number lines share a block or need a page-and-box join; whether the
  served window includes the header block. If this does not hold for most traced cases, stop or re-scope the unit and record the reason (and
  any `spans` backfill as the deferred data operation needing approval and a backup).
- **Files:** `backend/app/chat/reader.py` (select `spans` for page-text blocks on demand), `backend/app/chat/tools.py` (`_served_rows` or the
  page-text equivalent used by agent and judge; `_take_quote` maps a bound cell back to its stored word span), `backend/tests/support/replay.py`
  (`rebuild_source_text` serves block ranges through the same binding so replay and judge_live see what the agent and judge see), `backend/app/chat/rereader.py` (judge evidence), `backend/app/chat/meaning.py`
  (`_period_lines`/`column_period` consume the binding), `backend/app/chat/verify.py` (`JUDGE_VERSION`), `backend/tests/unit/test_tables.py`,
  `test_geometry.py`, new synthetic fixture via `backend/scripts/generate_fixtures.py --only positions`.
- **Approach:** for a run of header and number lines on one page, bind each number to the header column whose x-interval it falls in (display
  frame per the stored-boxes learning); serve "label: value" pairs when the logical order disagrees with the drawn order; no positions →
  served as stored, binding "unknown"; the judge's evidence for an unbound row carries a "column binding unknown" note, and a judge failure that
  rests on a column or cell mismatch for such a row is kept as a "not verified" mark, never a cut or removal. Each served cell maps back to its
  stored word span, so a quote of a bound row keeps its exact anchor, pages and governing note.
- **Test scenarios:**
  - A Latin-labelled numeric row on a Hebrew page stored reversed → served in drawn order with column labels to both agent and judge.
  - A right-to-left Hebrew table row → unchanged (negative control); a row with no spans → unchanged, binding unknown.
  - A rotated or CropBox-offset page → binding computed in the display frame.
  - take_value of a cell from a bound row → the exact stored word span, the same scale and unit as the unbound reading.
  - Scripted judge failing an unbound row for a column mismatch → the row stays with a mark; the same verdict on a bound row is handled as usual.
  - The replay's rebuilt text of a synthetic reversed row differs from the stored text and equals what the agent is served.
- **Verification:** replay plus judge_live on the traced reversed-row turns: cuts and dependency removals (eligible) no longer happen; judge
  agreement across three calls on those rows rises; no right-to-left row changes (new = 0).

### U5. Judge removals: rescue over read sources, bounded confirmation, smallest cut (M4)
- **Goal:** A correct value-bearing claim is not removed on one unstable verdict or for want of evidence the turn already read.
- **Requirements:** R6, R9, R11, R19. KTD9.
- **Dependencies:** U0, U1 (removal wording).
- **Files:** `backend/app/chat/verify.py` (`_rescue_sources`, `RESCUABLE_FAILURES` for scope cells, a confirm step after `_reread`, the cache
  write of the confirmed verdict, cut granularity), `backend/app/config.py` (a new cap setting for confirmations — not a model setting),
  `backend/app/chat/final_check.py` (the final check reuses the confirmed verdict), `backend/tests/unit/test_verify_reread.py`,
  `test_verify_edit_ladder.py`, `test_verify_cache.py`, `test_final_check.py`.
- **Execution note:** measure the confirming call's flip rate and cost with judge_live on the traced removal turns before choosing its default
  cap; the default may be 0 (off) if the measured gain does not justify the cost.
- **Approach:** deterministic rescue first (no model call); then the confirming call only for claims that would remove a value-bearing unit or
  cell, capped and time-gated; remove only if both fail; disagreement → keep the grounded part with the mark; cut the failing span when exact.
  Verdicts (first, confirmed, rescued) are cached and reused by the claim's datum and source place, never by its round position or number, so
  a repair round that renumbers or reorders claims and the final check keep the earlier decision (R9; round 22 fixed this for addition
  answers, this unit extends it to claim verdicts).
- **Test scenarios:**
  - A row the judge fails while another read source (same document, unit, scope) holds it → kept, holder cited, marked.
  - The holder is in another property's unit → still removed (negative control).
  - Scripted judge: first call fails, confirmation supports → kept and marked; both fail → removed; the cached verdict is the confirmed one.
  - The cap reached or too little time → the original behaviour, recorded.
  - A sentence with one wrong figure among correct ones → only the failing span is cut when exact.
  - A repair round that reorders the answer's sentences → each claim keeps its earlier verdict by datum and source place; a changed datum is
    judged again.
  - A stated date that a removal would take with its sentence → kept when the date's own claim is supported.
- **Verification:** replay and judge_live: of the traced wrong judge removals (eligible), how many are kept (fixed); of the traced right
  removals, none is kept (new = 0); confirmation calls and cost per turn reported.

### U6. Smaller deterministic causes (M8)
- **Goal:** take_value does not refuse a value it can read honestly; conditions next to a datum reach it; a final check that cannot remove a
  unit does not fail the whole turn. Each sub-fix is tied to traced final-arm failures of round 22: unit/basis refusals 3 events (4 rows) and a
  verbatim refusal on split words 1 event (2 rows); conditions with no carrier 6 events (7 rows); a final check that failed the turn 1 event
  (6 rows). A sub-fix without a reproducing traced case is moved to Deferred to Follow-Up Work.
- **Requirements:** R3, R8, R19, R20.
- **Dependencies:** U0.
- **Files:** `backend/app/chat/tools.py` (`_take_quote` whitespace-tolerant verbatim, `_settle_meaning` ranked unit evidence),
  `backend/app/chat/anchors.py` (`locate_quote`), `backend/app/chat/verify.py` (`SupportScope.verbatim`), `backend/app/chat/meaning.py`
  (`units_attested` reads a percentage only as the cell's unit, not from a name), `backend/app/chat/items.py` (`condition_spans` neighbourhood by
  structure; agent-declared conditions compared with `conditions.read`), `backend/app/chat/final_check.py` (withhold by index with a recorded
  cause instead of raising when `apply` removes nothing), tests in `test_take_value_resolve.py`, `test_tools_notes.py`, `test_meaning.py`,
  `test_conditions.py`, `test_final_check.py`.
- **Approach:** the three verbatim comparisons change together, mapping back to exact token positions; unit evidence ranked cell > all header
  rows > row label words > caption/notes, a conflict registers the value as ambiguous (existing flag) instead of refusing; condition carriers by
  structure; the final-check fallback keeps the fixed-point guarantee.
- **Test scenarios:**
  - A quote whose source has a word split by a line break → taken, with exact citation positions.
  - A currency cell whose row label contains a percentage → currency; a percent cell → percent.
  - A condition in the neighbouring note cell of the same row → carried; one in another row → not.
  - A final check whose failing unit is already gone → the answer is saved with the cause recorded, never an unchecked text.
- **Verification:** replay: the traced refusals and the failed turn (eligible) succeed (fixed) with exact citations; no new unit mismatch is
  accepted (new = 0).

### U7. Narrow-fix inventory and removal check
- **Goal:** Every narrow rule a U1–U6 mechanism superseded is gone from the active path, or kept with a recorded reason.
- **Requirements:** R20. KTD2.
- **Dependencies:** U1–U6.
- **Files:** the modules above; `backend/tests/unit/` as touched.
- **Approach:** walk the inventory; for each rule: replaced (no caller left), kept (still decides something the replacement does not, with the
  failing case), or deferred.
- **Test expectation:** covered by the owning units' tests and the replay; this unit adds none of its own.
- **Verification:** grep shows no caller of a removed rule; the replay on all stored real drafts shows harmed = 0 against the unit-by-unit
  baseline.

### U8. Multi-part needs and evidence next to what was read (M6, M7), behind a gate
- **Goal:** Decide from evidence whether the repair gate or unfollowed cursors cause the largest loss of required items, and fix only what is
  proven.
- **Requirements:** R14, R15. KTD11.
- **Dependencies:** measurement step: U0 (gate record) only; implementation step: the measurement's enable decision.
- **Files (if enabled):** `backend/app/chat/items.py` (`candidates` anchors from read sources in asked units), `backend/app/chat/verify.py`
  (`_check_requirements`), `backend/app/chat/engine.py` (tools-off completion rounds against the allowance end; an unfollowed-cursor notice
  before the last reading step), tests in `test_chat_engine.py`, `test_chat_limits.py`, `test_chat_completeness.py`.
- **Execution note:** two steps. (1) Measurement, right after U0: run the oracle stage (fixed sources) on the traced conversations with U0's
  gate and cursor records and count, per required need lost in round 22 to M6/M7, whether the gate refused a repair, a cursor was left, or the
  requirement was judged full. (2) Implementation, only for the channel the measurement shows is the blocker: enable a change only if a focused
  oracle run shows a gain in required needs with no new timeouts and within budget; otherwise record "deferred" with the measurement.
- **Test scenarios (if enabled):**
  - A requirement component whose source lists five members of which the answer states three → the repair round asks for the two by quote.
  - A tools-off completion round within the allowance → runs; one that would exceed it → recorded as refused.
  - An unfollowed cursor on a cited source → the notice lists it once before the last reading step.
- **Verification:** oracle-stage counts of required needs (eligible, fixed, new) and timeouts per arm.

### U9. Regression of the earlier rounds' guarantees
- **Goal:** Nothing the earlier rounds protected regresses.
- **Requirements:** R16, R17, R18.
- **Dependencies:** U1–U8.
- **Approach:** full host suite; the opt-in real-model tests of rounds 20-22 (a run where the model did not produce the needed state is "path
  not exercised"); permissions, office isolation, history and time-selector tests.
- **Test expectation:** none of its own — runs existing suites.
- **Verification:** suite green; real-model results reported as pass / fail / path not exercised.

### U10. Staged measurement on the final build
- **Goal:** Report what each fix did on real failures and whether the whole improves, with a decision the design can make.
- **Requirements:** R12, R13, R14, R15. KTD12, KTD13.
- **Dependencies:** U9.
- **Approach:**
  1. Write the decision rule (KTD13 numbers fixed from stages 1-3 eligible counts) in the private design file before stage 4, with the number of
     conversations and repetitions and the minimal decidable difference computed from round-22 per-conversation counts.
  2. Stage 1 replay, stage 2 judge_live, stage 3 oracle, each with eligible/fired/fixed/new, time and cost per fix.
  3. Stage 4 only if stages 1-3 show the target mechanisms fire and help: both arms (control = round-22 measured build; final = this round's
     build), the same conversations, concurrent, balanced; provider-failed conversations rerun in both arms with the same list; originals kept.
  4. Blind pair grading on enumerated units; agreement on 20%; paired cluster bootstrap.
- **Test expectation:** none — a measurement unit.
- **Verification:** results stored per run id, code version, material and settings; the report states improved / not worse / not decided per
  metric with intervals.

### U11. Browser check
- **Goal:** The new wordings and any page-text binding display correctly.
- **Requirements:** R2, R18.
- **Dependencies:** U9.
- **Approach:** extend the mocked e2e spec for the new gap and removal wordings and, if U4 shipped, a bound page-text row; a manual pass in
  the built-in browser on office B (synthetic).
- **Test expectation:** e2e scenarios for each new wording and the bound row; RTL and 375 px.
- **Verification:** e2e green; manual pass recorded.

### U12. Evaluation doc and learning
- **Goal:** The public record of round 23, generic only.
- **Requirements:** R14, R17.
- **Dependencies:** U10.
- **Approach:** "סבב 23" in `docs/evaluation/conversational-rag.md`: mechanisms and their share, fixes and removed rules, staged results with
  intervals, what did not improve or is undecided, the evaluation correction, time, cost, model split; one learning under `docs/solutions/`.
- **Test expectation:** none — documentation.
- **Verification:** a scan of committed files and the PR body finds no customer content.

---

## Verification Contract
- Each unit: failing synthetic reproduction first; property tests with negative controls (R19); focused tests via the host wrapper; replay on
  the stored real drafts with eligible/fired/fixed/new counts (R15).
- Before measurement: full host suite green; ruff clean; frontend typecheck and lint; the e2e spec.
- Measurement: KTD12/KTD13 design; results never overwritten; decisions "improved", "not worse", "not decided" with paired intervals.

## Definition of Done
- U0–U3, U5–U7 and U9–U12 complete; U4 complete, or stopped at its data probe with the reason and any `spans` backfill recorded as the
  deferred data operation; U8 ends with a recorded enable or defer decision backed by its measured required-need counts.
- Every fixed mechanism reports eligible, fired, fixed, new errors, time and cost on real failures.
- The end-to-end decision is reported by the pre-registered rule, including "not decided" when the interval includes zero.
- PR AriGabay/rag#2 updated (round-23 section, residuals); CI decided; no merge, no deploy.
