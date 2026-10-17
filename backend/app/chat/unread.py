"""Documents the user may see that were not read, as the chat tells them (round 9 U6, KTD11; R5, R18, R25).

A first version that failed, is waiting or is being processed is never current, so every lookup over current
versions is blind to it: the chat answered "not found in the documents checked" about a file the user could see on
the documents screen. ``app.platform.search.unread_documents`` lists the visible documents whose latest version was
not read (RLS decides what is visible, so a file of a group the user cannot see is never in it). This module words
what is said about them, in three places and never as an absence of data:

- a document the user's words name by its title: its state and reason, never search hits over it (the resolution of a
  follow-up, ``find_documents``, ``list_documents`` and ``search`` for the model);
- the gap line of a component whose named document was not read (reason ``document_not_read``);
- when the turn located nothing, ONE notice listing the unread files as not checked: at most ``NOTICE_FILES``, those
  whose title shares the most words with the question first, then the most recently uploaded, and a count of the rest.
  The composer writes it as a server span (``compose.SERVER_KINDS``: ``unread_notice``), so the final check takes it
  as the server's own text and a later turn never reads it as a fact the answer stated."""

from __future__ import annotations

from app.platform.search import question_words_in_title

NOTICE_FILES = 5
REASON_CHARS = 200

STATE_WAITING = "ממתין לתחילת הקריאה"
STATE_READING = "הקריאה עדיין מתבצעת"
STATE_FAILED = "העיבוד נכשל"


def state_text(d: dict) -> str:
    """A file's state and reason in a few words: failed with the reason it stored, waiting, or still being read with the
    pages read so far when it reported them."""
    if d["status"] == "failed":
        reason = " ".join(str(d.get("reason") or "").split())
        if len(reason) > REASON_CHARS:
            reason = reason[:REASON_CHARS].rstrip() + "…"
        return f"{STATE_FAILED}: {reason}" if reason else STATE_FAILED
    if d["status"] == "pending":
        return STATE_WAITING
    progress = d.get("progress") or {}
    if progress.get("pages_done") is not None and progress.get("pages_total"):
        return f"{STATE_READING}: נקראו {progress['pages_done']} מתוך {progress['pages_total']} עמודים"
    return STATE_READING


def title_of(d: dict) -> str:
    return " ".join(str(d["title"]).replace("«", " ").replace("»", " ").replace("*", " ").split())


def line(d: dict) -> str:
    return f"«{title_of(d)}» — {state_text(d)}"


def ordered(files: list[dict], question: str) -> list[dict]:
    """The files by the words their title shares with the question, then the most recent upload first."""
    return sorted(files, key=lambda d: (-len(question_words_in_title(question, d["title"])),
                                        -(d["uploaded_at"].timestamp() if d.get("uploaded_at") else 0),
                                        d["title"], str(d["document_id"])))


def notice(files: list[dict], question: str) -> tuple[str, list[dict]]:
    """The one notice of the files that were not read, and the files it names: ("", []) when there are none."""
    if not files:
        return "", []
    ranked = ordered(files, question)
    shown, rest = ranked[:NOTICE_FILES], len(ranked) - NOTICE_FILES
    lines = ["קבצים שאתה רשאי לראות לא נקראו, ולכן לא נבדקו:"] + [f"- {line(d)}" for d in shown]
    if rest > 0:
        lines.append("ועוד קובץ אחד שלא נקרא." if rest == 1 else f"ועוד {rest} קבצים שלא נקראו.")
    return "\n".join(lines), shown


def message(files: list[dict], words: str) -> str:
    """What a follow-up that names unread documents is told: they were not read, so nothing was searched in them."""
    if len(files) == 1:
        return (f"המסמך {line(files[0])}. לכן לא בדקתי בו את השאלה, ואין לי מידע על מה שכתוב בו. "
                "אפשר לשאול שוב אחרי שהקריאה תסתיים.")
    listed = "\n".join(f"- {line(d)}" for d in files)
    return (f"המסמכים שמתאימים ל\"{words}\" לא נקראו, ולכן לא בדקתי בהם את השאלה ואין לי מידע על מה שכתוב בהם:\n"
            f"{listed}\nאפשר לשאול שוב אחרי שהקריאה תסתיים.")


def tool_block(files: list[dict]) -> str:
    """For the model, after a listing or a search: the documents the query names that were not read. They have no
    passages to search; an answer says they were not read, never that the data is not in them."""
    if not files:
        return ""
    lines = ["מסמכים שהשאילתה מזכירה בכותרתם ולא נקראו (אין בהם קטעים לחיפוש; אין לומר שהנתון אינו בהם):"]
    lines += [f"- document_id={d['document_id']} | {line(d)}" for d in files]
    return "\n".join(lines)


def gap_detail(files: list[dict]) -> str:
    """The state of the documents a gap line names, for the line."""
    return "; ".join(f"{state_text(d)}" if len(files) == 1 else f"«{title_of(d)}»: {state_text(d)}" for d in files)
