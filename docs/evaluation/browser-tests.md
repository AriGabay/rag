# Browser tests (U15, gate 8)

Playwright 1.63 on Chromium only (Chrome Headless Shell 153). Run on 2026-10-05 on macOS 15.6.1 (Apple M1 Max) with Node 22.20.
The run used the real stack: the Compose API at `http://localhost:8000`, seeded by `backend/scripts/seed_demo.py` with synthetic data, and the production Next build served on the host.

## How to run

```bash
cd frontend
npx playwright install chromium           # once (~100 MB headless shell + Chromium)
npm run build
cp -R .next/static .next/standalone/.next/static && cp -R public .next/standalone/public
PORT=3000 HOSTNAME=127.0.0.1 node .next/standalone/server.js &   # proxies /api/* to BACKEND_URL (build time, default :8000)
npx playwright test                       # E2E_BASE_URL overrides http://localhost:3000
```

The config is `frontend/playwright.config.ts`: one worker, because the tests share one stack and its worker queue. Reports go to `frontend/playwright-report/`. Failure traces go to `frontend/test-results/artifacts/`. Screenshots go to `frontend/test-results/screenshots/`. All three directories are gitignored.

### Data rules the tests follow

- **Office A is read-only.** The tests only ask questions there, as `dana@demo.test` (and they log in as `admin-a` once to check the nav). Office A's documents and approvals are shared with the API eval and load runs.
- **Office B is the write target.** The tests upload `D5_synthetic_harozim_mixed_formats.pdf` and `BAD_synthetic_encrypted.pdf` there and approve one record.
  - Uploads are deduplicated by SHA-256, so each upload test first logically deletes leftover copies of its own fixture in office B through the API. This is a `beforeAll`, guarded so it only runs as `admin-b`. It makes reruns deterministic.
  - Deleted copies still appear to admins with a "נמחק" badge, and the row locators skip them.
- **Wrong-password test.** It uses `admin-b` and causes one failed login per run. The lockout is 10 failures in 15 minutes, and office A users are never touched.

## Round 7 (2026-10-06): full run after the review fixes, with the real model

This round used the stack rebuilt from the branch head, `seed_demo.py` (now also seeding the held-out v2 documents K1–K8 into office A, group G4), and office A in cloud mode (OpenAI `gpt-5.4-mini`). The office's consent was re-acknowledged for OpenAI: the per-provider consent check correctly put the office in limited mode first. The new specs are `chat-races.spec.ts` and `facts-review-stale.spec.ts`. Both mock the API with `page.route`, apart from login.

| run | result | notes |
|---|---|---|
| 1 | 28 passed, **2 failed**, 1 skipped | Two real defects, both fixed. **(a)** "אילו שומות מזכירות היתר בנייה?", asked while the data-kind clarification was open, was taken as the answer "שווי שנקבע בשומות": the model labeled it a reply, and the new structural check accepted it because the option shares the word "שומות". Now a question whose content words are not all words of the chosen option is a new question. **(b)** "ומה לגבי 2023?" in a new conversation fell to limited mode. A real-model replay planned `clarify` with no question in 2 of 3 runs, and the server rejected that as invalid. Now the server asks its own short question. Both have unit counter-tests |
| 2 | **30 passed**, 0 failed, 1 skipped | Full suite on the fixed head, including the real-model conversations (`general-conversations.spec.ts`, `chat-messages.spec.ts`) |

The skipped spec is "limited mode: the ממ״ד question…". It needs office A in limited mode. This stack runs with `DEMO_MODE=true`, so turning cloud off gives demo mode, not limited mode. A run with cloud briefly off confirmed the skip reason; cloud was then re-enabled. The limited path is covered by the API tests (`test_turns.py`, gate 7).

One passing run does not show that the real-model conversations are stable. The failure in run 1 came from model variance (2 of 3 replays) meeting a rigid server rule, which is why both fixes are server rules with tests, not prompt changes.

## Results (round 1, 2026-10-05)

