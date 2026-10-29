---
title: "Completeness belongs to the answer version, omitted list members need an item ledger, and a scope note must travel with its table"
date: 2026-10-19
category: logic-errors
module: backend chat completeness, repair round, table reads and verification (app/chat/verify.py, items.py, coverage.py, final_check.py, tools.py; app/platform/search.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "A component judged partial on the repaired answer, with a server-validated quote of the omitted part, still ends full"
  - "An answer gives three of four rates or eight of ten units the source lists, and the requirements check calls the component full"
  - "A note right after a table limits its figures to one approach; the answer shows a figure as the property's total value, or the qualifier the agent wrote is cut, or the sentence is removed"
  - "Items not located sit one to five blocks from a table the turn already read"
  - "A retrieval test fails about once in two hundred runs"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [completeness, requirement-votes, verdict-cache, answer-version, item-ledger, repair-round, scope-qualifier, table-context, retrieval-tiebreak, flaky-test, rag, chat]
---

# Completeness belongs to the answer version, omitted list members need an item ledger, and a scope note must travel with its table

## Problem
Round 16 gave the requirements judge the passages the turn had read, but on its final build the answers still left out what was read. Its three runs scored 9–10 read-and-omitted required items each (PR AriGabay/rag#2).

One answer was wrong in all three runs. It showed a figure from a sensitivity table as the property's total value, although the paragraph right after the table limits the table to one valuation approach.

## Root causes
1. **A stale vote decided the component.**
   - A component's status was the best status across every vote of the turn. A `full` vote carried from an unchanged sentence of an earlier answer version therefore beat a fresh `partial` vote on the repaired answer, even when the fresh vote carried a server-validated quote of the missing part.
   - Votes are also batch-relative: each judge batch scores every component over its own sentences only. So "the latest vote wins" would be just as wrong.
   - Completeness is a property of one whole answer version.
2. **Nothing recorded the members of a list.**
   - Most omissions were members of a list the source enumerates.
   - The frozen request had one coarse component per question, and no structure held which members the source gives and which the answer states. A lenient judge could call a partial list full.
3. **The scope note never reached the reader.** The qualifier was a separate paragraph block after the table, and a table read returned only the table. Downstream, each stage treated the qualifier as unsupported:
   - the judge cut the qualifier the agent did write;
   - the final check removed the sentence because it lacked the qualifier, instead of adding the qualifier.
4. **The answer was usually one to five blocks away.**
   - 14 of 20 items that were not located sat that close to a table the turn had already read.
   - The table read gave no pointer to what follows it, and most turns stopped by choice with budget left.
5. **The random tiebreak.**
   - Tied passages were ordered by a random chunk UUID.
   - A test whose premise was "the datum is not among the retrieved passages" passed only by luck (2 failures in 400 runs).

## What fixed it
- **Decide on the version.**
  - Every vote carries the digest of the answer version it judged (ordered sentence keys plus the read-evidence signature) and the component's evidence signature.
  - A carried vote counts only while its sentences and that evidence are unchanged. Otherwise its sentences are judged again in the same call, so no round is added.
  - The best status is taken only among the current version's batches and the admitted carries. A validated quote of a missing item then caps the component at partial.
  - Vote history is kept for diagnostics, apart from the current decision (`requirement_decisions`).
- **An item ledger inside the existing answer and check path.**
  - **The agent writes it first.** The agent's final structured answer carries, per frozen component, the items the read sources give: source, verbatim quote, value, condition and a state (given, cannot be verified with a reason, or does not apply with a reason).
    - The field is declared **before** the answer text in the strict schema. A ledger written after the prose only mirrors what the prose already left out.
  - **The server validates it never up.** It uses the same checks as the judge's support quotes, and keeps one ledger per turn across repair versions.
  - **Coverage is by attribution, never by presence.** A sentence covers an item only when it cites the item's source (or a value taken from it), states the value and the item's date, and, when needed to tell items apart, the condition.
  - **The existing requirements call checks the list.** It sees the ledger and names members the ledger missed, and the server validates them into the ledger.
  - **Omitted items drive the repair.** They cap the component and ask a tools-off completion from the source already read.
  - **What is still missing is named.** It gets a server-written line saying where it was read and that it was not used, never "not found".
- **The table read carries its context.**
  - The read includes the heading path and the paragraphs right before and after the table, up to the next structural boundary, the appraisal-unit edge or a size cap. Each part carries its block reference, plus a pointer to what follows.
  - Blocks shown whole are not sent again.
  - Block numbers inside those tags are kept out of the numbers a source "states".
- **A missing scope is added, never cut.**
  - The judge may report a value shown wider than its source's scope. The server attaches the scope phrase from the value's own table or section, with its citation, or writes a precise note that the scope was not verified.
  - The sentence is never removed for this.
- **Deterministic retrieval.**
  - Ties break on chunk order.
  - The flaky test now excludes its datum by design: the fillers carry the query words and the datum does not. It is not marked flaky.

## What the measurement showed (same references and scoring, three runs each)
- **Better than round 16:**
  - required items correct: 63.0 vs 60.3;
  - read-and-omitted: 7.3 vs 9.7;
  - wrong answers: 1 vs 3;
  - cost per full answer: lower.
- **Not reached:**
  - Read-and-omitted is back to the round-15 level (7), not halved.
  - Full answers averaged 18.3.
  - Runs varied by up to 8 required items.
- **On a new held-out set of multi-component questions:**
  - conditions were the weakest shape (22 of 40);
  - verification removals were the largest cause.
- Details: `docs/evaluation/conversational-rag.md`, round 17.

## Prevention
- **Judge completeness on the answer the user will see.** Never merge verdicts from different answer versions by "best", and never by "latest" either when verdicts are batch-relative. Bind each verdict to the version and the evidence it saw.
- **A structure filled by the same model that omits moves the number little.**
  - Ordering the ledger before the prose and checking it against the read passages helps.
  - Measure per question shape and per run.
  - Expect variance larger than the change.
- **Bind context by structure, and test the real layout.**
  - A paragraph between two tables is not attached to either, which prevents applying another table's note.
  - The same guard turned the real case (a note that follows the table it scopes, with another table after it) into a "scope not verified" note in one run. Position alone cannot tell which table a between-tables note scopes.
- **A server-written qualifier needs the same scope rules as a model-written one.** The existing "as written in the source" annotation path attached a period word next to a count of months on the held-out set.
- **Fix a flaky test's premise.** Reproduce the cause first (hundreds of repetitions), then make the test hold by design.

## Related Issues
- `docs/solutions/logic-errors/read-evidence-never-reached-the-answer-and-units-need-agreeing-page-structure.md` (round 16): the read passages in the judge call, validated support quotes, "read and not used" and the repair that keeps verified claims, which this round builds on.
- `docs/solutions/logic-errors/datum-context-is-lost-between-extraction-cache-and-display.md` (round 14): "a cache stores a result, not a verdict" and "not read is not 'not stated'", which the version binding and the table context extend.
