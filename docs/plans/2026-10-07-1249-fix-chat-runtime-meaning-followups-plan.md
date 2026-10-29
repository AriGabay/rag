---
title: Working chat environment, meaning-preserving answers, resolved follow-ups, explicit gaps and honest coverage - Plan
type: fix
date: 2026-10-07
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Working chat environment and the remaining answer failures - Plan

## Goal Capsule

- **Objective:** An appraiser opens the chat in the project's normal local environment and talks to the real model. They get answers that:
  - keep every number's meaning (area basis, period, VAT, approximation), adding nothing the source lacks;
  - keep a correction about the datum they were discussing;
  - say plainly when the requested datum is not in the checked scope;
  - describe coverage at the level that was actually checked.
- **Means:**
  - fix the project's container egress to the model provider (KTD1);
  - strict structural exemptions in verification (KTD2);
  - a deterministic meaning check between each presented number and its evidence (KTD3);
  - a resolved-request step for follow-ups, validated against the conversation focus (KTD4);
  - structured absence reporting (KTD5);
  - graded coverage (KTD6);
  - the review leftovers (KTD7);
  - an evaluation that separates automated score, reference corrections, manual review and real failures (KTD8).
- **Authority hierarchy:**
  1. the user's request in this session;
  2. this plan's Product Contract;
  3. the KTDs;
  4. each unit's Approach.

  The earlier plans of PR #2 govern everything this plan does not change: `docs/plans/2026-10-06-0856-feat-general-question-engine-plan.md` and `docs/plans/2026-10-07-0820-fix-conversational-rag-gaps-plan.md`.
- **Stop conditions:**
  - a fix would weaken RLS, verification or evidence;
  - a fix would change Docker globally or affect another project;
  - real report content would enter git;
  - a settled decision proves infeasible.
- **Execution profile:** `ce-work` in return-to-caller mode inside `/lfg`, on branch `feat/general-question-engine`. Commits and push are allowed, and PR #2 may be updated. No merge, force-push, data deletion or production deploy.

---

## Product Contract

### Summary

This round makes the chat work end to end in the project's Docker stack, then closes the failures the last evaluation left. Engine changes stay inside `backend/app/chat/`, with the supporting compose, migration, chat UI component, evaluation and docs files the units name; ingestion and the UI shell are unchanged.

### Problem Frame

**The environment.** The containers cannot reach `api.openai.com`: TCP to its addresses times out. Other hosts behind the same CDN, and other providers, connect. On the host, `api.openai.com` is reached through a local component (`curl` connects to 127.0.0.1). Last round's real-model runs therefore used a backend and worker started on the host. The restored Docker stack cannot talk to the model.

**The failures,** all confirmed at 64b7e4f:
- `verify.structural_kind` exempts every Markdown heading and short label. A heading that states a fact passes when the judge gives it no verdict. The judge policy itself calls headings `not_factual`.
- Answers still drop an area basis written next to a value (מ״ר אקוו׳) or a period. Verification removes qualifiers the source lacks, but never notices a missing one.
- "רציתי את השווי ולא את השכירות", in new phrasings, still returns a total value after a per-m² question. The focus is only advice to the model.
- When the requested datum is absent (plot area), answers give a near datum without saying the requested one was not found.
- The coverage ledger marks a document "checked" when a search returned one passage from it. Full coverage of the document list reads as full coverage of the data.
- `ws.once` keys an opened block range without its scope. A section opened after neighbors of the same range is sent as a `same_as` reference to the shorter clip.
- Left from the last review:
  - removed-claim detail (`verification.rounds`) is returned on the normal message API;
  - `summary_meta` is not bound to the summary text it describes;
  - the stop e2e test races the answer.

### Requirements

**Environment**
- R1. Within the backend and worker containers, diagnose the provider egress layer by layer: DNS, TCP, TLS, proxy. Compare it with the host. Record the findings without printing any key.
- R2. Fix connectivity at the project's compose/service level only. No Docker reset, and no setting that affects other projects or containers.
- R3. If R2 is impossible now, leave exactly one documented runtime with start and stop commands, in which backend and worker run once each. No duplicate backend or worker.
- R4. Finish with a real conversation through the UI in the environment left running:
  - the real model answers;
  - a source opens;
  - history survives a reload.

