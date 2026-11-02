---
title: "A server addition needs a bound datum and place, and its fixes come from replaying real drafts"
date: 2026-10-22
category: logic-errors
module: backend chat verification, composition, meaning and table serving (app/chat/verify.py, compose.py, coverage.py, items.py, meaning.py, engine.py, calc.py; app/extraction/tables.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "A condition note of one list item is written onto its sibling item"
  - "A capitalised total gets a period tag such as 'per month' taken from its column header or label, or a basis word is attached to a rate"
  - "A 'partly verified' mark or qualifier lands on the last number of a sentence, or on a number that only happens to appear in the same source"
  - "A removal note says 'another property' although the check decided something else (scope, partial support, a source not read)"
  - "A 'read and not used' line names an item the answer did state, in other words, under another heading or as a figure"
  - "A source the verifier could not read is reported as 'not stated in the source', and its datum is removed"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [server-addition, binding, datum, uncertainty-line, qualifier, period, gap-line, removal-note, table-order, repair-loop, replay, controlled-measurement, rag, chat]
---

# A server addition needs a bound datum and place, and its fixes come from replaying real drafts

## Problem
After round 20 of PR AriGabay/rag#2, many of the wrong statements left in answers were written by the server, not by the model. The server wrote:
- condition notes onto the wrong item;
- periods and bases onto the wrong number;
- "partly verified" marks on whatever number came last in a sentence;
- generic removal notes;
- "read and not used" lines for items the answer already stated.

A focused real-model A/B (29 turns, three repetitions per arm) counted 45 to 65 wrong server additions per 87 turns in the control build. Round 21 fixed this in the same PR, which is not yet merged.

## What fixed it, and the part the code does not say

### Unbound means uncertainty, never the last number
- **Binding.** Every server-written span in the final answer carries what it belongs to: the claim ids and the datum (a ledger item, a V# or a C#). See `ServerSpan.claim_ids` and `ServerSpan.datum` in `compose.py`. The span is placed at that datum's own tokens, taken from its value tokens, fractions and token places (`verify.datum_anchor`).
- **No unique datum and place.** Nothing is written inline, and the datum itself stays in the answer. One focused uncertainty line names up to three unbound data and counts the rest (`verify.Unbound`, `uncertainty_line`).
- **Forbidden anchors:**
  - the last number in the sentence;
  - word proximity;
  - a number that also appears in the source;
  - one member of an enumeration (`_list_members_only`).
- **The one sentence-level binding allowed.** A claim's clause may be bound only when all of its evidence carries a single scope (`_uniform_scope`).
- **Why the code alone does not say this.** Each forbidden anchor was the old, working-looking implementation. A future "simple fix" that anchors on the nearest number will reproduce the round-20 symptoms.

### Meaning belongs to the datum that owns it
These are ownership rules (`meaning.py`), and each comes from a different real shape:
- A capitalised total row takes no period from its header or label (`capitalised_label`, `_recurring_row`).
- A row's own period beats its column header's period.
- In a column layout, a period in the header belongs to the column (`column_period`), not to the row numbers.
- English period words count as period words.
- A basis is not attached to a rate.
- Number-form items are different data: seven floors is not seven percent.
- A read that failed or was never made is `NOT_READ`, never "not stated". Unknown never removes a datum.

### A note says what happened to that claim
- Each removal, conditional, coverage or request note states what actually happened to the specific claim. It never says "another property" unless the attribution check decided exactly that.
- Tidying keeps a repeated line when the copies sit under different headings (`coverage.tidy_cuts`).

### A gap line is proven against the final answer
- A "read and not used" line is written only when the item really is missing from the final text. The checks: a quote found in the answer's blocks (`items.quote_in_blocks`), the same table row (`_row_key`), abbreviation tokens, figures stated, a heading-only item, a dated twin.
- Different wording does not prove an omission, and a cited source does not prove coverage.

### Audit after the last edit; verdict keys hold the defining lead
- Server additions are audited inside composition, after the last edit (`coverage.audit_server_spans`, `compose.Doc.audit`). An addition whose datum was moved or cut is caught there.
- A verdict is reused only while the unit text, its evidence and its defining lead line (unit, period, VAT, basis, scope; `verify.defining_line`) are unchanged.

### Tables stored in reversed cell order are paired at serve time
- **When a table counts as reversed.** Only when all three hold: the cell boxes descend, the cells hold no Hebrew, and there is a left-to-right cue in drawn order (`extraction.tables.stored_reversed`). A plain reversal rule broke right-to-left tables elsewhere.
- **What is served.** The table goes in drawn order to both the agent and the verifier (`serve_structure`). Each cell carries its column label and its stored column index, because span identity and locators are keyed by the stored index.
- **What is not touched.** Nothing stored is rewritten. Re-reading stored pages needs the user's approval.

### The repair loop keeps what verification supported
- A number the verification supported survives the rewrite, and the answer states what was not verified.
- A "multiple values" partial only marks the claim; a units defect still repairs it.
- A restored VAT basis is bound at its number and never written twice.

### A context is not a plot
- A round-21 shortcut decided that values from the same appraisal context are the same subject in a calculation. The code review found that this silently combined two plots of one appraisal.
- Inside one context, subject designators still decide: digits or single letters, but not years (`calc._subject_designators`).

## The method that found the fixes
- **Capture.** Every verified draft and its sources' locators are kept in the message diagnostics (`engine.draft_record`). The diagnostics stay behind the same permission as the documents those drafts cite.
- **Replay before measuring.** The server's deterministic path then runs privately on the same real drafts, with no model call, and each candidate fix is counted as harmed and fixed claims.
- **Real drafts first, synthetic tests second.** Three replay passes each found new harms from earlier fixes that the synthetic tests did not show: list members, far operators, sentences wrapped across lines, dashes, and uncited named supports. The fixes were found on real drafts, and synthetic tests were written afterwards to pin the generic shape.
- **Gate before full measurement.** Only then a focused real-model A/B ran: the same 29 turns, three repetitions per arm, a control built at the same time, and blind graders. It gated the full measurement.

## Measured
Focused A/B, final build against the same-time control, 87 turns each, on 2026-10-22:

| Measure | Control | Final |
|---|---|---|
| Wrong server additions | 65 | 30 |
| Condition or unit on a neighbouring item | 7 | 0 |
| Supported information deleted | 27 | 11 |
| Wrong removals of required items | 21 | 13 |
| Wrong gap lines | 27 | 15 |
| Full answers | 37 | 39 |
| Required items correct | 282 | 290 |
| Missing caveats | 41 | 41 |
| Failed turns | 0 | 0 |

- Cost per run was about the same.
- Wrong answers were 5 in the control and 7 in the final build, and both new ones came from the model.

What is still wrong:
- the model swapping columns on page text, which table pairing does not reach;
- removals caused by the judge;
- missing caveats;
- the model's own wrong answers.

All evaluation sets are regression; no independent material exists. The full measurement is recorded in `docs/evaluation/conversational-rag.md`, round 21.

## Prevention
Before adding any text the server writes into an answer, name the datum it belongs to and the exact place it goes. If either cannot be decided from evidence, write the uncertainty line instead. Then replay the change on stored real drafts and count harmed claims before running the model.

## Related Issues
- `docs/solutions/logic-errors/overlap-is-not-identity-keep-the-supported-cell-and-attribute-below-the-property.md` (round 20, whose "still wrong" list this round took on)
- `docs/solutions/logic-errors/meaning-was-lost-in-verification-not-the-model-and-conditions-need-their-own-check.md`
- `docs/solutions/logic-errors/completeness-belongs-to-the-answer-version-and-omissions-need-an-item-ledger.md`
