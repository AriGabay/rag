"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type {
  ChatAnswer,
  ChatCheckedSource,
  ChatComponent,
  ChatDiagnostics,
  ChatPartialRow,
  ChatReread,
  ChatRemoval,
  ChatRemovalDecision,
  ChatSource,
  ChatValue,
  ChatVerificationActions,
  ViewerNav,
  ViewerTarget,
} from "@/lib/chatTypes";
import { ApiError, chatApi, isAbortError } from "@/lib/api";
import {
  CHANGE_ACTION_LABEL,
  CUT_MARKER,
  WITHHELD_CAUSE_TEXT,
  WITHHELD_DEFAULT_TEXT,
  type ChangeAction,
  FAILURE_KIND_LABEL,
  failureKindText,
  rowChangeAction,
  plainClaimText,
} from "@/lib/format";
import { type CitedRecords, assumptionText, citedTarget } from "./cited";

/** The marker the user clicked in the answer's body: which cut, and a counter so that clicking it again opens it again. */
export interface CutFocus {
  ordinal: number;
  tick: number;
}

/** The removal note's button in the answer's body (round 13 U12, KTD14): every press is an idempotent "open all" (`open`
 * is always true); `tick` makes a repeated press count. The first removal takes the focus when they open. */
export interface ExpandAll {
  tick: number;
  open: boolean;
}

/** The partial marks of a viewer who has none to open: one stable array, so a prop never changes identity for nothing. */
export const NO_PARTIALS: ChatPartialRow[] = [];

/** A "verified in part" mark the owner pressed (round 14 U8, KTD9): which mark, the claims it covers (each is one entry of the
 * diagnostics route) and a counter so that pressing the same mark again moves the focus to its entries again. */
export interface PartialFocus {
  ordinal: number;
  claimIds: string[];
  tick: number;
}

/** One line per action that touched a claim (round 8 U8): what changed in the text first, then the qualifiers and
 * the citations it added. */
export function ActionLines({ actions }: { actions: ChatVerificationActions }) {
  const n = (k: keyof ChatVerificationActions) => actions[k];
  return (
    <>
      {n("removed") > 0 && (
        <li>{n("removed") === 1 ? "האימות הסיר טענה אחת מהתשובה." : `האימות הסיר ${n("removed")} טענות מהתשובה.`}</li>
      )}
      {n("cut") > 0 && (
        <li>
          {n("cut") === 1
            ? "הושמט חלק שלא אומת ממשפט אחד (מסומן בתשובה)."
            : `הושמטו חלקים שלא אומתו מ-${n("cut")} משפטים (מסומנים בתשובה).`}
        </li>
      )}
      {n("rewritten") > 0 && (
        <li>{n("rewritten") === 1 ? "טענה אחת נוסחה מחדש ונבדקה שוב." : `${n("rewritten")} טענות נוסחו מחדש ונבדקו שוב.`}</li>
      )}
      {n("withheld") > 0 && (
        <li>
          {n("withheld") === 1
            ? "טענה אחת לא הוצגה כי בדיקתה לא הושלמה."
            : `${n("withheld")} טענות לא הוצגו כי בדיקתן לא הושלמה.`}
        </li>
      )}
      {n("qualified") > 0 && (
        <li>
          {n("qualified") === 1
            ? "טענה אחת נשארה בתשובה עם סייג מהמקור."
            : `${n("qualified")} טענות נשארו בתשובה עם סייג מהמקור.`}
        </li>
      )}
      {n("cited") > 0 && (
        <li>
          {n("cited") === 1
            ? "לטענה אחת צורף ציטוט מהמקור שנבדק."
            : `ל-${n("cited")} טענות צורף ציטוט מהמקור שנבדק.`}
        </li>
      )}
    </>
  );
}

/** A diagnostics request shared by the entries of one message while it is in flight (`RemovalsSection`): `waiting`
 * counts the expanded entries still waiting for it; the last one to stop waiting aborts it. */
type SharedDiagnostics = {
  messageId: string;
  promise: Promise<ChatDiagnostics>;
  ctrl: AbortController;
  waiting: number;
};

type Diagnostics = (signal: AbortSignal) => Promise<ChatDiagnostics>;

/** The row's decision in the diagnostics: by claim id (round 8 U7); only a row of an older message, which has no
 * claim id, is paired by its position and kind (the old log held the removals alone, in the rows' order). */
