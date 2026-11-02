---
title: "A second judge reading keeps a failed claim only on explicit support of that claim; a split into true parts can still be a wrong pairing"
date: 2026-10-29
category: logic-errors
module: backend chat verification - the bounded confirming reading (app/chat/verify.py confirm_removals, _read_second, _confirm_candidates) and the private judge-reliability experiment
problem_type: logic_error
component: service_layer
symptoms:
  - "A claim the first judge reading failed is kept after a second reading although no claim of the second reading supports it (the sentence verdict decided)"
  - "A claim the second reading answers partial is kept whole"
  - "A rule that confirms a split of the claim into supported sub-claims keeps wholly wrong claims whose label/value pairing is wrong"
  - "Re-running the same first reading keeps most defective claims once in three calls"
  - "A measurement shows the confirming call ran but cannot say how it ended"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [verification, judge, second-reading, confirm, claim-level, split, pairing, attribution, instability, evidence-widening, answer-shape, diagnostics, experiment, recorded-outputs, rescoring, rag, chat]
---

# A second judge reading keeps a failed claim only on explicit support of that claim; a split into true parts can still be a wrong pairing

## Problem
Round 23 of PR AriGabay/rag#2 (unmerged at the time of writing) added a bounded second reading by the same verify model before a value-bearing claim is removed. Its first version kept a claim whenever the second reading did not fail it. Three behaviours slipped through:
- the sentence-level verdict (`partial` or `supported`) decided when the second reading returned no claim for the span;
- a claim returned as `partial` was kept whole;
- a cut of a number (as opposed to a removal) got no second reading at all.

## What the live experiment showed
Drafts and evidence were fixed: 51 claims whose truth was checked against the source, 23 of them supported claims the first reading failed and 15 defective. Only the reading varied, 3 calls per case. The policies were then applied offline to the same recorded outputs.
- **The first reading is unstable in both directions.** Re-run three times, it rescued 11 of 23 supported claims and kept 9 of 15 defective claims in at least one call. Agreement between two calls of the same model is not evidence of correctness.
- **The old policy kept 12 of 15 defective claims** (it rescued 17 of 23). The claim-level policy (keep only on an explicit `supported` claim for that span with shown evidence) kept 1 or 2 of 15 and rescued 7 to 10 of 23. The pre-registered rule allowed it.
- **About half the time the judge split the claim into sub-claims.** A rule that confirmed a split whenever every number, qualifier and content word stood in a supported sub-claim rescued 1 or 2 more cases. But it newly kept two wholly wrong claims: each sub-claim was true on its own, and the pairing (which label goes with which value, which column a figure belongs to) was wrong. That rule was rejected. Correctness of a claim with several values lies in the combination, and no per-part check can certify it.
- **Wider evidence did not explain the wider arm's gains.** The two extra rescues got no new anchor from the extra sources; the judge simply answered with one claim instead of splitting it. The one case where the extra sources did add the claim's numbers (from another document) led to keeping a defective claim.

## Solution
- **Keep only on explicit support.** `_read_second` keeps a claim only when the second reading returns a claim for that span (positional, on token boundaries) that is `supported` with shown evidence. A `partial` claim keeps only its supported part, only when every kept number stands in a supported sub-claim and the cut separates no number from its condition, negation, period, unit or locator. The following never keep anything: a sentence verdict alone, a missing claim, a split without failing parts, invalid output, a timeout or an error.
- **Protect cuts too.** A cut of a number gets the rescue and the second reading like a removal.
- **Don't lose the claim's own evidence.** The second reading also sees a same-document, same-version, same-unit source that supplies a number the claim's own sources lack (`_confirm_candidates`). It is never handed everything the first batch saw.
- **Record the outcomes.** The outcome per claim (flipped, partly kept, both failed, not confirmed, cap, no time) and any timeout reuse go into the turn diagnostics (`repair_gate`). A measurement can only report a path it recorded.

## Prevention
- **Measure the judge on fixed material.** Use fixed drafts and fixed evidence, and record what each call was shown. Apply competing policies to the same recorded outputs, and validate the offline re-scoring tool against the recorded decisions (it reproduced 105 of 105 per arm here) before trusting a re-score.
- **Separate the effects.** Policy, the extra reading and wider evidence are three different effects; keep them apart.
- **Accept the measured cost of the strict rule.** It still loses correct claims when two readings of the same model disagree; the targeted live check found one such claim. It also removes a whole table row when one cell is wrong, because a cut never applies to table rows. These are documented trade-offs, not reasons to loosen the rule.
