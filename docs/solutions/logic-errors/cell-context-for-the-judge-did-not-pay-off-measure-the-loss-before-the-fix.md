---
title: "Giving the judge a table cell's header context did not pay off: most lost answer content is lost before the draft, and repair paths rarely apply"
date: 2026-10-29
category: logic-errors
module: backend chat answer pipeline - value registration (app/chat/tools.py take_value), judge evidence (app/chat/verify.py _source_parts), final check (app/chat/final_check.py) - and the private evaluation method
problem_type: logic_error
component: service_layer
symptoms:
  - "Correct required items go missing although the turn read the passage that holds them"
  - "A supported claim citing a table value is cut; the judge was shown only the cell, not its column header or the table title"
  - "A table value is registered with no column header (most text-stored tables)"
  - "A final check removes a whole sentence after a partial re-judgement"
  - "A clearly printed table total is marked as an uncertain reading"
root_cause: logic_error
resolution_type: documentation_update
severity: medium
tags: [evaluation, negative-result, diagnosis, table-cell, column-header, value-registration, judge-evidence, final-check, cut, reading-status, letter-spacing, completeness, read-not-used, pre-registration, rag, chat]
---

# Giving the judge a table cell's header context did not pay off: most lost answer content is lost before the draft, and repair paths rarely apply

## Problem
After round 23 of PR AriGabay/rag#2 (unmerged at the time of writing), correct required items held steady at 668 in both arms. Wrong required items fell from 14 to 3, but missing ones rose from 78 to 93. The goal of the follow-up was to return more correct and complete information without passing more wrong information.

## What the diagnosis found
These figures come from traces of the stored end-to-end runs, every failure graded against the source.
- **Most missing required items are lost before the answer is written.** Of 30 traced, 19 were never retrieved or were read and not used. Both arms lose these together. The two arms differ only in verification and repair, where the control delivered the 7 items the final arm lost.
- **Completeness signals do not check the text.** In 10 of 13 read-but-not-used cases the requirement trace said the component was complete, and an item ledger entry said "covered", although the item was not in the final text.
- **Most wrong cuts of table claims came after the judge saw a value without its header.** In 6 of 7, the judge saw only the cell quote of a registered value, without the column header or table title that held the support.
- **The column header was usually missing at registration.** It was absent on 67 of 92 table values, because text-stored tables carry no structured header.
- **A false "uncertain reading" mark came from ingestion.** It came from the letter-spacing detector, which mistakes equal-pitch single-digit table columns for letter-spaced text.

## What was tried and measured (pre-registered, fixed drafts and sources)
- **Recording the cell's context at registration (header path, row label, caption, scale note, footnotes).** Re-registering all 3,221 recorded calls raised resolved column headers from 335 to 806 of 1,096 cell values. Statuses, values and units were unchanged. In the final version no changed place had a wrong context; the graded sample of 60 had none wrong. Two wrong shapes found on the way were fixed: a standalone header spread over its neighbour's columns, and a single identifier row read as a year header.
- **Showing that context to the judge.** On the same drafts and the same call packing, the judge kept 4 of 5 supported claims in both arms. The new context rescued the case whose condition sat in the caption, but lost another to call-to-call variance. It also newly kept a wrong row-label swap in 2 of 3 calls. The pre-registered gain of +2 was not reached, so neither change was kept.
- **Cutting a failing clause in the final check instead of removing the sentence.** Stored verdicts keep no claim spans, so a replay cannot exercise this path. A live re-judgement of the 11 sentences where it applies fixed none of the 5 eligible ones. The judge now supported three of them, one partial had no spans, and one was removed by a deterministic number gate. The cut fired once in 17 attempts. Not kept.
- **Confirming a letter-spacing-flagged cell by an identical second reading at the same position.** It fired on 0 of 58 flagged calls, because the stored second readings were partial or differed. Not kept. The cause is at ingestion; fixing it means re-reading stored documents, which needs the owner's approval.

## Guidance
- **Trace the loss to a stage before choosing a fix.** Here most of the loss was before the draft, which no verification change can recover.
- **Check that a path is reachable.** A fix whose path real data does not reach (stored verdicts without spans, partial second readings) is not a fix. Check reachability on recorded data before building on it.
- **Don't add an unused representation.** A structured representation the active path does not consume is not an improvement, however correct it is. The patches for all four changes are kept privately for a later round that has a consumer for them.
- **Next candidates, from the same diagnosis.** Make "covered" and "complete" check that the item's datum is in the final text. Fix the letter-spacing detector at ingestion, with approval for the re-read.
