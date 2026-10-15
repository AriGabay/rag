"""Round 13 U12 (R17, KTD14): a removal's detail survives to the user. The stored original claim keeps up to 2,000
characters (it was cut at 300), and the normal path's verification keeps the count of claims the server edited. No
database: the shaping functions run on built records. Every text is synthetic."""

from __future__ import annotations

from app.chat import api, decisions, verify

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
