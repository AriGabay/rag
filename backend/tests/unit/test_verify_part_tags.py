"""The tags of a table read's context parts (round 17 U4: ``[לפני הטבלה · פסקה · בלוק 47] text``) are in the verifier-visible
``Source.text``, but the block number in a tag is not a number the source states (round 17 U2, extra task): it never backs a
claim's number, and only the part's own text counts. Synthetic text only."""

from __future__ import annotations

import uuid

from app.chat import tools as T
from app.chat import verify
from app.chat.tools import Workspace
from app.chat.verify import split_units

TABLE = "\n".join(["ניתוח רגישות לשווי", "תרחיש | שווי", "בסיס | 4,100,000", "אופטימי | 4,400,000"])
AFTER = T.TablePart("after", "paragraph", 47, "הטבלה מתייחסת לשלושה תרחישים בלבד", False)
BEFORE = T.TablePart("before", "paragraph", 12, "הטבלה שלהלן מציגה ניתוח רגישות", True)


def _ws(*parts: T.TablePart) -> Workspace:
    ws = Workspace(ctx=None)
    text = "\n".join([*(T._part_line(p) for p in parts if p.side == "before"), TABLE,
                      *(T._part_line(p) for p in parts if p.side == "after")])
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="רגישות",
                  location="טבלה 1", kind="table", text=text)
    return ws


def _unstated(text: str, ws: Workspace) -> list[str]:
    return [n for p in verify.deterministic(split_units(text), ws, "?") if p.check == "unstated_number"
            for n in p.unstated]


def test_the_helper_removes_the_tag_of_a_part_and_keeps_the_text_of_the_part():
    line = T._part_line(AFTER)
    assert "בלוק 47" in line
    stripped = verify.without_part_tags(f"{line}\n{T._part_line(BEFORE)}\n{TABLE}")
    assert "47" not in stripped and "12" not in stripped
    assert "הטבלה מתייחסת לשלושה תרחישים בלבד" in stripped and "בסיס | 4,100,000" in stripped


def test_the_pointer_of_a_part_the_turn_already_received_is_removed_too():
    line = T._part_line(AFTER, {47: "S13"})
    assert "S13" in line and "13" not in verify.without_part_tags(line)


def test_a_claim_number_equal_to_a_blocks_number_and_absent_from_the_text_is_still_unstated():
    ws = _ws(AFTER)
    assert _unstated("הטבלה מציגה 47 תרחישים, והשווי בבסיס הוא 4,100,000 ₪ [S1].", ws) == ["47"]


def test_a_number_the_text_of_a_part_states_stays_stated():
    ws = _ws(T.TablePart("after", "paragraph", 47, "הטבלה מתייחסת ל-3 תרחישים בלבד", False))
    assert _unstated("הטבלה מתייחסת ל-3 תרחישים [S1].", ws) == []


def test_a_number_of_the_table_stays_stated_when_parts_are_present():
    ws = _ws(AFTER, BEFORE)
    assert _unstated("השווי בבסיס הוא 4,100,000 ₪ [S1].", ws) == []


def test_the_block_number_is_not_in_the_turns_number_pool_but_the_parts_text_numbers_are():
    ws = _ws(T.TablePart("after", "paragraph", 47, "הטבלה מתייחסת ל-3 תרחישים בלבד", False), BEFORE)
    pool = verify._all_numbers(ws)
    assert "47" not in pool and "12" not in pool
    assert {"3", "4100000"} <= pool


def test_an_uncited_unit_with_a_block_number_is_unstated_against_the_whole_turn():
    ws = _ws(AFTER)
    assert _unstated("יש 47 תרחישים.", ws) == ["47"]


def test_the_number_check_of_a_cited_source_reads_the_text_without_tags():
    ws = _ws(AFTER)
    body = dict(verify._texts(ws, ["S1"]))["S1"]
    assert "47" not in body and "בסיס | 4,100,000" in body
    # the judge still reads the tags: the verifier-visible text is unchanged
    assert "בלוק 47" in verify._source_parts(ws, "S1")[1]
