# API contract

All endpoints live under `/api`. The browser reaches them through the Next.js proxy, so the session cookie (`rag_session`, HttpOnly, SameSite=Lax) is same-origin. The server derives the office and user from the session only; any `office_id` sent by a client is ignored.

Errors return `{"detail": "<Hebrew message>"}` with status 400/401/403/404/409/413/415/422/429. 404 is used for anything the user may not see, so existence never leaks.

Money, areas, and prices per sqm are serialized as **strings** (exact decimals). Dates are ISO `YYYY-MM-DD`.

## Auth

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/auth/login` | `{email, password}` | `{ok: true}` + cookie |
| POST | `/api/auth/logout` | — | `{ok: true}` |
| GET | `/api/auth/me` | — | `{user: {id, email, full_name, role: "admin"\|"employee", can_upload}, office: {id, name}, groups: [{id, name}], demo_mode}` |

## Documents

| Method | Path | Notes |
|---|---|---|
| POST | `/api/documents` | multipart: `files` (1–20), `group_id`, optional `document_id` (upload a new version). Response `{results: [{filename, status: "accepted"\|"duplicate"\|"rejected", reason?, document_id?, version_id?}]}`. A rejected file never blocks the others. |
| GET | `/api/documents?q=&status=` | `{documents: [DocumentSummary]}` |
| GET | `/api/documents/{id}` | `DocumentSummary` plus `versions: [Version]` |
| DELETE | `/api/documents/{id}` | logical delete, admin or uploader; `{ok: true}` |
| GET | `/api/documents/{doc_id}/versions/{version_id}/file` | streams the original (`application/pdf` inline). Link to a page with `#page=N`. |
| GET | `/api/search?q=&limit=` | hybrid content search: `{results: [{chunk_id, document_id, version_id, title, page_list, section, snippet, score}]}` |

`DocumentSummary`: `{id, title, group: {id, name}, deleted, created_at, current_version: Version | null, versions_count}`

`Version`: `{id, version_no, filename, status: "pending"|"processing"|"ready"|"needs_review"|"failed"|"superseded", status_reason, page_count, pages_incomplete, records_total, records_needing_review, is_current, created_at, processed_at, mime_type}`

## Review

| Method | Path | Notes |
|---|---|---|
| GET | `/api/review/queue?document_id=` | `{items: [ReviewItem]}` |
| GET | `/api/review/records/{occurrence_id}` | `RecordDetail` |
| POST | `/api/review/records/{id}/approve` | `{note?}` → `RecordDetail` |
| POST | `/api/review/records/{id}/correct` | `{field, value, note}` (note required) → `RecordDetail`; 422 with Hebrew message on invalid number/date |
| POST | `/api/review/records/{id}/reject` | `{note}` → `RecordDetail` |
| POST | `/api/review/dedup/{id}/merge` | → `{ok: true}` |
| POST | `/api/review/dedup/{id}/keep-separate` | → `{ok: true}` |

`ReviewItem` is one of:

- `{kind: "record", id, document: {id, title}, version_id, page_no, verification_status, summary: {data_kind, city, neighborhood, address, price, area, area_type, transaction_date, valuation_date}, flags: {conflict, missing_critical: [field], ocr}}`
- `{kind: "dedup", id, reason, a: TransactionSummary, b: TransactionSummary}` where `TransactionSummary = {transaction_id, data_kind, address, price, area, area_type, transaction_date, sources: [Source]}`

`RecordDetail`: `{id, document: {id, title}, version_id, page_no, table_index, row_index, text_span, is_docx, verification_status, conflict_flag, missing_critical, ocr, review_note, fields: [{field, label, original_text, normalized_value, status, source_path, previous: [...]}], computed_price_per_sqm, stated_price_per_sqm, calc_definition, file_url}`

Field names: `data_kind, city, neighborhood, address, block, parcel, sub_parcel, property_type, rooms, transaction_date, valuation_date, report_date, area, area_type, price, currency, vat_basis, price_per_sqm_stated`.

### Facts review (extracted attributes, KTD9, R16)

