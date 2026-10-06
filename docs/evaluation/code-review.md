# Code review of the general question engine (round 7)

A multi-reviewer review of the branch against `main` at `05df270`. The reviewers were correctness, security, adversarial, testing, maintainability, performance, reliability, data migration, API contract, frontend races, project standards and learnings. An independent validator then re-checked the eight highest-impact findings. Seven were confirmed with read-only probes: #1, #2, #3, #4, #5, #8 and #10. #6 was confirmed by a code trace and was not run end to end.

On top of its usual lens, the review covered the areas the user named:

- clarification handling and the "a short reply is an answer" rule;
- negation;
- topic change;
- attribute canonicalization;
- background extraction jobs;
- the quote and source fallback;
- normalization;
- computation correctness.

The table below shows what changed for each finding and which test pins it. Numbers are the review's finding numbers.

## Findings fixed

| # | severity | finding | fix | pinned by |
|---|---|---|---|---|
| 2 | P1 | The quote fallback re-cited a subject value the model invented to a comparable's table cell, and auto-validated it | A quote missing from the cited source is looked for only in that source's neighbourhood: the same table row, or the same stored chunk. Only an unknown handle falls back to the one place that holds the quote, and that value goes to review | `test_fact_validation.py::test_quote_held_only_by_a_comparable_row_is_not_moved_there`, `…unknown_handle_with_one_holder_goes_to_review`, `…quote_held_by_two_cells…is_ambiguous` |
| 3 | P1 | A floor number was auto-validated as a count of another noun ("2 X בקומה 3" gave 3) | A count counts the noun written with it. A counted-noun unit (floor, room, dwelling) that the attribute does not name is rejected (`counted_other`). The next value's label starts after this value's noun | `test_floor_number_is_not_a_count_of_another_noun`, `test_rooms_are_not_balconies_but_are_rooms` |
| 5 | P1 | "אין" / "ללא" became a zero count in any sentence | A zero word must govern a word of the attribute. A qualified zero ("אין X נוספת", "אין X בבניין הסמוך") goes to review. "אין צורך ב…" and "אין מידע" are rejected | `test_a_zero_word_about_something_else_is_no_value`, `test_a_qualified_zero_goes_to_review`, `test_counts_written_as_words_and_none_as_zero` |
| — | — | A value whose naming term names another attribute of the office went to review as a "synonym" (failure analysis GQ32, GQ38, GQ56) | A term or cell label whose distinctive words all name another registered attribute, and not this one, is rejected (`other_attribute`). Partial overlap is still only a synonym and goes to review | `test_a_term_naming_another_attribute_is_rejected`, `test_a_synonym_whose_words_are_partly_another_attributes_is_not_rejected` |
| 6 | P1 | Answering a referent clarification dropped the chosen source, so the comparison never resumed | The pending clarification keeps the sides already chosen and the interrupted task's queries. Choosing a source resumes the comparison with both sides | `test_turns.py::test_choosing_the_second_source_resumes_the_comparison`, `test_interpret.py::test_choosing_a_source_resumes_the_comparison_with_both_sides` |
| 8, 46 | P2/P3 | The reply matcher stripped retrieval stopwords, so the option "שומות" could not be chosen. A negated option ("לא כולל") tied with its positive | Reply matching uses its own small particle list and gives every word a polarity: a negation scopes to the end of its clause | `test_long_or_negated_replies_naming_an_option_are_replies`, `test_a_negated_option_label_matches_only_a_negated_reply` |
| 25, 36, 51 | P2/P3 | The "≤ 5 words = a reply" rule misrouted long replies and accepted short new questions | The length rule is gone. On the rules path, a question word, "X או Y" or "אין X" is never a reply. On the model path, a turn the model read as a reply becomes a new question when it brings its own attribute, entities, conditions or computation, or asks a question that names none of the chosen option's words. Counter-tests cover a ≤5-word question with its own condition, a ≤5-word question naming no option, a long reply naming the option, and a reply with new entities | `test_short_questions_and_alternatives_are_not_replies`, `test_a_short_question_with_its_own_condition_is_a_new_question`, `test_a_long_reply_that_names_the_option_is_a_reply`, `test_a_reply_that_names_new_entities_is_a_new_question` |
| 10 | P2 | The eval scorer passed wrong answers: absence facts were never checked, and values were matched as substrings | Polarity is checked, numbers and Hebrew values are matched as whole tokens, and coverage is checked per document. See `scorer-changes.md` | `tests/unit/test_eval_scorer.py` (60 tests with negative controls) |
| 35 | P2 | The scorer failed justified clarifications, and the gate-8 xfail masked every facet of an item | A clarification raised by the compute tool, with the expected key, is accepted. Known gaps are scoped to the named facets | `test_eval_scorer.py`, gate 8 |
| 11 | P2 | A state-conflict retry re-applied a clarification answer to a different pending clarification | The retry is refused (409 STATE_CONFLICT) when the fresh pending clarification differs | `test_turn_api_reliability.py` |
| 12 | P2 | A pending turn left behind by a crash answered 409 forever | A pending reservation older than the turn deadline plus the slowest model call is abandoned. The turn runs again, and a stale run cannot complete the row | `test_turn_api_reliability.py` |
| 14 | P2 | A prompt-version bump orphaned reviewed facts | Verified and corrected facts of earlier extraction versions keep counting. A value a reviewer rejected is not counted again when a new extraction finds it | `compute_facts` (reviewed facts are read across versions) |
| 16 | P2 | A dimension upgrade re-read documents but kept the old facts beside the new ones | The upgrade deletes the unreviewed facts. Reviewed ones go back to review, because their canonical value predates the unit | `test_naming_the_dimension_later_replaces_unreviewed_facts_instead_of_adding_to_them` |
| 21, 22 | P2 | Failed extractions were retried on every turn, one INSERT per version | A document-specific failure (refusal, invalid, incomplete, unsupported) is not re-sent. Other failures wait a 120 s cooldown. Pending ledger rows are written in one statement | `test_a_document_specific_failure_is_not_sent_again_on_every_turn`, `test_provider_error_in_job_leaves_version_ready_and_later_turn_requeues` |
| 23 | P2 | A trusted-only computation (cloud off) dropped auto-validated values without counting them | They are counted as awaiting review | `compute_facts` audit states |
| 29 | P2 | Removing a chip before any answer returned 422 | The edit changes the open clarification's context and asks it again | `test_removing_a_chip_before_any_answer_changes_the_open_clarification` |
| 31 | P2 | Prompt tags were not neutralized in the answer and judge inputs (CWE-77) | `llm.prompt_text` / `prompt_attr` are applied to every document-derived string in the answer, judge, extraction and legacy prompts | `test_a_span_cannot_close_its_evidence_block_or_forge_a_claim`, `test_content_answers.py` |
| 32 | P2 | The locate negation window matched a negation of a different word in the same sentence | A negation must govern its word: before it in the same clause, with no coordinator or "יש" between | `test_locate_scoring.py`, `test_search.py` counter-tests |
| 33 | P2 | Model calls were not bounded by the turn deadline, and SDK retries doubled each timeout | `structured(deadline=)`: the timeout is capped at the time left and retries are off. Below one second no call is made. Wired into compose, judge, compare, inline extraction and replan | `test_llm_deadline.py` |
| 34 | P2 | Offices that had consented to Anthropic were sending to OpenAI after the upgrade | Consent is per provider. Consent given for another provider puts the office in limited mode, "re-acknowledge required", until an admin re-acknowledges. The admin screen shows the reason | `test_admin_lifecycle.py` |
| 37, 38, 49 | P2/P3 | Frontend races: a draft typed during a turn was wiped; navigation was unguarded; 409 polling could not be cancelled | Sequence tokens, a navigation lock, draft-preserving clearing, and `AbortController` on unmount | `frontend/e2e/chat-races.spec.ts` |
| 39 | P2 | Fact review acted on a stale list | Optional `expected_status` / `expected_value` precondition, answered with 409 on mismatch. The UI always sends it | `test_facts_review.py`, `frontend/e2e/facts-review-stale.spec.ts` |
| 43 | P3 | The coverage row of the API contract fell out of the admin table | Moved | — |
| 47 | P3 | The conflict note said values were excluded when a verified value was used | A conflict records whether a reviewed value was included, and the note says so | `compute_facts` (`included`) |
| 48 | P3 | Locate re-derived word forms for every passage | `_word_forms` is cached | `test_locate_scoring.py` |