function pairDecision(d: ChatDiagnostics, row: ChatRemoval, index: number): ChatRemovalDecision | undefined {
  if (!Array.isArray(d.removed)) return undefined;
  if (row.claim_id) return d.removed.find((x) => x.claim_id === row.claim_id);
  const item = d.removed[index];
  return item && !item.claim_id && (item.failure_kind || "absent_from_source") === row.failure_kind ? item : undefined;
}

/** The claims verification took out of or changed in the text: each row's kind, action and the server's fixed
 * sentence; expanded, its draft, reason and checked sources from the diagnostics route (`RemovalItem`), paired by claim
 * id. Entries expanded while a request is in flight share it; it is dropped once it settles, so every later expand
 * asks the route again (nothing is cached). Claims rewritten, qualified or cited have no row on the normal path; their
 * count leads to the same details (`OtherChanges`). */
export function RemovalsSection({
  removals,
  otherChanges,
  components,
  messageId,
  focus,
  expandAll,
  listId,
  onOpenSource,
  partialFocus,
  partials,
}: {
  removals: ChatRemoval[];
  otherChanges: number;
  components: ChatComponent[];
  messageId: string;
  focus: CutFocus | null;
  /** A partial mark's press (round 14 U8): opens the other changes on the mark's claims. */
  partialFocus?: PartialFocus | null;
  /** The reason sentences of the text's partial marks (`verification.partials`): the fallback when the detail cannot load. */
  partials?: ChatPartialRow[];
  /** The removal note's action: opens every removal using the one shared request. */
  expandAll?: ExpandAll | null;
  /** The id of the removals list, which the note's button names in `aria-controls`. */
  listId?: string;
  onOpenSource?: (nav: ViewerNav) => void;
}) {
  const byId = new Map(components.map((c) => [c.id, c]));
  const rowIds = useMemo(() => new Set(removals.map((r) => r.claim_id).filter((x): x is string => !!x)), [removals]);
  const shared = useRef<SharedDiagnostics | null>(null);
  const diagnostics = useCallback(
    (signal: AbortSignal): Promise<ChatDiagnostics> => {
      let entry = shared.current;
      if (!entry || entry.messageId !== messageId) {
        const ctrl = new AbortController();
        const created: SharedDiagnostics = {
          messageId,
          promise: chatApi.diagnostics(messageId, ctrl.signal),
          ctrl,
          waiting: 0,
        };
        const drop = () => {
          if (shared.current === created) shared.current = null;
        };
        created.promise.then(drop, drop);
        shared.current = created;
        entry = created;
      }
      const mine = entry;
      mine.waiting += 1;
      signal.addEventListener(
        "abort",
        () => {
          mine.waiting -= 1;
          if (mine.waiting === 0) {
            mine.ctrl.abort();
            if (shared.current === mine) shared.current = null;
          }
        },
        { once: true },
      );
      return mine.promise;
    },
    [messageId],
  );
  // an entry is keyed by its claim id (an older row, which has none, by its position)
  const seen = new Map<string, number>();
  const keyed = removals.map((r, i) => {
    const base = r.claim_id ?? `row-${i}`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return { row: r, index: i, key: n ? `${base}#${n}` : base };
  });
  return (
    <>
      {keyed.length > 0 && (
        <ul className="removals" data-testid="removals" id={listId}>
          {keyed.map(({ row, index, key }) => (
            <RemovalItem
              key={key}
              removal={row}
              index={index}
              component={row.component ? byId.get(row.component) : undefined}
              diagnostics={diagnostics}
              focus={focus}
              expandAll={expandAll ?? null}
              onOpenSource={onOpenSource}
            />
          ))}
        </ul>
      )}
      {otherChanges > 0 && (
        <OtherChanges
          count={otherChanges}
          rowIds={rowIds}
          diagnostics={diagnostics}
          onOpenSource={onOpenSource}
          partialFocus={partialFocus ?? null}
          partials={partials ?? NO_PARTIALS}
        />
      )}
    </>
  );
}

type Loaded<T> =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "error" }
  /** The route answered 404 (a document behind the answer is no longer visible), or it has no matching entry. */
  | { phase: "gone" }
  | { phase: "ready"; value: T };

/** An expandable entry whose content comes from the diagnostics route on every expand (`diagnostics`: the section's
 * request, shared while in flight) and is dropped on collapse: nothing is cached, so a draft is never shown once the
 * route no longer serves it. `pick` takes the entry's part of the response (undefined: no matching entry). */
