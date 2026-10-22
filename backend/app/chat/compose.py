"""One composer for every edit the server makes to an answer after the model wrote it (round 8 U4, KTD4, KTD6).

The server edits the answer's Markdown after the last verification: the cuts, partial marks, qualifiers and inserted
citations of ``VerifyReport.apply``, the removal note or replacement sentence, the request-mismatch note, the second
``apply`` after the absences were superseded, the gap paragraph, ``tidy``, the limit sentence and the coverage or
membership note. Whatever it writes must stay told apart from what the model wrote: a later check that could not tell
them apart would judge the server's own sentences, and a follow-up turn would read a removal note as a fact.

``Doc`` is the text with a tag per character: 0 for what the model wrote, a span number for what the server inserted.
Every edit goes through ``Doc`` (a splice, a deletion), so the tags move with the text and the registry — the list of
``ServerSpan`` (offsets and a fixed kind, never text) — indexes the FINAL Markdown exactly, whatever was cut, collapsed
or appended after it. ``compose`` runs the editors in the order ``engine.finish`` runs them and returns the final
answer with its registry; the editors keep their own logic and write through ``Doc``.

The spans a kind covers:

- ``marker``: «[N: חלק שלא אומת הושמט]», in place of a claim's span cut from a sentence (the edit ladder, step 2), with N
  its ordinal in the final text; ``partial_mark``: " *(אומת חלקית)*" after a partly supported sentence — the one span
  that also carries its ordinal among the marks of the final text and the claim ids of the unit it marks (round 14 U7,
  KTD9: ids, never text), so the client links it to the diagnostics entries of those claims;
  ``qualifier``: a qualifier written next to a number, or after a total row whose component went — also the scope its source
  states for a value the answer showed wider (round 17 U5, R10); ``scope_note``: the note next to a value that its scope in the
  source was not verified, and where it was read (round 17 U5, R11); ``citation``: a
  ``[S#]`` inserted for support the server found; every addition inside the answer (these, the partial mark and the cut marker) is
  registered with the claims it is about and, for a note of a ledger item or a calculated value, the datum (``ServerSpan.datum``: an
  item id or a V#/C#), and sits inside that datum's own clause or cell (round 21 U2, KTD1);
- ``removal_note``: the note that says how many claims were removed; ``replacement``: the sentence that replaces an
  answer nothing of which was supported; ``request_note``: the note that the datum is not the one requested;
- ``gap_paragraph``: the lines that state each component not given; ``item_gap``: the lines that state each item of the item
  ledger the answer does not give, or declared not verifiable or not applicable (round 17 U3, R8; ``coverage.item_gap_lines``);
  ``limit_sentence``: what a limit the turn reached
  means for the answer; ``coverage_note`` and ``membership_note``: the coverage ledger's note; ``unread_notice``: the
  one notice of the files the user may see that were not read, written after the gap paragraph when the turn located
  no document (round 9 U6, KTD11); ``uncertainty_line``: the one line, in the gap area after the item lines, that names the data a
  condition or a scope of the source could not be bound to (round 21 U2, KTD2): the server writes nothing inline for them.

``Doc`` also keeps, for every character of the model's own text, the offset it had in the text the model wrote (``origin``;
-1 for a server span), so the final check (round 8 U6, ``app.chat.final_check``) can tell which sentence of the answer a
sentence of the saved text came from, whatever was cut around it. The registry is what is saved with the message
(``SERVER_SPANS_FIELD``); ``strip_server_spans`` gives a later turn the earlier answer without the server's words (R28), and
``history_notes`` gives it, beside that prose, the meaning those words carried - the condition, scope, caveat or gap - as labelled system
notes by ``SPAN_ROLES`` (round 22 U11, KTD10, R19): UI and citation spans are dropped, the rest become a note on their datum."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.chat import slots
from app.providers.llm import prompt_attr, prompt_text

if TYPE_CHECKING:
    from app.chat.engine import FinalAnswer

# the additions inside the answer that name a datum (``ServerSpan.claim_ids``, ``datum``): the audit of ``Doc.audit`` reads these
AUDITED_KINDS = ("qualifier", "scope_note", "citation", "partial_mark")
SERVER_KINDS = ("marker", "partial_mark", "qualifier", "citation", "removal_note", "request_note", "gap_paragraph",
                "limit_sentence", "coverage_note", "membership_note", "replacement", "unread_notice",
                "conditional_note", "item_gap", "scope_note", "uncertainty_line")
# the marker a cut span leaves: an ordinal, so the log entry of the claim it replaced is found by it
MARKER = "«[{n}: חלק שלא אומת הושמט]»"
PARTIAL_MARK = " *(אומת חלקית)*"
SERVER_SPANS_FIELD = "server_spans"  # the key under which a stored answer holds its registry (``ServerSpan.as_dict``)


def text_digest(markdown: str) -> str:
    """The digest of the saved Markdown (R17): sha256 of its UTF-8 bytes, as the checked text is recorded."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def line_cut(start: int, end: int, is_last: bool) -> tuple[int, int]:
    """The range that removes the line ``text[start:end]`` with one newline: its own, or the one before it when the line
    is the last (it has none after it)."""
    return (max(start - 1, 0), end) if is_last else (start, end + 1)


