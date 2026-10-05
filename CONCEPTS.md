# Concepts

Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Tenancy and access

### Office

An appraisal firm using the system, and the hard isolation boundary: every document, record, question, answer, and cached result belongs to exactly one office, and nothing of one office is ever visible to, counted for, or revealed to another.

A user belongs to exactly one office, and the office is always taken from the user's authenticated session, never from anything the client sends.

### Document group

A named set of an office's documents that is the unit of access below the office: an office admin sees every group, while an employee sees only the documents of the groups they are a member of.

Group visibility follows a document into everything derived from it, so a record or passage that comes from a group the user cannot see never appears in their answers, statistics, sources, or review queue.
