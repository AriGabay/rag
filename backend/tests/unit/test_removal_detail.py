"""Round 13 U12 (R17, KTD14): a removal's detail survives to the user. The stored original claim keeps up to 2,000
characters (it was cut at 300), and the normal path's verification keeps the count of claims the server edited. No
database: the shaping functions run on built records. Every text is synthetic."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.chat import api, decisions, verify
from app.db import TenantContext

LONG = "שורה סינתטית של טענה ארוכה על שווי הנכס ועל שיעור ההיוון. " * 20  # ~1,200 characters, over the old 300


def _removed(raw: str) -> verify.Logged:
    unit = verify.Unit(index=0, raw=raw, text=raw, ids=["S1"], start=0, end=len(raw))
    problem = verify.Problem(unit, "מספר לא נמצא במקור", failure_kind="absent_from_source", claim_id="k0123456789ab")
    return verify.Logged("k0123456789ab", "removed", unit, [], problem)


def test_a_removal_longer_than_300_characters_is_kept_whole_up_to_2000():
    assert 300 < len(LONG) < 2000
    assert decisions.decision(_removed(LONG), [])["text"] == LONG
    huge = "א" * 5000
    assert decisions.decision(_removed(huge), [])["text"] == huge[:decisions.TEXT_LIMIT] and decisions.TEXT_LIMIT == 2000


def test_public_verification_keeps_the_edited_count():
    shown = api.public_verification({"judged": 5, "judge_status": "ok", "removed": 1, "edited": 3})
    assert shown["edited"] == 3, shown
    assert "edited" not in api.public_verification({"judged": 5, "judge_status": "ok"}), "older answers have none"


# --- round 14 U7 (R14, KTD9, AE10): a partial mark opens a structured detail ---------------------------------------------

CLAIM = "k0123456789ab"
KEPT = "דמי הניהול במגדל סינתטי הם 33 ₪ למ\"ר"
UNSTATED = "לחודש"


def _qualified(*, whole_unit: bool = False) -> verify.Logged:
    raw = f"{KEPT} {UNSTATED} [S1]."
    unit = verify.Unit(index=0, raw=raw, text=raw, ids=["S1"], start=0, end=len(raw))
    record = verify.ClaimRecord(
        id=CLAIM, unit_id="u1", unit=0, start=0, end=len(raw), text=raw if whole_unit else UNSTATED, kind="datum",
        verdict="partial", action="qualified", sources_checked=["S1", "S2"], whole_unit=whole_unit,
        rereads=[{"source": "S1", "scope": "next_page", "outcome": "not_found"},
                 {"source": "S2", "scope": "continuation", "outcome": "partial", "expanded_source": "S3"}])
    problem = verify.Problem(unit, "נתמך חלקית: התקופה לא צוינה במקור", "partial", defect="units", failure_kind="wrong_unit",
                             check="judge", claim_id=CLAIM)
    return verify.Logged(CLAIM, "qualified", unit, [record], problem)


def test_a_qualified_entry_states_what_was_supported_what_was_not_why_and_what_was_checked():
    entry = decisions.decision(_qualified(), [])
    assert KEPT in entry["supported"]
    assert entry["not_verified"] == UNSTATED
    assert entry["why"] == {"failure_kind": "wrong_unit", "check": "judge"}
    assert entry["checked"]["sources"] == ["S1", "S2"]
    # every re-read is listed, with the source it expanded to
    assert [(x["source"], x["scope"], x["outcome"]) for x in entry["checked"]["rereads"]] == [
        ("S1", "next_page", "not_found"), ("S2", "continuation", "partial")]
    assert entry["checked"]["rereads"][1]["expanded_source"] == "S3"
    assert entry["remaining"] == UNSTATED and entry["claim_id"] == CLAIM  # the existing fields stay


def test_a_unit_judged_partial_as_a_whole_names_the_judges_reason_as_what_was_not_verified():
    entry = decisions.decision(_qualified(whole_unit=True), [])
    assert "התקופה לא צוינה במקור" in entry["not_verified"] and entry["supported"]


def test_only_a_qualified_entry_has_the_structured_detail():
    removed = decisions.decision(_removed("טענה סינתטית"), [])
    assert not {"supported", "not_verified", "why", "checked"} & set(removed)


def test_public_partials_show_the_class_sentence_and_ids_and_nothing_the_row_was_stored_with():
    stored = [{"ordinal": 1, "claim_id": CLAIM, "claim_ids": [CLAIM], "failure_kind": "wrong_unit", "check": "judge",
               "text": "טענה סינתטית שלא אמורה לעבור", "draft": KEPT},
              {"ordinal": 2, "claim_id": "not-an-id", "claim_ids": ["x"], "failure_kind": "free text", "check": "x y",
               "text": "אחר"},
              {"ordinal": 0, "failure_kind": "wrong_unit"}, "junk"]
    shown = api.public_verification({"judged": True, "judge_status": "ok", "partials": stored})["partials"]
    assert shown[0] == {"ordinal": 1, "claim_id": CLAIM, "claim_ids": [CLAIM], "failure_kind": "wrong_unit",
                        "check": "judge", "text": verify.PARTIAL_SENTENCES["wrong_unit"]}
    # an unknown class reads as the unspecified one, an id that is no claim id is dropped, a row with no ordinal is dropped
    assert shown[1] == {"ordinal": 2, "failure_kind": None, "text": verify.PARTIAL_SENTENCES["unspecified"]}
    assert len(shown) == 2


def test_a_message_stored_before_partial_rows_reads_without_them_and_without_error():
    shown = api.public_verification({"judged": True, "judge_status": "ok", "removed": 0, "partial": 1})
    assert shown["partial"] == 1 and "partials" not in shown
    assert "partials" not in api.public_verification({"judged": True, "judge_status": "ok", "partials": "x"})


class _Row(SimpleNamespace):
    """A messages row as ``_message_json`` reads it."""


def _row(role: str, owner: UUID) -> _Row:
    return _Row(status="done", error=None, age=0, answer={"kind": "rag", "markdown": "x"}, content="x", role=role,
                id=uuid4(), progress=[], reply_to=None, client_id=None, created_at=datetime.now(UTC),
                cancel_requested=False, time_preference=None, usage=[], user_id=owner, data_version=None,
                scope_hash=None, conversation_id=uuid4())


def _ctx(role: str, user_id: UUID) -> TenantContext:
    return TenantContext(office_id=uuid4(), user_id=user_id, role=role)


def test_the_message_says_whether_the_viewer_may_open_diagnostics():
    owner, other = uuid4(), uuid4()
    row = _row("assistant", owner)
    assert api._message_json(row, ctx=_ctx("employee", owner))["can_view_diagnostics"] is True
    assert api._message_json(row, ctx=_ctx("admin", other))["can_view_diagnostics"] is True
    assert api._message_json(row, ctx=_ctx("employee", other))["can_view_diagnostics"] is False
    assert api._message_json(row)["can_view_diagnostics"] is False  # no viewer: nothing is offered
    assert api._message_json(_row("user", owner), ctx=_ctx("employee", owner))["can_view_diagnostics"] is False