def strip_server_spans(markdown: str, spans) -> str:
    """The answer without the spans the server wrote in it (R28): ``spans`` are the registry as saved with the message
    (``ServerSpan`` or the dicts ``ServerSpan.as_dict`` gives; offsets into ``markdown``). What a removal note, a partial
    mark, a gap line or a coverage note said is not something the earlier answer stated, so a later turn is not shown it.
    Spaces left around a removed span are closed and the blank lines it leaves are collapsed. Without a registry (an older
    message), or with one that does not index this text, the text is returned as it is."""
    ranges: list[tuple[int, int]] = []
    for s in spans or ():
        a, b = (s.start, s.end) if isinstance(s, ServerSpan) else (s.get("start"), s.get("end"))
        if not (isinstance(a, int) and isinstance(b, int)) or not 0 <= a <= b <= len(markdown):
            return markdown
        ranges.append((a, b))
    if not ranges:
        return markdown
    out, pos = [], 0
    for a, b in sorted(ranges):
        out.append(markdown[pos:max(pos, a)])
        pos = max(pos, b)
    out.append(markdown[pos:])
    text = re.sub(r"[ \t]{2,}", " ", "".join(out))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- what a later turn is shown of each kind of server span (round 22 U11, KTD10, R19) ---------------------------------------------------------
# The role of every kind in ``SERVER_KINDS``: a ``meaning`` span says something that changes how a datum is read (its condition, scope, the
# part left out, what was not found, what a limit meant), so the next turn sees it as a system note beside the answer; a ``ui`` span is a
# display or citation device (the ``[S#]`` link, the cut placeholder) and is not shown. The cut placeholder is the one ui span that leaves a
# trace: a note that a part of the sentence was omitted, never the placeholder's text.
ROLE_MEANING, ROLE_UI = "meaning", "ui"
SPAN_ROLES = {
    "qualifier": ROLE_MEANING, "scope_note": ROLE_MEANING, "conditional_note": ROLE_MEANING, "uncertainty_line": ROLE_MEANING,
    "partial_mark": ROLE_MEANING, "removal_note": ROLE_MEANING, "request_note": ROLE_MEANING, "gap_paragraph": ROLE_MEANING,
    "item_gap": ROLE_MEANING, "coverage_note": ROLE_MEANING, "membership_note": ROLE_MEANING, "limit_sentence": ROLE_MEANING,
    "unread_notice": ROLE_MEANING, "replacement": ROLE_MEANING,
    "citation": ROLE_UI, "marker": ROLE_UI,
}
HISTORY_NOTES_MAX = 8  # notes shown for one earlier answer; the rest are counted
HISTORY_NOTES_CHARS = 1800  # the block's own budget (the answer prose has its own cut, which never takes a note)
HISTORY_NOTE_TEXT_CHARS = 240  # the server text of one note
HISTORY_LABEL_CHARS = 60  # the words of the answer that name the datum of a note
NOTE_LINE = 'הערת מערכת על "{label}": {text}'
OMISSION_NOTE = "חלק מהמשפט לא אומת מול המקור והושמט מהתשובה"
NOTES_OVERFLOW = "(ועוד {n} הערות מערכת לא הוצגו)"
WHOLE_ANSWER_LABEL = "התשובה כולה"
REPLACEMENT_STAND_IN = "(העוזר לא הצליח לבסס תשובה בתור זה)"
ONLY_NOTES_STAND_IN = "(בתור זה העוזר מסר רק הערות מערכת)"
_CITATION = re.compile(r"\s*\[[SMCPVA]\d+(?:\s*[,،;]\s*[SMCPVA]\d+)*\]")
_SENTENCE_END = re.compile(r"[.;!?]\s+|\n")