function useExpandable<T>(diagnostics: Diagnostics, pick: (d: ChatDiagnostics) => T | undefined) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<Loaded<T>>({ phase: "idle" });
  const ctrl = useRef<AbortController | null>(null);
  const isOpen = useRef(false);
  useEffect(() => () => ctrl.current?.abort(), []);

  const load = useCallback(() => {
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setState({ phase: "loading" });
    diagnostics(c.signal)
      .then((d) => {
        if (c.signal.aborted) return;
        const value = pick(d);
        setState(value !== undefined ? { phase: "ready", value } : { phase: "gone" });
      })
      .catch((err: unknown) => {
        if (c.signal.aborted || isAbortError(err)) return;
        setState(err instanceof ApiError && err.status === 404 ? { phase: "gone" } : { phase: "error" });
      });
  }, [diagnostics, pick]);

  const expand = useCallback(() => {
    if (isOpen.current) return;
    isOpen.current = true;
    setOpen(true);
    load();
  }, [load]);

  const collapse = useCallback(() => {
    if (!isOpen.current) return;
    ctrl.current?.abort();
    isOpen.current = false;
    setOpen(false);
    setState({ phase: "idle" });
  }, []);

  const toggle = useCallback(() => {
    if (isOpen.current) collapse();
    else expand();
  }, [expand, collapse]);

  return { open, state, load, expand, collapse, toggle };
}

/** What an expanded entry shows while its content loads, fails or is gone. */
function ExpandedState<T>({
  state,
  load,
  children,
  fallback,
}: {
  state: Loaded<T>;
  load: () => void;
  children: (value: T) => React.ReactNode;
  /** Shown when the detail could not be loaded (a failed request or a 404): a partial mark's reason sentence. */
  fallback?: React.ReactNode;
}) {
  return (
    <>
      {state.phase === "loading" && (
        <span className="removal-loading" role="status" data-testid="removal-loading">
          <span className="dot-pulse" aria-hidden="true" /> טוען את פירוט הטענה…
        </span>
      )}
      {state.phase === "error" && (
        <p className="removal-error" role="alert" data-testid="removal-error">
          טעינת הפירוט נכשלה.{" "}
          <button type="button" className="source-link" onClick={load}>
            ניסיון חוזר
          </button>
        </p>
      )}
      {state.phase === "gone" && (
        <p className="muted" data-testid="removal-unavailable">
          אין פירוט נוסף להצגה.
        </p>
      )}
      {(state.phase === "error" || state.phase === "gone") && fallback}
      {state.phase === "ready" && children(state.value)}
    </>
  );
}

/** The action's label, the same wherever an entry is shown. */
function ChangeBadge({ action, ordinal }: { action: ChangeAction; ordinal?: number }) {
  return (
    <span className="change-action" data-testid="change-action" data-action={action}>
      {CHANGE_ACTION_LABEL[action]}
      {action === "cut" && ordinal ? ` · סימון ${ordinal}` : ""}
    </span>
  );
}

/** The sentence a row shows: the server's fixed sentence for a claim taken out or withheld; for a cut, that part of the
 * sentence was left out (the server's sentence says "removed a claim"). */
function rowSentence(row: ChatRemoval, action: ChangeAction): string {
  return action === "cut" ? `הושמט חלק מהמשפט שלא אומת: ${failureKindText(row.failure_kind)}.` : row.text;
}

/** One row. Its draft text and reason are loaded from the diagnostics route on every expand and dropped on collapse.
 * While loading the kind stays visible; a failure offers a retry; a 404 leaves the kind only. A cut's marker in the
 * answer's body opens its own entry (`focus`, matched by the marker's ordinal). */
