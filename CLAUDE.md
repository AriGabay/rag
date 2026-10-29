# Project instructions

## Development model split (Claude Code)

These rules cover how Claude Code builds this project. They do not cover the models the RAG system itself calls.

- **Opus** (main agent): planning, diagnosis, problem solving and architecture decisions. Opus defines the solution and the acceptance criteria and checks what other agents return.
- **Sonnet**: implementation of defined units, and code and document review.
- **Haiku**: simple tasks such as code search, gathering information, running tests and small documentation edits.
- Set the model explicitly on every subagent. Do not run everything on Opus, and do not give two agents the same work.
- A complex problem found during implementation goes back to Opus for diagnosis and a decision, then to the right agent for the fix.

The RAG system's own models (chat, extraction, verification; `gpt-6-luna` by default in `backend/app/config.py`) are product settings. Do not change them as part of development work unless the user asks.

## Data safety

- The repository is public. Never commit customer documents, appraisal content, addresses, names, conversation text or keys. Real documents live only in the gitignored `real_documents/` and in the local office database.
- Never delete or reset appraisals, documents, conversations, permissions or stored data. If an action would delete anything, stop and get explicit approval first. State what would be deleted, why, the alternatives and the backup.
- Take a backup (`scripts/backup.sh`) before applying a migration to the office database.

## Tests

- Run pytest on the host only, never inside a compose container: `cd backend && env -u DATABASE_URL -u OWNER_DATABASE_URL .venv/bin/pytest`. The suite refuses any database other than `rag_test`.
- Never run two pytest processes at the same time.
- Tests use synthetic documents and the synthetic office B. Never write tests against office A, which holds real reports.
- Real-model tests are opt-in (`-m real_model`) and read the provider key from `.env`. Never print the key.

## Local stack

- Rebuild the backend image through the `migrate` service (`docker compose build migrate frontend`), then force-recreate backend, worker and frontend. `docker compose build backend` keeps the old image. After a rebuild, check a changed constant inside the container before measuring anything.

## Pull requests

- Update the existing PR. Do not merge or deploy to production unless the user asks.