`npx playwright test`: **14 passed** (41 s). 12 tests pass outright. The other 2 are marked `test.fail()`: each one exposes a confirmed app bug, fails as expected, and Playwright reports it as passed. `npm run typecheck` and `npm run lint` are clean.

| # | Spec › test | Covers | Result |
|---|---|---|---|
| 1 | `shell` › html is rtl/he, navigation is Hebrew, numbers stay in visual order | `<html dir="rtl" lang="he">`, body `direction: rtl`, Hebrew nav (no admin link for an employee), nav runs right to left, the year `2024` sits inside `<bdi>` and its glyphs are laid out left to right (measured with `Range` rects) | pass |
| 2 | `shell` › admin sees the admin link | Nav for admins | pass |
| 3 | `auth` › unauthenticated visit redirects to /login and returns after login | `/documents` → `/login` → back to `/documents` | pass |
| 4 | `auth` › wrong password shows a Hebrew error | "פרטי ההתחברות שגויים" in the form's `role=alert`, stays on `/login`; the empty-field message is in Hebrew | pass |
| 5 | `auth` › login then logout ends the session | Logout → `/login`; `/chat` bounces to `/login`; `/api/auth/me` is 401 | pass |
| 6 | `chat-messages` › AE1 | "מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת 2024?" shows the data-kind clarification (options "מחירי עסקאות" and "שווי שנקבע בשומות") with no number. Choosing transactions asks for the date type ("תאריך העסקה" or "המועד הקובע"), and the old clarification shows "כבר טופלה" | pass |
| 7 | `chat-messages` › unknown neighborhood abstains | The "אין מספיק מידע" badge, "אין במאגר המשרד רשומות עבור "נווה צדק"… לא חושב מספר", and no figures, ₪ or options | pass |
| 8 | `chat-messages` › AE4 + new conversation | A fully specified question, then the remaining clarifications (area basis, property type, VAT), give a numeric card. The mean (e.g. `25,336 ₪ למ״ר`) sits in `<bdi>` and its digits read left to right. "ומה לגבי 2023?" sent with **Enter** returns a numeric card whose conditions equal the 2024 ones except "תאריך העסקה: 2023 …". Reopening the conversation shows "תנאים שאושרו בשיחה" (חרוזים, 2023). **New conversation** clears that line, and the same follow-up asks for the data kind again (no conditions carried over) | pass |
| 9 | `chat-messages` › conversation started with "שיחה חדשה" is titled by its first question | History list usability | **expected fail, bug B2** |
| 10 | `full-flow` › upload, review, approve, ask, clarify, answer, open source (office B) | Upload through the file input gives the per-file result "התקבל לעיבוד". The status table polls until "מוכן" or "דורש בדיקה". **פרטים** opens the document panel, then "לבדיקת הנתונים של המסמך" opens the review queue filtered to that document. The record is shown beside an `iframe` whose `src` matches `/api/documents/…/versions/…/file#page=N` (in RTL the record is to the right of the PDF). Approve with a note shows "הרשומה אושרה." and "הערת בדיקה: …". In chat, the data-kind and date-type option buttons lead to a numeric card with "ממוצע מחירי המ״ר" and "מחיר משוקלל = סך מחירים חלקי סך שטחים", both values like `26,720 ₪ למ״ר`, the record count, and conditions (רמת גן / חרוזים / מחירי עסקאות). The sources list links to `/api/…/file#page=N` with `target=_blank`. `fetch(href)` with the page's cookie returns 200, `application/pdf`, and a body starting `%PDF-`. Clicking the link opens a new tab whose request for the file returns 200 `application/pdf` | pass |
| 11 | `keyboard` › choose a file with the file input and upload with Enter (office B) | Log in with the keyboard. Tab to the file input, then Space opens the chooser. Tab to "העלאה", then Enter. Result "התקבל לעיבוד". Tab to "פרטים", then Enter; the panel shows "נכשל" | pass |
| 12 | `keyboard` › ask with Enter and answer the clarification with the keyboard (office A) | Tab to "שיחה חדשה", then Enter. Tab to the question, type, then Enter. Focus moves to the new answer card. Tab to "מחירי עסקאות", then Enter gives the date-type clarification, which is also focused. Shift+Enter inserts a newline and does not send | pass |
| 13 | `office-b-failure` › password-protected PDF shows "נכשל" with the Hebrew reason | `BAD_synthetic_encrypted.pdf` is accepted. The list row turns "נכשל" with "מוגן בסיסמה", and the document panel shows "הקובץ מוגן בסיסמה ולא ניתן לעבד אותו" | pass |
| 14 | `office-b-failure` › status filter "נכשל" lists only failed documents | Documents status filter | **expected fail, bug B1** |