function RemovalItem({
  removal,
  index,
  component,
  diagnostics,
  focus,
  expandAll,
  onOpenSource,
}: {
  removal: ChatRemoval;
  index: number;
  component: ChatComponent | undefined;
  diagnostics: Diagnostics;
  focus: CutFocus | null;
  expandAll: ExpandAll | null;
  onOpenSource?: (nav: ViewerNav) => void;
}) {
  const action = rowChangeAction(removal);
  const pick = useCallback((d: ChatDiagnostics) => pairDecision(d, removal, index), [removal, index]);
  const { open, state, load, expand, toggle } = useExpandable(diagnostics, pick);
  const panelId = useId();
  const li = useRef<HTMLLIElement>(null);
  const handled = useRef(0);
  const focused = !!focus && action === "cut" && removal.ordinal === focus.ordinal;
  const tick = focused ? focus.tick : 0;
  useEffect(() => {
    if (!tick || tick === handled.current) return;
    handled.current = tick;
    expand();
    // the details were opened by the parent in this same commit; scroll once they are laid out
    const raf = window.requestAnimationFrame(() => li.current?.scrollIntoView({ block: "nearest" }));
    return () => window.cancelAnimationFrame(raf);
  }, [tick, expand]);

  // the removal note's button: every entry opens in the same commit, so they join one diagnostics request; the first
  // one scrolls into view and takes the focus once the details around it are laid out (round 13 U12, KTD14)
  const allTick = expandAll?.tick ?? 0;
  const allOpen = expandAll?.open ?? false;
  const handledAll = useRef(0);
  useEffect(() => {
    if (!allTick || allTick === handledAll.current) return;
    handledAll.current = allTick;
    if (!allOpen) return;
    expand();
    if (index === 0) {
      window.requestAnimationFrame(() => {
        li.current?.scrollIntoView({ block: "nearest" });
        li.current?.focus();
      });
    }
  }, [allTick, allOpen, index, expand]);

  return (
    <li
      ref={li}
      tabIndex={-1}
      className="removal"
      data-testid="removal"
      data-kind={removal.failure_kind}
      data-action={action}
      data-claim-id={removal.claim_id}
      data-ordinal={action === "cut" ? removal.ordinal : undefined}
      data-focused={focused ? "true" : undefined}
    >
      <div>
        <strong data-testid="removal-kind">{failureKindText(removal.failure_kind)}</strong>{" "}
        <ChangeBadge action={action} ordinal={removal.ordinal} />
        {component && (
          <span className="muted">
            {" "}
            · רכיב: <bdi>{component.text}</bdi>
          </span>
        )}
      </div>
      <div data-testid="removal-sentence">{rowSentence(removal, action)}</div>
      <button type="button" className="source-link" aria-expanded={open} aria-controls={panelId} onClick={toggle}>
        {action === "cut"
          ? open ? "הסתרת פרטי השינוי" : "פרטי השינוי"
          : open ? "הסתרת פירוט הטענה" : "פירוט הטענה שהוסרה"}
      </button>
      {open && (
        <div id={panelId} className="removal-detail" aria-busy={state.phase === "loading"}>
          <ExpandedState state={state} load={load}>
            {(decision) => <ChangeDetail action={action} decision={decision} row={removal} onOpenSource={onOpenSource} />}
          </ExpandedState>
        </div>
      )}
    </li>
  );
}

const OTHER_ACTIONS: readonly ChangeAction[] = ["rewritten", "qualified", "cited"];

/** The reason sentence of the mark(s) a press opened (`verification.partials`, matched by the mark's ordinal): what a viewer reads
 * when the entries cannot be loaded, or have no entry for the mark's claims. */
function PartialReason({ focus, partials }: { focus: PartialFocus | null; partials: ChatPartialRow[] }) {
  const rows = focus ? partials.filter((p) => p.ordinal === focus.ordinal) : [];
  if (rows.length === 0) return null;
  return (
    <p className="partial-reason" data-testid="partial-reason" role="note">
      {rows.map((p) => p.text).join(" ")}
    </p>
  );
}

/** Claims rewritten, kept with a qualifier or given a citation: no row on the normal path (they changed no claim out
 * of the text), so one toggle loads their entries from the diagnostics route, each under its own claim id. Round 14 U8
 * (KTD9): a "verified in part" mark's press (`focus`) opens the same section on the mark's claims, scrolls to their entries
 * and moves the focus there; when the route answers 404 or fails, the mark's reason sentence is shown in the entries' place.
 * The entries of the mark's claims are listed even when a row of the removals has them (a cut and a qualifier in one sentence). */
