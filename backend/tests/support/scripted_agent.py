"""Test-only model for the conversational loop: replays scripted steps (tool calls or a final answer) and
answers the verification judge. Every step records the input items it received, so tests can check what the
model was shown (history, prior references, tool outputs)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from app.providers.llm import AgentStep, CallStatus, Purpose
from tests.support.scripted_provider import ScriptedProvider


def call(name: str, **arguments) -> dict:
    return {"call": name, "arguments": arguments}


def final(markdown: str, status: str = "answered", claims: list | None = None, clarification: str = "",
          missing: str = "", documents: list[str] | None = None, scope: str = "focused", scope_query: str = "",
          omitted: list | None = None, focus: dict | None = None, requested: list | None = None) -> dict:
    return {"final": {"status": status, "answer_markdown": markdown, "claims": claims or [],
                      "clarification_question": clarification, "missing_info": missing,
                      "referenced_document_ids": documents or [], "scope_kind": scope, "scope_query": scope_query,
                      "omitted": omitted or [], "focus": focus, "requested": requested or []}}


class ScriptedAgent(ScriptedProvider):
    """``steps``: a list whose items are a list of tool calls (one model step calling them), a ``final(...)``
    dict, a ``CallStatus`` (a failed step), or a callable ``(items) -> one of those``."""

    name = "scripted-agent"
    model = "scripted-agent"

    def __init__(self, steps: list, judge: str | Callable[[str], dict] = "supported") -> None:
        super().__init__()
        self.steps = list(steps)
        self.seen: list[list] = []
        self.on_step: Callable[[int], None] | None = None
        verdict = judge

        def judge_fn(instructions: str, input: str) -> dict:
            if callable(verdict):
                return verdict(input)
            indexes = [int(i) for i in re.findall(r'<unit index="(\d+)"', input)]
            return {"verdicts": [{"index": i, "verdict": verdict, "reason": "בדיקה"} for i in indexes]}

        self.on(Purpose.VERIFY, judge_fn, repeat=True)

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