**Verification**
- R5. A heading, label or table header is exempt from needing a verdict only if it is neutral navigation text. Formatting, bold and a trailing colon are not evidence that text is non-factual.
- R6. The judge policy treats a heading that asserts something about a property, document or value as a claim.
- R7. Unsupported VAT information never appears as fact. The deterministic VAT check stays.

**Meaning**
- R8. Each number an answer presents is matched to the evidence it came from: the cited passage segment, or the stored measurement of the cited document with that value.
- R9. When the evidence states an area basis (as written), a period or an approximation that is needed to understand the number, the answer's sentence must carry it. When the evidence states a VAT status, the sentence must not contradict it.
- R10. Nothing is added that the evidence lacks. Unknown stays unknown.
- R11. Property and value role mismatches remain the judge's to catch, with explicit rules.
- R12. A missing necessary qualifier is first sent back for repair. If it is still missing in the final answer, the server annotates the number with the qualifier as written in the source and labels it as added from the source. The number is not removed.

**Follow-ups**
- R13. On a turn with conversation context, the request is resolved before any tool runs. The resolution records:
  - what the user changed;
  - what remains from the focus;
  - which documents;
  - the metric kind, unit, period and area basis.
- R14. The server validates the resolution against the focus. A field not justified by the user's words keeps its focus value. A changed metric keeps the per-area or total nature of the unit unless the user's words change it.
- R15. A genuinely ambiguous correction gets one short clarification.
- R16. The resolved request is given to the answering step. An answer whose datum contradicts it (a total where per-m² was requested) is a verification problem.

**Missing information**
- R17. When the requested datum is not found, the answer states this first, as one of three statuses, before any near datum, and the near datum is labelled as different:
  - not found in search;
  - the source was read partially;
  - the relevant section or table was read and the datum does not appear.
- R18. The status is validated against what the turn actually did. "Section read" requires an opened section or table of that document. "Partially read" requires a partial document.

**Coverage**
- R19. The ledger records, per matching document, the deepest level reached: located, passage retrieved, section or table read, datum verified (cited and verified).
- R20. The coverage note and the UI state document coverage and data coverage separately.
- R21. A table answer states rows presented against rows in the table.
- R22. The dedup key of an opened source includes its scope and clip length. A larger context is sent even when a smaller one of the same range was sent.

**Review leftovers**
- R23. Removed-claim detail is stored as diagnostics. The normal message API does not return it. A diagnostics endpoint returns it only to the message owner or an office admin, and only while the message is visible to them.
- R24. `summary_meta` records a hash of the summary text and the summarized message count. A summary is usable only when both match.
- R25. The stop test synchronizes on an active working state. A separate test covers an answer that finished before the stop (the request leaves it done).

**Evaluation**
- R26. Known failures become regression cases.
- R27. A wrong reference answer is corrected in the private set with a note of the evidence. The stored answer is rescored without a new model call.
- R28. The report keeps four things apart: the original automated score, reference corrections, manual review findings, and the real remaining failures.
- R29. A new set (v6) is written before any run, never used for development. It focuses on corrections, area bases, missing information and table completeness, in new phrasings.
- R30. If failures persist, a controlled comparison runs: the current model at another reasoning effort, and a stronger model if available. It uses the same sets, sources and system, and reports cost per correct answer. The model changes only on evidence of better quality and total cost per correct answer. Availability and parameters are checked against the official docs.

### Key Decisions

