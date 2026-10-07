---
title: Finding the property a Hebrew follow-up names - numbers, prefixes and the resolver's own quotes
date: 2026-10-07
category: logic-errors
module: backend chat follow-up resolution (app/chat/entities.py, app/chat/resolve.py)
problem_type: logic_error
component: service_layer
symptoms:
  - "A follow-up naming another house on the same street is answered from the property in focus"
  - "A follow-up about a comparable named inside the focus report gets a 'not found' clarification"
  - "A follow-up with a pronoun or generic word (\"בזה\", \"שלה\") lands on another report or asks a needless clarification"
  - "A floor, year or duration number in a follow-up switches the conversation to another property"
  - "After a clarification reply, the previous property's unit or address stays in the request"
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [hebrew, entity-resolution, follow-up, house-number, prefix-stripping, clarification, rag, chat]
---

# Finding the property a Hebrew follow-up names - numbers, prefixes and the resolver's own quotes

## Problem
When a follow-up changes the property ("טעיתי, התכוונתי ל..."), the server looks up the documents the user's words
name, under the user's permissions (`entities.lookup` over `documents_matching`), and drops the previous turn's
documents. Each rule below was needed only after a real-model run showed the lookup going wrong in a way unit
tests built from the plan did not predict (PR #2, round 4).

## Symptoms
- "הנרקיס 4" resolved to the focus report on "הנרקיס 14".
- A question about "<street> 30", a comparable inside the report on "<street> 26", got a "not found" clarification.
- "ומה השווי שלו?" was parsed with the subject "שלו"; the lookup landed on another report.
- "ומה השווי של הדירה בקומה 21?" switched to another property whose title holds 21.

## What Didn't Work
- **Trusting the shared search tokenizer for numbers.** `_scope_terms` and `_title_words` in
  `backend/app/platform/search.py` drop one-character tokens, so a one-digit house number never reaches the search or
  the title match.
- **Excluding every title with a different house number.** It also removed the focus report when the user named
  a comparable inside it.
- **Relaxing to "every word that matches some title".** Hebrew prefix stripping makes ordinary words match title
  words ("שאלה" and "האלה" both give "אלה"), so the relaxed query still required a word nothing held.
- **Looking up whatever the resolver quoted as the subject.** The model often quotes a pronoun or a generic noun.

## Solution
In `backend/app/chat/entities.py` and `backend/app/chat/resolve.py`:
- **Numbers:** count the query's numbers yourself (`_numbers`), as title terms however short. Only a **one-digit**
  house number that the title lacks rules a title out. A longer number was a search term, so a document that
  matched already holds it in its text (a comparable it names).
- **Bare numbers:** on the "unnamed words" path, a number counts only beside a title word or a unit label
  (`identifying(..., bare_numbers=False)`). A floor, a year or a duration names no property.
- **Relaxation:** when the whole query matches nothing, try the identifying words, then **address pairs only**
  (`identifying(..., pairs_only=True)`: a title word with the number beside it, or a Latin label).
- **The resolver's quotes:**
  - the model's words for a new subject trigger a lookup only when they hold a word that can name a document,
    when the user corrects the property, or when a new question names an address;
  - otherwise the follow-up stays on the focus;
  - when the model quoted only a generic word, the user's own title words that the focus lacks join the query.
- **Same documents:** a lookup that lands on the focus documents is no entity change, so the context stays.
- **Clarification replies:** a reply resolved among the offered documents makes the chosen document's title the
  subject.

## Why This Works
The failures share one cause. The lookup treated the parse and the tokenizer as reliable signals of *which*
document is named, when neither is: the tokenizer was built for ranking passages, not for addresses, and the
resolver's `user_words` are evidence that the user said something, not that it names a document. Requiring an
identifying word, an address pair or an explicit correction before moving the conversation ties a move to what
can actually name a document.

## Prevention
- Test entity resolution with addresses that differ by one digit, the same address in two towns, a comparable
  named inside the focus report, a floor or a year, a pronoun subject, and a bare reply to a clarification
  (`backend/tests/unit/test_resolve.py`).
- Before trusting a new lookup rule, run the follow-up regression set against the real model twice; read
  `message_diagnostics.resolution` (the raw parse, the server's decisions, the lookup) for every failure.

## Related Issues
- `docs/solutions/workflow-issues/real-model-before-after-eval-on-the-local-stack.md`
- Plan: `docs/plans/2026-10-07-1643-fix-chat-request-context-plan.md` (KTD1, KTD2)
