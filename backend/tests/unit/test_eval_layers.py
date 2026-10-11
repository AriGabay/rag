"""The layered eval checks on synthetic data: ingestion checked against a table's structure (a number in the
right table but another row fails), retrieval against every required text, calculation results at a stated
precision, the run record (build, image, models per purpose, the reading of each document used), and a rescore
of stored results that regrades every layer without a model call or a running stack."""

from __future__ import annotations

import json

import pytest
import yaml

from eval.scoring import calc_check, retrieval_check, shown_at_precision, structure_check

# A synthetic comparison table split over two pages, as /blocks returns it (rows as cells, or with their page).
TABLE = {"kind": "table", "page": 7, "section": "טבלת השוואה", "section_path": ["5 תחשיב", "טבלת השוואה"],
         "text": "...", "table": {
             "headers": ["נכס", "שטח (מ\"ר)", "שווי למ\"ר (₪)"], "caption": "להלן עסקאות השוואה ברחוב הדגמה",
             "title": ["עסקאות השוואה"], "notes": ["(*) כולל מע\"מ"],
             "rows": [{"page": 7, "cells": ["חנות 3", "120", "9,500"]},
                      {"page": 7, "cells": ["חנות 5", "80", "11,000"]},
                      {"page": 8, "cells": ["משרד 2", "9,500", "7,200"]}]}}
OTHER = {"kind": "table", "page": 12, "section": "נספח", "section_path": ["נספח"], "text": "...",
         "table": {"headers": ["סעיף", "סכום"], "caption": "עלויות", "title": [], "notes": [],
                   "rows": [["פיתוח", "9,500"], ["תכנון", "2,000"]]}}
PARA = {"kind": "paragraph", "page": 4, "section": "תיאור הנכס", "section_path": ["3 תיאור הנכס"],
        "text": "השטח הבנוי של הנכס הוא 184 מ\"ר, והמגרש 610 מ\"ר."}
BLOCKS = [PARA, TABLE, OTHER]


def _item(**kw) -> dict:
    return {"id": "I1", "document": "שומה הגפן", "table": "עסקאות השוואה", "row": "חנות 3",
            "column": "שווי למ\"ר", "value": "9,500", "unit": "₪"} | kw


# --- structural ingestion --------------------------------------------------------------------------------------

def test_a_value_in_the_named_row_and_column_passes():
    assert structure_check(BLOCKS, _item()) == []
    assert structure_check(BLOCKS, _item(page=7, value="9500")) == []  # 9,500 and 9500 are one number


def test_the_same_number_in_another_row_fails_and_says_where_it_is():
    problems = structure_check(BLOCKS, _item(row="חנות 5"))
    assert problems and "11,000" in problems[0]  # the named cell holds another value
    assert any("חנות 3" in p for p in problems)  # and the expected number sits in another row


def test_the_same_number_in_another_column_fails():
    assert structure_check(BLOCKS, _item(row="משרד 2", column="שווי למ\"ר"))  # 9,500 is its area, not its value


def test_a_page_mismatch_fails():
    problems = structure_check(BLOCKS, _item(page=8))
    assert problems and "7" in problems[0] and "8" in problems[0]


def test_the_row_page_is_the_rows_own_page_in_a_table_over_two_pages():
    assert structure_check(BLOCKS, _item(row="משרד 2", column="שטח", value="9,500", unit="מ\"ר", page=8)) == []


def test_the_table_is_found_by_its_caption_or_heading_and_another_table_with_the_number_does_not_count():
    assert structure_check(BLOCKS, _item(table="עלויות", row="פיתוח", column="סכום", unit=None)) == []
    problems = structure_check(BLOCKS, _item(table="טבלה שאינה קיימת"))
    assert problems == ["לא נמצאה טבלה «טבלה שאינה קיימת»"]


def test_a_missing_unit_fails():
    problems = structure_check(BLOCKS, _item(unit="דולר"))
    assert problems and "דולר" in problems[0]


def test_an_unknown_row_or_column_fails_by_name():
    assert any("חנות 9" in p for p in structure_check(BLOCKS, _item(row="חנות 9")))
    assert any("מחיר" in p for p in structure_check(BLOCKS, _item(column="מחיר")))