Facts are values of attributes nobody anticipated (for example a safe-room area), extracted on demand with a verbatim quote. Whoever can see a fact's document may review it; every read and write runs under the reviewer's RLS, so a fact in a document outside the reviewer's groups is a 404, and a conflicting value in such a document is never shown, not even as a flag. Only facts of current, undeleted versions at the attribute's current extraction version are listed.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/review/facts` | `{attributes: [{attribute: Attribute, documents: [{document: {id, title}, version_id, facts: [Fact]}]}]}`: facts with status `needs_review` or `auto_validated`, grouped by attribute then document, `needs_review` first |
| GET | `/api/review/facts/{fact_id}` | `Fact` plus `attribute: Attribute` and `facts_version` |
| POST | `/api/review/facts/{fact_id}/approve` | `{note?}` → status `verified`; returns the detail |
| POST | `/api/review/facts/{fact_id}/reject` | `{note}` (required; 422 `"יש לתעד את סיבת הדחייה"`) → status `rejected` |
| POST | `/api/review/facts/{fact_id}/correct` | `{value, unit, note?}` → status `corrected`. `value` is a non-negative number (thousands separators allowed; otherwise 422 `"יש להזין מספר תקין (לדוגמה 12 או 12.5)"`); `unit` is one of the attribute's `unit_options[].code` (`""` = no unit where allowed; otherwise 422 `"יש לבחור יחידה המתאימה למאפיין זה"`); a non-numeric attribute cannot be corrected here (422 `"ניתן לתקן כאן רק ערכים מספריים"`) |

`Attribute`: `{id, label, value_type: "numeric"|"text"|..., unit_dimension, canonical_unit, canonical_unit_label, unit_options: [{code, label}], facts_version}`.

`Fact`: `{id, status: "needs_review"|"auto_validated"|"verified"|"corrected"|"rejected", value, unit, unit_label, original: {value_text, unit, unit_label}, quote, page, url, document: {id, title}, version_id, entity_role: "subject"|"comparable"|"other", entity_descriptor, review_note, reviewed_at, previous: [history entries], conflicts: [brief Fact]}`. `value` is in the canonical unit; `original` is what the document says; `url` opens the source page (`#page=N`).

Every change appends the prior state to `previous`, bumps only that attribute's `facts_version` (answers built on the old facts become stale, see `Message.stale`) and writes an audit event (`fact_approve`, `fact_reject`, `fact_correct`). Trust tiers in answers: `verified` and `corrected` facts make the main figure; `auto_validated` facts are added only to the separately labeled preliminary figure; `needs_review` facts are excluded and counted in `coverage.facts.awaiting_review`.

## Chat

| Method | Path | Notes |
|---|---|---|
| GET | `/api/conversations` | `{conversations: [{id, title, updated_at}]}` |
| POST | `/api/conversations` | new conversation with an empty context → `{id}` |
| GET | `/api/conversations/{id}` | `{id, title, confirmed_conditions, pending_clarification, context, messages: [Message]}`; another user's conversation is 404 |
| POST | `/api/ask` | `AskBody` → `{conversation_id, question_id, turn_id, answer: Answer}` |

`AskBody`:

```json
{
  "conversation_id": "uuid (optional; a new conversation when absent)",
  "turn_id": "uuid (optional; generated by the server when absent)",
  "question": "free text (optional when clarification, filters or remove is given)",
  "clarification": {"key": "data_kind", "value": "transaction_price"},
  "filters": {"city": "...", "neighborhood": "...", "data_kind": "...", "date_field": "...", "year_from": 2024, "year_to": 2024, "property_type": "...", "area_type": "...", "vat_basis": "..."},
  "remove": ["years", "city", "attribute"]
}
```

- **Turns are idempotent (`turn_id`).** The client generates one `turn_id` per user action and reuses it on retries. A turn that already finished returns its stored result unchanged; one still running answers **409** `"השאלה עדיין בעיבוד"` (poll by posting the same body again); a failed turn runs again.
- **The server holds the conversation context.** `filters` are explicit edits of the context chips, applied as a delta to this turn (they are not the whole panel state). An edit that contradicts a value the question itself states asks a `filter_conflict` clarification (options `"<key>:<value>"`). `remove` clears chips by their `context.chips[].key` (`city`, `neighborhood`, `years`, `date_field`, `data_kind`, `property_type`, `area_type`, `vat_basis`, `attribute`, `metric`). A request with only `filters`/`remove` re-runs the last answered task with the edited context.
- **Clarifications** can be answered with a button (`clarification: {key, value}`) or in free text (`question`). Free text that answers the pending clarification resolves it (`answer.interpretation_note` says how it was read); an unrelated question is answered and the clarification stays open. A button with no matching pending clarification is **409**.
- Two concurrent turns on one conversation: the later one is re-applied once to the fresh context; if the context changes again meanwhile it answers **409** `"השיחה עודכנה בבקשה אחרת באותו זמן. נסו לשאול שוב."`.
- `"למה?"` and `"תראה לי את המקור"` answer from the previous answer's method and sources, re-authorized now (`answer.meta`), with no model call; they are never cached.
- A typed message while a clarification is pending is read as a change to it only when it keeps that task's attribute and metric and changes a condition ("ובגבעתיים?"); a self-contained question is a new question, and the clarification stays open.
- The stored turn (`questions.plan`) keeps `turn_plan`, the task the server ran after its policy (a turn the server answered with a clarification before any tool ran has task type `clarify`), and, for a model turn, `model_plan` as the model returned it.

