"""Round 26 U1 (R9, R11, R14, KTD3, KTD5, KTD6): the measurement driver of the grounded-clarification round.

It runs a file of conversation cases against a running stack, one arm at a time, and writes one JSONL line per turn.
It is public code and holds no data: the cases (development ones are public, held-out ones are private under
``real_documents/tests/round26/``) and the password (an env file named on the command line) come from outside, and the
password is never printed or recorded.

A case is a conversation::

    - id: c01
      split: dev            # dev | heldout
      kind: ambiguous       # ambiguous | clear | twin
      twin_of: c01          # optional: a twin names the ambiguous case whose answer it must match
      prefer_recent: false  # optional: the recency switch the case runs with
      typed_equivalent: "..."   # what a user would type to make the same choice as a click (the BASE arm's reply)
      turns:
        - ask: "..."
        - reply: "click:<match>"

A reply policy is one of ``click:<match>`` (the option whose label or detail contains ``<match>``; with no ``clarify``
payload on the previous answer, as on the base arm, the case's ``typed_equivalent`` is typed instead), ``number:<n>``
(types ``n``), ``text:<free text>``, ``dont_know`` (types «לא יודע»), ``all`` (clicks an "all" option when the answer
offers one, else types «כולן»), ``new_question:<text>`` and ``correction:<text>`` (type the text). A step may carry its own
``typed_equivalent`` that replaces the case's for that step.

The pure parts (policy parsing, option matching, the reply to send, the repeat detector, the turn record) take plain
dicts and do no I/O: ``tests/unit/test_clarify_driver.py`` tests them with fake payloads. The rest talks to the real
chat endpoints (``POST /api/chat/conversations/{id}/messages``, then ``GET /api/chat/messages/{id}`` while the answer
runs), the way ``eval.chat_eval.run_turn`` does.

Usage::

    cd backend && .venv/bin/python -m eval.clarify_driver --base http://localhost:18000 --env-file <env> \\
        --email <user> --cases <cases.yaml> --arm <base|new> --out <results.jsonl> [--dry] [--sha <file>] \\
        [--split dev|heldout] [--only id,id] [--runs 1]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from eval.chat_eval import POLL_SECONDS, TURN_TIMEOUT, SetHashMismatch, call_cost, check_set_sha

SPLITS = ("dev", "heldout")
KINDS = ("ambiguous", "clear", "twin")
DONT_KNOW_TEXT = "לא יודע"
ALL_TEXT = "כולן"
TEXT_POLICIES = ("text", "new_question", "correction")
POLICIES = ("click", "number", *TEXT_POLICIES, "dont_know", "all")


class CaseError(ValueError):
    """A case file or a policy that is not well formed."""


@dataclass
class Reply:
    """What one reply step sends: the typed ``content``, the button ``clarification`` when it is a click, and how it was resolved."""
    content: str
    clarification: dict | None = None
    how: str = "typed"  # clicked | typed | typed_equivalent | typed_fallback
    note: str | None = None
    matches: int = 0
    option: dict | None = None


@dataclass
class Totals:
    conversations: int = 0
    turns: int = 0
    cost: float = 0.0
    seconds: list[float] = field(default_factory=list)
    errors: int = 0


# ---------------------------------------------------------------- pure logic


def norm_text(s: str | None) -> str:
    """Case-folded, whitespace-collapsed, NFC text for matching labels."""
    return " ".join(unicodedata.normalize("NFC", s or "").casefold().split())


def parse_policy(policy: str) -> tuple[str, str]:
    """``kind:arg`` -> (kind, arg); ``dont_know`` and ``all`` take no argument."""
    if not isinstance(policy, str) or not policy.strip():
        raise CaseError(f"empty reply policy: {policy!r}")
    kind, sep, arg = policy.strip().partition(":")
    kind = kind.strip()
    if kind not in POLICIES:
        raise CaseError(f"unknown reply policy '{kind}' (known: {', '.join(POLICIES)})")
    if kind in ("dont_know", "all"):
        if sep and arg.strip():
            raise CaseError(f"policy '{kind}' takes no argument: {policy!r}")
        return kind, ""
    arg = arg.strip()
    if not arg:
        raise CaseError(f"policy '{kind}' needs an argument: {policy!r}")
    if kind == "number" and not arg.isdigit():
        raise CaseError(f"policy 'number' needs a whole number: {policy!r}")
    return kind, arg


def clarify_of(message: dict | None) -> dict | None:
    """The ``clarify`` payload of an assistant message (KTD5), on the message or inside its answer; None on the base arm."""
    if not message:
        return None
    c = message.get("clarify") or (message.get("answer") or {}).get("clarify")
    return c if isinstance(c, dict) and c else None


def options_of(message: dict | None) -> list[dict]:
    """The options the message shows, as ``{id, label, detail}`` dicts (empty without a ``clarify`` payload)."""
    c = clarify_of(message)
    return [o for o in (c.get("options") or []) if isinstance(o, dict)] if c else []


def option_set(options: list[dict]) -> frozenset[tuple[str, str]]:
    """An option set as (label, detail) pairs, order and case ignored, for the repeat detector."""
    return frozenset((norm_text(o.get("label")), norm_text(o.get("detail"))) for o in options)


def is_repeat(options: list[dict], earlier: list[frozenset]) -> bool:
    """True when this option set equals any earlier non-empty option set of the same conversation."""
    return bool(options) and option_set(options) in earlier


def match_options(options: list[dict], needle: str) -> list[dict]:
    """The options whose label contains ``needle``; when none does, those whose detail does. Order kept."""
    n = norm_text(needle)
    if not n:
        return []
    by_label = [o for o in options if n in norm_text(o.get("label"))]
    return by_label or [o for o in options if n in norm_text(o.get("detail"))]


def find_all_option(options: list[dict]) -> dict | None:
    """An "all" option among the shown options: id ``all`` or a label that says «כולן» / «הכל» / «כל היחידות»."""
    for o in options:
        label = norm_text(o.get("label"))
        if norm_text(str(o.get("id", ""))) == "all" or label.startswith(("כולן", "כולם", "הכל", "כל ")):
            return o
    return None


def asked_of(message: dict | None) -> bool:
    """Whether the assistant asked: a clarification status, or a ``clarify`` payload, or a clarification question."""
    if not message:
        return False
    a = message.get("answer") or {}
    return (message.get("status") == "clarification" or a.get("status") == "clarification"
            or clarify_of(message) is not None or bool(a.get("clarification")))


def resolve_reply(policy: str, previous: dict | None, *, typed_equivalent: str | None = None) -> Reply:
    """What to send for a reply step, given the previous assistant message (None when there is none).

    A click is a button choice only when the previous message carries a ``clarify`` payload with a matching option; on
    an arm that has none the case's ``typed_equivalent`` is typed. A click that matches nothing is typed (the
    equivalent, else the match text) and flagged ``typed_fallback``, so a policy that did not do what it says is
    visible in the record rather than silently different."""
    kind, arg = parse_policy(policy)
    if kind == "number":
        return Reply(arg)
    if kind == "dont_know":
        return Reply(DONT_KNOW_TEXT)
    if kind in TEXT_POLICIES:
        return Reply(arg)
    opts = options_of(previous)
    clarify = clarify_of(previous) or {}
    if kind == "all":
        o = find_all_option(opts)
        if o is not None and clarify.get("id") is not None:
            return Reply(str(o.get("label") or ALL_TEXT), {"id": clarify["id"], "option_id": o["id"]}, "clicked",
                         option=o)
        return Reply(ALL_TEXT, how="typed_fallback" if clarify else "typed_equivalent",
                     note="no all option offered" if clarify else None)
    # click:<match>
    if not clarify:
        if typed_equivalent:
            return Reply(typed_equivalent, how="typed_equivalent")
        return Reply(arg, how="typed_fallback", note="no clarify payload and no typed_equivalent")
    hits = match_options(opts, arg)
    if hits and clarify.get("id") is not None:
        o = hits[0]
        return Reply(str(o.get("label") or arg), {"id": clarify["id"], "option_id": o["id"]}, "clicked",
                     "several options match" if len(hits) > 1 else None, len(hits), o)
    return Reply(typed_equivalent or arg, how="typed_fallback", note="no option matches the click")


def validate_cases(cases: list[dict]) -> list[dict]:
    """The cases, checked: ids unique, split and kind known, turns start with an ask, every step is exactly one of ask
    and reply with a parseable policy. Raises ``CaseError`` naming the case."""
    seen: set[str] = set()
    for c in cases:
        cid = str(c.get("id", ""))
        if not cid or cid in seen:
            raise CaseError(f"case id missing or repeated: {cid!r}")
        seen.add(cid)
        if c.get("split") not in SPLITS:
            raise CaseError(f"{cid}: split must be one of {SPLITS}")
        if c.get("kind") not in KINDS:
            raise CaseError(f"{cid}: kind must be one of {KINDS}")
        turns = c.get("turns")
        if not isinstance(turns, list) or not turns:
            raise CaseError(f"{cid}: no turns")
        if "ask" not in turns[0]:
            raise CaseError(f"{cid}: the first step must be an ask")
        for i, s in enumerate(turns, 1):
            if not isinstance(s, dict) or ("ask" in s) == ("reply" in s):
                raise CaseError(f"{cid} step {i}: exactly one of ask and reply")
            if "ask" in s and not str(s["ask"]).strip():
                raise CaseError(f"{cid} step {i}: empty ask")
            if "reply" in s:
                parse_policy(s["reply"])
    return cases


def load_cases(path: str | Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise CaseError(f"{path}: expected a list of cases or a mapping with `cases`")
    return validate_cases(cases)


def select_cases(cases: list[dict], *, split: str = "", only: set[str] | None = None) -> list[dict]:
    return [c for c in cases if (not split or c["split"] == split) and (not only or c["id"] in only)]


def read_env_file(path: str | Path) -> dict[str, str]:
    """``KEY=VALUE`` lines (comments and blanks skipped, matching quotes stripped). Values are never printed."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip().removeprefix("export ").strip()] = v
    return out


