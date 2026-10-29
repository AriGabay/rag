---
title: "Meaning was lost in verification, not in the model: locate the stage on fixed sources, check a condition as part of its item, and fix each removal at its mechanism"
date: 2026-10-20
category: logic-errors
module: backend chat verification, item ledger and server text (app/chat/conditions.py, items.py, coverage.py, meaning.py, verify.py, tools.py, contexts.py, final_check.py; app/answering/verify.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "A single item's condition ('only for an approved plan', 'excluding the parking floors') is missing from the answer, and no check notices because no other item shares its value"
  - "A correct statement in the draft is removed by verification: a monthly rent whose period word the PDF text layer split, a list of years joined by commas, an amount in millions against a full-digit source with the same VAT status"
  - "An answer about an appraisal's calculation pages, or about the second report in a file, is removed as 'another property'"
  - "The server writes 'read and not used' next to an item the answer gives, or a 'per month' note next to a count of months"
  - "A stronger answering or judging model costs 5-20 times more and the answers do not improve"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [conditions, verification, false-removal, controlled-experiment, stage-grading, model-comparison, candidate-items, server-text, period, vat, number-forms, table-scope, rag, chat]
---

# Meaning was lost in verification, not in the model: locate the stage on fixed sources, check a condition as part of its item, and fix each removal at its mechanism

## Problem
After round 17 (PR AriGabay/rag#2), conditions were the weakest question shape on a multi-component held-out set (22 of 40), and verification removals were the largest cause of loss. It was not known where the loss happened: in the sources the agent read, in the model, in how the answer was built, or in verification. Comparing the agent's item ledger with its answer cannot find an item the agent dropped from both.

## How the stage was located
- **A controlled experiment.**
  - 9 conversations (14 turns, 51 required items) taken from the failing cases.
  - The sources were fixed by injecting reads of the evidence pages.
  - The draft written before verification was captured for each turn.
  - Every required item was graded blind at each stage: in the sources, in the ledger, in the draft, in the last draft, in the final answer, and where it was lost.
- **Three arms, two runs each:**
  - the current model;
  - a stronger model building the answer;
  - a stronger model as the judge.
- **What it showed:**
  - The draft held about 80% of the required items in every arm. The stronger agent did not raise that, at about 20 times the cost.
  - The stronger judge did not stop removals, at 5-7 times the cost.
  - 7-10 correct items per run were lost between the draft and the final answer, and most dropped conditions were lost there.
  - Server-written "read and not used" lines were wrong in 59 of 81 cases.
  - 39 of 58 items that were read and never written sat inside a span the answer already cited.
- **The removals had mechanisms, not model errors.** Of 13 removals of required items, 12 were wrong and 1 was mixed. Each traced to a specific reader:
  - a period word split by the PDF text layer;
  - a trailing comma that made a year an amount;
  - a comma-joined list of years read as one number;
  - one header chosen for a source holding several tables;
  - a header row that lost its empty edge cells;
  - VAT compared as digit strings ("16.07 million" against "16,070,000");
  - an appraisal context cut where the printed page numbering restarted;
  - a file title standing for a second report in the same file;
  - a removal cascading to a dependent claim with its own support;
  - a bold label classified as unclassifiable.

## What fixed it
- **Each removal fixed at its mechanism, with a test that the protection still removes.**
  - Durations, counts and periodic rates are read apart.
  - Numbers are compared by value across forms.
  - VAT attaches only to amounts.
  - A context with no identity of its own continues the one before it; one with its own address, parcel or name stays another property.
  - All of it is read-time only, with no re-ingestion.
- **A condition is part of its item's meaning, checked separately from telling similar items apart.**
  - Every covered item whose own span (row, row note, attributed paragraph, heading) carries a material condition gets a condition outcome, even when it is the only one of its kind.
  - Closed operator classes (negation, "only", "except", "subject to", thresholds) are decided by the server. A word of the same class counts as a rephrasing.
  - Everything else rides the existing requirements call. Its outcome counts only with a quote inside the item's own span.
  - A missing condition is completed from the source, or attached next to the value verbatim with its citation. Only a judge-confirmed contradiction can remove a statement.
  - When both the operator class and the operand are absent from the sentence, the condition is missing without the judge.
  - A cited span that carries a condition becomes a server-built item, so turns where the agent declared no ledger are checked too.
- **Candidate items come from the structure of what the answer cites.**
  - Candidates are the other rows of a cited table, the other members of a list, and the value-bearing sentences of a cited passage.
  - The existing call assigns each candidate to a request component, or marks it not asked.
  - The server keeps an assignment only when the request words stand in the candidate's own text.
- **A table qualifier needs evidence.** It is attributed only with adjacency, a reference word ("the table above") and the heading's scope, never by position alone.
- **Server lines are built from verified material.**
  - Items are named from the source.
  - The agent's reason is shown only when it is grounded.
  - Removal notes state their cause.
- No model changed and no call was added. The judge contract changed inside the existing calls.

## What the measurement showed
- **Focused cases, sources fixed, against a round-17 control run in the same session.** The grading was blinded, and a third grader cross-checked it.

  | Measure | Final build | Round-17 control |
  |---|---|---|
  | Required items correct in the final answer | 38 / 38 | 34 / 32 |
  | Written then removed | 4 / 4 | 5 / 8 |
  | Conditions dropped or distorted | 1 / 2 | 10 / 11 |
  | Wrong server lines | 4 / 4 | 10 / 10 |

- **Full sets: a smaller effect.**
  - The diagnostic set averaged 18.3 full answers, the same as round 17, with three provider timeouts.
  - The previous held-out set rose from 70 to 74 of 93 required items.
  - The new held-out set scored 79 of 100, with 93% of the conditions kept among the items the answer gives.
- **Still wrong:**
  - "another property" judgments on one appraisal's own pages;
  - an annual column header in some real tables;
  - a decimal comma;
  - false "read and not used" lines.
- Details: `docs/evaluation/conversational-rag.md`, round 18.

## Prevention
- **Locate the stage before changing the model or the code.**
  - Fix the sources, capture the draft before verification, and grade every required item at every stage.
  - A stronger model fixes neither a deterministic verifier nor the server's own text.
- **Measure against a control run in the same session.**
  - Run-to-run variance (15-22 full answers on one set) exceeds most effects, so a single earlier run is no baseline.
  - Assign graders so that no grader sees only one build. This round found that confound and cross-checked it.
- **A check that cannot decide deterministically may only hint, never remove.**
  - A polarity reading alone would have removed correct sentences.
  - A generic heading ("valuation method") is no condition.
- **Every protection a fix touches gets a test that it still removes**: another property, an opposite VAT status, an invented number.
- **Server text is held to the same standard as model text**: a line the server writes is checked or built from the source.

## Related Issues
- `docs/solutions/logic-errors/completeness-belongs-to-the-answer-version-and-omissions-need-an-item-ledger.md` (round 17): the item ledger, coverage by attribution and scope notes that this round extends with conditions and candidates.
- `docs/solutions/logic-errors/read-evidence-never-reached-the-answer-and-units-need-agreeing-page-structure.md` (round 16): validated support and "read and not used, never not found".
- `docs/solutions/logic-errors/datum-identity-needs-a-source-grounded-component-decided-pairwise.md` (round 13): a failed check cuts only its clause, and a PDF line break inside a VAT phrase reads as a space.