## Attribute canonicalization (user item 3)

`attributes.same_attribute` treats two names as one attribute only when all of these hold:

- they have the same distinctive words up to form (singular or plural, with or without a prefix or article, final letters unified);
- they have the same measure words ("גודל" and "שטח" are one measure; "גובה" and "רוחב" are two);
- they have the same qualifiers ("נטו", "ברוטו", "כולל", "רשום");
- their unit dimensions do not conflict.

`resolve_attribute` uses the rule after the exact label and before creating a new definition, and records the new phrasing as an alias. Numeric and non-numeric definitions never share facts: a numeric definition extracts only numbers. Text, boolean and date requests share one definition. The exact-label path now checks the dimension too, and a new definition's key includes its dimension.

The interpreter sees each definition's `value_type`. Handles keep their numbers when a definition is hidden. The interpreter is shown every definition that was read at least once, up to 60.

Counter-tests: `test_attribute_identity.py` and `test_attributes_registry.py::test_names_that_differ_in_meaning_or_unit_never_merge`. Reuse without re-reading: `test_a_rephrased_question_reuses_extracted_facts_without_reading_again`.

## Topic change and grounded places

The model sometimes copied the conversation's city into a topic change (GQ49.2, AE4). It also added a city the question never named (GQ40 Givatayim, GQ43 Tel Aviv). A place condition is now kept only when the turn's own words name it (`plan._grounded_places`). A follow-up still inherits places through the state.

