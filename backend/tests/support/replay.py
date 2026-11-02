"""The A/B replay's source rebuild (round 21 U1, KTD8, KTD9): a captured source's text, read again from stored data through the serve path
the turn read it by, without the model. A turn's diagnostics keep, per cited id, a locator (``engine.LOCATOR_FIELDS``); this module
turns one back into the text a verifier is shown. The old and the new serve path are compared by calling it before and after a change.

Test support only: the production code reads no locator back, and the private replay script (under ``real_documents/``, never committed)
imports this function. It reads only what ``tools.read_scope`` and ``reader.blocks_between`` read, under the tenant context it is given."""

from __future__ import annotations

from uuid import UUID

from app.chat import reader
from app.chat import tools as T
from app.db import TenantContext, tenant_tx


def rebuild_source_text(ctx: TenantContext, locator: dict) -> str:
    """The text of the source ``locator`` places, as the serve path gives it now. A whole table is read as ``read`` of the table reads it; a
    source with a block range is its blocks as ``tools._served_rows`` serves them, in reading order, one per line; a search hit with only a chunk is the chunk. A locator that
    places none of these raises ``ValueError``."""
    ref = {k: locator[k] for k in ("version_id", "reading_id", "block_start", "block_end", "table_index", "chunk_id")
           if locator.get(k) is not None}
    ws = T.Workspace(ctx=ctx)
    if locator.get("kind") == "table" and locator.get("table_index") is not None and not locator.get("chunk_id"):
        if locator.get("table_part"):
            raise ValueError("a part of a table is placed by its first row, which the locator does not hold")
        with tenant_tx(ctx) as conn:  # the text ``read`` returned to the verifier: the table with the context it is read with
            head = reader.version(conn, UUID(str(locator["version_id"])))
            return T._read_table(ws, conn, head, locator["table_index"]).text
    start = locator.get("block_start")
    if start is not None:
        with tenant_tx(ctx) as conn:
            version_id = UUID(str(locator["version_id"]))
            # the rows are served as the agent and the verifier are served them: a table stored reversed in drawn order, a page-text row of numbers
            # under its column labels (round 23 U4, KTD8) - one function, so the replay reads what they read
            rows = T._served_rows(ws, conn, version_id, reader.blocks_between(conn, version_id, start, locator.get("block_end")))
        return "\n".join(r.text for r in rows if r.text)
    if locator.get("chunk_id"):
        return T.read_scope(ws, "S0", "neighbors", ref=ref).text
    raise ValueError(f"the locator places no text: {locator}")