def stand_in(spans) -> str:
    """The one line an earlier answer leaves when nothing but server text is left of it, so the turn is not lost (KTD10): a replacement
    sentence says the assistant could not ground an answer; any other all-server answer says it gave only system notes."""
    kinds = {(s.kind if isinstance(s, ServerSpan) else (s or {}).get("kind")) for s in spans or ()}
    return REPLACEMENT_STAND_IN if "replacement" in kinds else ONLY_NOTES_STAND_IN


def _note_text(raw: str) -> str:
    """The server text of a span as a note: no citations, markup, blockquote marks or enclosing brackets, on one line."""
    t = _CITATION.sub("", raw)
    t = re.sub(r"(?m)^\s*>+\s*", "", t).replace("**", "").replace("*", "")
    t = " ".join(t.split())
    while len(t) > 1 and t[0] == "(" and t[-1] == ")" and "(" not in t[1:-1]:
        t = t[1:-1].strip()
    return t.strip(" ")


def _cap(t: str, limit: int) -> str:
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def _nearby_words(markdown: str, at: int, ranges: list[tuple[int, int]]) -> str:
    """The answer's own words just before ``at``, in its clause: the label of an addition that has no datum record. A span at the start of a
    line is not about the words before it (a gap line, a coverage note), so it is about the whole answer."""
    line_start = markdown.rfind("\n", 0, at) + 1
    kept, pos = [], line_start
    for a, b in sorted(r for r in ranges if r[1] > line_start and r[0] < at):
        kept.append(markdown[pos:max(pos, min(a, at))])
        pos = max(pos, min(b, at))
    kept.append(markdown[pos:at])
    before = " ".join(_CITATION.sub("", "".join(kept)).split())
    if not before:
        return WHOLE_ANSWER_LABEL
    clause = _SENTENCE_END.split(before + " ")[-1] or before
    clause = clause.strip(" ,;:-–()") or before
    if len(clause) > HISTORY_LABEL_CHARS:
        clause = clause[-HISTORY_LABEL_CHARS:]
        clause = clause.split(" ", 1)[1] if " " in clause else clause
    return clause.strip(" ,;:-–()") or WHOLE_ANSWER_LABEL


def history_notes(markdown: str, spans, labels: dict[str, str] | None = None) -> list[str]:
    """The system-note lines of an earlier answer for a later turn (round 22 U11, KTD10, R19): one ``NOTE_LINE`` per meaning-bearing span of
    its stored registry, in the text's order, naming the datum it belongs to - ``labels`` (the datum id -> the label the answer stored for
    it) when the span has a datum, otherwise the answer's own words just before it. Citations and markers are not notes (a marker leaves
    ``OMISSION_NOTE``). The document-derived part is sanitized for a prompt, capped in length and one line; at most ``HISTORY_NOTES_MAX``
    notes and ``HISTORY_NOTES_CHARS`` characters, the rest counted in a last line. A message with no registry, or one that does not index
    this text (an older message), has no notes: nothing is derived from its Markdown. A note is context for reading the earlier answer, never
    evidence for a claim of the next one: verification grounds only in the turn's sources."""
    found: list[tuple[int, int, str, str]] = []
    for s in spans or ():
        d = s.as_dict() if isinstance(s, ServerSpan) else s
        a, b, kind = d.get("start"), d.get("end"), d.get("kind")
        if not (isinstance(a, int) and isinstance(b, int)) or not 0 <= a <= b <= len(markdown):
            return []
        if kind in SPAN_ROLES:
            found.append((a, b, kind, d.get("datum") or ""))
    found.sort()
    ranges = [(a, b) for a, b, _, _ in found]
    lines: list[str] = []
    for a, b, kind, datum in found:
        if SPAN_ROLES[kind] == ROLE_UI:
            note = OMISSION_NOTE if kind == "marker" else ""
        else:
            note = _note_text(markdown[a:b])
        if not note:
            continue
        label = (labels or {}).get(datum) if datum else None
        at_line_start = not markdown[:a].rstrip(" \t").rsplit("\n", 1)[-1].strip()
        label = label or (WHOLE_ANSWER_LABEL if at_line_start else _nearby_words(markdown, a, ranges))
        line = NOTE_LINE.format(label=prompt_attr(_cap(" ".join(label.split()), HISTORY_LABEL_CHARS)),
                                text=prompt_text(_cap(note, HISTORY_NOTE_TEXT_CHARS)))
        if line not in lines:
            lines.append(line)
    out, used = [], 0
    for line in lines:
        if len(out) >= HISTORY_NOTES_MAX or used + len(line) > HISTORY_NOTES_CHARS:
            break
        out.append(line)
        used += len(line)
    if len(lines) > len(out):
        out.append(NOTES_OVERFLOW.format(n=len(lines) - len(out)))
    return out


