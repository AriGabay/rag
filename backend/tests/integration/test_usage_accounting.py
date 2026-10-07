"""Usage and cost accounting (U1, R28): every model call of a turn is recorded per purpose and model, with its
token buckets and estimated cost — also when the turn fails, is cancelled or breaks. The model is scripted and
reports synthetic token counts; no content is recorded."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from app.providers.llm import CallStatus, Purpose, usage_cost
from tests.conftest import login
from tests.integration.test_chat import (
    ANSWER,
    _first_turn,
    _resolution,
    cloud,
    new_conversation,
    office,  # noqa: F401 - the fixture
    send,
)
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

MODEL = "gpt-6-luna"
TOKENS = {"input_tokens": 1000, "cached_input_tokens": 400, "cache_write_tokens": 100, "output_tokens": 50}


class PricedAgent(ScriptedAgent):
    """A scripted agent on a priced model whose every call reports all four token buckets."""

    model = MODEL

    def agent_step(self, *args, **kw):
        step = super().agent_step(*args, **kw)
        for k, v in TOKENS.items():
            setattr(step, k, v)
        step.model = MODEL
        return step

    def structured(self, *args, **kw):
        result = super().structured(*args, **kw)
        for k, v in TOKENS.items():
            setattr(result, k, v)
        result.model = MODEL
        return result


def rows(o) -> list:
    with tenant_tx(o.system) as conn:
        return conn.execute(text(
            "SELECT purpose, model, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, cost_usd,"
            " ok, status FROM provider_usage ORDER BY id")).all()


EXPECTED_COST = usage_cost(MODEL, **TOKENS)


def test_every_call_of_a_turn_is_recorded_with_its_purpose_model_buckets_and_cost(client, office, monkeypatch):  # noqa: F811
    cid = _first_turn(client, office, monkeypatch)
    second = PricedAgent([[call("search", query="שווי", document_ids=None, limit=None)],
                          final("השווי למ\"ר בנוי ברוטו הוא 9,500 ₪, ללא מע\"מ [S1].")])
    second.on("agent", _resolution(relation="new_question"))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    with tenant_tx(office.system) as conn:
        conn.execute(text("DELETE FROM provider_usage"))  # only the priced turn
    m = send(client, cid, "ומה השווי?")
    assert m["status"] == "done", m
    got = rows(office)
    assert [r.purpose for r in got] == ["resolve", "agent", "agent", "verify"]
    for r in got:
        assert (r.model, r.input_tokens, r.cached_input_tokens, r.cache_write_tokens, r.output_tokens) == (
            MODEL, 1000, 400, 100, 50)
        assert r.ok and r.status == "ok"
        assert float(r.cost_usd) == pytest.approx(EXPECTED_COST) and EXPECTED_COST == pytest.approx(91.5e-6)
    # the message's own record (diagnostics and the evaluation read it) carries the same per call
    assert [(u["purpose"], u["model"], u["cache_write_tokens"]) for u in m["usage"]] == [
        ("resolve", MODEL, 100), ("agent", MODEL, 100), ("agent", MODEL, 100), ("verify", MODEL, 100)]
    assert all(u["cost_usd"] == pytest.approx(EXPECTED_COST) for u in m["usage"])


def test_a_turn_that_fails_after_two_calls_still_records_both(client, office, monkeypatch):  # noqa: F811
    agent = PricedAgent([[call("search", query="שווי", document_ids=None, limit=None)], CallStatus.RATE_LIMITED])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי?")
    assert m["status"] == "failed"
    assert [(r.purpose, r.ok, r.status) for r in rows(office)] == [("agent", True, "ok"),
                                                                  ("agent", False, "rate_limited")]
    assert [u["status"] for u in m["usage"]] == ["ok", "rate_limited"]


def test_a_cancelled_turn_records_the_calls_made_before_the_stop(client, office, monkeypatch):  # noqa: F811
    agent = PricedAgent([[call("search", query="שווי", document_ids=None, limit=None)], final("השווי 9,500 ₪ [S1].")])
    cloud(monkeypatch, office, agent)

    def press_stop(step: int) -> None:
        if step == 1:  # stop pressed while the second call is in flight: its result is discarded, its cost is not
            with tenant_tx(office.system) as conn:
                conn.execute(text("UPDATE messages SET cancel_requested = true WHERE role = 'assistant'"))

    agent.on_step = press_stop
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי?")
    assert m["status"] == "cancelled"
    assert [r.purpose for r in rows(office)] == ["agent", "agent"]
    assert len(m["usage"]) == 2


def test_a_turn_that_breaks_unexpectedly_still_records_its_calls(client, office, monkeypatch):  # noqa: F811
    agent = PricedAgent([[call("search", query="שווי", document_ids=None, limit=None)], ANSWER])
    cloud(monkeypatch, office, agent)

    def broken(*args, **kw):
        raise RuntimeError("synthetic verifier crash")

    monkeypatch.setattr("app.chat.engine.verify_answer", broken)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי?")
    assert m["status"] == "failed"
    assert [r.purpose for r in rows(office)] == ["agent", "agent"]


def test_an_unpriced_model_is_recorded_with_unknown_cost(client, office, monkeypatch):  # noqa: F811
    agent = ScriptedAgent([[call("search", query="שווי", document_ids=None, limit=None)], ANSWER])  # unpriced
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות הראויים?")
    assert m["status"] == "done"
    got = rows(office)
    assert got and all(r.model == "scripted-agent" and r.cost_usd is None for r in got)
    assert all(u["cost_usd"] is None for u in m["usage"])


def test_a_failed_measurement_run_keeps_its_usage(db):
    import time

    from app.config import get_settings
    from app.extraction.docx import extract_docx
    from app.measurements.extract import MeasurementRunFailed, extract_version
    from app.platform import pipeline
    from tests.factories import make_document, make_office
    from tests.support.scripted_provider import ScriptedProvider
    from tests.unit.test_docx_blocks import FIXTURE

    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, "דוח סינתטי", sha="9" * 64)
    info = pipeline.VersionInfo(ver, doc, "k", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                None)
    with tenant_tx(a.system) as conn:
        pipeline.persist_extraction(conn, info, extract_docx(FIXTURE.read_bytes(), time.monotonic() + 60, get_settings()))
    broken = ScriptedProvider().on(Purpose.MEASURE, CallStatus.RATE_LIMITED, repeat=True)
    with pytest.raises(MeasurementRunFailed):
        extract_version(a.system, ver, broken)
    got = rows(a)
    # the run failed and rolled back, but the calls it made were billed and stay recorded
    assert got and all(r.purpose == "measure" and r.status == "rate_limited" and not r.ok for r in got)
