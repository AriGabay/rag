"""Test-only model for the conversational loop: replays scripted steps (tool calls or a final answer), answers
the request analysis of a first turn (``app.chat.request``) and the verification judge. Every step records the
input items it received, so tests can check what the model was shown (history, prior references, tool outputs).

The request analysis answers with the components a test gives (``request=``: a list of ``component(...)``, a
``CallStatus`` for a failed call, or a callable ``(input) -> one of those or a dict``); by default with one
information component per part the first scripted final answer declares, and with none when it declares none —
an empty analysis, so the judge derives the requirements as before the analysis existed (the fallback).

The analysis also carries the question's intent (round 10): by default ``unknown`` — the turn's intent is the claim
(round 15 U6: evidence never overrides it), and ``unknown`` plans both pools; a test that wants a property or a
professional reading gives ``intent="property"`` (a value of ``app.chat.intent.Value``, or a dict of the whole part, or a
callable ``(input) -> either``).

A ``final(...)`` focus is sent with ``units: []`` unless it gives ``units`` (the U# handles the answer declares, or — for a
clarification between units — offers): the strict schema has the model send every field."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from app.chat.request import RequestAnalysis
from app.providers.llm import AgentStep, CallStatus, Purpose
from tests.support.scripted_provider import ScriptedCall, ScriptedProvider


def call(name: str, **arguments) -> dict:
    return {"call": name, "arguments": arguments}


LOCATORS = ("source", "pages", "section", "table", "cursor")


def read(**locator) -> dict:
    """A ``read`` call with one locator given and the others null, as the strict schema has the model send it."""
    return call("read", target={k: locator.get(k) for k in LOCATORS})


def final(markdown: str, status: str = "answered", claims: list | None = None, clarification: str = "",
          missing: str = "", documents: list[str] | None = None, scope: str = "focused", scope_query: str = "",
          omitted: list | None = None, focus: dict | None = None, requested: list | None = None,
          parts: list | None = None) -> dict:
    return {"final": {"status": status, "answer_markdown": markdown, "claims": claims or [],
                      "clarification_question": clarification, "missing_info": missing,
                      "referenced_document_ids": documents or [], "scope_kind": scope, "scope_query": scope_query,
                      "omitted": omitted or [],
                      # the strict schema has the model send every field: a focus that declares no unit says ``units: []``
                      "focus": {"units": []} | focus if focus is not None else None,
                      # the strict schema has the model send every field: a claim tied to no component says ""
                      "requested": [{"component": ""} | r if isinstance(r, dict) else r for r in requested or []],
                      "parts": parts or []}}


def requirement(text: str = "", status: str = "full", units: list | None = None, related: list | None = None,
                id: str = "", calculation: bool = False) -> dict:
    """A judge's score of one requirement: ``text`` when the call derives it, ``id`` when it scores a frozen one."""
    return {"id": id, "text": text, "calculation": calculation, "status": status, "units": units or [],
            "related": related or [], "reason": "בדיקה"}


def intent_part(value: str = "unknown", reference: str = "none", item: int | None = None, topic: str = "",
                standard_refs: list | None = None) -> dict:
    """The intent part of an analysis or of a resolution, as the model returns it."""
    return {"intent": value, "reference": reference, "item": item, "topic": topic,
            "standard_refs": standard_refs or []}


def component(text: str, kind: str = "information", id: str = "", parent: str = "", conditional: bool = False,
              subject: str = "", parameters: list | None = None, compares: list | None = None,
              aspect: str = "") -> dict:
    """One component of the request as the analysis (or the resolve call) returns it; ``id`` is the model's own
    label (the server assigns the ``N#`` ids), ``parent`` the label of the component it refines, ``aspect`` what an
    instruction is about (``citation`` is checked by the server from the answer's units)."""
    return {"id": id, "text": text, "kind": kind, "parent": parent, "conditional": conditional, "subject": subject,
            "parameters": parameters or [], "compares": compares or [], "aspect": aspect}