`context` (for the chips): `{attribute, metric, city, neighborhood, years: {from, to} | null, data_kind, date_field, chips: [{key, label, value}]}`. `pending_clarification`: `{key, question, options}` or null; both survive refresh and reopen.

`Message`: `{question_id, question, answer: Answer | null, stale: bool, hidden: bool, created_at}`. `stale` means the data version or the user's permissions changed since the answer was produced, or a fact of an attribute the answer used was reviewed, corrected or newly extracted (only that attribute's facts version counts) — show a badge and "שאל שוב". `hidden` means a source was deleted or became unauthorized: render one placeholder line, no body, no sources.

`Answer`:

```json
{
  "kind": "numeric | clarification | content | combined | abstain",
  "text": "plain text; [E1] markers refer to sources by evidence id",
  "provider": "template | mock | cloud | extractive",
  "demo": false,
  "mode": "cloud | demo | limited | error",
  "clarification": {"key": "data_kind", "question": "...", "options": [{"value": "transaction_price", "label": "מחירי עסקאות"}]},
  "numeric": {
    "conditions": [{"label": "עיר", "value": "רמת גן"}],
    "record_count": 4,
    "mean_price_per_sqm": "25000.00",
    "weighted_price_per_sqm": "26666.67",
    "median_price_per_sqm": "25000.00",
    "min_price_per_sqm": "20000.00",
    "max_price_per_sqm": "30000.00",
    "currency": "ILS",
    "uncertain_duplicates": 0,
    "conflicts": 0
  },
  "preliminary": {"value": "13.00", "record_count": 2, "values": ["12", "14"]},
  "claims": [{"text": "...", "kind": "explicit | inferred | computed", "evidence_ids": ["E1"]}],
  "abstention_kind": "not_found | not_stated | not_extracted_or_verified | insufficient_permission_scope | null",
  "dropped_claims": 0,
  "sources": [{"evidence_id": "E1", "document_id": "...", "version_id": "...", "title": "...", "page_list": [3], "section": null, "row": 2, "snippet": "...", "url": "/api/documents/<d>/versions/<v>/file#page=3"}],
  "coverage": {"text": "...", "docs_pending": 0, "docs_failed": 1, "docs_needs_review": 0, "records_awaiting_verification": 2,
               "facts": {"in_scope": 9, "found": 5, "not_stated": 2, "partial_scan": 0, "pending": 2, "failed": 0, "not_yet_extracted": 2, "awaiting_review": 1, "conflicts": 0, "unknown_metadata": 0}},
  "method": "חילוץ הנתון מהמסמכים עם ציטוט מדויק וחישוב בקוד: 9 מסמכים בתחום, ערך נמצא ב-5",
  "conditions": [{"label": "עיר", "value": "רמת גן"}],
  "partial": false,
  "pending_extraction": 0,
  "interpretation_note": "התשובה הובנה כמענה לשאלת ההבהרה: מחירי עסקאות.",
  "cleared": [{"key": "city", "label": "עיר", "value": "גבעתיים"}],
  "limitations": ["..."],
  "cached": true
}
```

- A price question keeps the `numeric` block above. Another computed attribute uses `numeric: {conditions, attribute, operation, value, unit, record_count, values, value_filter, value_type}`; for an extracted attribute `record_count` and `value` cover reviewed values only, and `preliminary` (labeled separately) adds the values the server validated but no person reviewed. `coverage.facts` holds the extraction coverage counts.
  - `value_type` is `numeric`, or `text` / `boolean` / `date` for an attribute stated in words (a designation, a status): its operation is `count` or `values`, and `values` lists the distinct normalized values (the text shows how many documents state each).
  - `value_filter` (`{op: "<"|"<="|">"|">="|"="|"!=", value}` or null) is a condition on each case's value ("smaller than 11"). With `operation: "count"`, `value` is how many cases meet it and `record_count` is how many cases were observed (`"2"` of `3`); the text lists the matching documents. The condition also appears in `conditions` as `{"label": "תנאי על הערך", "value": "קטן מ-11 מ״ר"}`.
  - A question that names an address, block/parcel or document title computes and searches over the documents that name it only (never a mean over other properties); `steps[].args.documents` in the stored turn lists them. When one address is the subject of several documents, each document's statement is shown and `limitations` says so; an address that names no visible document is stated in `limitations`.