def test_a_header_row_inside_the_body_names_the_columns():
    ocr = {"kind": "table", "page": 3, "section": "", "section_path": [], "text": "",
           "table": {"headers": [], "caption": "סקר שכירות", "title": [], "notes": [],
                     "rows": [["כתובת", "שכ\"ד חודשי"], ["הדגמה 1", "4,200"], ["הדגמה 2", "5,100"]]}}
    item = {"id": "I", "table": "סקר", "row": "הדגמה 2", "column": "שכ\"ד", "value": "5,100"}
    assert structure_check([ocr], item) == []
    assert structure_check([ocr], item | {"value": "4,200"})


def test_a_paragraph_value_under_its_heading():
    item = {"id": "I", "heading": "תיאור הנכס", "near": "השטח הבנוי", "value": "184", "unit": "מ\"ר", "page": 4}
    assert structure_check(BLOCKS, item) == []
    assert structure_check(BLOCKS, item | {"page": 5})
    assert structure_check(BLOCKS, item | {"heading": "נספח"})
    assert structure_check(BLOCKS, item | {"value": "185"})


# --- retrieval -------------------------------------------------------------------------------------------------

HITS = ["שווי למ\"ר בעסקאות ההשוואה: 9,500 ₪", "זכויות בנייה לפי תכנית מקומית", "דמי שכירות חודשיים"]


def test_retrieval_with_two_required_texts_fails_when_only_one_is_found():
    problems, data = retrieval_check(HITS, {"expect_all": ["זכויות בנייה", "היטל השבחה"]})
    assert problems == ["לא בתוצאות הראשונות: היטל השבחה"]
    assert data["ranks"] == {"זכויות בנייה": 2, "היטל השבחה": None}
    assert retrieval_check(HITS, {"expect_all": ["זכויות בנייה", "דמי שכירות"]})[0] == []


def test_retrieval_counts_only_the_top_k():
    assert retrieval_check(HITS, {"expect_all": ["דמי שכירות"], "k": 2})[0]
    problems, data = retrieval_check(HITS, {"expect": "שווי למ\"ר"})  # the single-text form keeps working
    assert problems == [] and data["rank"] == 1


# --- calculation -----------------------------------------------------------------------------------------------

def test_a_calculation_result_at_the_stated_precision():
    assert shown_at_precision("8.87", "8.871934", 2)
    assert not shown_at_precision("8.9", "8.871934", 2)  # fewer decimals than stated
    assert not shown_at_precision("8.88", "8.871934", 2)
    assert shown_at_precision("8.872", "8.871934", 2)  # more decimals, each one right
    assert not shown_at_precision("8.874", "8.871934", 2)
    assert shown_at_precision("1,234,567", "1234567.4", 0)
    assert not shown_at_precision("1,234,568", "1234567.4", 0)


def test_calc_check_reads_the_displayed_numbers_of_the_answer():
    answer = {"status": "answered", "markdown": "התשואה היא 8.87% [C1], על הכנסה של 1,250,000 ₪ [S1]."}
    assert calc_check(answer, {"calc": [{"value": "8.8719", "precision": 2}]}) == []
    assert calc_check(answer, {"calc": [{"value": "1250000", "precision": 0}]}) == []
    rounded = {"status": "answered", "markdown": "התשואה היא כ-8.9% [C1]."}
    problems = calc_check(rounded, {"calc": [{"value": "8.8719", "precision": 2}]})
    assert problems and "8.8719" in problems[0]
    assert calc_check(rounded, {"calc": [{"value": "8.8719", "precision": 1}]}) == []
    assert calc_check(rounded, {"calc": [{"value": 8.87}]})  # a YAML float: precision is its own decimals


# --- the run record and a rescore without the stack -----------------------------------------------------------

def _stored() -> list[dict]:
    doc = {"id": "d1", "title": "שומה הגפן 12", "version_id": "v1", "ingestion_version": 4, "reading_id": "r-77",
           "blocks": BLOCKS}
    usage = [{"purpose": "agent", "model": "gpt-6-luna", "input_tokens": 100, "output_tokens": 10, "cost_usd": 0.01},
             {"purpose": "verify", "model": "gpt-5.4-mini", "input_tokens": 50, "output_tokens": 5, "cost_usd": 0.01}]
    return [
        {"kind": "run", "id": "run", "ok": True, "detail": [],
         "data": {"commit": "abc1234", "dirty": False, "build": None, "image": "sha256:feed", "documents": {"d1": doc}}},
        {"kind": "ingestion", "id": "I1", "ok": True, "detail": [], "data": {"layer": "structure"}},
        {"kind": "retrieval", "id": "R1", "ok": True, "detail": [], "data": {"rank": 1, "hits": HITS}},
        {"kind": "answers", "id": "C1", "ok": True, "detail": [], "data": {"turns": [{
            "ask": "מה התשואה?", "status": "done", "seconds": 4.0, "usage": usage,
            "answer": {"status": "answered", "markdown": "התשואה היא 8.9% [C1].", "sources": []}}]}},
    ]