- **The container egress is fixed at the project level, and the fallback is single-instance.** (session-settled: user-directed — chosen over documenting the host workaround only and over resetting Docker: the user must be able to use the chat for real). Governs R1, R2, R3, R4.
- **Formatting is not evidence of non-factuality.** (session-settled: user-directed — chosen over exempting every Markdown heading: a claim styled as a heading is still a claim). Governs R5, R6.
- **Every presented number is matched to its evidence, using current-turn structured data.** (session-settled: user-directed — chosen over a fixed-field answering route, repository-wide pre-extraction and prompt-only reminders: meaning loss persisted). Governs R8, R9, R10, R11, R12, R7.
- **Follow-ups are resolved in context before searching, and the resolution is validated.** (session-settled: user-directed — chosen over lists of known sentences: generality). Governs R13, R14, R15, R16.
- **Missing information is stated explicitly, with three distinct statuses.** (session-settled: user-directed — chosen over presenting a near datum as the answer: correctness). Governs R17, R18.
- **Coverage is graded and the dedup key is fixed.** (session-settled: user-directed — chosen over "checked = a search hit": honest coverage). Governs R19, R20, R21, R22.
- **The review leftovers are closed as listed.** (session-settled: user-directed — chosen over leaving them as residuals: completeness). Governs R23, R24, R25.
- **The evaluation separates its layers, and models are compared under control.** (session-settled: user-directed — chosen over re-asking the model to improve a score and over switching models without evidence: honest measurement). Governs R26, R27, R28, R29, R30.
- **Carried constraints:**
  - the official SDK and Responses API, with the key server-side only;
  - the model from configuration;
  - defined tools and RLS;
  - exact computation and fail-closed verification;
  - a public repo, so real data stays private;
  - tests run on isolated data;
  - no merge, force-push, data deletion or deploy.

  (session-settled: user-directed — chosen over free SQL, a client-side key and publishing real data: security and customer data.) Governs R1, R23, R26–R30.

### Acceptance Examples

- AE1. Covers R2, R3, R4. In the runtime left running — the Docker backend when R2 succeeds, or the single documented host instance when R3 applies — a chat question through `http://localhost:3000/chat` is answered by the real model. A source opens in the panel, and a reload shows the conversation.
- AE2. Covers R5. With the judge skipping it:
  - "# הנכס פנוי" is removed;
  - "**השווי נקבע ל-9,800 ₪**" is removed;
  - a table whose header "| הנכס פנוי | כן |" fails is removed whole (header, separator and rows);
  - "## מקורות" stays.
- AE3. Covers R9, R12. The evidence reads "השווי למ״ר אקוו׳ נקבע ל-14,250 ₪" and the answer writes "השווי למ״ר הוא 14,250 ₪". The result is a repair. If the qualifier is still missing at the end, the number is annotated "(מ״ר אקוו׳, כפי שנכתב במקור)".
- AE4. Covers R10. The evidence "שכ״ד 63 ₪ למ״ר" has no period. An answer "63 ₪ למ״ר לחודש" is a problem. An answer "63 ₪ למ״ר" is accepted.
- AE5. Covers R13, R14, R16. The focus is rent per m², with documents [D]. The message "דווקא את השווי, לא השכירות" resolves to value per m² in D. An answer giving only a total value is a problem.
- AE6. Covers R17, R18. The question asks for the plot area; the section was read and holds only built area. The answer starts "שטח המגרש לא מופיע בסעיף 'תיאור הנכס' שנבדק", then "השטח הבנוי (נתון אחר): …".
- AE7. Covers R19, R20. A set answer retrieves a passage from each of three documents but opens none. The ledger shows document coverage 3/3 and section reading 0/3, and the note says the data were not read in full.
- AE8. Covers R23. `GET /api/chat/messages/{id}` carries no `rounds` and no removed-claim text, also for a message stored in the old shape. The diagnostics endpoint returns them to the owner and to an admin of the same office, 404s for another user and for an admin of another office, and hides them after a revoke.

### Success Criteria

- AE1–AE8 hold. AE1 is checked live; the rest by automated tests.
- The v3, v4 and v5 regression runs are reported against the last round's numbers on the same cases.
- v6 is reported once, held-out, with the four layers of R28 kept apart.

### Scope Boundaries

- No changes to ingestion, measurement extraction or the UI shell.
- No streaming.
- No global Docker settings.
- No model change without R30's evidence.

---

## Planning Contract

### Key Technical Decisions