@dataclass(frozen=True)
class ServerSpan:
    """A run of the final Markdown the server wrote: offsets and a fixed kind (``SERVER_KINDS``), never its text. A
    ``partial_mark`` also has its ``ordinal`` (1, 2, ... in the text's order, like the ordinal of a cut marker) and the
    ``claim_ids`` of the unit it marks (round 14 U7, KTD9). Round 21 U2 (KTD1): any addition inside the answer carries the ``claim_ids`` it
    is about and its ``datum`` (a ledger item id, a V# or a C#) when it has them, so the registry says what each server word belongs to."""

    start: int
    end: int
    kind: str
    ordinal: int = 0
    claim_ids: tuple[str, ...] = ()
    datum: str = ""

    def as_dict(self) -> dict:
        out = {"start": self.start, "end": self.end, "kind": self.kind}
        if self.kind == "partial_mark":
            out |= {"ordinal": self.ordinal, "claim_ids": list(self.claim_ids)}
        elif self.claim_ids:
            out["claim_ids"] = list(self.claim_ids)
        if self.datum:
            out["datum"] = self.datum
        return out


DATUM_PREFIX = "@"  # in a ``ref`` tuple: the entry that names the datum (an item id, a V#, a C#) rather than a claim


def ref_of(claim_ids=(), datum: str = "") -> tuple[str, ...]:
    """The ``ref`` of a server addition (``Doc.splice``): the claims it is about, then the datum it is bound to (``DATUM_PREFIX``)."""
    return (*dict.fromkeys(c for c in claim_ids if c), *((DATUM_PREFIX + datum,) if datum else ()))