SPEC = {"title": "סט סינתטי",
        "ingestion": [_item(row="חנות 5"), {"id": "I2", "document": "הגפן", "must_read": ["השטח הבנוי"]}],
        "retrieval": [{"id": "R1", "query": "q", "expect_all": ["זכויות בנייה", "היטל השבחה"]}],
        "answers": [{"id": "C1", "turns": [{"ask": "מה התשואה?",
                                            "expect": {"calc": [{"value": "8.8719", "precision": 2}]}}]}]}


def test_rescore_regrades_every_layer_from_stored_results():
    from eval.chat_eval import rescore

    out = {(r.kind, r.id): r for r in rescore(SPEC, _stored())}
    assert not out[("ingestion", "I1")].ok  # the corrected expectation names another row
    assert out[("ingestion_text", "I2")].ok
    assert not out[("retrieval", "R1")].ok and out[("retrieval", "R1")].data["ranks"]["היטל השבחה"] is None
    c1 = out[("answers", "C1")]
    assert not c1.ok and c1.data["turns"][0]["calc_problems"]
    assert out[("run", "run")].data["image"] == "sha256:feed"


def test_a_stored_result_without_evidence_is_kept_as_it_was():
    from eval.chat_eval import rescore

    old = [{"kind": "ingestion", "id": "I2", "ok": False, "detail": ["לא נקרא: x"], "data": {}}]
    (r,) = rescore({"ingestion": [{"id": "I2", "document": "הגפן", "must_read": ["x"]}]}, old)
    assert r.kind == "ingestion_text" and not r.ok and r.detail == ["לא נקרא: x"]


def test_report_records_build_image_models_per_purpose_and_the_reading_of_each_document(tmp_path):
    from eval.chat_eval import report, rescore

    text = report(rescore(SPEC, _stored()), tmp_path / "r.md", "t")
    assert "abc1234" in text and "sha256:feed" in text
    assert "agent: gpt-6-luna" in text and "verify: gpt-5.4-mini" in text
    assert "| שומה הגפן 12 | 4 | r-77 |" in text
    for layer in ("## ingestion:", "## ingestion_text:", "## retrieval:", "## answers:"):
        assert layer in text
    assert "שכבת חישוב: 0/1" in text


def test_rescore_from_the_command_line_makes_no_network_call(tmp_path, monkeypatch):
    import httpx

    from eval import chat_eval

    def no_network(*a, **kw):
        raise AssertionError("rescore must not reach the stack")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(chat_eval, "login", no_network)
    (tmp_path / "set.yaml").write_text(yaml.safe_dump(SPEC, allow_unicode=True), encoding="utf-8")
    (tmp_path / "res.json").write_text(json.dumps(_stored(), ensure_ascii=False), encoding="utf-8")
    assert chat_eval.main(["--set", str(tmp_path / "set.yaml"), "--out", str(tmp_path / "out"),
                           "--rescore", str(tmp_path / "res.json")]) == 0
    (rescored,) = (tmp_path / "out").glob("rescored-*.json")
    kinds = {r["kind"] for r in json.loads(rescored.read_text(encoding="utf-8"))}
    assert {"run", "ingestion", "ingestion_text", "retrieval", "answers"} <= kinds


def test_the_build_is_the_checkout_commit_or_the_given_build():
    from eval.chat_eval import build_info

    info = build_info(None, None)
    assert info["commit"] is None or len(info["commit"]) == 40
    given = build_info("deadbeef", "sha256:abc")
    assert given["build"] == "deadbeef" and given["image"] == "sha256:abc"


@pytest.mark.parametrize("rows", [[["חנות 3", "120", "9,500"]], [{"page": None, "cells": ["חנות 3", "120", "9,500"]}]])
def test_rows_without_a_page_take_the_tables_page(rows):
    block = TABLE | {"table": TABLE["table"] | {"rows": rows}}
    assert structure_check([block], _item(page=7)) == []
    assert structure_check([block], _item(page=6))
