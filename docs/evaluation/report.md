# Evaluation report: general question engine (U12)

Consolidated report for the plan `docs/plans/2026-10-06-0856-feat-general-question-engine-plan.md` (R27–R29). Detailed results: [eval-results.md](eval-results.md) (existing 77-turn set), [real-model-sample.md](real-model-sample.md) (held-out set with the real model, with a per-item failure analysis), [example-conversations.md](example-conversations.md) (real transcripts), [browser-tests.md](browser-tests.md), [load-test.md](load-test.md), [extraction-experiment.md](extraction-experiment.md). All data is synthetic.

## Bottom line

- **Previous capabilities: kept.** The existing evaluation scores 77/77 against the live stack with office A in cloud mode. No expected answer was changed. Acceptance gates 1–7 stay green.
- **The engine reaches held-out topics through one path.** No topic word occurs in `backend/app` (guard test). Every held-out question is interpreted into a validated plan and runs the same tools. No held-out item hit a price clarification except GQ53, a generic "average size" question.
- **Real-model quality gate: FAIL.** With `gpt-5.4-mini`, 31 of 54 held-out items (57.4%) pass every scored facet of the question file; the plan's threshold is 80% (an assumption the plan made, not a user requirement). AE3 and AE6 (GQ23) pass; AE1, AE4, AE5 and two AE6 items fail at least one facet. The plan treats this as a stop condition before the PR.
- **Progress across improvement rounds** (same question file, never edited after the first run; the model is not deterministic, so ±3 items is noise):

  | round | change | held-out passing | computation items |
  |---|---|---|---|
  | 1 | first full engine | 8/54 (14.8%) | 0/10 |
  | 2 | entity scope, precise locate, key/value tables, value filters, claim verification fixes, routing integration | 27/54 (50.0%) | 1/10 |
  | 3 | dimensions from a fixed vocabulary, upgrade dimensionless definitions | 26/54 (48.1%) | 1/10 |
  | 4 | bind values to their own labels, word roots, handle consistency | 26/54 (48.1%) | 0/10 |
  | 5 | quotes compared without separator punctuation | 31/54 (57.4%) | 2/10 |
  | 6 | cite by quote when the handle is wrong, locate variants as alternatives, clarifications kept on new questions | 31/54 (57.4%) | 3/10 |

- **What still fails, by cause** (round 6; details per item in [real-model-sample.md](real-model-sample.md)):
  - recall of single values in extraction: a value the document states is not extracted in every run (model variance), so averages use fewer observations than the answer key;
  - design choices stricter than the answer key: an area given only as inner dimensions (W×H) and two areas summed in one sentence go to human review instead of into a figure (settled: uncertain values go to review);
  - a clarification raised by a tool rather than by the plan is scored as the wrong task type (GQ51, GQ52, GQ54), although the user does see the right clarification;
  - version comparisons that cite the right documents but not the page holding the changed assumption.
- **The scripted gate 8 is green only with registered gaps.** With recorded real-model plans and a perfect (oracle) extractor, 52 of 64 checks pass every non-wording facet (it started at 35–36). The other 12 are strict `xfail`s, each tied to a named system gap or answer-key mismatch; a gap that gets fixed turns its `xfail` into a failure, so the list cannot go stale.
- **Two answers in the real sample were wrong and still passed verification** (GQ55, GQ61; see below). They are the most serious finding.

## What changed (summary)

Each turn is interpreted into a strict, server-validated `TurnPlan` (task type, turn relation, condition delta, attribute, metric, at most four steps from a closed tool set). The server's policy decides which clarifications survive. The state applies only what the turn states, with an idempotent `turn_id`. The bounded tools are SQL over verified records, on-demand fact extraction with verbatim quotes and coverage, search, locate and compare. Answers are composed as claims checked in two layers (code, then a model judge). Extracted facts live in `facts` plus `fact_extraction_ledger`, with trust tiers (reviewed → main figure; auto-validated → separately labeled preliminary figure; needs review → excluded and counted). The cache key is the canonical task plus every version it depends on. OpenAI (`gpt-5.4-mini`, Responses API, `store=false`) is the default provider. Per-office opt-in, honest modes (`cloud | error | demo | limited`) and a real connection test are in place. See [architecture.md](../architecture.md) and [api-contract.md](../api-contract.md).