class ScriptedAgent(ScriptedProvider):
    """``steps``: a list whose items are a list of tool calls (one model step calling them), a ``final(...)``
    dict, a ``CallStatus`` (a failed step), or a callable ``(items) -> one of those``."""

    name = "scripted-agent"
    model = "scripted-agent"

    def __init__(self, steps: list, judge: str | Callable[[str], dict] = "supported",
                 request: list | CallStatus | Callable[[str], Any] | None = None,
                 intent: str | dict | Callable[[str], Any] = "unknown") -> None:
        super().__init__()
        self.steps = list(steps)
        self.request = request
        self.intent = intent
        # read now: the analysis runs while the first step is being taken from ``steps``
        self._first_final = next((s["final"] for s in self.steps if isinstance(s, dict) and "final" in s), None)
        self.seen: list[list] = []
        self.on_step: Callable[[int], None] | None = None
        verdict = judge

        def judge_fn(instructions: str, input: str) -> dict:
            if callable(verdict):
                return verdict(input)
            indexes = [int(i) for i in re.findall(r'<unit index="(\d+)"', input)]
            out = {"verdicts": [{"index": i, "verdict": verdict, "reason": "בדיקה", "supported_by": []}
                                for i in indexes]}
            # the turn's requirements: derived from the answer's declared parts, each given in full by the batch (a
            # test of completeness scripts its own judge)
            if "<derive_requirements>" in input:
                out["requirements"] = [requirement(text=h, units=indexes)
                                       for h in re.findall(r"<hint>(.*?)</hint>", input, re.S)]
            elif "<requirements>" in input:
                out["requirements"] = [requirement(id=i, units=indexes)
                                       for i in re.findall(r'<requirement id="([A-Z][\d.]+)"', input)]
            return out

        self.on(Purpose.VERIFY, judge_fn, repeat=True)

    def _analysis(self, input: str) -> Any:
        response = self.request(input) if callable(self.request) else self.request
        if response is None:  # the default: the parts the first scripted final answer declares
            response = [component(p["ask"]) for p in (self._first_final or {}).get("parts") or []]
        if not isinstance(response, list):
            return response
        said = self.intent(input) if callable(self.intent) else self.intent
        return {"components": response, "intent": intent_part(said) if isinstance(said, str) else said}

    def structured(self, purpose: Purpose, instructions: str, input: str, schema, **kw: Any):
        if schema is not RequestAnalysis:
            return super().structured(purpose, instructions, input, schema, **kw)
        result = self._result(lambda i, x: self._analysis(x), instructions, input, schema)
        self.calls.append(ScriptedCall(Purpose(purpose), instructions, input, schema, result))
        return result

    def agent_step(self, instructions: str, items: list, tools: list[dict], final_schema: dict, **kw: Any) -> AgentStep:
        self.seen.append(list(items))
        index = len(self.seen) - 1
        if self.on_step is not None:
            self.on_step(index)
        if not self.steps:
            return AgentStep(CallStatus.ERROR, [], [], None, detail="no more steps")
        step = self.steps.pop(0)
        if callable(step):
            step = step(items)
        if isinstance(step, CallStatus):
            return AgentStep(step, [], [], None, detail="scripted failure")
        if isinstance(step, dict) and "final" in step:
            return AgentStep(CallStatus.OK, [{"role": "assistant", "content": json.dumps(step["final"])}], [],
                             step["final"], input_tokens=10, output_tokens=10, latency_ms=1)
        calls = []
        for i, c in enumerate(step):
            calls.append(SimpleNamespace(type="function_call", name=c["call"], arguments=json.dumps(c["arguments"]),
                                         call_id=f"call_{index}_{i}"))
        output = [{"type": "function_call", "name": c.name, "arguments": c.arguments, "call_id": c.call_id}
                  for c in calls]
        return AgentStep(CallStatus.OK, output, calls, None, input_tokens=10, output_tokens=10, latency_ms=1)

    def tool_outputs(self, step: int) -> list[str]:
        """The tool outputs the model saw at the given step."""
        return [i["output"] for i in self.seen[step] if isinstance(i, dict) and i.get("type") == "function_call_output"]