def credentials(env: dict[str, str], email: str | None, password_key: str) -> tuple[str, str]:
    """(email, password) from the env file; the ``--email`` argument wins over ``EVAL_EMAIL``. Names, not values, in errors."""
    mail = email or env.get("EVAL_EMAIL")
    pw = env.get(password_key) or env.get("EVAL_PASSWORD")
    if not mail:
        raise CaseError("no email: pass --email or put EVAL_EMAIL in the env file")
    if not pw:
        raise CaseError(f"no password: the env file has neither {password_key} nor EVAL_PASSWORD")
    return mail, pw


def _count(x) -> int | None:
    if isinstance(x, (list, tuple, dict)):
        return len(x)
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def message_cost(m: dict) -> float | None:
    """Sum of the calls' ``cost_usd`` the message exposes (``usage``), pricing a call that lacks it from the price table
    the way ``chat_eval`` does; None when the message exposes no usage."""
    usage = m.get("usage")
    if not usage:
        return None
    total = 0.0
    for u in usage:
        c = u.get("cost_usd")
        if c is None:
            try:
                c = call_cost(u)
            except Exception:  # noqa: BLE001 - an unpriced call counts as nothing, like chat_eval
                c = None
        total += c or 0.0
    return round(total, 6)


def cited_ids(answer: dict) -> list[str]:
    """Document ids the answer cites (its sources), first-seen order, no repeats."""
    out: list[str] = []
    for s in answer.get("sources") or []:
        d = s.get("document_id")
        if d and d not in out:
            out.append(d)
    return out


