---
title: "Offering the omitted siblings to the verifier did not pay off: the gap is set by which requests count as a set and by the time left for a repair, not by what the verifier sees"
date: 2026-10-29
category: logic-errors
module: "backend chat completeness - requirements judge, item ledger and repair (app/chat/items.py, verify.py, coverage.py, engine.py) and the private offline experiment E1 on fixed drafts (round 25; code reverted)"
problem_type: logic_error
component: service_layer
symptoms:
  - "A required item is missing from the answer although the turn read the passage that states it: 100 of 202 missing required items in the last end-to-end measurement were read but never written into the draft"
  - "The requirements trace says 'full' for the component in 65% of the read-but-not-drafted items, so no repair is asked"
  - "The item ledger lists the omitted part in only 25% of those items, so neither a gap line nor the repair round can name it"
  - "Offering the sibling content of the used block to the requirements judge raised reach of the omitted part only from 20.0% to 25.5%"
root_cause: logic_error
resolution_type: documentation_update
severity: medium
tags: [evaluation, negative-result, pre-registration, completeness, requirements-judge, item-ledger, read-not-used, sibling-block, repair-time, set-request]
---

# Offering the omitted siblings to the verifier did not pay off: the gap is set by which requests count as a set and by the time left for a repair, not by what the verifier sees

## Problem
After the round-23 closure measurement of PR AriGabay/rag#2 (unmerged at the time of writing), required items were still missing from answers that had read the passage holding them. The round-25 goal was to return more complete information without passing more wrong information, by detecting an omitted part at verification and completing the answer.

## What the diagnosis found
These figures come from traces of the last end-to-end measurement (both arms), every missing required item graded against the source. There were 202 missing required items.
- **Most are lost before the verifier sees anything.** Storage 0, never located 36, located but needed context not read 33, read but not in the draft 100, written then lost in verification or repair 33, wrongly graded 0.
- **Completeness signals do not check the text.** In the 100 read-but-not-drafted cases the requirement trace said "full" for 65%. The item ledger listed the omitted part in 25%, and marked it covered in 76% of those it listed. Gap lines named the item in 6%.
- **The verifier cannot flag what it is not shown.** The requirements call is offered only the siblings of what the answer draws on (`candidates`, `backend/app/chat/items.py:1092`, bounded by `CANDIDATES_PER_SOURCE` and `CANDIDATE_SOURCES` at `items.py:908-909`). A component caps only on an item it asked for; an item the server built from a cited span never caps it (`is_server_item`, `backend/app/chat/verify.py:11856`, skipped in `_resolve`, `verify.py:3165`).

## What was tried and measured (pre-registered E1, offline)
E1 replayed the fixed drafts of the round-23 closure measurement, final arm, on the 55 read-but-not-drafted required items. Negative controls were 30 complete turns and the narrow-question stratum. The change was built and then reverted within PR AriGabay/rag#2.
- **Offering siblings to the existing requirements call.** The block the answer used, its siblings in the same source, set requests only. An assignment counted only from a vote that saw the component's surviving unit. The same block across S# was matched by version, reading and appraisal unit. A sibling repeating a stated value was judged by its own label. Reach of the omitted part rose from 20.0% to 25.5% of the items. The rule needed at least +30 points and at least 50%.
- **An item with its own condition counts only with the condition.** It added 0. All 112 missing condition clauses were already stated by the lead-in or the header.
- **A value-bearing item counts as covered only in a block citing its own source.** It added 0. The own-attribution rule changed the reason on one item.
- **Completing the answer by insertion.** It would have fitted the recorded repair window in 4 of 14 turns (the window is `seconds_to_deadline` of at least 60 s: a 20 s step plus a 40 s judge; the rule needed 30%). A full rewrite fitted 5 of 14. The gate itself is `REPAIR_MIN_SECONDS` and `REPAIR_TIME_FACTOR` (`backend/app/chat/engine.py:394-396`), applied at `engine.py:1205` as `deadline - max(REPAIR_MIN_SECONDS, REPAIR_TIME_FACTOR * answer_seconds)`.
- **Controls were clean.** 0 of 100 negative-control items changed. No sibling was offered on single-datum questions. With the settings off, the 114 replayed drafts were identical to the previous code.
- **Two attempts were used.** The second fixed a replay defect named before the rerun: search-hit sources lacked `reading_id`. The reach figure above is from the second attempt.
- **Not run.** E2 (live) and the completion step, so no spend. The change was removed, not left behind a disabled setting (reverted in PR AriGabay/rag#2).

## Why it missed
- **21 of 41 misses were requests the set cue did not classify as sets.** That cue is the gate that keeps narrow questions from being expanded (`_SET_CUES` in `backend/app/chat/request.py`, removed with the revert). Loosening it would expand single-datum questions, which the controls showed the change otherwise avoided.
- **10 more were blocks that offered other spans but not the omitted one.**
- **The remaining misses of the old kind were mostly sources that offered no candidate at all.**
- **The repair window is too short.** Even a perfect detection reaches a completed answer in a minority of turns.

## Guidance
- **Measure reach and time-window reachability offline on recorded turns before building detection or completion.** Both were cheap here, and both decided the outcome. The live experiment was never needed.
- **A detection that cannot reach the omitted part without expanding narrow questions does not pay.** The verifier is not the constraint.
- **The replay harness must reproduce the production candidate pool.** It matched the recorded pool in 42 of 47 turns that recorded one. Check that match before reading any reach figure from a replay.
- **A change that misses its pre-registered thresholds is removed.** Keep the records of the measurement, not the code.
- **Next candidates, from the same diagnosis.** How the agent decides at draft time that a request asks for a set (not at verify time), and how the time budget is split between reading and repair.
