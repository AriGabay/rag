"""The log of a turn, shaped (round 8 U7, R22): one entry per handled claim — what the verification did to it and why —
from the verification's own records (``verify.Logged``). The API stores it as diagnostics and links the user's rows to it
by claim id; it is verification-domain shaping, so it lives beside ``verify`` and not in the HTTP module."""

from __future__ import annotations

from app.chat import verify
from app.chat.compose import MARKER


def entry_ids(entry: verify.Logged) -> list[str]:
    """The ids an entry was checked against: those its decision named, the sources its claims were checked against and the
    sources its re-reads read and expanded (each once, in order)."""
    ids = list(entry.problem.checked_ids or []) if entry.problem is not None else []
    for r in entry.records:
        ids += r.sources_checked
        for x in r.rereads:
            ids += [x.get("source"), x.get("expanded_source")]
    return [i for i in dict.fromkeys(ids) if isinstance(i, str) and i]


# what the log says of an action that no check failed (a failing one has the problem's own reason)
ACTION_REASONS = {
    "rewritten": "הטענה נוסחה מחדש בסבב התיקון, והנוסח החדש נבדק",
    "qualified": "הטענה נתמכה חלקית; השרת סימן אותה או הוסיף לה סייג מהמקור",
    "cited": "המקור נמצא בבדיקה; השרת צירף אותו כציטוט",
    "cut": "חלק מהמשפט לא נתמך במקורות; השרת הסיר רק אותו",
    "removed": "הטענה הוסרה",
    "withheld": "הטענה לא נבדקה והוצאה מהתשובה",
}
REREAD_KEYS = ("source", "scope", "outcome", "reason", "expanded_source")


def decision(entry: verify.Logged, sources: list[dict]) -> dict:
    """One entry of the log (round 8 U7, R22): the claim id and the action; the original (draft) text and what remains of it
    (``text`` and ``remaining``); the verdict and the reason; the failure kind, the check, the component and whether a
    repair was asked (round 7); the cause when the claim could not be checked; the ids checked (``checked_ids``) and the
    sources checked (``sources_checked``, with ``sources`` as the client opens them); the re-reads attempted; the result of
    the final check on the text as saved."""
    p, r, action = entry.problem, entry.record, entry.action
    removed = action in verify.GONE_ACTIONS
    base = p.as_dict() if p is not None else verify.Problem.undecided_dict(entry.unit)
    claim = r.text if r is not None else entry.unit.text
    original = base["text"] if removed and p is not None else ((r.draft if r is not None else "") or claim)
    if action == "cut" and r is not None:
        base["failure_kind"] = r.failure_kind()
    reread: list[dict] = []
    for rec in entry.records:
        for x in rec.rereads:
            item = {k: x[k] for k in REREAD_KEYS if x.get(k) not in (None, "")}
            if item not in reread:
                reread.append(item)
    if removed:
        remaining = ""
    elif action == "cut":
        remaining = MARKER.format(n=entry.ordinal) if entry.ordinal > 0 else ""
    else:
        remaining = claim
    return base | {
        "claim_id": entry.claim_id, "action": action, "text": original, "remaining": remaining,
        "verdict": r.verdict if r is not None else "",
        "reason": base["reason"] or ACTION_REASONS.get(action, ""),
        "cause": base["cause"] or (r.cause if r is not None else ""),
        "sources_checked": list(dict.fromkeys(i for rec in entry.records for i in rec.sources_checked)),
        "sources": sources, "rereads": reread,
        "post_check": next((rec.post_check for rec in entry.records if rec.post_check), "n/a"),
    } | ({"ordinal": entry.ordinal} if action == "cut" and entry.ordinal > 0 else {})