## The 14 checks: where each is proven

Test names are real (`backend/tests/...`, `frontend/e2e/...`). "Real sample" refers to [real-model-sample.md](real-model-sample.md).

| # | Check | Proven by | Status |
|---|---|---|---|
| 1 | ממ״ד not sent to price clarification | `unit/test_interpret.py::test_safe_room_average_goes_to_the_model_when_cloud_is_on`, `::test_safe_room_average_in_limited_mode_searches_content_without_price_clarification`; `integration/test_turns.py::test_safe_room_average_is_computed_from_extracted_facts_in_cloud_mode`, `::test_safe_room_question_without_cloud_lists_passages_and_never_asks_about_prices`; `general-conversations.spec.ts` "full conversation: ממ״ד → …" and "limited mode: the ממ״ד question …"; gate 8 GQ28/GQ45; real sample GQ28 | green for ממ״ד; **real sample GQ53** (a generic "average size") still reached the price clarification |
| 2 | New topic reaches sources or a reasoned abstention | `acceptance/test_gate8_generality.py::test_general_item[*]` (60 items), `::test_a_held_out_attribute_needs_no_code_change`; `unit/test_no_topic_vocabulary.py`; abstention kinds in `integration/test_turns.py` and `unit/test_compose.py` | gate 8 green with 12 registered gaps; real sample 31/54 |
| 3 | Follow-ups keep and change context | `unit/test_state.py::test_follow_up_changes_only_what_it_states`, `::test_previous_year_changes_only_the_year_and_is_idempotent`; `integration/test_turns.py::test_same_turn_posted_twice_applies_once`; `general-conversations.spec.ts` full conversation; gate 8 GQ47/GQ48/GQ49 | state correct in the real sample (GQ47, GQ48, GQ49 turn 2); the follow-up computations themselves fail (extraction) |
| 4 | Comparison uses both sides | `integration/test_compare.py::test_two_versions_with_a_changed_rate_are_cited_and_labeled_by_version`, `::test_one_sided_evidence_gives_an_incomplete_comparison`; `integration/test_turns.py::test_compare_uses_the_conversation_referent_and_reads_both_versions` | green in tests; **real sample 0/8 comparison, version and conflict items**: compare needs `S#` handles from the conversation, and GQ22 compared the wrong documents |
| 5 | Exhaustive computation, not top-k | `integration/test_fact_extraction.py::test_ae7_nine_versions_mixed_outcomes`, `::test_above_sync_limit_enqueues_and_worker_completes`; `acceptance/test_gate1_calculations.py`; eval category `numeric_over_top_k` | green |
| 6 | Missing data and duplicates handled | `integration/test_fact_extraction.py::test_same_subject_counts_once_and_differing_values_are_flagged`, `::test_unknown_filter_metadata_is_counted_not_included`; `acceptance/test_gate1_calculations.py` (dedup); gate 8 coverage facets | green |
| 7 | Free-text clarification accepted | `unit/test_interpret.py::test_free_text_clarification_answer_through_the_model`, `::test_unrelated_question_keeps_the_pending_clarification`; `integration/test_turns.py::test_free_text_reply_resolves_the_clarification`, `::test_unrelated_question_keeps_the_pending_clarification`; `general-conversations.spec.ts` "a clarification answered in free text resolves", "an unrelated question keeps the clarification pinned" | green; real sample GQ51 turn 2 passed; **GQ52 turn 2 failed** (the model read an unrelated question as a change of the clarification) |
| 8 | New question does not inherit conditions | `unit/test_state.py::test_new_question_starts_from_fresh_conditions`, `::test_topic_change_clears_place_attribute_and_metric`; `integration/test_turns.py::test_topic_change_clears_place_and_attribute_and_reports_them`; gate 8 GQ49 | green (real sample GQ49 turn 2 passed) |
| 9 | OpenAI called on relevant paths | `unit/test_providers.py::test_real_openai_structured_echo` (real smoke, needs the key); `acceptance/test_gate7_provider_use.py::test_cloud_provider_receives_only_authorized_evidence_and_verified_numbers`, `::test_structured_path_makes_no_model_call`; `integration/test_turns.py::test_interpreter_usage_row_has_tokens_and_latency`; real sample usage: 72 interpret, 147 extract, 52 answer, 34 verify calls | green |
| 10 | `OPENAI_KEY` reaches the server unexposed | `unit/test_settings.py::test_openai_key_wins_over_fallback`, `::test_keys_never_rendered`; `integration/test_admin_lifecycle.py::test_auth_failure_is_persisted_shown_and_never_leaks_the_key`; `admin-provider.spec.ts`; secret scan `git grep -nE 'sk-[A-Za-z0-9_-]{20,}'` | green |
| 11 | Correct status without provider or on failure | `integration/test_admin_lifecycle.py::test_no_key_status_and_test_without_network`, `::test_enabled_without_key_is_error_and_answers_limited_never_demo`, `::test_limited_mode_without_demo`; `integration/test_turns.py::test_provider_auth_failure_mid_turn_is_visible_and_not_cached`, `::test_error_mode_shows_the_failure_and_calls_no_model` | green |
| 12 | No cross-user or cross-office leakage | `integration/test_rls.py::test_conversations_and_questions_are_private_to_their_user`, `::test_facts_from_a_hidden_group_are_invisible_to_employee`; `acceptance/test_gate3_isolation.py`; `integration/test_fact_extraction.py::test_conflict_from_a_hidden_group_is_invisible_to_the_employee`; `acceptance/test_gate8_generality.py::test_held_out_corpus_is_seeded_and_isolated`, GQ45 (yossi), GQ46 (office B), GQ56 (dana) | green; no leak in the real sample (isolation facet never failed) |
| 13 | Deletion and permission change invalidate | `acceptance/test_gate5_invalidation.py` (all four tests); `integration/test_turns.py::test_cache_invalidated_on_delete`, `::test_cache_invalidated_on_group_removal`, `::test_fact_review_invalidates_only_answers_of_that_attribute` | green |
| 14 | Previous capabilities stay green | acceptance gates 1–7 (`backend/tests/acceptance`), `unit/test_fixtures_ground_truth.py::test_general_section_leaves_existing_answer_key_unchanged`, existing eval 77/77 with the held-out group seeded | green |