- **KTD1. Egress fix, decided by diagnosis.** (session-settled: user-directed — chosen over documenting the workaround and over resetting Docker: real chat.)
  - **Diagnosis order:**
    1. resolver inside the container (A and AAAA records);
    2. TCP to each address on 443, and to another CDN host as a control;
    3. a TLS handshake with SNI `api.openai.com`, to a reachable address;
    4. container proxy variables;
    5. the same steps on the host, and the identity of the host's local component that answers `api.openai.com` on 127.0.0.1:443.
  - **Fix candidates, all per-service and opt-in in the project:**
    - (a) if the host's component accepts connections for `api.openai.com` and serves a certificate the container already trusts, use `extra_hosts: ["api.openai.com:host-gateway"]` on backend and worker;
    - (b) if the host exposes a proxy port, set `HTTPS_PROXY`/`NO_PROXY` on backend and worker;
    - (c) if neither works, a single host runtime. Tracked scripts in `scripts/` (`local-host-runtime-start.sh`, `local-host-runtime-stop.sh`):
      - start stops the compose `backend` and `worker`, then starts one uvicorn and one worker on the host, against the compose database and a `FILE_STORAGE_ROOT` holding a copy of the `filedata` volume (so originals and pictures open);
      - the frontend container's `BACKEND_URL` points at `http://host.docker.internal:8000`, or the host-run frontend at `localhost:8000`;
      - stop stops the host processes and restores the compose services.
  - **TLS:** verification is never disabled, and no CA is added to the containers. A certificate the container does not trust moves to the next branch. The ops doc records the component's identity and certificate issuer.
  - **Where it lives:** branches (a) and (b) go in an untracked `docker-compose.override.yml` (compose loads it automatically; it is added to `.gitignore`), generated from a tracked example. All branches are documented in `docs/operations/local-model-egress.md`. Other environments keep today's behaviour.
  - Considered and rejected: changing Docker Desktop's network settings, which is global.
- **KTD2. Neutral navigation is a narrow deterministic shape; everything else needs a verdict.** (session-settled: user-directed — chosen over exempting every heading: a claim styled as a heading is still a claim.)
  - A heading, label or table header with no verdict passes only if, once its markup is removed, it is one word without digits or citations.
  - All other units without a verdict fail.
  - `JUDGE_POLICY` drops "כותרת" from the not_factual list and says a heading that asserts something is judged as a claim.
  - `JudgeVerdict` gains `not_factual_kind: Literal["navigation", "other"]`. A not_factual verdict on a multi-word heading or label is accepted only when `not_factual_kind` is `navigation`. The existing number rule stays.
  - A not_factual verdict on a table header row is accepted when the row asserts no value (column names, including years).
  - When a table header row fails, `VerifyReport.apply` removes the whole table (header, separator and rows), so no broken Markdown table is left.