def turn_record(case: dict, *, arm: str, run: int, index: int, step: dict, sent: Reply | None, user: dict | None,
                assistant: dict | None, seconds: float | None, earlier_sets: list[frozenset]) -> dict:
    """One JSONL record for a turn. ``earlier_sets`` holds the option sets of this conversation's earlier assistant
    messages; the caller appends this turn's set afterwards."""
    a = (assistant or {}).get("answer") or {}
    opts = options_of(assistant)
    clarify = clarify_of(assistant)
    return {
        "record": "turn", "arm": arm, "run": run, "case": case["id"], "split": case["split"], "kind": case["kind"],
        "twin_of": case.get("twin_of"), "turn": index, "step": step,
        "sent": None if sent is None else {"content": sent.content, "clarification": sent.clarification,
                                           "how": sent.how, "note": sent.note, "matches": sent.matches},
        "user_message_id": (user or {}).get("id"), "assistant_message_id": (assistant or {}).get("id"),
        "status": (assistant or {}).get("status"), "answer_status": a.get("status"),
        "asked": asked_of(assistant), "has_clarify": clarify is not None,
        "clarify_id": (clarify or {}).get("id"), "clarify_state": (clarify or {}).get("state"),
        "clarify_question": (clarify or {}).get("question") or a.get("clarification"),
        "options": [{"label": o.get("label"), "detail": o.get("detail")} for o in opts],
        "repeat_options": is_repeat(opts, earlier_sets),
        "seconds": seconds, "cost_usd": message_cost(assistant or {}),
        "searches": _count(a.get("searches")), "steps": _count(a.get("steps")),
        "error": (assistant or {}).get("error"),
        "answer_markdown": a.get("markdown") or (assistant or {}).get("content") or None,
        "cited_document_ids": cited_ids(a),
    }