function OtherChanges({
  count,
  rowIds,
  diagnostics,
  onOpenSource,
  partialFocus,
  partials,
}: {
  count: number;
  rowIds: Set<string>;
  diagnostics: Diagnostics;
  onOpenSource?: (nav: ViewerNav) => void;
  partialFocus: PartialFocus | null;
  partials: ChatPartialRow[];
}) {
  const pick = useCallback(
    (d: ChatDiagnostics) => {
      if (!Array.isArray(d.removed)) return undefined;
      const partialIds = new Set(partials.flatMap((p) => p.claim_ids ?? (p.claim_id ? [p.claim_id] : [])));
      const found = d.removed.filter(
        (x): x is ChatRemovalDecision & { claim_id: string; action: ChangeAction } =>
          !!x.claim_id &&
          !!x.action &&
          ((OTHER_ACTIONS.includes(x.action) && !rowIds.has(x.claim_id)) ||
            (partialIds.has(x.claim_id) && (x.action === "qualified" || x.action === "cut"))),
      );
      return found.length > 0 ? found : undefined; // none: the same "no further detail" line as a missing entry
    },
    [rowIds, partials],
  );
  const { open, state, load, expand, toggle } = useExpandable(diagnostics, pick);
  const panelId = useId();
  const list = useRef<HTMLUListElement>(null);
  // a mark's press: open the section once (a second press of any mark only moves the focus, or loads again after a failure)
  const tick = partialFocus?.tick ?? 0;
  const handledOpen = useRef(0);
  useEffect(() => {
    if (!tick || tick === handledOpen.current) return;
    handledOpen.current = tick;
    if (open && (state.phase === "error" || state.phase === "gone")) load();
    else expand();
  }, [tick, open, state.phase, expand, load]);
  // once the entries are laid out, the first of the mark's claims scrolls into view and takes the focus
  const handledFocus = useRef(0);
  const claimKey = partialFocus?.claimIds.join(",") ?? "";
  useEffect(() => {
    if (!tick || state.phase !== "ready" || handledFocus.current === tick) return;
    handledFocus.current = tick;
    const raf = window.requestAnimationFrame(() => {
      const ids = claimKey.split(",");
      const el = Array.from(list.current?.querySelectorAll<HTMLElement>("[data-claim-id]") ?? []).find((x) =>
        ids.includes(x.dataset.claimId ?? ""),
      );
      el?.scrollIntoView({ block: "nearest" });
      el?.focus();
    });
    return () => window.cancelAnimationFrame(raf);
  }, [tick, state.phase, claimKey]);
  const focusIds = new Set(partialFocus?.claimIds ?? []);
  const reason = <PartialReason focus={partialFocus} partials={partials} />;
  return (
    <div className="other-changes" data-testid="other-changes">
      <button type="button" className="source-link" aria-expanded={open} aria-controls={panelId} onClick={toggle}>
        {open ? "הסתרת פרטי השינויים" : `פרטי השינויים בטענות שנשארו בתשובה (${count})`}
      </button>
      {open && (
        <div id={panelId} className="removal-detail" aria-busy={state.phase === "loading"}>
          <ExpandedState state={state} load={load} fallback={reason}>
            {(entries) => (
              <>
                <ul className="removals" data-testid="changes" ref={list}>
                  {entries.map((x) => (
                    <li
                      key={x.claim_id}
                      tabIndex={-1}
                      className="removal"
                      data-testid="change"
                      data-action={x.action}
                      data-claim-id={x.claim_id}
                      data-focused={focusIds.has(x.claim_id) ? "true" : undefined}
                    >
                      <ChangeBadge action={x.action} />
                      <ChangeDetail action={x.action} decision={x} onOpenSource={onOpenSource} />
                    </li>
                  ))}
                </ul>
                {partialFocus && !entries.some((x) => focusIds.has(x.claim_id)) && reason}
              </>
            )}
          </ExpandedState>
        </div>
      )}
    </div>
  );
}

/** A checked source's line: what it is and where, from its own record. */
function checkedLabel(s: ChatCheckedSource): string {
  switch (s.type) {
    case "sources":
      return `${s.title} — ${s.location}`;
    case "values":
      return `${s.label}: ${s.value_text} — ${s.title}`;
    case "measurements":
      return `${s.metric}: ${s.value_text} — ${s.title}`;
    case "computations":
      return `חישוב ${s.id}${s.label ? `: ${s.label}` : ""}`;
    case "assumptions":
      return `הנחה שלך: ${s.label} = ${assumptionText(s)}`;
  }
}

/** A text that is nothing but the server's cut marker. */
const MARKER_ONLY = new RegExp(`^${CUT_MARKER.source}$`);