class Doc:
    """Markdown with the spans the server wrote in it, kept exact through every edit (see the module docstring)."""

    def __init__(self, text: str = ""):
        self.text = text
        self._tag = [0] * len(text)
        self._origin = list(range(len(text)))
        self._kind: dict[int, str] = {}
        self._ref: dict[int, tuple] = {}
        # for each span, the offset in the model's draft of the model character it was written right after (-1: none before it): its anchor
        # in model-text coordinates, which no later edit moves (round 21 U6, KTD1, KTD4)
        self._anchor: dict[int, int] = {}
        self._count = 0

    def splice(self, start: int, end: int, insert: str = "", kind: str | None = None, ref: tuple = ()) -> None:
        """Replace ``text[start:end]`` by ``insert``; what is inserted is a server span of ``kind`` (``ref``: what the
        composer links it to, a claim id), or the model's own when ``kind`` is None."""
        tag = 0
        if kind and insert:
            assert kind in SERVER_KINDS, kind
            self._count += 1
            tag = self._count
            self._kind[tag], self._ref[tag] = kind, ref
            self._anchor[tag] = self._origin_before(start)
        self.text = self.text[:start] + insert + self.text[end:]
        self._tag[start:end] = [tag] * len(insert)
        self._origin[start:end] = [-1] * len(insert)

    def _origin_before(self, at: int) -> int:
        """The draft offset of the model character nearest before ``at``, the server's own characters skipped; -1 when none, and -2 when the
        nearest is model text with no draft offset (text the model wrote that an edit replaced)."""
        i = at - 1
        while i >= 0 and self._tag[i]:
            i -= 1
        return -1 if i < 0 else (self._origin[i] if self._origin[i] >= 0 else -2)

    def append(self, text: str, kind: str) -> None:
        self.splice(len(self.text), len(self.text), text, kind)

    def delete(self, ranges: list[tuple[int, int]]) -> None:
        """Remove every range (they may overlap), last first, so each stays valid."""
        merged: list[list[int]] = []
        for a, b in sorted(r for r in ranges if r[1] > r[0]):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        for a, b in reversed(merged):
            self.splice(a, b)

    def edit(self, edits: list[tuple[int, int, str, str | None, tuple]]) -> None:
        """The edits ``(start, end, insert, kind, ref)`` over the text as it stands, by span, last first, so earlier
        spans stay valid and no edit depends on matching text again. An edit inside a span already removed (a row of a
        removed table) is dropped."""
        done_from = len(self.text) + 1
        for a, b, insert, kind, ref in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
            if b > done_from:
                continue
            self.splice(a, b, insert, kind, ref)
            if b > a:
                done_from = a

    def tidy_blank_lines(self) -> None:
        """No run of blank lines longer than one, and no whitespace around the text."""
        self.delete([(m.start() + 2, m.end()) for m in re.finditer(r"\n{3,}", self.text)])
        lead = len(self.text) - len(self.text.lstrip())
        trail = len(self.text.rstrip())
        self.delete([(0, lead), (trail, len(self.text))])

    def model_line(self, at: int) -> tuple[int, str, list[int]]:
        """The line of the text holding ``at``: where it starts, its text with the server's characters blanked, and the draft offset of each
        of its characters (-1 for the server's)."""
        a = self.text.rfind("\n", 0, at) + 1
        b = self.text.find("\n", at)
        b = len(self.text) if b < 0 else b
        blank = "".join(" " if self._tag[i] else self.text[i] for i in range(a, b))
        return a, blank, self._origin[a:b]

    def survivors(self, ranges: list[tuple[int, int]], letters: bool = True) -> list[int]:
        """The positions of the letters and digits (``letters``; else every character but a space) of the text that the model wrote at the draft
        ranges ``ranges`` and that the edits left."""
        return [i for i, o in enumerate(self._origin)
                if o >= 0 and (self.text[i].isalnum() if letters else not self.text[i].isspace())
                and any(a <= o < b for a, b in ranges)]

    def audit(self, datum_ranges, kinds: tuple[str, ...] = AUDITED_KINDS) -> int:
        """The audit of the server's additions inside the answer (round 21 U6, KTD4; R1): each addition of ``kinds`` that names a datum is
        dropped when the datum's text is no longer in the answer, and follows it when the datum's place changed; the rest are left as they are.
        ``datum_ranges(span)``: the datum's text as draft ranges (model-text coordinates), or None when the caller cannot tell (the addition
        is then left alone: unknown never removes). The addition stays where it was written when the model character it was written after is
        still right before it; when that character is still in the text but something else stands between, it is moved to follow it; when
        the character went (a cut took the end of the datum's clause) it follows the last character of the datum's text before it, and when
        nothing of the datum's text is left before it, it goes. Deterministic, no judge call; a second run changes nothing. Returns the
        number of additions dropped or moved."""
        changed = 0
        for _ in range(len(self._kind) + 1):  # one change at a time on the text as it stands; each makes its span settled
            action = self._next_audit(datum_ranges, kinds)
            if action is None:
                break
            span, ref, anchor_at = action
            text, tag = self.text[span.start:span.end], self._tag[span.start]
            self.delete([(span.start, span.end)])
            if anchor_at is not None:
                at = anchor_at - (span.end - span.start) if anchor_at > span.start else anchor_at
                self.splice(at, at, text, self._kind[tag], ref)
            changed += 1
        return changed

    def _next_audit(self, datum_ranges, kinds):
        pos = None  # the position of each draft character in the text, built when a span needs it
        for span, ref in self.refs():
            tag = self._tag[span.start]
            if span.kind not in kinds or not (span.claim_ids or span.datum) or self._anchor.get(tag, -1) < 0:
                continue
            here = self._origin_before(span.start)
            if here == -2 or here == self._anchor[tag]:
                continue  # at its anchor (or the text before it is not the draft's: nothing to compare)
            ranges = datum_ranges(span)  # asked only of a span that moved
            if ranges is None:
                continue
            alive = self.survivors(ranges)
            if not alive:
                return span, ref, None  # the datum is no longer in the answer: its addition goes
            if pos is None:
                pos = {o: i for i, o in enumerate(self._origin) if o >= 0}
            if self._anchor[tag] in pos:
                return span, ref, pos[self._anchor[tag]] + 1  # the character it followed is there: it follows it again
            before = [i for i in self.survivors(ranges, letters=False) if self._origin[i] < self._anchor[tag]]
            if before:
                return span, ref, max(before, key=lambda i: self._origin[i]) + 1  # the end of the datum's clause went: it follows what is left
            return span, ref, None
        return None

    def clear(self) -> None:
        self.splice(0, len(self.text))

    def refs(self) -> list[tuple[ServerSpan, tuple]]:
        """Each server span with what the composer linked it to, in the text's order."""
        out: list[tuple[ServerSpan, tuple]] = []
        i, n, marks = 0, len(self.text), 0
        while i < n:
            tag, j = self._tag[i], i
            while j < n and self._tag[j] == tag:
                j += 1
            if tag:
                kind, ref = self._kind[tag], self._ref[tag]
                claims = tuple(x for x in ref if not x.startswith(DATUM_PREFIX))
                datum = next((x[len(DATUM_PREFIX):] for x in ref if x.startswith(DATUM_PREFIX)), "")
                if kind == "partial_mark":  # the mark's place among the marks of the text, and the claims it covers
                    marks += 1
                    out.append((ServerSpan(i, j, kind, marks, claims, datum), ref))
                else:
                    out.append((ServerSpan(i, j, kind, 0, claims, datum), ref))
            i = j
        return out

    def spans(self) -> list[ServerSpan]:
        return [s for s, _ in self.refs()]

    def origins(self) -> list[int]:
        """For each character of the text, its offset in the text the model wrote; -1 for a character the server wrote."""
        return list(self._origin)

    def masked(self) -> str:
        """The model's own text: what is left when every server span is removed."""
        return "".join(c for c, t in zip(self.text, self._tag, strict=True) if not t)

    def renumber(self, kind: str, template: str) -> list[tuple[int, tuple]]:
        """Write ``template.format(n=ordinal)`` into every span of ``kind``, ordinals in the order of position in the
        text (a span an earlier stage removed leaves no gap). Returns each ordinal with the span's ``ref``."""
        found = self.refs()
        spans = [(s, ref) for s, ref in found if s.kind == kind]
        for n, (s, ref) in reversed(list(enumerate(spans, 1))):
            new = template.format(n=n)
            if self.text[s.start:s.end] != new:
                self.splice(s.start, s.end, new, kind, ref)
        return [(n, ref) for n, (_, ref) in enumerate(spans, 1)]


