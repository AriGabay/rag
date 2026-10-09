"""An assumption (A#) in a stored answer names the user message it quotes, so the breakdown can link back to it (U7).

Database-free: the turn's user messages are plain rows, and the stored assumptions are their public dicts."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from app.chat import api, calc

U1 = UUID("00000000-0000-0000-0000-000000000011")
U2 = UUID("00000000-0000-0000-0000-000000000012")
U3 = UUID("00000000-0000-0000-0000-000000000013")
A1 = UUID("00000000-0000-0000-0000-0000000000a1")


def _row(id_, role, content):
    return SimpleNamespace(id=id_, role=role, content=content)


def _assumption(aid: str, turn: int, current: bool) -> dict:
    return calc.Assumption(aid, Decimal("5"), "5%", "percent", "עליית עלויות", "אם העלויות יעלו ב-5%", turn,
                           current).public()


def test_user_message_ids_follow_the_engine_numbering():
    # the engine numbers the history's user messages with content (1 = first), then the current message
    rows = [_row(U1, "user", "שאלה ראשונה"), _row(A1, "assistant", "תשובה"), _row(U2, "user", ""),
            _row(U3, "user", "שאלה שנייה")]
    assert api._user_message_ids(rows, "current-id") == [str(U1), str(U3), "current-id"]


def test_an_assumption_names_the_user_message_it_quotes():
    ids = [str(U1), str(U3), "current-id"]
    earlier, current = _assumption("A1", 1, False), _assumption("A2", 3, True)
    out = api._assumption_origins([earlier, current], ids)
    assert [a["message_id"] for a in out] == [str(U1), "current-id"]
    # the user's own words stay with it
    assert out[0]["quote"] == "אם העלויות יעלו ב-5%"


def test_an_assumption_whose_message_is_unknown_has_no_message_id():
    out = api._assumption_origins([_assumption("A1", 4, False), _assumption("A2", 0, False)], [str(U1)])
    assert [a["message_id"] for a in out] == [None, None]
    assert api._assumption_origins([_assumption("A1", 1, True)], None)[0]["message_id"] is None
