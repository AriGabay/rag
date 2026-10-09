"""Provider contract (U1, KTD3, R5, R7, R8): status taxonomy per provider, selection by settings.

The SDK clients run against an in-process HTTP transport, so SDK exceptions are the real ones raised from
real status codes. The ``real_model`` test is opt-in (``-m real_model``) and reads the repo-root ``.env``.
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, ConfigDict

from app.config import MODEL_PURPOSES, Settings
from app.providers import llm
from app.providers.llm import (
    AnswerOutput,
    AnthropicLLM,
    CallStatus,
    LLMResult,
    MockLLM,
    OpenAIProvider,
    ProviderCallError,
    Purpose,
    StructuredResult,
)
from tests.support.scripted_provider import ScriptedProvider

FAKE_KEY = "test-openai-key-not-real-0001"
REPO_ROOT = Path(__file__).resolve().parents[3]


class Echo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def _usage() -> dict:
    return {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
            "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}


def _response(content: list[dict], status: str = "completed", reason: str | None = None) -> dict:
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-5.4-mini", "status": status,
            "incomplete_details": {"reason": reason} if reason else None,
            "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": content}],
            "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "usage": _usage()}


def _text(value: str) -> list[dict]:
    return [{"type": "output_text", "text": value, "annotations": []}]


def _error(code: str, kind: str = "invalid_request_error") -> dict:
    return {"error": {"message": "stubbed failure", "type": kind, "param": None, "code": code}}


class Transport:
    """Answers every request with ``reply`` (a response or an exception) and records the request."""

    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def openai_provider(reply, model: str = "gpt-5.4-mini", effort: str = "none") -> tuple[OpenAIProvider, Transport]:
    transport = Transport(reply)
    client = openai.OpenAI(api_key=FAKE_KEY, max_retries=0,
                           http_client=httpx2.Client(transport=httpx2.MockTransport(transport)))
    return OpenAIProvider(FAKE_KEY, model, reasoning_effort=effort, client=client), transport


def call(provider, purpose=Purpose.INTERPRET) -> StructuredResult:
    return provider.structured(purpose, "החזר את הטקסט כפי שהוא.", "שלום", Echo, max_output_tokens=64)


# --- OpenAI -------------------------------------------------------------------------------------------


def test_openai_parsed_response_is_ok_with_usage():
    provider, transport = openai_provider(httpx2.Response(200, json=_response(_text('{"text": "שלום"}'))))
    r = call(provider)
    assert r.status == CallStatus.OK and r.ok
    assert r.parsed == Echo(text="שלום")
    assert (r.input_tokens, r.output_tokens) == (11, 7) and r.latency_ms is not None
    sent = json.loads(transport.requests[0].content)
    assert sent["store"] is False and "temperature" not in sent
    assert sent["model"] == "gpt-5.4-mini" and sent["max_output_tokens"] == 64
    assert sent["reasoning"] == {"effort": "none"}
    assert sent["text"]["format"]["strict"] is True
    assert transport.requests[0].headers["authorization"] == f"Bearer {FAKE_KEY}"


def _sent(transport: Transport, i: int = 0) -> dict:
    return json.loads(transport.requests[i].content)


def _ok() -> httpx2.Response:
    return httpx2.Response(200, json=_response(_text('{"text": "שלום"}')))


@pytest.mark.parametrize("effort", ["none", "low"])
def test_luna_requests_carry_its_effort_and_no_unsupported_parameter(effort):
    provider, transport = openai_provider(_ok(), "gpt-6-luna", effort)
    assert call(provider, Purpose.VERIFY).ok
    provider.agent_step("הוראות", [{"role": "user", "content": "שלום"}], [], Echo.model_json_schema())
    provider.structured_image(Purpose.VISION, "הוראות", "תמלל", b"\x89PNG", Echo)
    for i in range(3):
        sent = _sent(transport, i)
        assert sent["model"] == "gpt-6-luna" and sent["reasoning"] == {"effort": effort}
        assert "temperature" not in sent and "top_p" not in sent and "prompt_cache_retention" not in sent
        assert "minimal" not in transport.requests[i].content.decode()


def test_one_shot_calls_send_explicit_cache_mode_without_a_breakpoint():
    """A cache write costs more than ordinary input on gpt-6-luna; a call no later call extends writes nothing."""
    provider, transport = openai_provider(_ok(), "gpt-6-luna", "low")
    provider.structured_image(Purpose.VISION, "הוראות", "תמלל", b"\x89PNG", Echo)
    call(provider, Purpose.RESOLVE)
    for i in range(2):
        sent = _sent(transport, i)
        assert sent["prompt_cache_options"] == {"mode": "explicit"} and "prompt_cache_key" not in sent
        assert "prompt_cache_breakpoint" not in transport.requests[i].content.decode()
    image = _sent(transport)["input"][0]["content"][1]
    assert image["type"] == "input_image" and image["detail"] == "high"


def test_agent_step_keeps_implicit_caching_under_a_hashed_office_key():
    office_a, office_b = "11111111-2222-4333-8444-555555555555", "66666666-7777-4888-8999-000000000000"
    key_a, key_b = llm.office_cache_key(office_a), llm.office_cache_key(office_b)
    assert key_a != key_b and key_a == llm.office_cache_key(office_a)
    for office, key in ((office_a, key_a), (office_b, key_b)):
        assert office not in key and office.replace("-", "") not in key
    provider, transport = openai_provider(_ok(), "gpt-6-luna", "low")
    provider.agent_step("הוראות", [{"role": "user", "content": "שלום"}], [], Echo.model_json_schema(),
                        cache_key=key_a)
    sent = _sent(transport)
    assert sent["prompt_cache_key"] == key_a and "prompt_cache_options" not in sent
    assert sent["include"] == ["reasoning.encrypted_content"]


def test_a_last_agent_step_keeps_its_tools_and_only_sets_the_tool_choice():
    """The forced final step (KTD12): the same tools, so the cached prefix survives, with ``tool_choice`` none."""
    tools = [{"type": "function", "name": "search", "description": "חיפוש", "strict": True,
              "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}]
    provider, transport = openai_provider(_ok(), "gpt-6-luna", "low")
    provider.agent_step("הוראות", [], tools, Echo.model_json_schema(), cache_key=llm.office_cache_key("x"))
    provider.agent_step("הוראות", [], tools, Echo.model_json_schema(), cache_key=llm.office_cache_key("x"),
                        tool_choice="none")
    free, last = _sent(transport, 0), _sent(transport, 1)
    assert "tool_choice" not in free and last["tool_choice"] == "none"
    assert last["tools"] == free["tools"] == tools and last["prompt_cache_key"] == free["prompt_cache_key"]


def test_mini_is_sent_no_cache_options():
    provider, transport = openai_provider(_ok(), "gpt-5.4-mini", "low")
    call(provider, Purpose.VERIFY)
    provider.agent_step("הוראות", [], [], Echo.model_json_schema(), cache_key=llm.office_cache_key("x"))
    assert "prompt_cache_options" not in _sent(transport, 0) and _sent(transport, 0)["reasoning"] == {"effort": "low"}
    assert "prompt_cache_options" not in _sent(transport, 1) and _sent(transport, 1)["prompt_cache_key"]


def test_unknown_model_is_sent_no_optional_parameter():
    provider, transport = openai_provider(_ok(), "house-model", "low")
    call(provider, Purpose.VERIFY)
    provider.agent_step("הוראות", [], [], Echo.model_json_schema(), cache_key=llm.office_cache_key("x"))
    provider.structured_image(Purpose.VISION, "הוראות", "תמלל", b"\x89PNG", Echo)
    for i in range(3):
        sent = _sent(transport, i)
        assert not {"reasoning", "include", "prompt_cache_options", "prompt_cache_key"} & set(sent)
    assert "detail" not in _sent(transport, 2)["input"][0]["content"][1]


def test_an_effort_the_model_does_not_support_is_never_sent():
    provider, transport = openai_provider(_ok(), "gpt-6-luna", "low")
    provider.structured(Purpose.VISION, "הוראות", "שלום", Echo, reasoning_effort="minimal")
    assert "reasoning" not in _sent(transport)


def test_openai_cached_input_tokens_are_recorded():
    body = _response(_text('{"text": "שלום"}'))
    body["usage"]["input_tokens_details"]["cached_tokens"] = 512
    provider, _ = openai_provider(httpx2.Response(200, json=body))
    assert call(provider).cached_input_tokens == 512
    body["usage"].pop("input_tokens_details")
    provider, _ = openai_provider(httpx2.Response(200, json=body))
    assert call(provider).cached_input_tokens is None  # not reported: unknown, never zero


def test_openai_agent_step_records_cached_input_tokens():
    body = _response(_text('{"text": "שלום"}'))
    body["usage"]["input_tokens_details"]["cached_tokens"] = 2048
    provider, _ = openai_provider(httpx2.Response(200, json=body))
    step = provider.agent_step("הוראות", [{"role": "user", "content": "שלום"}], [], Echo.model_json_schema())
    assert step.ok and step.cached_input_tokens == 2048 and step.input_tokens == 11


def test_openai_cache_write_tokens_and_model_are_recorded_on_every_result():
    body = _response(_text('{"text": "שלום"}'))
    body["usage"]["input_tokens_details"] = {"cached_tokens": 3, "cache_write_tokens": 5}
    provider, _ = openai_provider(httpx2.Response(200, json=body))
    r = call(provider)
    assert (r.cached_input_tokens, r.cache_write_tokens, r.model) == (3, 5, "gpt-5.4-mini")
    step = openai_provider(httpx2.Response(200, json=body))[0].agent_step(
        "הוראות", [{"role": "user", "content": "שלום"}], [], Echo.model_json_schema())
    assert (step.cached_input_tokens, step.cache_write_tokens, step.model) == (3, 5, "gpt-5.4-mini")
    # a failed call still names the model it was sent to; its token buckets stay unknown
    failed = call(openai_provider(httpx2.Response(429, json=_error("rate_limit_exceeded", "requests")))[0])
    assert failed.status == CallStatus.RATE_LIMITED and failed.model == "gpt-5.4-mini"
    assert failed.cache_write_tokens is None


# --- Cost ---------------------------------------------------------------------------------------------


def test_cost_prices_each_token_bucket_at_its_own_rate():
    # gpt-6-luna per 1M: input 0.10, cached 0.01, cache write 0.125, output 0.50; ordinary input is what is
    # neither read from nor written to the cache
    cost = llm.usage_cost("gpt-6-luna", input_tokens=1000, cached_input_tokens=400, cache_write_tokens=100,
                          output_tokens=50)
    assert cost == pytest.approx((500 * 0.10 + 400 * 0.01 + 100 * 0.125 + 50 * 0.50) / 1e6)
    # gpt-5.4-mini has no cache-write surcharge: a write is billed as ordinary input
    mini = llm.usage_cost("gpt-5.4-mini", input_tokens=1000, cached_input_tokens=400, cache_write_tokens=100,
                          output_tokens=50)
    assert mini == pytest.approx((600 * 0.75 + 400 * 0.075 + 50 * 4.50) / 1e6)


def test_long_prompts_are_priced_higher_for_the_whole_request():
    at = llm.usage_cost("gpt-6-luna", input_tokens=272_000, cached_input_tokens=0, cache_write_tokens=0,
                        output_tokens=1000)
    assert at == pytest.approx((272_000 * 0.10 + 1000 * 0.50) / 1e6)
    over = llm.usage_cost("gpt-6-luna", input_tokens=300_000, cached_input_tokens=100_000, cache_write_tokens=10_000,
                          output_tokens=1000)
    assert over == pytest.approx((190_000 * 0.20 + 100_000 * 0.02 + 10_000 * 0.25 + 1000 * 0.75) / 1e6)


def test_unknown_model_or_unreported_tokens_cost_unknown_never_zero():
    assert llm.usage_cost("some-unpriced-model", input_tokens=10, cached_input_tokens=0, cache_write_tokens=0,
                          output_tokens=10) is None
    assert llm.usage_cost(None, input_tokens=10, cached_input_tokens=0, cache_write_tokens=0, output_tokens=10) is None
    assert llm.usage_cost("gpt-6-luna", input_tokens=None, cached_input_tokens=None, cache_write_tokens=None,
                          output_tokens=None) is None
    # a dated snapshot of a priced model is priced as that model; unreported cache buckets price as ordinary input
    assert llm.usage_cost("gpt-6-luna-2026-09-30", input_tokens=1000, cached_input_tokens=None,
                          cache_write_tokens=None, output_tokens=0) == pytest.approx(1000 * 0.10 / 1e6)


def test_usage_entry_carries_model_cache_buckets_and_cost():
    r = StructuredResult(CallStatus.OK, None, input_tokens=1000, output_tokens=50, latency_ms=12,
                         cached_input_tokens=400, cache_write_tokens=100, model="gpt-6-luna")
    u = llm.usage_entry("verify", r)
    assert set(u) == set(llm.USAGE_FIELDS)
    assert (u["purpose"], u["model"], u["cache_write_tokens"]) == ("verify", "gpt-6-luna", 100)
    assert u["cost_usd"] == pytest.approx(91.5e-6)
    # a result without its own model is entered under the provider's model
    assert llm.usage_entry("agent", StructuredResult(CallStatus.TIMEOUT), model="gpt-6-luna")["model"] == "gpt-6-luna"
    assert llm.usage_entry("agent", StructuredResult(CallStatus.TIMEOUT))["cost_usd"] is None


def test_price_table_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_PRICES", json.dumps({"house-model": {"input": 1, "cached_input": 0.5, "output": 2}}))
    s = Settings()
    assert s.llm_prices["house-model"].cache_write is None and "gpt-6-luna" not in s.llm_prices


def test_openai_refusal_item_maps_to_refusal():
    provider, _ = openai_provider(httpx2.Response(200, json=_response([{"type": "refusal", "refusal": "לא"}])))
    r = call(provider)
    assert r.status == CallStatus.REFUSAL and r.parsed is None


@pytest.mark.parametrize("content", [_text('{"text": "שלו'), []], ids=["truncated-json", "no-output"])
def test_openai_incomplete_max_output_tokens_maps_to_incomplete(content):
    provider, _ = openai_provider(httpx2.Response(200, json=_response(content, "incomplete", "max_output_tokens")))
    r = call(provider)
    assert r.status == CallStatus.INCOMPLETE and r.detail == "max_output_tokens"
    assert r.input_tokens == 11


def test_openai_incomplete_content_filter_maps_to_refusal():
    provider, _ = openai_provider(httpx2.Response(200, json=_response([], "incomplete", "content_filter")))
    assert call(provider).status == CallStatus.REFUSAL


def test_openai_malformed_json_maps_to_invalid():
    provider, _ = openai_provider(httpx2.Response(200, json=_response(_text("not json"))))
    r = call(provider)
    assert r.status == CallStatus.INVALID and r.parsed is None


class RaisingClient:
    """Minimal client whose parse raises a parse-time SDK error (not an ``APIError``)."""

    def __init__(self, exc: Exception):
        self.exc = exc
        self.responses = self
        self.with_raw_response = self

    def with_options(self, **_):
        return self

    def parse(self, **_):
        raise self.exc


@pytest.mark.parametrize("exc, status", [
    (openai.LengthFinishReasonError(completion=ChatCompletion.construct(usage=None)), CallStatus.INCOMPLETE),
    (openai.ContentFilterFinishReasonError(), CallStatus.REFUSAL),
])
def test_openai_parse_time_errors(exc, status):
    provider = OpenAIProvider(FAKE_KEY, "gpt-5.4-mini", client=RaisingClient(exc))
    assert call(provider).status == status


@pytest.mark.parametrize("reply, status", [
    (httpx2.Response(401, json=_error("invalid_api_key")), CallStatus.AUTH),
    (httpx2.Response(403, json=_error("unsupported_country_region_territory")), CallStatus.AUTH),
    (httpx2.Response(404, json=_error("model_not_found")), CallStatus.MODEL_UNAVAILABLE),
    (httpx2.Response(400, json=_error("model_not_found")), CallStatus.MODEL_UNAVAILABLE),
    (httpx2.Response(429, json=_error("rate_limit_exceeded", "requests")), CallStatus.RATE_LIMITED),
    (httpx2.Response(429, json=_error("insufficient_quota", "insufficient_quota")), CallStatus.QUOTA),
    (httpx2.Response(400, json=_error("invalid_image")), CallStatus.INVALID),
    (httpx2.Response(422, json=_error("unprocessable")), CallStatus.INVALID),
    (httpx2.Response(400, json=_error("content_policy_violation")), CallStatus.REFUSAL),
    (httpx2.Response(500, json=_error("server_error", "server_error")), CallStatus.ERROR),
    (httpx2.ReadTimeout("slow"), CallStatus.TIMEOUT),
    (httpx2.ConnectError("down"), CallStatus.ERROR),
], ids=["401", "403", "404-model", "400-model", "429", "429-quota", "400", "422", "400-policy", "500", "timeout",
        "connection"])
def test_openai_sdk_errors_map_to_statuses(reply, status):
    provider, _ = openai_provider(reply)
    r = call(provider)
    assert r.status == status and r.parsed is None
    assert FAKE_KEY not in repr(r)


def test_openai_answer_wrapper():
    body = '{"answer": "ראו [E1].", "used_evidence_ids": ["E1"], "insufficient_evidence": false}'
    provider, transport = openai_provider(httpx2.Response(200, json=_response(_text(body))))
    evidence = [{"evidence_id": "E1", "title": "דוח", "page_list": [1], "text": "קטע"}]
    result = provider.answer("מה?", evidence, None)
    assert isinstance(result, LLMResult)
    assert (result.text, result.used_ids, result.insufficient, result.demo) == ("ראו [E1].", ["E1"], False, False)
    sent = json.loads(transport.requests[0].content)
    assert sent["instructions"] == llm.SYSTEM_POLICY and "קטע" in sent["input"]
    assert sent["text"]["format"]["name"] == AnswerOutput.__name__


def test_answer_wrapper_raises_on_failure_status():
    provider, _ = openai_provider(httpx2.Response(401, json=_error("invalid_api_key")))
    with pytest.raises(ProviderCallError) as info:
        provider.answer("מה?", [{"evidence_id": "E1", "title": "t", "page_list": [], "text": "x"}], None)
    assert info.value.status == CallStatus.AUTH and FAKE_KEY not in str(info.value)


# --- Anthropic ----------------------------------------------------------------------------------------


def anthropic_provider(reply) -> AnthropicLLM:
    client = anthropic.Anthropic(api_key="test-anthropic-not-real", max_retries=0,
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(Transport(reply))))
    return AnthropicLLM("test-anthropic-not-real", "claude-stub", client=client)


def _message(text: str, stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-stub",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 2}}


@pytest.mark.parametrize("reply, status", [
    (httpx2.Response(200, json=_message('{"text": "שלום"}')), CallStatus.OK),
    (httpx2.Response(200, json=_message('{"text": "של', "max_tokens")), CallStatus.INCOMPLETE),
    (httpx2.Response(200, json=_message("", "refusal")), CallStatus.REFUSAL),
    (httpx2.Response(200, json=_message("not json")), CallStatus.INVALID),
    (httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "x"}}),
     CallStatus.AUTH),
    (httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "x"}}),
     CallStatus.MODEL_UNAVAILABLE),
    (httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}),
     CallStatus.RATE_LIMITED),
    (httpx2.ReadTimeout("slow"), CallStatus.TIMEOUT),
], ids=["ok", "max_tokens", "refusal", "invalid", "401", "404", "429", "timeout"])
def test_anthropic_adapter_statuses(reply, status):
    r = call(anthropic_provider(reply))
    assert r.status == status
    if status == CallStatus.OK:
        assert r.parsed == Echo(text="שלום") and r.input_tokens == 3


# --- Mock and scripted --------------------------------------------------------------------------------


@pytest.mark.parametrize("purpose", [Purpose.INTERPRET, Purpose.EXTRACT, Purpose.VERIFY, Purpose.CLARIFY])
def test_mock_supports_only_answer(purpose):
    r = call(MockLLM(), purpose)
    assert r.status == CallStatus.UNSUPPORTED and r.parsed is None


def test_mock_answer_is_demo_labeled():
    evidence = [{"evidence_id": "E1", "title": "t", "page_list": [1],
                 "text": "השמאי המכריע קבע הפחתה של עשרה אחוזים בשל היטל השבחה בשכונה."}]
    result = MockLLM().answer("מה קבע השמאי המכריע לגבי היטל השבחה?", evidence, None)
    assert result.demo is True and MockLLM.demo is True
    assert "[E1]" in result.text and result.used_ids == ["E1"]


def test_scripted_provider_replays_by_purpose_and_match():
    p = ScriptedProvider().on(Purpose.INTERPRET, {"text": "א"}, match="שלום").on(Purpose.EXTRACT, CallStatus.TIMEOUT)
    assert call(p).parsed == Echo(text="א")
    assert call(p).status == CallStatus.ERROR  # used once, nothing left
    assert call(p, Purpose.EXTRACT).status == CallStatus.TIMEOUT
    assert [c.purpose for c in p.calls] == [Purpose.INTERPRET, Purpose.INTERPRET, Purpose.EXTRACT]
    p.on(Purpose.ANSWER, AnswerOutput(answer="ראו [E1].", used_evidence_ids=["E1"], insufficient_evidence=False))
    assert p.answer("q", [{"evidence_id": "E1", "title": "t", "page_list": [], "text": "x"}], None).used_ids == ["E1"]


# --- Selection by settings ----------------------------------------------------------------------------


@pytest.fixture
def configured(monkeypatch):
    """Point the provider module at fresh settings built from the patched environment."""
    for name in ("OPENAI_KEY", "OPENAI_API_KEY", "OPENAI_MODEL", "LLM_PROVIDER", "ANTHROPIC_API_KEY",
                 *(f"{kind}_{p.upper()}" for kind in ("MODEL", "EFFORT") for p in MODEL_PURPOSES)):
        monkeypatch.delenv(name, raising=False)

    def apply(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        s = Settings(_env_file=None)
        monkeypatch.setattr(llm, "get_settings", lambda: s)
        llm._provider.cache_clear()
        return s

    yield apply
    llm._provider.cache_clear()


def test_openai_selected_needs_only_openai_key(configured):
    configured(OPENAI_KEY=FAKE_KEY)
    assert llm.selected_provider_configured() and llm.cloud_configured()
    provider = llm.get_selected_provider()
    assert isinstance(provider, OpenAIProvider) and provider.model == "gpt-6-luna"
    assert llm.get_selected_provider() is provider  # one client per key and model
    assert llm.get_cloud_provider() is provider


def test_openai_selected_without_key_is_not_configured(configured):
    configured(ANTHROPIC_API_KEY="test-anthropic-not-real")
    assert not llm.selected_provider_configured()


def test_anthropic_selected_checks_anthropic_key(configured):
    configured(LLM_PROVIDER="anthropic", OPENAI_KEY=FAKE_KEY)
    assert not llm.selected_provider_configured()
    configured(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="test-anthropic-not-real")
    assert llm.selected_provider_configured()
    assert isinstance(llm.get_selected_provider(), AnthropicLLM)


PURPOSES = [Purpose(p) for p in MODEL_PURPOSES]


def test_every_purpose_gets_luna_with_its_effort(configured):
    configured(OPENAI_KEY=FAKE_KEY)
    got = {p: llm.get_provider(p) for p in PURPOSES}
    assert {p.value: (g.model, g.reasoning_effort) for p, g in got.items()} == {
        "agent": ("gpt-6-luna", "medium"), "verify": ("gpt-6-luna", "low"), "vision": ("gpt-6-luna", "low"),
        "measure": ("gpt-6-luna", "low"), "resolve": ("gpt-6-luna", "none"), "summary": ("gpt-6-luna", "none")}
    # one provider per (model, effort); every provider of one key shares one client (and connection pool)
    assert got[Purpose.VERIFY] is got[Purpose.VISION] and got[Purpose.RESOLVE] is got[Purpose.SUMMARY]
    assert got[Purpose.AGENT] is not got[Purpose.RESOLVE]
    assert got[Purpose.AGENT].client is got[Purpose.RESOLVE].client


def test_the_single_override_switches_every_purpose(configured):
    configured(OPENAI_KEY=FAKE_KEY, OPENAI_MODEL="gpt-5.4-mini")
    assert {llm.get_provider(p).model for p in PURPOSES} == {"gpt-5.4-mini"}
    assert llm.get_selected_provider().model == "gpt-5.4-mini"


def test_a_purpose_model_switches_only_that_purpose(configured):
    configured(OPENAI_KEY=FAKE_KEY, MODEL_VISION="gpt-5.4-mini")
    assert {p.value: llm.get_provider(p).model for p in PURPOSES} == {
        p.value: "gpt-5.4-mini" if p == Purpose.VISION else "gpt-6-luna" for p in PURPOSES}


def test_for_purpose_routes_a_selected_provider_and_keeps_a_test_double(configured):
    configured(OPENAI_KEY=FAKE_KEY, MODEL_SUMMARY="gpt-5.4-mini")
    base = llm.get_selected_provider()
    assert llm.for_purpose(base, Purpose.SUMMARY) is llm.get_provider(Purpose.SUMMARY)
    assert llm.for_purpose(base, Purpose.SUMMARY).model == "gpt-5.4-mini"
    double = ScriptedProvider()
    assert llm.for_purpose(double, Purpose.SUMMARY) is double
    unrouted = OpenAIProvider(FAKE_KEY, "gpt-6-luna", client=object())
    assert llm.for_purpose(unrouted, Purpose.VERIFY) is unrouted


def test_the_vision_reader_uses_the_vision_model(configured):
    from uuid import uuid4

    from app.extraction.vision import ModelVisionReader

    configured(OPENAI_KEY=FAKE_KEY, MODEL_VISION="gpt-5.4-mini", EFFORT_VISION="medium")
    reader = ModelVisionReader(uuid4())
    assert (reader.provider.model, reader.provider.reasoning_effort) == ("gpt-5.4-mini", "medium")


def test_anthropic_serves_every_purpose_with_its_model(configured):
    configured(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="test-anthropic-not-real")
    assert {type(llm.get_provider(p)) for p in PURPOSES} == {AnthropicLLM}


def test_provider_cache_is_not_keyed_by_raw_key():
    assert llm._key_hash(FAKE_KEY) != FAKE_KEY and FAKE_KEY not in llm._key_hash(FAKE_KEY)


# --- Real model (opt-in) ------------------------------------------------------------------------------


@pytest.mark.real_model
def test_real_openai_structured_echo(monkeypatch):
    """One tiny structured call through the real API with the repo key. Never prints the key."""
    for name in ("OPENAI_KEY", "OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=REPO_ROOT / ".env")
    key = s.openai_api_key.get_secret_value()
    if not key:
        pytest.skip("no OpenAI key in the environment or the repo .env")
    provider = OpenAIProvider(key, s.openai_model, reasoning_effort=s.openai_reasoning_effort)
    r = provider.structured(Purpose.TEST, "החזר בשדה text את המילה שבקלט בדיוק כפי שהיא.", "שלום", Echo,
                            max_output_tokens=200)
    print(f"real_model status={r.status} model={s.openai_model} detail={r.detail}")
    assert r.status == CallStatus.OK, (r.status, r.detail)
    assert r.parsed is not None and "שלום" in r.parsed.text


def _non_strict_objects(schema, path: str = "") -> list[str]:
    """Every object of a JSON schema whose ``required`` does not list each of its properties, by path."""
    out: list[str] = []
    if isinstance(schema, dict):
        if "properties" in schema and set(schema.get("required") or []) != set(schema["properties"]):
            out.append(f"{path or '/'}: {sorted(set(schema['properties']) - set(schema.get('required') or []))}")
        for k, v in schema.items():
            out += _non_strict_objects(v, f"{path}/{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            out += _non_strict_objects(v, f"{path}[{i}]")
    return out


def test_the_turns_final_answer_and_tool_schemas_are_valid_strict_structured_output():
    """The agent step sends ``FINAL_SCHEMA`` and the tools as strict schemas, unconverted: strict mode rejects an
    object that leaves any property out of ``required`` (U9: a field with a default, ``Requested.component``, failed
    every real turn with a 400)."""
    from app.chat import engine
    from app.chat import tools as T

    assert _non_strict_objects(engine.FINAL_SCHEMA) == []
    for tool in T.TOOLS:
        if tool.get("strict"):
            assert _non_strict_objects(tool["parameters"]) == [], tool["name"]