Screenshots (regenerated on every run):

- `frontend/test-results/screenshots/review-record-beside-pdf.png`: the review screen with the record table beside the source frame (office B, D5, page 1).
- `frontend/test-results/screenshots/chat-numeric-answer.png`: the office B clarification chain and the numeric answer card with figures, conditions, limitations, coverage and sources.
- `frontend/test-results/screenshots/chat-followup-2023.png`: the office A follow-up answer for 2023.

In headless Chromium the PDF iframe paints blank because the headless shell has no PDF viewer. The tests therefore check the iframe's `src` and fetch the file instead of looking at the rendering.

## Bugs found

**B1. The documents status filter is ignored by the API.** `backend/app/platform/documents.py:211`

```python
def list_documents(q: str | None = None, status_filter: str | None = None, ...)
```

FastAPI reads the query parameter `status_filter`, but `docs/api-contract.md` and `frontend/lib/api.ts` (`api.documents`) send `status`. Choosing "נכשל" (or any other status) in the documents screen therefore returns every document. Confirmed with the API directly: `?status=failed` returns all documents, while `?status_filter=failed` returns only the failed ones.

Fix: `status_filter: str | None = Query(None, alias="status")`. The rename exists to avoid shadowing `fastapi.status`.

Exposed by `e2e/office-b-failure.spec.ts` › status filter (`test.fail()`).

**B2. Conversations started from "שיחה חדשה" keep the placeholder title forever.** `backend/app/answering/api.py:67-77`

`POST /api/conversations` stores the title "שיחה חדשה". `/api/ask` sets a title from the question only when it creates the conversation itself (`_conversation(..., title)`). Every conversation started from the button therefore stays "שיחה חדשה" in the history list, even after questions are asked, and they cannot be told apart.

Fix: when saving the first question of a conversation whose title is still the placeholder, set `title = question[:80]`. For example, in `_save`: `UPDATE conversations SET title = :q WHERE id = :c AND title = 'שיחה חדשה' AND NOT EXISTS (SELECT 1 FROM questions WHERE conversation_id = :c AND id <> :qid)`. A nullable title with a frontend fallback would also work.

Exposed by `e2e/chat-messages.spec.ts` › conversation title (`test.fail()`).

### Observations (not marked as failures)

- **Retry after a failed upload.** Re-uploading the same bytes after a failed version is accepted as a new document rather than reported as "כבר הועלה", because `docs_hash_lookup` skips `failed` versions. This looks intentional (it allows a retry), but repeated retries of an unprocessable file create several failed documents.
- **Confirmed conditions only appear after reopening.** The chat page shows "תנאים שאושרו בשיחה" only when a conversation is reopened from the list. It does not appear right after an answer in the active conversation (`frontend/app/chat/page.tsx`, where `activeConditions` is set only in `openConversation`). The numeric card does show its own conditions.
- **English codes in the review table.** The review table's "ערך מנורמל" column shows internal codes such as `appraised_value`, `apartment`, `net` and `included`, not Hebrew labels.
- **Median hidden below three records.** The median is `null` for fewer than 3 records (`backend/app/answering/service.py:125`), so the card shows "חציון: —". This is by design.