# ---------------------------------------------------------------- network


class StepFailed(Exception):
    """A step the case cannot continue after: an unexpected 5xx, another refusal or a turn that never finished."""


def send(c: httpx.Client, cid: str, reply: Reply, prefer_recent: bool | None) -> dict:
    """POST one message; returns the ``{user, assistant}`` pair. 409 ``clarification_stale`` comes back as ``{"refused": body}``."""
    body: dict = {"content": reply.content, "client_id": str(uuid.uuid4())}
    if reply.clarification:
        body["clarification"] = reply.clarification
    if prefer_recent is not None:
        body["prefer_recent"] = prefer_recent
    r = c.post(f"/api/chat/conversations/{cid}/messages", json=body)
    if r.status_code >= 500:
        raise StepFailed(f"unexpected {r.status_code} on send")
    if r.status_code == 409 and "clarification_stale" in r.text:
        return {"refused": r.text[:300]}
    if r.status_code >= 400:
        raise StepFailed(f"send refused {r.status_code}: {r.text[:200]}")
    return r.json()


def wait_for(c: httpx.Client, mid: str) -> tuple[dict, float]:
    started = time.monotonic()
    while True:
        r = c.get(f"/api/chat/messages/{mid}")
        if r.status_code >= 500:
            raise StepFailed(f"unexpected {r.status_code} while polling")
        if r.status_code >= 400:
            raise StepFailed(f"poll refused {r.status_code}")
        m = r.json()
        took = round(time.monotonic() - started, 1)
        if m["status"] not in ("running", "cancelling"):
            return m, took
        if time.monotonic() - started > TURN_TIMEOUT:
            raise StepFailed(f"turn still {m['status']} after {TURN_TIMEOUT}s")
        time.sleep(POLL_SECONDS)


def plan_lines(case: dict) -> list[str]:
    lines = [f"{case['id']} [{case['split']}/{case['kind']}]" + (f" twin_of={case['twin_of']}" if case.get("twin_of") else "")]
    for i, s in enumerate(case["turns"], 1):
        if "ask" in s:
            lines.append(f"  {i}. ask: {s['ask']}")
        else:
            kind, arg = parse_policy(s["reply"])
            eq = s.get("typed_equivalent") or case.get("typed_equivalent")
            tail = f" (base arm types: {eq})" if kind == "click" and eq else ""
            lines.append(f"  {i}. reply {kind}{':' + arg if arg else ''}{tail}")
    return lines


