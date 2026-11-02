---
title: Prove a new conversational path is exercised before measuring it (mocked UI shape, model asking rate, base ceiling)
date: 2026-11-02
category: workflow-issues
module: chat clarifications (backend/app/chat/clarify.py, frontend/components/chat, backend/eval/clarify_driver.py)
problem_type: workflow_issue
component: development_workflow
severity: high
applies_when:
  - "A new UI element is driven by a server payload and its browser tests mock the API"
  - "A feature only runs when the model chooses it (a clarification, a tool, a mode) and the prompt also offers an alternative"
  - "A pre-registered acceptance rule compares a new arm with the base by a margin"
tags: [evaluation, real-model, clarification, playwright, mocks, api-contract, pre-registration, ceiling]
---

# Prove a new conversational path is exercised before measuring it

## Context

Round 26 (PR #2, plan `docs/plans/2026-11-02-1055-feat-grounded-clarifications-that-continue-the-task-plan.md`) added structured
clarifications with option buttons. Unit, integration and 14 Playwright tests were green, and a paired real-model measurement ran on
public and held-out cases. The new path was still almost never exercised, for three separate reasons.

- **The buttons never rendered against the real API.** The server sends the clarification inside the answer (`answer.clarify`,
  `docs/api-contract.md`). The client read a top-level `message.clarify`. The route-mocked spec `frontend/e2e/clarification.spec.ts` built
  its mock in the same wrong shape, so every e2e test passed while production showed no buttons. Only the code review's API-contract and
  correctness reviewers caught it, by comparing the client's read with the backend's payload. The mock and the client now use
  `answer.clarify`.
- **The real model rarely took the path.** The policy says "when both readings fit in a short answer with sources, show both instead of
  asking" (`POLICY` in `backend/app/chat/engine.py`). With it, the product model asked a structured clarification on 1 of 102 measured
  turns and on 0 of 8 ambiguous questions in the browser check. The server re-prompted 0 times, so nothing was suppressed: the model chose
  the both-readings answer. The round-25 base asked text questions on 13 turns.
- **The acceptance margin was unreachable.** The pre-registered rule needed NEW >= BASE + 2 on 9 dev ambiguous cases. The base already
  answered 9/9 correctly after the user's typed reply, so no implementation could pass.

## Guidance

1. **Build a mock from the backend's real payload, not from the client's types.** Copy the shape from the API contract or from a backend
   integration test that reads the real response (here `m["answer"]["clarify"]`). Add at least one check that renders a payload the real
   server stored. Here the isolated-stack browser check opened a real stored clarification and confirmed the buttons, the chosen mark,
   refresh and the mobile layout.
2. **Measure the model's rate of taking the new path before the paired run.** Run a handful of the intended cases on the real model and
   count how often it takes the path. If the prompt offers an alternative (here, both readings), expect the model to prefer it. Report that
   rate next to accuracy, because "accuracy unchanged" can mean the path never ran.
3. **Check the base's ceiling before fixing a margin.** Score the base on the cases first. If it is at or near the maximum, a "+N over
   base" rule is unreachable. Choose cases the base gets wrong, or state an outcome the base cannot reach, such as turns saved or a
   question asked only when needed.
4. **Run the whole integration suite before calling a unit done.** A round-26 change to the pending slot (the files a text clarification
   touched are again a pool for a full-name reply) reversed a round-15 decision. The targeted subset passed. Two older tests in
   `test_chat_units.py` and `test_round10_sequences.py` caught it only in the full suite.

## Why This Matters

Green tests and a clean paired measurement looked like evidence that clarifications worked and did not help accuracy. In fact the UI path
was broken, the model path almost never ran, and the rule could not pass. Without the three checks above, a later round could tune
prompts or "improve" the feature against a measurement that never exercises it.

## When to Apply

- Before trusting a route-mocked browser suite for a payload-driven UI element.
- Before a paired real-model measurement of any behaviour the model opts into.
- Before writing a pre-registered margin against a base.

## Examples

- Mock before: `message("m2", "assistant", text, answer, { clarify: {...} })`, a top-level field the server never sends.
- Mock after: `message("m2", "assistant", text, { ...answer, clarify: { ...clarify, state, chosen } })`, the server's shape.
- Asking rate: NEW structured clarifications 1/102 turns (D04, run 1); BASE text questions 13/102. Both arms had equal accuracy after the
  user's reply (dev ambiguous 9/9, held-out ambiguous 5/5), so the measurement could not show a gain either way.
