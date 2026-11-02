"use client";

import { useId } from "react";
import { ALL_OPTION_ID, type ChatClarify } from "@/lib/chatTypes";

/** What the buttons show of a clarification that is not open (KTD5): a short state label. */
const STATE_LABEL: Record<string, string> = {
  answered: "נענתה",
  closed: "לא פעילה",
  stale: "לא פעילה",
};

/** The label of the reserved "all" button; also the text a click on it sends. */
export const ALL_LABEL = "כולן";

/**
 * Round 26 U5 (R5, R9, R11, R13, KTD5): the structured clarification of an assistant message: the question (unless the answer's body already
 * says it) and the short reason once, then the options as a labelled group of buttons with number hints. Enabled only while the
 * server's state is `open` and no turn runs or is being sent; an answered clarification marks the chosen option and dims the rest, a
 * closed or stale one shows all of them disabled with a short label. Free text always goes in the message box (a typed reply or an
 * option number is bound by the server), so nothing here replaces the composer.
 */
export function ClarifyOptions({
  clarify,
  busy,
  showQuestion,
  note,
  onChoose,
}: {
  clarify: ChatClarify;
  /** A turn is running or a send is in flight: the buttons wait. */
  busy: boolean;
  /** False when the answer's body already contains the question, so it is not shown twice. */
  showQuestion: boolean;
  /** An inline message next to the buttons (the 409 "השאלה כבר לא פתוחה"). */
  note?: string | null;
  onChoose: (optionId: string, label: string) => void;
}) {
  const noteId = useId();
  const open = clarify.state === "open";
  const disabled = !open || busy;
  const answered = clarify.state === "answered";
  const chosen = clarify.chosen_option_id ?? null;
  const choices = [
    ...clarify.options.map((o) => ({ id: o.id, label: o.label, detail: o.detail })),
    ...(clarify.allow_all ? [{ id: ALL_OPTION_ID, label: ALL_LABEL, detail: "" }] : []),
  ];
  const stateLabel = STATE_LABEL[clarify.state];
  return (
    <section className="clarify" data-testid="clarify" data-state={clarify.state} dir="rtl">
      {showQuestion && clarify.question && (
        <p className="clarify-question" data-testid="clarify-question">
          {clarify.question}
        </p>
      )}
      {clarify.reason && (
        <p className="clarify-reason" data-testid="clarify-reason">
          {clarify.reason}
        </p>
      )}
      <div
        role="group"
        aria-label={clarify.question || "אפשרויות להבהרה"}
        aria-describedby={note ? noteId : undefined}
        className="clarify-options"
      >
        {choices.map((c, i) => {
          const isChosen = answered && chosen === c.id;
          return (
            <button
              key={c.id}
              type="button"
              className="clarify-option"
              data-testid="clarify-option"
              data-option-id={c.id}
              data-chosen={isChosen ? "true" : "false"}
              data-dimmed={answered && !isChosen ? "true" : "false"}
              aria-pressed={answered ? isChosen : undefined}
              disabled={disabled}
              onClick={() => {
                if (!disabled) onChoose(c.id, c.label);
              }}
            >
              <span className="clarify-num" aria-hidden="true">
                {c.id === ALL_OPTION_ID ? "∗" : i + 1}
              </span>
              <span className="clarify-text">
                <bdi className="clarify-label">{c.label}</bdi>
                {c.detail && <bdi className="clarify-detail">{c.detail}</bdi>}
              </span>
              {isChosen && (
                <span className="clarify-check" aria-hidden="true">
                  ✓
                </span>
              )}
            </button>
          );
        })}
      </div>
      {stateLabel && (
        <p className="clarify-state" data-testid="clarify-state">
          {stateLabel}
        </p>
      )}
      {note && (
        <p id={noteId} className="clarify-note" role="alert" data-testid="clarify-note">
          {note}
        </p>
      )}
    </section>
  );
}
