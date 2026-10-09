"""Reading-cache keys name the OCR languages (KTD11, R30) and verified values are cached per reading with what their
source attests only (KTD12, R29): database-free.

Migration 0016 rewrote the configuration component of every stored ``image_readings`` and ``region_readings`` key
to ``<model_config>|ocr=heb+eng``; the keys the code builds must be byte-identical for the default languages, so
the readings made before it keep hitting, and differ for other languages, so a reading made with other OCR settings
is never served. A cached verified value keeps the facts its source gave and the fields of its record the source
attests; a later take re-derives everything else from that turn's meaning. Synthetic values only."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.chat import calc
from app.chat import tools as T
from app.config import Settings, get_settings
from app.extraction import regions
from app.extraction.docx import _reading_key

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0016_source_positions.py"


def _migration_suffix() -> str:
    spec = importlib.util.spec_from_file_location("migration_0016", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.OCR_SUFFIX


class _Vision:
    config = "scripted-model:medium"


# --- OCR languages in reading-cache keys (KTD11) -----------------------------------------------------------------

def test_the_default_key_is_the_form_migration_0016_wrote():
    suffix = _migration_suffix()
    default = Settings.model_fields["ocr_languages"].default
    assert suffix == "|ocr=heb+eng" and default == "heb+eng"
    assert regions.model_config(None, default) == "none" + suffix  # an OCR-only reading
    assert regions.model_config(_Vision(), default) == "scripted-model:medium" + suffix
    assert _reading_key(b"png", "להלן תשריט:", _Vision(), default).model_config == "scripted-model:medium" + suffix


def test_other_ocr_languages_make_another_key():
    vision = _Vision()
    assert regions.model_config(vision, "eng") != regions.model_config(vision, "heb+eng")
    assert regions.model_config(None, "heb") != regions.model_config(None, "heb+eng")
    assert _reading_key(b"png", "ctx", vision, "eng") != _reading_key(b"png", "ctx", vision, "heb+eng")


def test_a_docx_picture_key_without_explicit_languages_uses_the_configured_ones():
    vision = _Vision()
    assert _reading_key(b"png", "ctx", vision) == _reading_key(b"png", "ctx", vision, get_settings().ocr_languages)


def test_the_inspect_key_names_the_configured_ocr_languages(monkeypatch):
    vision = _Vision()
    assert T.inspect_config(vision) == regions.model_config(vision, get_settings().ocr_languages)
    monkeypatch.setattr(get_settings(), "ocr_languages", "eng")
    assert T.inspect_config(vision) == "scripted-model:medium|ocr=eng"


# --- verified values: what is cached (KTD12) ----------------------------------------------------------------------

DOC, VER = uuid.UUID(int=1), uuid.UUID(int=2)
PARAGRAPH = "לטענת המשיבה השווי למ\"ר אקוו' הוא 9,500 ₪ ללא מע\"מ."


def _src(**kw) -> T.Source:
    base = {"sid": "S1", "document_id": DOC, "version_id": VER, "title": "שומה סינתטית", "section": "5. תחשיב",
            "location": "עמוד 3", "kind": "context", "text": PARAGRAPH, "block_start": 0, "block_end": 0,
            "reading_id": "r1", "status": "complete"}
    return T.Source(**(base | kw))


def _taken() -> dict:
    return T._take_quote(_src(), PARAGRAPH, {"quote": PARAGRAPH, "number": "9,500"}, [(0, PARAGRAPH, 3)])


def _given(**kw) -> dict:
    return {"kind": "value_per_area", "unit": "ILS_per_sqm", "period": "unknown", "vat": "unknown", "area_basis": "",
            "subject": "דירה לדוגמה", "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""} | kw


def _value(prov: dict, **kw) -> calc.Value:
    base = dict(vid="V1", value=Decimal("9500"), written="9,500", source_id="S1", document_id=DOC, version_id=VER,
                reading_id="r1", title="שומה סינתטית", location="עמוד 3", label="שווי למ״ר לפי המודל",
                kind="value_per_area", unit="ILS_per_sqm", period="unknown", vat="included", area_basis="",
                subject="נושא שהמודל קבע", role="comparison", provenance=prov, locator={"quote": PARAGRAPH},
                quote=PARAGRAPH, section="5. תחשיב", stated_by="המשיבה", stance="adopted",
                attribution="לטענת המשיבה", reading="clear")
    return calc.Value(**(base | kw))


def test_the_source_facts_round_trip_and_settle_the_meaning_as_a_fresh_take_would():
    taken = _taken()
    path = ("5. תחשיב", "עמדת המשיבה")
    back, back_path = T._facts_from_json(json.loads(json.dumps(T._facts_json(taken, path), ensure_ascii=False)))
    assert back_path == path
    for given in (_given(), _given(period="month"), _given(stance="adopted", stated_by="המשיבה"),
                  _given(stance="claim", scenario="מצב קיים", kind="value")):
        assert T._settle(back, given, back_path) == T._settle(taken, given, path)
    with pytest.raises(T.ToolError):  # a contradiction is refused exactly as on a fresh take
        T._settle(back, _given(unit="sqm"), back_path)


def test_the_cached_record_keeps_only_what_the_source_attests():
    prov = {"unit": "source", "period": "not_stated", "vat": "model_asserted", "area_basis": "source",
            "kind": "source", "stance": "model_asserted", "stated_by": "source", "scenario": "not_stated"}
    record = T._cache_record(_value(prov))
    assert record["unit"] == "ILS_per_sqm" and record["stated_by"] == "המשיבה" and record["kind"] == "value_per_area"
    # model-asserted meaning, and what the model alone named, are never cached
    assert not {"vat", "stance", "period", "scenario", "label", "subject", "role"} & set(record)
    assert set(record["provenance"].values()) == {"source"} and "vat" not in record["provenance"]
    assert record["value"] == "9500" and record["quote"] == PARAGRAPH and record["reading_id"] == "r1"


@pytest.mark.parametrize("reading, status, method, uncertain, reread, ok", [
    ("clear", "complete", None, False, None, True),
    ("", "complete", None, False, None, True),  # no region of its own, in a clearly read source
    ("", "uncertain_reading", None, False, None, False),
    ("uncertain", "complete", None, False, None, False),
    ("clear", "complete", None, True, None, False),
    ("clear", "uncertain_reading", "vision", False, None, False),  # a model's transcription made in the turn
    ("clear", "uncertain_reading", "vision", False, (True, "clear"), True),  # settled by a focused re-read
])
def test_only_a_value_read_clearly_is_cached(reading, status, method, uncertain, reread, ok):
    ws = T.Workspace(ctx=None)
    value = _value({"unit": "source"}, reading=reading)
    if uncertain:
        ws.uncertain_values["V1"] = "unclear"
    assert T._cacheable(ws, value, _src(status=status, method=method), reread) is ok


def test_a_cached_value_without_a_reading_id_is_not_cached():
    assert T._cacheable(T.Workspace(ctx=None), _value({"unit": "source"}, reading_id=None), _src(), None) is False


CELL = {"table_index": 0, "row": "סה\"כ", "row_number": 3, "column": "הכנסות (₪)", "column_number": 2}


@pytest.mark.parametrize("loc, ok", [
    ({}, True),
    ({"row": "סה\"כ", "column": "הכנסות (₪)"}, True),
    ({"row": "סה״כ", "column_number": 2, "number": "12,450,000"}, True),  # another spelling of the abbreviation
    ({"row": "שלב א", "column": "הכנסות (₪)"}, False),
    ({"row_number": 2}, False),
    ({"column": "עלויות (₪)"}, False),
    ({"number": "10,400,000"}, False),
    ({"quote": "הכנסות 12,450,000"}, False),
])
def test_a_cached_value_is_taken_only_by_its_own_locator(loc, ok):
    assert T._locator_agrees({"locator": CELL, "value": "12450000"}, loc) is ok


# --- verified values: a later turn takes the cached value (Q#) --------------------------------------------------

@pytest.fixture
def cached(monkeypatch):
    """One cached value of a visible current version; ``tenant_tx``, the version and the cache are faked (the
    database's part is in ``tests/integration/test_verified_values.py``)."""
    taken = _taken()
    prov = {"unit": "source", "period": "not_stated", "vat": "model_asserted", "area_basis": "source",
            "kind": "source", "stance": "model_asserted", "stated_by": "source", "scenario": "not_stated"}
    earlier = _value(prov, area_basis="מ״ר אקוו׳")
    stub = {"kind": "quote", "document_id": str(DOC), "version_id": str(VER), "reading_id": "r1", "block_start": 0,
            "block_end": 0, "pages": [3], "section": "5. תחשיב", **taken["anchor"]}
    doc = T._cache_doc(earlier, taken, ("5. תחשיב",), stub, _src(), (0, 0))
    doc = json.loads(json.dumps(doc, ensure_ascii=False))
    state = {"doc": doc, "visible": True, "reading": "r1"}

    def bound(conn, ws, handle, data):
        if not state["visible"]:
            raise T.ToolError(T.MSG_UNAVAILABLE)
        if state["reading"] != data["reading_id"]:
            raise T.ToolError(T.MSG_STALE_HANDLE.format(handle=handle))
        return SimpleNamespace(version_id=VER, document_id=DOC, reading_id=state["reading"], title="שומה סינתטית",
                               partial=False)

    monkeypatch.setattr(T, "tenant_tx", lambda ctx: contextlib.nullcontext())
    monkeypatch.setattr(T, "_bound", bound)
    monkeypatch.setattr(T, "_cached_value", lambda conn, version_id, reading_id, locator: state["doc"])
    ws = T.Workspace(ctx=None)
    handle = T._cached_handle(ws, VER, "r1", DOC, doc["record"]["locator"])
    return ws, handle, state


def test_a_cached_value_is_reused_without_the_model_asserted_fields_of_the_earlier_turn(cached):
    ws, handle, _ = cached
    out = T.tool_take_value(ws, handle, {}, _given(), "שווי למ״ר")
    v = ws.values["V1"]
    assert out.startswith("V1 נרשם") and v.value == Decimal("9500") and v.reading == "clear"
    # the earlier turn asserted VAT included and an adopted stance; this turn did not, and gets neither
    assert v.vat == "excluded" and v.provenance["vat"] == "source"  # what the source itself says about VAT
    assert v.provenance["stance"] != "model_asserted" and v.subject == "דירה לדוגמה" and v.role == "other"
    assert v.source_id in ws.sources and ws.sources[v.source_id].reading_id == "r1"
    assert ws.anchors["V1"]["kind"] == "quote" and ws.anchors["V1"]["reading_id"] == "r1"  # the reused value's anchor
    assert T.value_status(ws, "V1") == T.STATUS_AUTO


def test_a_cached_value_checks_the_new_meaning_against_its_source(cached):
    ws, handle, _ = cached
    with pytest.raises(T.ToolError):
        T.tool_take_value(ws, handle, {}, _given(unit="sqm"), "שטח")
    out = T.tool_take_value(ws, handle, {}, _given(stance="estimate", scenario="תרחיש שהמודל קבע"), "שווי")
    assert ws.values["V1"].certainty == "model_asserted" and "קביעה שלך" in out


def test_a_cached_value_of_a_document_no_longer_visible_or_read_again_is_not_used(cached):
    ws, handle, state = cached
    state["reading"] = "r2"
    with pytest.raises(T.ToolError, match="עובד מחדש"):
        T.tool_take_value(ws, handle, {}, _given(), "שווי")
    state["reading"], state["visible"] = "r1", False
    with pytest.raises(T.ToolError, match="אינו זמין"):
        T.tool_take_value(ws, handle, {}, _given(), "שווי")
    assert not ws.values


def test_a_cached_value_refuses_another_locator(cached):
    ws, handle, _ = cached
    with pytest.raises(T.ToolError, match=handle):
        T.tool_take_value(ws, handle, {"quote": "השווי", "number": "9,500"}, _given(), "שווי")