@dataclass
class Edited:
    """A report's edits to an answer: the text as a ``Doc``, with the status and claims the edits leave."""

    doc: Doc
    status: str
    claims: list = field(default_factory=list)

    def answer(self, base: FinalAnswer) -> FinalAnswer:
        return base.model_copy(update={"answer_markdown": self.doc.text, "claims": self.claims, "status": self.status})


@dataclass
class Composition:
    """The finished answer with the registry of the spans the server wrote in it (``spans``, indexing
    ``answer.answer_markdown`` exactly), the components as stated (``outcomes``) and the coverage ledger."""

    answer: FinalAnswer
    spans: list[ServerSpan]
    outcomes: list[dict] = field(default_factory=list)
    ledger: dict = field(default_factory=dict)
    # for each character of the final Markdown, its offset in the model's draft (-1: the server's); transient — it is
    # not saved, the final check reads it (``Doc.origins``)
    origin: list[int] = field(default_factory=list)
    # the lists the final answer printed, built here from its own structure (round 22 U12, KTD14: ``slots.lists_of``) and stored in the
    # conversation's context; transient like ``origin``
    lists: list[dict] = field(default_factory=list)


def compose(ws, answer: FinalAnswer, report, turn, *, question: str, limit_sentences: list[str] | None = None
            ) -> Composition:
    """Every edit after the model's answer and its verification, in one pass (KTD6), in the order the engine always
    made them: the report's cuts, marks, qualifiers and citations, the removal note or the replacement sentence and
    the request-mismatch note; the units stating an absence the server states removed and the report applied again; the
    conditional note (one caveat per calculation, round 9 R23), the gap paragraph and the tidying; the notice of the files that were not read; the limit sentences of the limits the turn reached; and the coverage ledger's note.
    Returns the answer, the registry of the server's spans in it, the components as stated and the ledger (empty for a
    clarification, which asks and states nothing). The side effects of the ledger — a cited document is ``verified`` —
    are not made here (``coverage.touch_verified``): the engine makes them once, for the documents the final text cites."""
    from app.chat import coverage

    edited, outcomes = coverage.compose_components(ws, answer, report, turn)
    doc = edited.doc
    unread_shown = _unread_notice(ws, doc, report, question) if edited.status != "clarification" else []
    if unread_shown and edited.status == "answered":
        edited.status = "partial"  # files were not checked: never an answer that looks complete
    if limit_sentences and edited.status != "clarification":
        body = doc.text.strip()
        doc.append(("\n\n" if body else "") + " ".join(limit_sentences), "limit_sentence")
        edited.status = "partial"
    ledger: dict = {}
    if edited.status != "clarification":
        before = edited.answer(answer)
        ledger, built = coverage.build(ws, before, question)
        if unread_shown:
            ledger["unread_documents"] = [{"document_id": str(d["document_id"]), "title": d["title"]}
                                          for d in unread_shown]
        edited.status = built.status
        _take_appended(doc, built.answer_markdown, "membership_note" if ledger.get("membership") else "coverage_note")
    report.mark_markers(doc.renumber("marker", MARKER))
    spans = doc.spans()
    report.mark_partials([(s.ordinal, s.claim_ids) for s in spans if s.kind == "partial_mark"])
    return Composition(edited.answer(answer), spans, outcomes, ledger, doc.origins(),
                       [] if edited.status == "clarification" else _printed_lists(ws, doc.text, spans))