- **KTD3. The meaning check runs beside the deterministic number check.**
  - **Data source.** For each number in a unit, the evidence qualifiers are the union of:
    1. measurements loaded this turn (M#) with that value;
    2. stored measurements of the cited document with that value, one query per turn;
    3. the source segment that contains the number. A segment ends at a line or a " | " cell boundary;
    4. for a table or table-row source, the number's column header, and the table's title, caption and notes;
    5. for any source, qualifiers stated in a sentence with no number (like the VAT check's `general`).
  - **Needed qualifiers:**
    - the area basis as written, normalized for comparison (prefix and abbreviation variants);
    - the period, when the evidence says month or year;
    - the approximation, when the value form is approximate or a range.
  - **Problems:**
    - A needed qualifier missing from the unit is a *missing-qualifier* problem.
    - A period, basis or VAT status in the unit that is absent from all of the evidence above, or contradicted by it, is a *blocking* problem (generalizing the VAT check).
  - **Judging.** Missing-qualifier problems are recorded apart from blocking problems. The unit is still sent to the judge.
  - **Repair.**
    - All problems go through the existing repair and rewrite rounds.
    - After the final round, a missing-qualifier problem is resolved by annotating the number after it, `(<qualifier as written>, כפי שנכתב במקור)`. This applies only when exactly one qualifier of that kind is attested for the number, the unit has no other deterministic problem, and its judge verdict is supported or partial. Otherwise the unit fails as today.
  - No fixed answer fields: the model still writes the answer.
- **KTD4. A resolved request before tools, validated by the server.**
  - **When it runs.** When the turn has history or a focus, one structured call runs first (the selected provider's configured model, purpose AGENT, reasoning effort low, small schema) and returns:

    ```
    ResolvedRequest {kind: new_topic|follow_up|correction|clarification_answer,
      standalone_question, changed_fields: [{field, user_words}],
      metric_kind, unit, period, area_basis, documents: [document_id], ambiguity}
    ```
  - **Validation:**
    - a field differing from the focus must appear in `changed_fields`, with `user_words` that occur in the user's message (a normalized substring) and that contain vocabulary of that field (from the label vocabularies of metric kinds, units, periods and area bases, or a document title or property word for documents);
    - otherwise the field is reset to the focus value;
    - a metric change keeps the focus unit's per-area or total class, mapped through the measurement vocabulary (value ↔ value_per_area, rent ↔ rent_per_area), unless the unit itself is in `changed_fields`;
    - when `metric_kind` changes, `period`, `vat`, `area_basis` and the metric as written become unknown unless the user's words set them. Only the subject, the documents and the per-area or total class carry over;
    - documents the user cannot see are dropped. When `documents` is in `changed_fields` with valid `user_words`, the resolved documents are empty, so the tools locate them; they are not reset to the focus documents;
    - `kind: new_topic` is accepted only with documents or subject in `changed_fields` and valid `user_words`; otherwise it is treated as `follow_up`;
    - `ambiguity` set together with `kind` correction gives a clarification answer, without tools.
  - **How it is used:**
    - the standalone question and a "requested datum" block replace the raw message as the model's task;
    - the raw message is still shown;
    - verification compares the answer's focus with the requested kind and unit, and a mismatch is a problem.
  - **Persistence.** Each validated ResolvedRequest is stored on its message. When the focus is null, the next turn falls back to it.
  - **Failure.** If the resolve call fails or returns an invalid shape, the turn proceeds with the raw message and the focus as today, and the failure is recorded in the turn's usage and diagnostics.
  - A new topic clears the focus.
- **KTD5. Absence is a structured field.**
  - `FinalAnswer.requested` is a list, one entry per requested datum: `{label, document_ids, status: found|not_found_search|source_partial|section_checked_absent, checked_where}`. `checked_where` is an S# opened this turn. `requested` supersedes `missing_info` as the absence signal.
  - The server validates each status against `ws.activity` and downgrades unsupported claims (KTD6 levels):
    - `section_checked_absent` is kept only when one of its documents has a section or table opened with `open_source` (scope section or table) and `checked_where` names that opening; `find_measurements` rows do not count;
    - `source_partial` requires a partial read of one of its documents.
  - When `status != found`, the server prefixes the answer with a deterministic sentence for the status, naming the requested datum and, for `section_checked_absent`, the server-recorded section name. The prefix cites `checked_where` and is judged like any unit.
  - The judge rule: a near datum presented as the requested one is unsupported.
- **KTD6. Graded coverage.**
  - **Levels.** `ws.activity[doc]` records the deepest level reached: located < retrieved < read (`open_source` section or table, or `find_measurements` rows) < verified (cited by a unit that passed verification). It also records the sections and tables opened with scope section or table, by S# and name, for KTD5.
  - **Ledger and note:**
    - the ledger keeps `matching` and adds `levels: {document_id: level}`;
    - the note states "N מסמכים מתאימים · נקראו (סעיף/טבלה) K · נשלפו קטעים בלבד מ-L · נתון מאומת מ-M";
    - `complete` requires a verified datum, or an explained omission, for every matching document. Retrieval alone does not count.
  - **Tables:** `tables_presented: [{title, rows, values_presented}]`.
  - **UI:** `Message.tsx` shows the levels.
  - **Dedup:** the `ws.once` key adds scope and clip length.
- **KTD7. Review leftovers:**
  - **Diagnostics storage.** Migration 0008 adds a table `message_diagnostics (message_id, office_id, user_id, rounds jsonb, removed jsonb, document_ids text[])`:
    - its RLS is tenant isolation plus a restrictive policy `user_id = app_user() OR app_role() IN ('admin','system')`;
    - the `messages` policies stay unchanged;
    - the migration copies the existing `answer->'verification'->'rounds'` of stored messages into it.
  - **The normal path.**
    - `_answer_payload` stops writing `rounds` into `answer`, and `_message_json` drops `verification.rounds` from any answer it returns, old shape included;
    - `verification.problems` on the normal path keeps only a count and the generic notice; the removed-claim text moves to diagnostics.
  - **The endpoint** `GET /api/chat/messages/{id}/diagnostics` returns 404 unless every id in `document_ids` is visible to the caller (the same check as `_hidden`). An admin's read is written to the audit log.
  - `summary_meta` adds `summary_sha256` and `summary_message_count`, and `_summary_usable` checks both.
  - **The stop path:**
    - `run_message` finishes a message it finds in `cancelling` as cancelled, so a stop that lands before the thread starts does not leave "עוצר…";
    - an API test cancels a turn before its thread starts and expects `cancelled`;
    - the stop e2e test waits for a progress step past "queued" before clicking;
    - a new API test cancels a done message, and the message stays done.
- **KTD8. Evaluation.**
  - Reference corrections are YAML fields `reference_corrected: {date, evidence, was}` on the private items.
  - `--rescore` reports both the original and the corrected grade.
  - v6 is written before any run of this round, on real reports and new synthetic documents in office C. It runs once, with a configuration chosen before it.
  - The comparison runs only if the regression sets still show the targeted failures, and only on v3–v5:
    - same sets, `chat_reasoning_effort` at medium;
    - a stronger model only if `models.retrieve` confirms it;
    - two runs per configuration;
    - decision rule: switch only if more conversations pass in both runs, at no higher cost per correct answer.
  - Cost per correct answer = total cost / passed conversations.

### Assumptions

- **Diagnosis decides KTD1's branch.** If only branch (c) works, the user gets a single-instance host runtime with documented commands. The final report says Docker egress is still blocked and why.
- **The synthetic v6 documents** are new, invented and generated by the script; office C only.

### Sequencing

U1 (environment) runs first, because every real-model check needs it. Then the code units in order: U2, U3, U4, U6, U5, U7. Then U8 (evaluation), which writes v6 before any of its runs.

### Risks and Mitigations

- **The extra resolve call adds latency and cost on follow-ups.** It is small (low effort, short input). It runs only with history or a focus. The cost table measures it.
- **Annotation text could read awkwardly.** It is only applied after the repair rounds fail. Manual review checks it.
- **The host component may terminate TLS with a local CA.** Branch (a) then fails, (b) does not apply, and the runtime falls to (c). Adding that CA to the containers is excluded by KTD1's TLS rule; the ops doc names the option for the user to decide.
- **Stricter verification removes more text.** Headings, tables and qualifier problems now fail closed. The regression runs and manual review watch for answers left too thin.

---

## Implementation Units

### U1. Container egress to the model provider

**Goal:** the Docker backend and worker reach the provider, or a single-instance documented alternative runs (R1–R4, KTD1).

**Requirements:** R1, R2, R3, R4.

**Dependencies:** none.

**Files:**
- `docker-compose.override.example.yml` (new, tracked; branches (a) and (b))
- `.gitignore` (adds `docker-compose.override.yml`)
- `docs/operations/local-model-egress.md` (new)
- `scripts/local-host-runtime-start.sh`, `scripts/local-host-runtime-stop.sh`, only if branch (c)
- `docs/solutions/workflow-issues/real-model-before-after-eval-on-the-local-stack.md` (update)

**Approach:**
1. Diagnose in order: DNS, TCP, TLS (SNI), proxy, host component. Use no key; print only status codes, the component's identity and the certificate issuer.
2. Apply the first working branch. Never disable TLS verification.
3. Recreate only backend and worker.
4. Probe the provider's `/v1/models` from inside the container without printing headers: an expected 401 without a key, or 200 with the configured key and only the status printed.
5. Run the admin connection test endpoint.

**Test scenarios:**
- The connection test from the admin API, in the runtime left running, returns `ok`.
- AE1, checked live in the browser.
- Exactly one backend and one worker run: `docker compose ps` for branches (a) and (b); with branch (c), the start script stops the compose services first, and the process list shows one host backend and one host worker.

**Verification:** a live chat answer through the UI from the environment left running.

### U2. Strict structural exemptions

**Goal:** R5, R6 (KTD2).

**Requirements:** R5, R6.

**Dependencies:** none.

**Files:**
- `backend/app/chat/verify.py`
- `backend/tests/unit/test_verify_units.py`

**Approach:** narrow `structural_kind` exemptions to one-word, digit-free text; update `JUDGE_POLICY`; add `not_factual_kind` to `JudgeVerdict` and require `navigation` for not_factual on multi-word headings and labels; remove a whole table when its header row fails.

**Test scenarios:**
- Covers AE2. With the judge skipping them, claim headings, labels and table headers fail; "## מקורות" and "**סיכום**" pass.
- The judge returns not_factual on "## הנכס פנוי" with `not_factual_kind: other`: the unit fails.
- The judge returns not_factual on "## פירוט לפי מסמך" with `not_factual_kind: navigation`: the unit passes.
- A table header "| נכס | 2023 | 2024 |" judged not_factual passes; the table stays.
- A failed header row removes its table whole, and the rest of the answer renders.

**Verification:** unit tests pass.

### U3. Meaning check between a presented number and its evidence

**Goal:** R8–R12 (KTD3).

**Requirements:** R8, R9, R10, R11, R12, R7.

**Dependencies:** U2.

**Files:**
- `backend/app/chat/meaning.py` (new)
- `backend/app/chat/verify.py`
- `backend/app/chat/engine.py`
- `backend/tests/unit/test_meaning.py` (new)
- `backend/tests/integration/test_chat.py`

**Approach:**
1. Gather the evidence qualifiers per number as the union KTD3 defines (measurements, segment, column header and table notes, number-free statements).
2. Compare them with the unit's own words.
3. Emit missing-qualifier and blocking problems with explicit repair instructions; missing-qualifier units still go to the judge.
4. Add a final-round annotation for numbers missing a qualifier, under KTD3's conditions.

**Test scenarios:**
- Covers AE3. Area basis missing: repair, then annotation.
- Covers AE4. Unsupported period: problem.
- "פלדלת" kept as written; "ברוטו" kept.
- Approximation ("בכ-") dropped: problem.
- A period stated only in a table's column header ("שכ״ד לחודש"), written in the answer: no problem.
- A VAT status stated once for the whole source, written in the answer: no problem.
- Two different area bases attested for the same number: repair, never annotation.
- A missing-qualifier unit the judge finds unsupported: fails, not annotated.
- New property names and amounts in every case.
- A number with no qualifiers in the evidence: no problem.

**Verification:** unit and integration tests pass.

### U4. Resolved request for follow-ups

**Goal:** R13–R16 (KTD4).

**Requirements:** R13, R14, R15, R16.

**Dependencies:** none.

**Files:**
- `backend/app/chat/resolve.py` (new)
- `backend/app/chat/engine.py`
- `backend/app/chat/api.py`
- `backend/app/chat/verify.py` (requested-datum check)
- `backend/tests/unit/test_resolve.py` (new)
- `backend/tests/integration/test_chat.py`

**Approach:**
1. Make one structured call when history or a focus exists.
2. Validate it in code.
3. Inject the requested-datum block.
4. Run the clarification path when the correction is ambiguous.
5. Add a verification check that the answer's focus matches the request.

**Test scenarios:**
- Covers AE5. The period of the rent focus does not carry over to the value.
- `changed_fields` without user words, or with user words lacking the field's vocabulary: the field is reset.
- A unit change justified by the user's words ("השווי הכולל") is honored.
- A follow-up naming another property: the resolved documents are empty and the tools locate it.
- `new_topic` without documents or subject in `changed_fields`: treated as follow_up.
- A new topic clears the focus.
- An ambiguous correction gives a clarification, and no tools are called.
- A revoked focus document is dropped.
- A null focus falls back to the stored resolved request of the previous message.
- The resolve call fails: the turn proceeds with the raw message and focus, and the failure is recorded.

**Verification:** tests pass; real-model probes of the correction cases.

### U5. Explicit absence

**Goal:** R17, R18 (KTD5).

**Requirements:** R17, R18.

**Dependencies:** U6 (levels).

**Files:**
- `backend/app/chat/engine.py`
- `backend/app/chat/coverage.py`
- `backend/app/chat/api.py`
- `backend/tests/integration/test_chat_coverage.py`

**Approach:** add the `requested` list, validate each status against the activity levels and recorded openings, add the deterministic prefix sentence with the server-recorded section name, and add the judge rule.

**Test scenarios:**
- Covers AE6.
- "section_checked_absent" without an opened section is downgraded to not_found_search.
- "section_checked_absent" backed only by `find_measurements` rows is downgraded.
- `checked_where` naming an S# not opened this turn is ignored and the status downgraded.
- A partially read document gives source_partial.
- status found leaves the answer unchanged.

**Verification:** tests pass.

### U6. Graded coverage and the dedup key

**Goal:** R19–R22 (KTD6).

**Requirements:** R19, R20, R21, R22.

**Dependencies:** none.

**Files:**
- `backend/app/chat/tools.py`
- `backend/app/chat/coverage.py`
- `backend/app/chat/api.py`
- `frontend/components/chat/Message.tsx`
- `frontend/lib/chatTypes.ts`
- `backend/eval/scoring.py`
- `backend/tests/integration/test_chat_coverage.py`
- `frontend/e2e/chat-ledger.spec.ts`

**Approach:** record levels instead of flags, mark verified after verification, add the new note wording and the table presentation counts, show them in the UI, change the dedup key, and make scoring read the levels.

**Test scenarios:**
- Covers AE7.
- Neighbors then section of the same range: the section is sent in full.
- A table answer with 2 of 9 values states it.
- The e2e shows levels.

**Verification:** tests pass.

### U7. Review leftovers

**Goal:** R23–R25 (KTD7).

**Requirements:** R23, R24, R25.

**Dependencies:** none.

**Files:**
- `backend/alembic/versions/0008_message_diagnostics.py`
- `backend/app/chat/api.py`
- `frontend/components/chat/Message.tsx` (generic removed-claims notice)
- `backend/eval/chat_eval.py` (reads diagnostics)
- `backend/tests/integration/test_chat_permissions.py`
- `backend/tests/integration/test_chat.py`
- `frontend/e2e/chat.spec.ts`
- `docs/api-contract.md`

**Approach:** move the rounds and removed-claim text to the `message_diagnostics` table behind the endpoint, migrating old messages; strip them from the normal payload; add the hash and count in `summary_meta`; finish a message found in `cancelling` as cancelled; synchronize the stop test and add the done-before-stop test.

**Test scenarios:**
- Covers AE8, including a message stored in the old shape and an admin of another office (404).
- An admin read writes an audit entry.
- A rewritten summary with stale meta is not used.
- A stopped running turn ends cancelled.
- A stop that lands before the thread starts ends cancelled.
- A stopped done turn stays done.

**Verification:** tests pass; the e2e passes 3 times in a row.

### U8. Evaluation and controlled comparison

**Goal:** R26–R30 (KTD8).

**Requirements:** R26, R27, R28, R29, R30.

**Dependencies:** U1–U7.

**Files:**
- `backend/eval/chat_eval.py` (`reference_corrected`, dual grading)
- `backend/scripts/make_eval_docs.py` (v6 documents)
- private sets (`real_documents/eval/`)
- `docs/evaluation/conversational-rag.md` (aggregates)

**Approach:**
1. Write v6 (questions, references and synthetic documents) before any run of this round.
2. Rescore v5 with the documented correction.
3. Run v3, v4 and v5 twice on the final code.
4. Run v6 once.
5. Run the comparison on v3–v5 only if the targeted failures persist.

**Test scenarios:**
- A unit test of dual grading: original fail and corrected pass are both reported.

**Verification:** the report holds the four separate layers and the cost-per-correct-answer table.

---

## Verification Contract

- **Unit and integration tests:** `cd backend && uv run pytest -q -p no:warnings` (run alone).
- **Lint and type checks:** ruff; `npm run typecheck && npm run lint`.
- **Browser:** Playwright in `frontend/e2e`.
- **Real-model checks,** run in the environment U1 leaves running:
  - a UI conversation;
  - the v3, v4 and v5 regression sets;
  - v6;
  - the comparison, when it runs.

## Definition of Done

- U1–U8 are done and AE1–AE8 are proven.
- The environment left running answers through the UI.
- The report separates the four layers.
- No real data is in git.
- The PR is updated, not merged.
- No abandoned code remains.
