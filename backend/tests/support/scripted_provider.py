"""Test-only provider that replays recorded structured outputs (KTD3).

Responses are registered per purpose, optionally only for inputs containing ``match``. A response is a
dict (validated into the requested schema), a Pydantic instance, a ``CallStatus`` (a failure with no
output), a ready ``StructuredResult``, or a callable ``(instructions, input) -> any of those``. Each
registration answers once unless ``repeat=True``. Every call is recorded in ``calls``; an unscripted
call returns ``error`` so a missing script fails loudly instead of looking like a real answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.providers.llm import BaseProvider, CallStatus, Purpose, StructuredResult


@dataclass
class _Entry:
    purpose: Purpose
    response: Any
    match: str | None
    repeat: bool
    used: bool = False


@dataclass
class ScriptedCall:
    purpose: Purpose
    instructions: str
    input: str
    schema: type[BaseModel]
    result: StructuredResult = field(repr=False)


class ScriptedProvider(BaseProvider):
    name = "scripted"
    model = "scripted"
    demo = False

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self.calls: list[ScriptedCall] = []

    def on(self, purpose: Purpose | str, response: Any, *, match: str | None = None,
           repeat: bool = False) -> ScriptedProvider:
        self._entries.append(_Entry(Purpose(purpose), response, match, repeat))
        return self

    def structured(self, purpose: Purpose, instructions: str, input: str, schema: type[BaseModel], *,
                   max_output_tokens: int | None = None, deadline: float | None = None,
                   reasoning_effort: str | None = None) -> StructuredResult:
        purpose = Purpose(purpose)
        entry = next((e for e in self._entries if e.purpose == purpose and not (e.used and not e.repeat)
                      and (e.match is None or e.match in input)), None)
        if entry is None:
            result = StructuredResult(CallStatus.ERROR, detail="unscripted")
        else:
            entry.used = True
            result = self._result(entry.response, instructions, input, schema)
        self.calls.append(ScriptedCall(purpose, instructions, input, schema, result))
        return result

    @staticmethod
    def _result(response: Any, instructions: str, input: str, schema: type[BaseModel]) -> StructuredResult:
        if callable(response) and not isinstance(response, type):
            response = response(instructions, input)
        if isinstance(response, StructuredResult):
            return response
        if isinstance(response, CallStatus):
            return StructuredResult(response)
        try:
            parsed = response if isinstance(response, schema) else schema.model_validate(response)
        except ValidationError:
            return StructuredResult(CallStatus.INVALID, detail="schema")
        return StructuredResult(CallStatus.OK, parsed, input_tokens=0, output_tokens=0, latency_ms=0)