def _printed_lists(ws, markdown: str, spans) -> list[dict]:
    """The lists and tables the composed answer printed (round 22 U12, KTD14, R20), built once, here, from the answer's own structure and
    never again from stored Markdown: the text without the server's own spans, each list with the documents its items cite (an item that
    cites none falls back to the documents the answer cites), so the conversation can gate every list apart (R22)."""
    from app.chat import coverage

    text = strip_server_spans(markdown, spans).replace("\\", "")  # a citation may be written with escaped brackets
    return slots.lists_of(text, docs_of=lambda t: coverage.cited_documents(ws, t), fallback_documents=coverage.cited_documents(ws, text))


def _unread_notice(ws, doc: Doc, report, question: str) -> list[dict]:
    """The one notice of the visible files that were not read (KTD11, R5), appended to ``doc`` as a server span: when the
    turn tried to locate a document (it searched or listed) and the saved text cites none. A document a gap line already
    states is not listed again. Returns the files the notice names (at most ``unread.NOTICE_FILES``)."""
    from app.chat import coverage, unread

    if not (ws.searches or ws.scope is not None or ws.unread_named) or coverage.cited_documents(ws, doc.text):
        return []
    stated = ws.unread_named if any(g["reason"] == "document_not_read" for g in report.gaps or ()) else {}
    files = [d for d in ws.unread_files() if str(d["document_id"]) not in stated]
    notice, shown = unread.notice(files, question)
    if notice:
        doc.append(("\n\n" if doc.text.strip() else "") + notice, "unread_notice")
    return shown


def _take_appended(doc: Doc, built: str, kind: str) -> None:
    """The text the coverage ledger returns is the answer with a note appended; the note is the server's. Anything
    else it changed is taken as the server's too, from the first character that differs (the registry stays exact)."""
    old = doc.text
    if built == old:
        return
    common = 0
    for a, b in zip(old, built, strict=False):
        if a != b:
            break
        common += 1
    doc.splice(common, len(old), built[common:], kind)