def run_case(c: httpx.Client, case: dict, *, arm: str, run: int, sink) -> tuple[list[dict], str | None]:
    """Runs one conversation; writes each turn's record to ``sink`` as it completes. Returns the records and the
    reason the case stopped early (None when it ran to the end)."""
    created = c.post("/api/chat/conversations")
    if created.status_code >= 400:
        return [], f"could not create the conversation: {created.status_code}"
    cid = created.json()["id"]
    prefer_recent = case.get("prefer_recent")
    records: list[dict] = []
    sets: list[frozenset] = []
    previous: dict | None = None
    stopped = None
    for i, step in enumerate(case["turns"], 1):
        sent = None
        try:
            if "ask" in step:
                sent = Reply(str(step["ask"]).strip())
            else:
                sent = resolve_reply(step["reply"], previous,
                                     typed_equivalent=step.get("typed_equivalent") or case.get("typed_equivalent"))
            pair = send(c, cid, sent, prefer_recent)
            if "refused" in pair:  # a stale choice is a finding, not a crash: record it and go on
                rec = turn_record(case, arm=arm, run=run, index=i, step=step, sent=sent, user=None, assistant=None,
                                  seconds=None, earlier_sets=sets)
                rec.update(status="refused_409", error=pair["refused"])
            else:
                m, took = wait_for(c, pair["assistant"]["id"])
                rec = turn_record(case, arm=arm, run=run, index=i, step=step, sent=sent, user=pair["user"],
                                  assistant=m, seconds=took, earlier_sets=sets)
                previous = m
                if options_of(m):
                    sets.append(option_set(options_of(m)))
        except (StepFailed, httpx.HTTPError) as e:
            rec = turn_record(case, arm=arm, run=run, index=i, step=step, sent=sent, user=None, assistant=None,
                              seconds=None, earlier_sets=sets)
            rec.update(status="driver_error", error=str(e)[:300])
            stopped = f"step {i}: {e}"
        records.append(rec)
        sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sink.flush()
        if stopped:
            break
    return records, stopped


def summarize(totals: Totals) -> list[str]:
    med = statistics.median(totals.seconds) if totals.seconds else 0.0
    return [f"conversations: {totals.conversations}", f"turns: {totals.turns}",
            f"total cost: ${totals.cost:.4f}", f"median seconds per turn: {med:.1f}",
            f"conversations stopped early: {totals.errors}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cases", required=True, help="the cases YAML")
    ap.add_argument("--arm", required=True, help="the arm label recorded on every line (e.g. base, new)")
    ap.add_argument("--out", required=True, help="the JSONL file written (appended to if it exists)")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--env-file", help="env file holding the password (DEMO_PASSWORD by default) and optionally EVAL_EMAIL")
    ap.add_argument("--email", default="")
    ap.add_argument("--password-key", default="DEMO_PASSWORD")
    ap.add_argument("--split", choices=SPLITS, default="")
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--runs", type=int, default=1, help="runs per case (the pre-registration asks for two)")
    ap.add_argument("--sha", default="", help="a `shasum -a 256` line of the frozen cases file: a different file is refused")
    ap.add_argument("--dry", action="store_true", help="print the planned steps and nothing else (no login, no network)")
    args = ap.parse_args(argv)
    if args.sha:
        try:
            check_set_sha(args.cases, args.sha)
        except (SetHashMismatch, OSError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
    try:
        cases = select_cases(load_cases(args.cases), split=args.split, only={x for x in args.only.split(",") if x})
    except (CaseError, OSError, yaml.YAMLError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.dry:
        for case in cases:
            print("\n".join(plan_lines(case)))
        print(f"{len(cases)} cases x {args.runs} runs, arm {args.arm}")
        return 0
    if not args.env_file:
        print("refused: --env-file is required for a real run", file=sys.stderr)
        return 2
    try:
        email, password = credentials(read_env_file(args.env_file), args.email or None, args.password_key)
    except (CaseError, OSError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    c = httpx.Client(base_url=args.base, timeout=60)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    if r.status_code >= 400:
        print(f"refused: login failed ({r.status_code})", file=sys.stderr)
        return 2
    totals = Totals()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as sink:
        for case in cases:
            for run in range(1, args.runs + 1):
                records, stopped = run_case(c, case, arm=args.arm, run=run, sink=sink)
                totals.conversations += 1
                totals.errors += 1 if stopped else 0
                for rec in records:
                    if rec.get("assistant_message_id"):
                        totals.turns += 1
                        totals.cost += rec.get("cost_usd") or 0.0
                        if rec.get("seconds") is not None:
                            totals.seconds.append(rec["seconds"])
                sink.write(json.dumps({"record": "case", "arm": args.arm, "run": run, "case": case["id"],
                                       "split": case["split"], "kind": case["kind"], "turns": len(records),
                                       "stopped": stopped}, ensure_ascii=False) + "\n")
                sink.flush()
                print(f"{case['id']} run {run}: {len(records)} turns" + (f" STOPPED {stopped}" if stopped else ""))
    print("\n".join(summarize(totals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
