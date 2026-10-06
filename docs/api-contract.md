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

## Chat

| Method | Path | Notes |
|---|---|---|
| GET | `/api/conversations` | `{conversations: [{id, title, updated_at}]}` |
| POST | `/api/conversations` | new conversation with no confirmed conditions → `{id}` |
| GET | `/api/conversations/{id}` | `{id, title, confirmed_conditions, pending_clarification, messages: [Message]}` |
| POST | `/api/ask` | `{conversation_id?, question?, filters?: {city?, neighborhood?, data_kind?, date_field?, year_from?, year_to?}, clarification?: {key, value}}` → `{conversation_id, question_id, answer: Answer}` |

`Message`: `{question_id, question, answer: Answer | null, stale: bool, hidden: bool, created_at}`. `stale` means the data version or the user's permissions changed since the answer was produced (show a badge and "שאל שוב"). `hidden` means a source was deleted or became unauthorized: render one placeholder line, no body, no sources.

`Answer`:

```json
{
  "kind": "numeric | clarification | content | combined | abstain",
  "text": "plain text; [E1] markers refer to sources by evidence id",
  "provider": "template | mock | cloud | extractive",
  "demo": false,
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
  "sources": [{"evidence_id": "E1", "document_id": "...", "version_id": "...", "title": "...", "page_list": [3], "section": null, "row": 2, "snippet": "...", "url": "/api/documents/<d>/versions/<v>/file#page=3"}],
  "coverage": {"text": "...", "docs_pending": 0, "docs_failed": 1, "docs_needs_review": 0, "records_awaiting_verification": 2},
  "limitations": ["..."]
}
```

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