- Fact sources also carry `value` and `tier` (`verified | preliminary`); their `snippet` is the verbatim quote.
- A comparison adds `compare: {sides: [{label, document_id, version_id, evidence_count}], incomplete, missing_sides, conflicts}`. Sides come from the conversation's sources or from the documents the question names; one named document with several versions compares its two latest versions (labeled by version). With neither, a `referent` clarification asks which documents to compare.
- A document list (`locate`) shows only documents whose passages carry the question's topic, best first, with the pages of their supporting passages.
- `abstention_kind` is set on every abstention, including the price path when no verified record matches (`not_found`, or `not_extracted_or_verified` when matching records await verification) and a combined answer whose computation found nothing.
- `partial` is true when the turn deadline cut a step short or documents in scope are still being extracted; such answers are never cached. `cached` appears only on a cache hit (sources re-authorized, coverage recomputed). `cleared` lists the context items this turn cleared (a new question or topic change). `interpretation_note` is one line for the UI, or null.

## Admin (admin role only)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/admin/users` | `{users: [{id, email, full_name, role, can_upload, is_active, group_ids}]}` |
| POST | `/api/admin/users` | `{email, full_name, password, role, can_upload, group_ids}` |
| PATCH | `/api/admin/users/{id}` | any of `{role, can_upload, is_active, group_ids, password}`; deactivation revokes sessions |
| GET | `/api/admin/groups` | `{groups: [{id, name, document_count}]}` |
| POST | `/api/admin/groups` | `{name}` |
| GET | `/api/admin/settings` | `{cloud_llm_enabled, provider: "openai"\|"anthropic", provider_name: "OpenAI"\|"Anthropic (Claude)", model, key_present, mode, mode_status, untested, last_test: {provider, model, ok, status, tested_at} \| null, retention_note, acknowledged_at}` (see below) |
| PUT | `/api/admin/settings` | `{cloud_llm_enabled, acknowledge: true}`; enabling without `acknowledge: true` → 422; every change bumps `settings_version`; returns the settings object |
| POST | `/api/admin/provider/test` | no body; runs the connection test and returns the settings object with the new `last_test`; employee → 403 |

Provider status (R8, KTD5; derived in `backend/app/providers/status.py`, the only place that decides which provider answers):

- `key_present` is a boolean only. The key, or any prefix of it, never appears in a response, log line or audit event.
- `mode`:
  - `cloud`: cloud use enabled, key present, and the last connection test passed or has not run for the current provider and model (`untested: true`).
  - `error`: enabled and the last test failed, or enabled with no key. `mode_status` names the failure (`missing_key` or the test status). Questions are then answered from the sources only, with a limitation naming the failure; never with the demo mock.
  - `demo`: disabled and `DEMO_MODE` on: answers come from the clearly labeled mock (`provider: "mock"`, `demo: true`).
  - `limited`: disabled and not demo: answers are assembled from the sources only.
- Connection test: one structured call (purpose `test`) through the selected provider with a fixed synthetic Hebrew prompt (no office content), checking the parsed echo. It is therefore allowed while cloud use is off. With no key it records `missing_key` without any network call. `status` is one of `ok`, `missing_key`, `auth`, `model_unavailable`, `timeout`, `rate_limited`, `quota`, `refusal`, `incomplete`, `invalid`, `error`. The result is stored per office in `office_settings` (`provider_test_*`, `provider_tested_at`), audited as `provider_test`, and a result that changes the mode bumps `settings_version` so no answer cached under the old mode is served.
- `retention_note`: the selected provider's data-retention note shown with the acknowledgement (OpenAI: `store=false`, yet abuse-monitoring logs may be kept up to 30 days unless the organization has Zero Data Retention).
| GET | `/api/admin/coverage` | `{documents_by_status: {status: count}, records: {total, verified, awaiting_verification, needs_review}, open_dedup_candidates, review_queue_count}` |