## Existing evaluation (77 turns)

`cd backend && uv run python scripts/eval.py` against the live stack on 2026-10-06 (commit `8b5e629`, with this unit's eval fixes). Office A was in `cloud` mode (`gpt-5.4-mini`) and office B in `demo` mode. The held-out group was seeded.

- **77/77 correct**: numeric exact 36/36, justified abstention 19/19, answered 52/52, source recall 18/18, evidence 37/37, isolation 11/11, clarification precision 36/36. Providers seen: template 63, cloud 13, extractive 1 (an office B demo answer).
- **Cache.** The answer cache was not cleared, because no data may be changed. Its keys include the settings, data versions and the provider mode, so earlier answers are not reused across mode changes. 4 of 129 requests were cache hits, and all four came from inside the same run: price questions with conditions identical to an earlier item (F01, F03). No result depends on them.
- **Cloud mode versus the original demo/extractive assumption:** no failures, so nothing had to be classified. Content answers in office A are now composed by the real model (13 turns, `provider: cloud`). Fully explained price questions still make no model call.
- **Changed expectations: none.** The script was fixed to know the new group (`ידע כללי` = G3; dana sees G1 and G3, yossi does not) and to read the provider `mode` (the removed `effective_provider` field).

## Held-out general set (61 items, `backend/eval/questions_general.yaml`)

| run | provider | items passing | held-out | AE items |
|---|---|---|---|---|
| gate 8 (`test_gate8_generality.py`) | scripted: recorded real-model plans (44) or hand-written ones (17), oracle extraction, stub composer | 35–36/60 on the non-wording facets, + 24 strict known gaps | 33–34/54 | AE1, AE3–AE6 registered as gaps; AE4 passes its state check |
| real sample (`--set general --real-sample`), round 6 | OpenAI `gpt-5.4-mini`, live stack | 35/60 (GQ37 not run: needs cloud off) | **31/54 (57.4%)** | AE3, AE6 (GQ23) |

The scripted run measures the system with interpretation and extraction taken out of the picture. The gap from 35 to 8 is the real model's share. It falls mostly on interpretation (15 routing errors besides the compare gap), on claim verification that drops correct answers (6), and on extraction, where stated values never pass server validation (17 items have an extraction cause).

### Failure causes in the real sample (52 failed items; primary cause)

| cause | items | main mechanisms |
|---|---|---|
| (a) interpretation / routing | 19 | compare without conversation handles (6); computations planned as `locate` or `answer`; wrong attribute reuse; GQ53 price clarification; GQ52 pending clarification misread; GQ54 invalid plan; GQ55 address not a filter |
| (b) retrieval | 11 | locate lists every document with lexical support (7); passages on page 2 missed (GQ05, GQ22, GQ58, GQ61) |
| (c) extraction / validation | 9 | key/value table cells cannot be named; dimensions in cm; counts in words; assumed units and synonyms go to review; text attributes created as numeric |
| (d) composition / verification | 8 | correct claims dropped by verification (passages shown instead); a conflict answered with one side; absence written as claims (no abstention kind); combined answers lose the computation's abstention kind; the price path's no-records abstention has no kind |
| (e) expectation / scoring rule | 5 | tool-raised clarifications have a non-`clarify` plan task type (GQ23, GQ51); strict page-level citation precision (GQ09, GQ11); an unasked figure required (GQ18) |

Per-item evidence: [real-model-sample.md](real-model-sample.md#failure-analysis-hand-written-from-the-stored-answers-plans-and-steps).

### The two wrong answers

- **GQ55** "מה שטח הממ״ד בדירה ברחוב האירוסים 12?": the answer was "11.5 מ״ר מ״ר (חושב במערכת)". The plan computed a mean over every document with a value (H1 12 m², H4v2 11 m²), because an address is not a filter. The composer then presented that mean as the apartment's value. The correct answer is 12 m², and the unit is duplicated.
- **GQ61** "מה ייעוד המגרש … האירוסים 12?": the answer was "מגורים ב׳ לפי תכנית רג/340", cited to H2. That is a different property; H1's zoning (מגורים ג׳, page 2) was not retrieved. The judge accepted the claim because its passage supports the words, but the passage is about another property.

## Limitations of this evaluation

- **One real-model sample.** The model is not deterministic. Between the aborted first run and the scored run, items changed outcome (GQ01 passed, then failed; GQ04 was planned as `clarify`, then `answer`), and GQ39 and GQ56 were planned differently on the re-ask after extraction. The pass rate should be read as an estimate from one sample of 60 items.
- **Synthetic data only.** Nine held-out documents written for this test. Real appraisals have longer text, scanned pages and messier tables. Quality on real documents is still unmeasured.
- **AE2 (cloud off) was not run live.** The live office A stays in cloud mode, and the evaluation does not change settings. The scripted gate 8 runs it, and it is a registered gap there.
- **The office A connection test had not run** (`untested: true`). The mode was `cloud` by the "not yet tested" rule, and every call returned `ok` except one invalid plan.
- **Scorer corrections after seeing results.** These are listed in the sample report. The question file was not edited. The same stored answers scored 5/60 before the corrections and 8/60 after.
- **Gate 8 is a harness, not a model.** The oracle extractor reports the answer key's quotes; the stub composer always cites the first passage. The gate asserts plan, tool, state, outcome kind, abstention kind, computed result and coverage, and never wording.

## Reproduce

```bash
cd backend && uv run python scripts/eval.py                                    # existing set, writes eval-results.md
cd backend && uv run python scripts/eval.py --set general --real-sample --json /tmp/general-real.json   # needs office A in cloud mode
cd backend && uv run python scripts/eval.py --set general --real-sample --rescore /tmp/general-real.json # score stored answers again
cd backend && uv run pytest -q tests/acceptance/test_gate8_generality.py        # scripted held-out gate (rag_test db)
```

For a pilot on authorized real appraisals: seed them into a separate stack (never into the repository), write an answer key for them in the `ground_truth.yaml` / `questions_general.yaml` format, enable cloud use for that office only after its owner accepts the retention note, and run the same scripts. Report the result separately from these synthetic numbers.

## What is not built

The MVP's list stands (README, "מגבלות ידועות"). For the general engine, these are missing as of this report:

- comparing documents named in the question (compare works only on sources already in the conversation);
- predicates in computations ("smaller than 11 m²", "after 2010");
- text-valued attributes;
- reading attribute names from key/value table rows;
- converting dimensions to areas;
- address- or entity-scoped computations.