/** The label of the reason an entry gives for its action; a withheld claim has no reason of its own (see `ChangeDetail`). */
const REASON_LABEL: Partial<Record<ChangeAction, string>> = {
  removed: "סיבת ההסרה:",
  cut: "סיבת ההשמטה:",
  rewritten: "סיבת הניסוח מחדש:",
  qualified: "הסייג:",
};

/** How a re-read's scope and outcome read (round 8 U5); an unknown value is shown as the server wrote it. */
const REREAD_SCOPE: Record<string, string> = {
  table_headers: "כותרות הטבלה",
  units_note: "הערת היחידות",
  next_page: "העמוד הבא",
  region: "אזור בעמוד",
  continuation: "המשך הסעיף",
  preceding: "תחילת הסעיף",
};
const REREAD_OUTCOME: Record<string, string> = {
  supported: "נתמך",
  partial: "נתמך בחלקו",
  unsupported: "לא נתמך",
  contradicts: "סותר את הטענה",
  check_failed: "הבדיקה נכשלה",
  no_context: "לא נמצא הקשר נוסף",
  no_time: "לא נשאר זמן לקריאה",
  bound: "הגעה למגבלת הקריאה",
  found: "נמצא",
  not_found: "לא נמצא",
};

function rereadLine(r: ChatReread): string {
  const scope = r.scope ? REREAD_SCOPE[r.scope] ?? r.scope : "";
  const outcome = r.outcome ? REREAD_OUTCOME[r.outcome] ?? r.outcome : "";
  return [`קריאה חוזרת של ${r.source}`, scope && `(${scope})`, outcome && `— ${outcome}`].filter(Boolean).join(" ");
}

/** Whether an entry has the detail a "verified in part" mark opens (round 14 U7): only an entry the server wrote it for. */
function hasPartialDetail(d: ChatRemovalDecision): boolean {
  return typeof d.supported === "string" || typeof d.not_verified === "string" || !!d.why || !!d.checked;
}

/** The detail of a claim a mark covers (round 14 U8, R14, AE10): what is supported, exactly what was not verified, why, and
 * what was checked — the sources and the re-reads. Each part only when the server sent it. */