A "change_clarification" with nothing pending that only states a condition is a follow-up (GQ48.3).

Pinned by `test_plan.py::test_a_place_the_question_does_not_name_is_dropped`, `…topic_change_does_not_keep_the_conversations_city…` and `…change_with_nothing_pending…is_a_follow_up`.

## Findings not fixed (residual)

| # | severity | finding | why it stays |
|---|---|---|---|
| 1, 4 | P1 (maintainability) | `facts.py` (≈1,300 lines) and `turn.py` (≈1,500 lines) each do several jobs | Pure moves along the seams the review named. This round changed behavior in both files, so the move was kept out of it to keep the diffs reviewable. Recommended as the next change, with no behavior change |
| 18 | P2 (performance) | The question-fallback address probe loads every chunk that contains a common word | Cost grows with the corpus. The demo corpus is small; no bound was measured |
| 27 | P2 (performance) | The gazetteer re-parses narrative report headers on every turn | Same: correct, but not cached per data version |
| 44 | P3 (data) | Legacy MVP conversations' conditions are not backfilled into the new state column | Old conversations start without context. No user data is lost |
| 45 | P3 (performance) | The document set and version metadata are recomputed up to three times per fact turn | Correct, redundant reads |
| — | maintainability | The answer payload is an untyped dict; `compose_answer` switches on demo mode; `list_attribute_handles` writes | Structural; no behavior defect |
| — | residual | The answer to "אין X" depends on whether the attribute's name includes the qualifier. "אין חניה צמודה" is zero for "מספר מקומות חניה צמודים" and goes to review for "מספר מקומות חניה" | Deliberate: an unnamed qualifier is held back for review, never counted |
| — | residual | Two appraisals of one property at different dates are one entity. A time-dependent value (a price) that differs between them is a conflict sent to review, not two observations | KTD8 keys entities by property, not by date |
| — | residual | `year_from` is sometimes used by the model in place of a value condition on a year attribute (GQ40, 2 of 3 replays) | The prompt states the rule. The server has no structural signal to tell them apart |
| — | residual | Property type, area basis, data kind and VAT basis cannot filter a computation over extracted facts | The answer now says so in a limitation, instead of silently showing the condition chip (GQ38) |
