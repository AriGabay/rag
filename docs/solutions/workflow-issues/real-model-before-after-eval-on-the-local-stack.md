---
title: Measuring a chat change before and after against the real model on the local stack
date: 2026-10-07
category: workflow-issues
module: backend evaluation (eval/chat_eval.py, local Docker Compose stack)
problem_type: workflow_issue
component: development_workflow
severity: medium
applies_when:
  - "Comparing quality, cost or latency of the conversational engine before and after a change on the same eval cases"
  - "The Docker backend cannot reach the model provider while the host can"
  - "Re-grading stored eval answers with new structured expectations"
  - "Running a held-out set while the code under evaluation is still changing"
tags: [evaluation, real-model, before-after, git-worktree, docker-networking, openai, chat-eval, rescore, held-out]
last_updated: 2026-10-07
---

# Measuring a chat change before and after against the real model on the local stack

## Context

The engine is judged on private eval sets (`real_documents/eval/*.yaml`, outside git) run by
`backend/eval/chat_eval.py` against a running backend. Model answers vary from run to run, so a single run before
and after a change cannot separate a regression from noise. In this round (PR #2, plan
`docs/plans/2026-10-07-0820-fix-conversational-rag-gaps-plan.md`) three things cost time to rediscover:
- serving the old code for a baseline without disturbing the branch;
- getting the real model at all when Docker could not reach it;
- re-grading answers that were stored before the structured checks existed.

## Guidance

**1. A baseline from the old code, against the same database.**
- Create a detached worktree at the commit before the change:

  ```bash
  git worktree add --detach <scratch>/baseline-tree <pre-change-sha>
  ```
- Serve it with the main checkout's virtualenv, so nothing is installed twice (`--app-dir` keeps the worktree's
  code first on the path):

  ```bash
  /path/to/Rag/backend/.venv/bin/python -m uvicorn app.main:app --app-dir <scratch>/baseline-tree/backend \
      --host 127.0.0.1 --port 8000
  ```
- The old code ignores columns added by newer migrations, so the database does not need to be rolled back. Do not
  run the old code's migrations.
- Keep the baseline's token accounting identical to the new code's. The accounting landed first, in its own commit
  (`cached_input_tokens` in `backend/app/providers/llm.py`), and the worktree was cut from that commit.

**2. When Docker cannot reach the provider, run the backend and worker on the host.**
- First run `scripts/check-model-egress.sh`, which checks DNS, TCP, TLS, proxy and HTTPS from inside both containers;
  see `docs/operations/local-model-egress.md`. In the next round the containers reached the provider again after
  they were recreated, with a host packet-tunnel VPN disconnected. Use the host runtime only when the check fails
  and the cause cannot be removed.
- **Symptom.** Turns failed with `provider openai agent step failed: error (APIConnectionError)` after long waits.
  From inside the containers, TCP to the `api.openai.com` addresses timed out, while other hosts behind the same
  CDN and the host itself connected. No code change fixes this; it is the Docker VM's route.
- **Workaround.** Run uvicorn and `python -m app.worker` on the host against the compose database, which is
  published on `127.0.0.1:5433`:
  - `DATABASE_URL=postgresql+psycopg://rag_app:<pw>@127.0.0.1:5433/rag`, plus the owner URL;
  - a scratch `FILE_STORAGE_ROOT`;
  - `docker compose stop backend worker`, so jobs and port 8000 are not shared.
- **`.env` parsing.** `.env` may hold `KEY = value` lines. `docker compose` accepts them, but `set -a; source .env`
  does not: it runs `KEY` as a command and the key silently stays empty, so the backend falls back to limited
  (search-only) mode. Parse `.env` as compose does (strip the spaces around `=` and the surrounding quotes) and
  never print the values.
- **Local embeddings.** The host virtualenv needs the optional `ml` extra (`uv sync --frozen --extra ml`; frozen
  leaves the lockfile alone) and the e5 model cache. Copy the cache out of the image
  (`docker cp <container>:/opt/hf/hub <dir>`) and set `HF_HOME=<dir> HF_HUB_OFFLINE=1`. Without it the app fails at
  startup with `No module named 'sentence_transformers'`.
- **Starting the scripts.** Start host processes with `.venv/bin/python` rather than `uv run`, so a sync cannot
  change the environment under a running server.

**3. Runs, payloads and diagnosis.**
- Run each set twice before and twice after the change, under the same concurrency (v3 and v4 side by side).
- Report a case as a regression only when it passed both times before and failed both times after. List the cases
  that flip within a pair separately.
- `chat_eval` now keeps each turn's full answer payload and per-call usage (input, cached input and output tokens,
  latency).
- Use `--rescore <results.json>` to grade stored answers with new expectations. It makes no model calls, so two
  runs can be compared on identical answers.
- When a claim disappears from an answer, read its diagnostics. `GET /api/chat/messages/{id}/diagnostics` returns,
  to the message's owner or an office admin, what each verification round removed or repaired, and why.
  `chat_eval` stores them with each turn under `diagnostics`; the answer itself carries counts only.
- To trace one question end to end, run `engine.run_turn` in-process with the real provider and wrap
  `verify.verify_answer` to print each round.

**4. A held-out set runs once, on the build that will ship.**
- Write the set, with its references, before any run of the round. A set whose results guided a fix is a regression
  set from then on.
- If the code changes after a held-out run has started (here: review fixes that changed how the meaning check
  attaches qualifiers), stop the run and discard its output unread. Then rebuild the Docker services and run the set
  once on the final build.
  - Results from the earlier build are not "the held-out score". They describe code that will not ship.
  - Reading them before the rerun would turn the set into a development set.
- Run the regression sets twice on that same build too, so all the numbers in the report describe one build. Earlier
  runs may be reported, labelled with their build, as an intermediate data point.
- **A provider outage in the middle of a run** (turns ending `failed` with a provider error while the egress
  check passes again minutes later) is not a result of the build. Rerun only the conversations that got no
  answer, on the same build, with `--only`, keep them in a separate `*-infra-rerun` directory, and report the
  scores as "including reruns after the outage". Never rerun conversations that got an answer, failed or not.
- **A held-out set that was read to diagnose a failure is a regression set from then on,** even when the fix
  comes after its one run. Report its score as measured on the build it ran on, label later fixes as verified by
  tests and targeted reruns only, and write a new held-out set for the next round.
- When manual review shows a reference answer was wrong, do not edit it silently. Keep the original under
  `reference_corrected: {date, evidence, was}` beside the corrected `expect`. `chat_eval` (live and `--rescore`) then
  reports the score against the original reference and the score after corrections separately, and lists each
  correction with its evidence.

## Why This Matters

Without these steps:
- a missing key looks like a model-quality drop (limited mode answers "pass" nothing);
- a Docker networking fault looks like provider instability;
- one noisy run can make a change look like a regression or a fix.

In this round, the same three probes of one question went from 1 of 3 to 3 of 3 after a fix. That is how the
correction behaviour was told apart from noise.

## When to Apply

- Any change to `backend/app/chat/` whose effect on answers, cost or latency must be shown, not assumed.
- Whenever `chat_eval` reports search-only answers, or turns fail with connection errors while the host reaches
  the provider.