function PartialDetail({ decision }: { decision: ChatRemovalDecision }) {
  const { supported, not_verified: notVerified, why, checked } = decision;
  const kind = why?.failure_kind ? FAILURE_KIND_LABEL[why.failure_kind] ?? "" : "";
  const rereads = checked?.rereads ?? [];
  const sources = checked?.sources ?? [];
  return (
    <div className="partial-detail" data-testid="partial-detail">
      {supported && (
        <p data-testid="partial-supported">
          <strong>נתמך:</strong> <bdi>{supported}</bdi>
        </p>
      )}
      {notVerified && (
        <p data-testid="partial-not-verified">
          <strong>לא אומת:</strong> <bdi>{notVerified}</bdi>
        </p>
      )}
      {(kind || why?.check) && (
        <p data-testid="partial-why">
          <strong>למה:</strong> {kind}
          {why?.check && (
            <span className="muted">
              {kind ? " · " : ""}בדיקה: <bdi>{why.check}</bdi>
            </span>
          )}
        </p>
      )}
      {(sources.length > 0 || rereads.length > 0) && (
        <div data-testid="partial-checked">
          <strong>מה נבדק:</strong>
          {sources.length > 0 && (
            <span>
              {" "}
              מקורות: <bdi>{sources.join(", ")}</bdi>
            </span>
          )}
          {rereads.length > 0 && (
            <ul>
              {rereads.map((r, i) => (
                <li key={`${r.source}-${i}`}>{rereadLine(r)}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** What an entry shows when expanded, by its action (round 8 U8, R23):
 * - removed: the reason, and the original under "rejected draft";
 * - withheld: that the check could not complete (never a fault of the claim), and the original under "unverified draft";
 * - cut: the original with the part that was not supported marked, and what stays in the answer;
 * - rewritten: the original, and the wording that stays;
 * - qualified: what stays and the qualifier's reason — no draft;
 * - cited: only the source that was attached — no draft.
 * The sources it was checked against open the viewer at their place, only as the route returned them. Draft text
 * appears nowhere else. */
function ChangeDetail({
  action,
  decision,
  row,
  onOpenSource,
}: {
  action: ChangeAction;
  decision: ChatRemovalDecision;
  row?: ChatRemoval;
  onOpenSource?: (nav: ViewerNav) => void;
}) {
  const checked = (decision.sources ?? []).filter((s) => s && typeof s === "object" && typeof s.id === "string");
  const records: CitedRecords = {
    sources: checked.filter((s): s is ChatSource & { type: "sources" } => s.type === "sources"),
    values: checked.filter((s): s is ChatValue & { type: "values" } => s.type === "values"),
    measurements: checked.filter((s) => s.type === "measurements") as ChatAnswer["measurements"],
  };
  const openable = ["sources", "values", "measurements"];
  const items: ViewerTarget[] = [];
  const targetIndex = new Map<ChatCheckedSource, number>();
  for (const s of checked) {
    const t = openable.includes(s.type) ? citedTarget(records, s.id) : null;
    if (t) {
      targetIndex.set(s, items.length);
      items.push(t);
    }
  }
  const cause = decision.cause || row?.cause || "";
  const remaining = (decision.remaining ?? "").trim();
  const reason = decision.reason?.trim();
  const reasonLabel = REASON_LABEL[action];
  const markerOnly = action === "cut" && MARKER_ONLY.test(remaining);
  // an entry a "verified in part" mark opens carries its own detail (round 14 U7): it replaces the generic reason and kept text
  const partial = hasPartialDetail(decision);
  return (
    <div data-testid="removal-detail" data-action={action}>
      {partial && <PartialDetail decision={decision} />}
      {!partial && reasonLabel && reason && (
        <p data-testid="removal-reason">
          <strong>{reasonLabel}</strong> <bdi>{reason}</bdi>
        </p>
      )}
      {action === "withheld" && (
        <p data-testid="removal-reason">
          <strong>הבדיקה לא הושלמה:</strong> {WITHHELD_CAUSE_TEXT[cause] ?? WITHHELD_DEFAULT_TEXT}
        </p>
      )}
      {action === "cited" && <p className="muted">צורף ציטוט מהמקור שנבדק.</p>}
      {action === "removed" && decision.repair_attempted && <p className="muted">לפני ההסרה נוסה תיקון, והוא לא אומת.</p>}
      {(action === "removed" || action === "withheld") && (
        <div className="removal-draft" data-testid="removal-draft" data-label={action === "removed" ? "rejected" : "unverified"}>
          <span className="removal-draft-label">{action === "removed" ? "טיוטה שנפסלה" : "טיוטה שלא אומתה"}</span>
          <blockquote dir="auto">{plainClaimText(decision.text)}</blockquote>
        </div>
      )}
      {(action === "cut" || action === "rewritten") && (
        <div className="removal-draft" data-testid="removal-draft" data-label="original">
          <span className="removal-draft-label">
            {action === "cut" ? "הנוסח המקורי — החלק שלא נתמך מסומן" : "הנוסח המקורי"}
          </span>
          <blockquote dir="auto">
            {action === "cut" ? (
              <mark className="change-unsupported" data-testid="change-unsupported">
                {plainClaimText(decision.text)}
              </mark>
            ) : (
              plainClaimText(decision.text)
            )}
          </blockquote>
        </div>
      )}
      {markerOnly && (
        <p className="change-kept" data-testid="change-kept">
          <strong>מה שנשאר בתשובה:</strong> במקום החלק שהושמט מופיע הסימון <bdi>{remaining}</bdi>; שאר המשפט נשאר כפי
          שנבדק.
        </p>
      )}
      {!partial && action !== "removed" && action !== "withheld" && action !== "cited" && remaining && !markerOnly && (
          <div className="change-kept" data-testid="change-kept">
            <strong>{action === "rewritten" ? "הנוסח שנשאר בתשובה (נבדק מחדש):" : "הטקסט שנשאר בתשובה:"}</strong>
            <blockquote dir="auto">{plainClaimText(remaining)}</blockquote>
          </div>
        )}
      {checked.length > 0 && (
        <div className="removal-sources">
          <span>{action === "cited" ? "המקור שצורף:" : "נבדק מול:"}</span>
          <ul>
            {checked.map((s, i) => {
              const at = targetIndex.get(s);
              return (
                <li key={`${s.type}-${s.id}-${i}`}>
                  {at !== undefined && onOpenSource ? (
                    <button
                      type="button"
                      className="source-link"
                      data-testid="removal-source"
                      onClick={() => onOpenSource({ items, index: at })}
                    >
                      {checkedLabel(s)}
                    </button>
                  ) : (
                    <bdi>{checkedLabel(s)}</bdi>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
