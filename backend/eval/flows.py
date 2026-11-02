"""Conversation helpers shared by the acceptance suite and the live evaluation runner.

They work with any httpx-compatible client (``fastapi.testclient.TestClient`` or ``httpx.Client``)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Flow:
    answer: dict
    conversation_id: str
    transcript: list[dict] = field(default_factory=list)  # every answer, in order
    clarifications: list[str] = field(default_factory=list)  # keys the system asked about
    latencies_ms: list[float] = field(default_factory=list)
    question_ids: list[str] = field(default_factory=list)  # one per request, in order
    error: str | None = None

    @property
    def kind(self) -> str:
        return self.answer.get("kind", "error")


def post_ask(client, body: dict) -> tuple[dict, float]:
    start = time.perf_counter()
    r = client.post("/api/ask", json=body)
    elapsed = (time.perf_counter() - start) * 1000
    if r.status_code != 200:
        return {"kind": "error", "status": r.status_code, "detail": r.text[:300]}, elapsed
    return r.json(), elapsed


def ask_flow(client, question: str, answers: dict | None = None, conversation_id: str | None = None,
             filters: dict | None = None, max_turns: int = 8, replies: dict | None = None,
             turn_id: str | None = None) -> Flow:
    """Ask ``question`` and answer the clarifications the system raises: with a button from ``answers``
    (key -> option value), or in free text from ``replies`` (key -> typed text, R20), which wins.

    Stops at the first clarification whose key is in neither (the final answer is then that
    clarification), or whose wanted button value is not among the offered options. ``turn_id`` makes the
    first request idempotent (resubmitting the same body returns the stored turn)."""
    answers, replies = answers or {}, replies or {}
    body = {"question": question, "conversation_id": conversation_id}
    if filters:
        body["filters"] = filters
    if turn_id:
        body["turn_id"] = turn_id
    data, ms = post_ask(client, body)
    flow = Flow(answer=data.get("answer", data), conversation_id=data.get("conversation_id", conversation_id or ""))
    flow.transcript.append(flow.answer)
    flow.latencies_ms.append(ms)
    flow.question_ids.append(data.get("question_id") or "")
    for _ in range(max_turns):
        a = flow.answer
        if a.get("kind") != "clarification":
            break
        key = a["clarification"]["key"]
        flow.clarifications.append(key)
        if key in replies:
            data, ms = post_ask(client, {"conversation_id": flow.conversation_id, "question": replies.pop(key)})
            flow.answer = data.get("answer", data)
            flow.transcript.append(flow.answer)
            flow.latencies_ms.append(ms)
            flow.question_ids.append(data.get("question_id") or "")
            continue
        if key not in answers:
            break
        options = [o["value"] for o in a["clarification"]["options"]]
        if answers[key] not in options:
            flow.error = f"clarification {key}: wanted {answers[key]!r}, offered {options}"
            break
        data, ms = post_ask(client, {"conversation_id": flow.conversation_id,
                                     "clarification": {"key": key, "value": answers[key]}})
        flow.answer = data.get("answer", data)
        flow.transcript.append(flow.answer)
        flow.latencies_ms.append(ms)
        flow.question_ids.append(data.get("question_id") or "")
    return flow
