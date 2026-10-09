"use client";

import {
  type CSSProperties,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  chatApi,
  fetchSourceImage,
  fileUrl,
  isAbortError,
  type PageScale,
  pageImageUrl,
  SourceError,
  type SourceFailureState,
} from "@/lib/api";
import type {
  ChatAnchor,
  ChatAnchorPage,
  ChatAnchorRect,
  ChatComputedAnchor,
  SourceBlock,
  SourceBlocks,
  ViewerNav,
  ViewerTarget,
} from "@/lib/chatTypes";
import { VALUE_STATUS, valueStatusText } from "@/lib/format";
import { SourceRevoked, SourceTextView, TableView } from "./SourcePanel";
import "./chat.css";

// The source viewer (U6, KTD5): the original page of the cited version, rendered by the server under the same
// permission checks as the file, with the anchor's snapshot rectangles drawn over it as fractions of the page. The
// extracted text is a separate tab; a DOCX opens a structured view of its section instead (it has no pages). The
// rectangles come from the answer's stored snapshot only, never from the response headers or a search of the page,
// so an old conversation shows its original place even after a reprocess (R11).

const MSG_STALE =
  "המסמך עובד מחדש אחרי שניתנה התשובה. מוצג העמוד המקורי של אותה גרסה, עם הסימון כפי שנשמר בתשובה.";
const MSG_STALE_STRUCTURED = "המסמך עובד מחדש אחרי שניתנה התשובה. מוצג הקטע כפי שצוטט בתשובה.";
const MSG_FAILURE: Record<SourceFailureState | "other", string> = {
  render_failed: "לא ניתן להציג את העמוד מהקובץ המקורי; מוצג הטקסט שחולץ.",
  file_missing: "הקובץ המקורי אינו זמין כעת; מוצג הטקסט שחולץ.",
  stale: "המיקום המצוטט שייך לקריאה קודמת של המסמך; מוצג הטקסט שחולץ.",
  other: "לא ניתן להציג את העמוד כעת; מוצג הטקסט שחולץ.",
};
const DEGRADED_NOTE: Record<string, string> = {
  no_geometry: "לעמוד הזה לא נשמרו מיקומים מדויקים.",
  no_cell_box: "לא נשמר מיקום לתא עצמו, ולכן מסומנת הטבלה.",
  not_located: "המקום המצוטט לא אותר בתוך הקטע.",
  unavailable: "המיקום המדויק אינו זמין.",
  anchor_lost: "מקומו של הנתון לא נמצא שוב אחרי עיבוד מחדש של המסמך.",
};
const LABEL_PAGE = "הסימון ברמת העמוד: לא נשמר מיקום מדויק יותר";
const LABEL_STRUCTURED = "מבנה המסמך (ללא עמודים)";
const TITLE_FALLBACK = "המסמך";
const TABLE_SOURCE: Record<string, string> = {
  vision: "הטבלה נקראה מתמונה (קריאה חזותית)",
  ocr: "הטבלה נקראה מתמונה (OCR)",
  emf: "הטבלה נקראה מתמונה וקטורית",
};
/** Precisions that promise a mark on the page: without a stored rectangle they are shown as page level. */
const MARKED = new Set(["span", "cell", "block", "region"]);

/** A document anchor the viewer can open (a computed result's anchor points at its inputs, not at a place). */
export function documentAnchor(a: ChatAnchor | ChatComputedAnchor | null | undefined): ChatAnchor | null {
  if (!a || typeof a !== "object" || a.v < 1 || a.precision === "computed") return null;
  const d = a as ChatAnchor;
  return d.document_id && d.version_id && Array.isArray(d.pages) && d.location ? d : null;
}

type ViewKind = "pages" | "structured" | "text";

function viewKind(anchor: ChatAnchor | null): ViewKind {
  if (!anchor) return "text";
  if (anchor.precision === "structured" || anchor.structured) return "structured";
  return anchor.pages.length > 0 ? "pages" : "text";
}

function hasRects(anchor: ChatAnchor): boolean {
  return anchor.pages.some((p) => p.rects.length > 0);
}

/** The precision as the header states it: never a cell (or a span) without a rectangle to show it (R6). */
function precisionLabel(anchor: ChatAnchor): string {
  if (anchor.precision === "structured") return anchor.precision_label || LABEL_STRUCTURED;
  if (MARKED.has(anchor.precision) && !hasRects(anchor)) return LABEL_PAGE;
  return anchor.precision_label;
}

/** Whether the anchor's rectangles are drawn: never for page precision. */
function drawsRects(anchor: ChatAnchor): boolean {
  return MARKED.has(anchor.precision);
}

function readingParam(anchor: ChatAnchor): string {
  return anchor.reading_id ?? "none";
}

function rectStyle(r: ChatAnchorRect): CSSProperties {
  const [x0, y0, x1, y1] = r;
  return {
    left: `${x0 * 100}%`,
    top: `${y0 * 100}%`,
    width: `${Math.max(0, x1 - x0) * 100}%`,
    height: `${Math.max(0, y1 - y0) * 100}%`,
  };
}

/** The pages the viewer stacks: the anchor's own, and the page of its column header when that is elsewhere (R10). */
function shownPages(anchor: ChatAnchor): ChatAnchorPage[] {
  const pages = [...anchor.pages].sort((a, b) => a.page - b.page);
  const header = anchor.table?.header;
  if (header && !pages.some((p) => p.page === header.page)) {
    // the header's page is laid out at a cited page's size before its image loads, so centring the cited row does
    // not shift when it arrives (the pages of one document share a size far more often than not)
    const known = pages.find((p) => p.width && p.height);
    pages.push({ page: header.page, printed_label: null, width: known?.width ?? null, height: known?.height ?? null,
                 rects: [], focus: [] });
    pages.sort((a, b) => a.page - b.page);
  }
  return pages;
}

// --- the viewer ----------------------------------------------------------------------------------------------------

interface SourceViewerProps {
  nav: ViewerNav;
  /** Moves to another target of `nav` (previous/next citation), in the same panel level. */
  onNavigate: (index: number) => void;
  /** Closes this level only (the panel stack pops it and returns focus to its opener). */
  onClose: () => void;
  /** A lower level of the panel stack: kept mounted, not shown. */
  hidden?: boolean;
}

/** One level of the panel stack showing a cited place. The wide mode and the zoom stay while moving between
 * citations; everything about a target (its tab, its failure, its revocation) starts fresh with it. */
export function SourceViewer({ nav, onNavigate, onClose, hidden }: SourceViewerProps) {
  const [wide, setWide] = useState(false);
  const [zoom, setZoom] = useState(false);
  // the target whose document stopped being visible (a new target starts unrevoked)
  const [revokedKey, setRevokedKey] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const target = nav.items[nav.index];
  const key = target ? `${nav.index}:${target.id}:${target.anchor?.version_id ?? target.source.version_id ?? "-"}` : "";
  const onRevoked = useCallback(() => setRevokedKey(key), [key]);

  useEffect(() => {
    if (!hidden) closeRef.current?.focus();
    // focus moves into the viewer when it opens, not on every navigation inside it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!target) return null;
  const revoked = revokedKey === key;
  const anchor = documentAnchor(target.anchor);
  return (
    <aside
      className="source-panel source-viewer"
      aria-label="תצוגת מקור"
      data-testid="source-viewer"
      data-wide={wide ? "true" : "false"}
      hidden={hidden}
    >
      <ViewerHeader
        target={target}
        anchor={anchor}
        revoked={revoked}
        closeRef={closeRef}
        onClose={onClose}
        wide={wide}
        onWide={() => setWide((w) => !w)}
      />
      {nav.items.length > 1 && <CitationNav nav={nav} onNavigate={onNavigate} />}
      {revoked ? (
        <SourceRevoked />
      ) : (
        <ViewerContent key={key} target={target} onRevoked={onRevoked} zoom={zoom} onZoom={() => setZoom((z) => !z)} />
      )}
    </aside>
  );
}

/** The body for one target: its notes, its tabs and its view. */
function ViewerContent({
  target,
  onRevoked,
  zoom,
  onZoom,
}: {
  target: ViewerTarget;
  onRevoked: () => void;
  zoom: boolean;
  onZoom: () => void;
}) {
  const anchor = documentAnchor(target.anchor);
  const kind = viewKind(anchor);
  const [tab, setTab] = useState<"page" | "text">(kind === "pages" ? "page" : "text");
  const [failure, setFailure] = useState<string | null>(null);
  const [staleSeen, setStaleSeen] = useState(false);
  const [jump, setJump] = useState<{ to: "header" | number; n: number } | null>(null);
  const stale = anchor?.degraded === "reading_changed" || staleSeen;

  const onFailure = useCallback((state: SourceFailureState | null) => {
    setFailure(MSG_FAILURE[state ?? "other"]);
    setTab("text");
  }, []);
  const onStale = useCallback(() => setStaleSeen(true), []);
  const uid = useId();
  const ids = { page: `${uid}-tab-page`, text: `${uid}-tab-text`, panel: `${uid}-tabpanel` };

  return (
    <>
      {anchor && (
        <AnchorNotes
          anchor={anchor}
          stale={stale}
          structured={kind === "structured"}
          onJump={kind === "pages" && tab === "page" ? (to) => setJump((j) => ({ to, n: (j?.n ?? 0) + 1 })) : null}
        />
      )}
      {kind === "pages" && (
        <div className="viewer-tabs" role="tablist" aria-label="אופן התצוגה">
          <button
            type="button"
            role="tab"
            id={ids.page}
            aria-selected={tab === "page"}
            aria-controls={ids.panel}
            data-testid="viewer-tab-page"
            onClick={() => setTab("page")}
          >
            העמוד המקורי
          </button>
          <button
            type="button"
            role="tab"
            id={ids.text}
            aria-selected={tab === "text"}
            aria-controls={ids.panel}
            data-testid="viewer-tab-text"
            onClick={() => setTab("text")}
          >
            הטקסט שחולץ
          </button>
        </div>
      )}
      {kind === "pages" && anchor && tab === "page" ? (
        <PageView
          anchor={anchor}
          scale={zoom ? "zoom" : "normal"}
          zoom={zoom}
          onZoom={onZoom}
          jump={jump}
          ids={ids}
          onRevoked={onRevoked}
          onFailure={onFailure}
          onStale={onStale}
        />
      ) : kind === "structured" && anchor ? (
        <StructuredView anchor={anchor} onRevoked={onRevoked} />
      ) : (
        <div
          className="viewer-tabpanel"
          id={ids.panel}
          role={kind === "pages" ? "tabpanel" : undefined}
          aria-labelledby={kind === "pages" ? ids.text : undefined}
        >
          <SourceTextView source={target.source} onRevoked={onRevoked} note={failure} />
        </div>
      )}
    </>
  );
}

function ViewerHeader({
  target,
  anchor,
  revoked,
  closeRef,
  onClose,
  wide,
  onWide,
}: {
  target: ViewerTarget;
  anchor: ChatAnchor | null;
  revoked: boolean;
  closeRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  wide: boolean;
  onWide: () => void;
}) {
  const loc = anchor?.location;
  // a readable title, else the readable location leads; never ids or garbled file names (R8)
  const title = anchor ? loc?.title ?? loc?.label ?? TITLE_FALLBACK : target.source.title;
  const subtitle = anchor ? (loc?.title ? loc.label : null) : target.source.location;
  const status = target.valueStatus ?? null;
  const structured = anchor ? viewKind(anchor) === "structured" : false;
  return (
    <header>
      <div className="t">
        <strong data-testid="viewer-title">{title}</strong>
        {!revoked && subtitle && <span data-testid="viewer-location">{subtitle}</span>}
        {!revoked && anchor && (
          <ul className="viewer-meta" aria-label="פרטי המיקום">
            {!structured && <PageLine anchor={anchor} />}
            {loc?.section && (
              <li data-testid="viewer-section">
                סעיף: <bdi>{loc.section}</bdi>
              </li>
            )}
            <li data-testid="viewer-precision">{precisionLabel(anchor)}</li>
            {status && (
              <li data-testid="viewer-value-status" title={VALUE_STATUS[status].description}>
                {valueStatusText(status)}
              </li>
            )}
          </ul>
        )}
        {!revoked && !anchor && status && (
          <ul className="viewer-meta">
            <li data-testid="viewer-value-status" title={VALUE_STATUS[status].description}>
              {valueStatusText(status)}
            </li>
          </ul>
        )}
      </div>
      <div className="viewer-header-actions">
        <button
          type="button"
          className="icon-btn viewer-wide-btn"
          aria-pressed={wide}
          aria-label={wide ? "צמצום תצוגת המקור" : "הרחבת תצוגת המקור"}
          title={wide ? "צמצום" : "הרחבה"}
          data-testid="viewer-wide"
          onClick={onWide}
        >
          {wide ? "⇥" : "⇤"}
        </button>
        <button
          ref={closeRef}
          type="button"
          className="icon-btn"
          onClick={onClose}
          aria-label="סגירת תצוגת המקור"
          data-testid="viewer-close"
        >
          ✕
        </button>
      </div>
    </header>
  );
}

/** The file page (or range) and, when detected and different, the page number printed on the page (R8). */
function PageLine({ anchor }: { anchor: ChatAnchor }) {
  const loc = anchor.location;
  const first = loc.page ?? anchor.pages[0]?.page ?? null;
  if (first == null) return null;
  const last = loc.page_end ?? (anchor.pages.length > 1 ? anchor.pages[anchor.pages.length - 1].page : null);
  const printed = loc.printed_page && loc.printed_page !== String(first) ? loc.printed_page : null;
  return (
    <li data-testid="viewer-page">
      {last != null && last !== first ? (
        <>
          עמודים <bdi>{`${first}–${last}`}</bdi> בקובץ
        </>
      ) : (
        <>
          עמוד <bdi>{first}</bdi> בקובץ
        </>
      )}
      {printed && (
        <span data-testid="viewer-printed-page">
          {" "}
          · מספר העמוד המודפס: <bdi>{printed}</bdi>
        </span>
      )}
    </li>
  );
}

/** Previous/next through the answer's citations in answer order (or a breakdown's inputs). */
function CitationNav({ nav, onNavigate }: { nav: ViewerNav; onNavigate: (index: number) => void }) {
  const last = nav.items.length - 1;
  return (
    <div className="viewer-toolbar viewer-citations" role="group" aria-label="מעבר בין המקורות" data-testid="viewer-nav">
      <button
        type="button"
        className="btn btn-small"
        disabled={nav.index <= 0}
        aria-label="המקור הקודם"
        data-testid="viewer-prev"
        onClick={() => onNavigate(nav.index - 1)}
      >
        → הקודם
      </button>
      <span data-testid="viewer-position">
        מקור <bdi>{nav.index + 1}</bdi> מתוך <bdi>{nav.items.length}</bdi>
      </span>
      <button
        type="button"
        className="btn btn-small"
        disabled={nav.index >= last}
        aria-label="המקור הבא"
        data-testid="viewer-next"
        onClick={() => onNavigate(nav.index + 1)}
      >
        הבא ←
      </button>
    </div>
  );
}

/** What qualifies the mark: the stale note, why the precision is lower, and a table's context (R6, R10). */
function AnchorNotes({
  anchor,
  stale,
  structured,
  onJump,
}: {
  anchor: ChatAnchor;
  stale: boolean;
  structured: boolean;
  /** Scrolls the page view to the column header or to a page; null when the page view is not shown. */
  onJump: ((to: "header" | number) => void) | null;
}) {
  const degraded = anchor.degraded && anchor.degraded !== "reading_changed" ? DEGRADED_NOTE[anchor.degraded] : null;
  const t = anchor.table;
  const pages = new Set(shownPages(anchor).map((p) => p.page));
  const cell = anchor.structured?.cell ?? null;
  const rowLabel = t?.row_label ?? cell?.row_label ?? null;
  const column = t?.column_header ?? cell?.column_header ?? null;
  return (
    <>
      {stale && (
        <p className="viewer-note" role="note" data-testid="viewer-stale">
          {structured ? MSG_STALE_STRUCTURED : MSG_STALE}
        </p>
      )}
      {degraded && (
        <p className="viewer-note muted" data-testid="viewer-degraded">
          {degraded}
        </p>
      )}
      {(t || cell) && (
        <div className="viewer-table" data-testid="viewer-table">
          <dl>
            {t?.title && (
              <div>
                <dt>טבלה</dt>
                <dd data-testid="viewer-table-title">
                  <bdi>{t.title}</bdi>
                </dd>
              </div>
            )}
            {rowLabel && (
              <div>
                <dt>שורה</dt>
                <dd data-testid="viewer-table-row">
                  <bdi>{rowLabel}</bdi>
                </dd>
              </div>
            )}
            {column && (
              <div>
                <dt>עמודה</dt>
                <dd data-testid="viewer-table-column">
                  <bdi>{column}</bdi>
                </dd>
              </div>
            )}
            {t?.unit_note && (
              <div>
                <dt>יחידות</dt>
                <dd data-testid="viewer-table-unit">
                  <bdi>{t.unit_note}</bdi>
                </dd>
              </div>
            )}
          </dl>
          {t?.source && TABLE_SOURCE[t.source] && <p className="muted small">{TABLE_SOURCE[t.source]}</p>}
          {onJump && t?.header && t.header.rects.length > 0 && (
            <button type="button" className="btn-link" data-testid="viewer-header-link" onClick={() => onJump("header")}>
              הצגת כותרת העמודה בעמוד
            </button>
          )}
          {t && t.notes.length > 0 && (
            <ul className="viewer-table-notes" data-testid="viewer-table-notes">
              {t.notes.map((n, i) => (
                <li key={i}>
                  הערה: <bdi>{n.text}</bdi>
                  {n.page != null && (
                    <>
                      {" "}
                      {onJump && pages.has(n.page) ? (
                        <button type="button" className="btn-link" onClick={() => onJump(n.page as number)}>
                          (עמוד <bdi>{n.page}</bdi>)
                        </button>
                      ) : (
                        <span className="muted">
                          (עמוד <bdi>{n.page}</bdi>)
                        </span>
                      )}
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

// --- page view -----------------------------------------------------------------------------------------------------

type PageLoad = { state: "loading" } | { state: "loaded"; url: string } | { state: "failed" };

/** The anchor's pages stacked in one scroll container, each with the snapshot's rectangles over it, the first
 * rectangle centred. Every image is fetched once: lost access shows the unavailable state (no retry), a typed
 * failure switches to the extracted text, and a stale reading keeps the page with the snapshot highlight. */
function PageView({
  anchor,
  scale,
  zoom,
  onZoom,
  jump,
  ids,
  onRevoked,
  onFailure,
  onStale,
}: {
  anchor: ChatAnchor;
  scale: PageScale;
  zoom: boolean;
  onZoom: () => void;
  jump: { to: "header" | number; n: number } | null;
  ids: { page: string; panel: string };
  onRevoked: () => void;
  onFailure: (state: SourceFailureState | null) => void;
  onStale: () => void;
}) {
  const pages = useMemo(() => shownPages(anchor), [anchor]);
  // by scale and page: a page not in it is still loading at that scale
  const [loads, setLoads] = useState<Record<string, PageLoad>>({});
  const loadOf = (page: number): PageLoad => loads[`${scale}:${page}`] ?? { state: "loading" };
  const [current, setCurrent] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const centred = useRef<string | null>(null);
  const draw = drawsRects(anchor);
  const firstRectPage = draw ? pages.find((p) => p.rects.length > 0)?.page ?? null : null;
  const file = fileUrl(anchor.document_id, anchor.version_id);

  useEffect(() => {
    const ctrl = new AbortController();
    const urls: string[] = [];
    let done = false;
    for (const p of pages) {
      const url = pageImageUrl(anchor.document_id, anchor.version_id, p.page, scale, readingParam(anchor));
      fetchSourceImage(url, ctrl.signal)
        .then((img) => {
          if (ctrl.signal.aborted) {
            URL.revokeObjectURL(img.url);
            return;
          }
          urls.push(img.url);
          if (img.readingState === "stale") onStale();
          setLoads((cur) => ({ ...cur, [`${scale}:${p.page}`]: { state: "loaded", url: img.url } }));
        })
        .catch((err: unknown) => {
          if (isAbortError(err) || ctrl.signal.aborted || done) return;
          if (err instanceof SourceError && err.revoked) {
            // the document is no longer visible: nothing more is requested from it
            done = true;
            ctrl.abort();
            onRevoked();
            return;
          }
          if (err instanceof ApiError && err.status === 401) return;
          setLoads((cur) => ({ ...cur, [`${scale}:${p.page}`]: { state: "failed" } }));
          if (err instanceof SourceError && err.state === "stale") {
            onStale();
            return;
          }
          // a page that cannot be drawn: the extracted text is shown instead
          done = true;
          ctrl.abort();
          onFailure(err instanceof SourceError ? err.state : null);
        });
    }
    return () => {
      ctrl.abort();
      for (const u of urls) URL.revokeObjectURL(u);
    };
  }, [anchor, pages, scale, onRevoked, onFailure, onStale]);

  // centre the first rectangle once its page is laid out (again after a zoom change)
  const firstPage = firstRectPage ?? pages[0]?.page ?? null;
  const firstLoaded = firstPage != null && loadOf(firstPage).state === "loaded";
  useLayoutEffect(() => {
    if (!firstLoaded) return;
    const k = `${scale}`;
    if (centred.current === k) return;
    centred.current = k;
    const el =
      scrollRef.current?.querySelector<HTMLElement>('[data-testid="viewer-highlight"]') ??
      scrollRef.current?.querySelector<HTMLElement>('[data-testid="viewer-page-frame"]');
    el?.scrollIntoView({ block: firstRectPage != null ? "center" : "start", inline: "center" });
  }, [firstLoaded, firstRectPage, scale]);

  useEffect(() => {
    if (!jump) return;
    const root = scrollRef.current;
    if (!root) return;
    const el =
      jump.to === "header"
        ? root.querySelector<HTMLElement>('[data-testid="viewer-header-highlight"]')
        : root.querySelector<HTMLElement>(`[data-testid="viewer-page-frame"][data-page="${jump.to}"]`);
    el?.scrollIntoView({ block: "center", inline: "center" });
  }, [jump]);

  const goPage = (i: number) => {
    const next = Math.max(0, Math.min(pages.length - 1, i));
    setCurrent(next);
    scrollRef.current
      ?.querySelector<HTMLElement>(`[data-testid="viewer-page-frame"][data-page="${pages[next].page}"]`)
      ?.scrollIntoView({ block: "start" });
  };

  const header = anchor.table?.header ?? null;
  return (
    <div
      className="viewer-tabpanel viewer-page-view"
      id={ids.panel}
      role="tabpanel"
      aria-labelledby={ids.page}
    >
      <div className="viewer-toolbar" role="toolbar" aria-label="כלי תצוגת העמוד">
        {pages.length > 1 && (
          <>
            <button
              type="button"
              className="btn btn-small"
              aria-label="העמוד הקודם"
              data-testid="viewer-page-prev"
              disabled={current <= 0}
              onClick={() => goPage(current - 1)}
            >
              ↑ עמוד קודם
            </button>
            <button
              type="button"
              className="btn btn-small"
              aria-label="העמוד הבא"
              data-testid="viewer-page-next"
              disabled={current >= pages.length - 1}
              onClick={() => goPage(current + 1)}
            >
              ↓ עמוד הבא
            </button>
          </>
        )}
        <button
          type="button"
          className="btn btn-small"
          aria-pressed={zoom}
          aria-label={zoom ? "הקטנת העמוד" : "הגדלת העמוד"}
          data-testid="viewer-zoom"
          onClick={onZoom}
        >
          {zoom ? "− הקטנה" : "+ הגדלה"}
        </button>
        <a className="viewer-file" href={file} target="_blank" rel="noreferrer">
          הורדת הקובץ המקורי
        </a>
      </div>
      <div className="viewer-pages" ref={scrollRef} data-testid="viewer-pages" data-scale={scale}>
        {pages.map((p, i) => {
          const load = loadOf(p.page);
          const ratio = p.width && p.height ? `${p.width} / ${p.height}` : undefined;
          const rects = draw ? p.rects : [];
          const focus = draw ? p.focus : [];
          const headerRects = header && header.page === p.page ? header.rects : [];
          const printed = p.printed_label && p.printed_label !== String(p.page) ? p.printed_label : null;
          // the cited place: its first rectangle, or the page itself when the mark is at page level
          const pageCited = !draw || anchor.pages.every((x) => x.rects.length === 0) ? i === 0 : false;
          return (
            <section key={p.page} className="viewer-page" aria-label={`עמוד ${p.page}`}>
              <div className="viewer-page-label" dir="rtl">
                עמוד <bdi>{p.page}</bdi>
                {printed && (
                  <>
                    {" "}
                    (מודפס: <bdi>{printed}</bdi>)
                  </>
                )}
              </div>
              <div
                className="page-frame"
                data-testid="viewer-page-frame"
                data-page={p.page}
                data-zoom={zoom ? "true" : "false"}
                data-cited={pageCited ? "true" : undefined}
                style={ratio ? { aspectRatio: ratio } : undefined}
              >
                {load.state === "loaded" && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={load.url}
                    alt={`עמוד ${p.page} של המסמך`}
                    data-testid="viewer-page-image"
                    data-page={p.page}
                    data-src={pageImageUrl(anchor.document_id, anchor.version_id, p.page, scale, readingParam(anchor))}
                    draggable={false}
                    style={ratio ? { height: "100%" } : undefined}
                  />
                )}
                {load.state === "loading" && (
                  <p className="viewer-page-status muted" role="status">
                    טוען את העמוד…
                  </p>
                )}
                {load.state === "failed" && <p className="viewer-page-status muted">התמונה אינה זמינה</p>}
                {headerRects.map((r, j) => (
                  <div
                    key={`h${j}`}
                    className="hl hl-header"
                    data-testid="viewer-header-highlight"
                    data-rect={r.join(",")}
                    style={rectStyle(r)}
                    aria-hidden="true"
                  />
                ))}
                {rects.map((r, j) => (
                  <div
                    key={`r${j}`}
                    className={`hl hl-${anchor.precision}`}
                    data-testid="viewer-highlight"
                    data-rect={r.join(",")}
                    data-cited={j === 0 && p.page === firstRectPage ? "true" : undefined}
                    style={rectStyle(r)}
                    aria-hidden="true"
                  />
                ))}
                {focus.map((r, j) => (
                  <div
                    key={`f${j}`}
                    className="hl hl-focus"
                    data-testid="viewer-focus"
                    data-rect={r.join(",")}
                    style={rectStyle(r)}
                    aria-hidden="true"
                  />
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}

// --- structured view (DOCX) ----------------------------------------------------------------------------------------

const CONTEXT = 3;

/** The nearest readable block before or after the cited range: one paragraph of context each side. */
function contextBlock(blocks: SourceBlock[], index: number, step: -1 | 1): SourceBlock | null {
  const sorted = [...blocks].sort((a, b) => (a.index - b.index) * step);
  return sorted.find((b) => (b.index - index) * step > 0 && b.kind !== "heading" && (b.text || "").trim()) ?? null;
}

function marked(text: string, highlight: [number, number] | null): ReactNode {
  if (!highlight) return text;
  const [a, b] = highlight;
  if (a < 0 || b <= a || b > text.length) return text;
  return (
    <>
      {text.slice(0, a)}
      <mark data-testid="structured-highlight">{text.slice(a, b)}</mark>
      {text.slice(b)}
    </>
  );
}

const norm = (s: string | null | undefined) => (s ?? "").replace(/\s+/g, " ").trim();

/** A DOCX citation (R9): the section heading path, the cited paragraph (its quoted words marked) or the table with
 * the cited cell marked, and one paragraph of context each side. The cited text comes from the snapshot, so a
 * reprocessed document still shows what was cited; the context comes from the same reading only. No page numbers. */
function StructuredView({ anchor, onRevoked }: { anchor: ChatAnchor; onRevoked: () => void }) {
  const s = anchor.structured;
  const [data, setData] = useState<SourceBlocks | null>(null);
  const start = anchor.block_start;
  const end = anchor.block_end ?? start;

  useEffect(() => {
    if (start == null || end == null) return undefined;
    const ctrl = new AbortController();
    chatApi
      .blocks(
        anchor.document_id,
        anchor.version_id,
        Math.max(0, start - CONTEXT),
        end + CONTEXT,
        ctrl.signal,
        readingParam(anchor),
      )
      .then(setData)
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        if (err instanceof ApiError && err.status === 404) onRevoked();
        // otherwise the snapshot alone is shown
      });
    return () => ctrl.abort();
  }, [anchor, start, end, onRevoked]);

  const blocks = data && !data.stale ? data.blocks : [];
  const before = start != null ? contextBlock(blocks, start, -1) : null;
  const after = end != null ? contextBlock(blocks, end, 1) : null;
  const cell = s?.cell ?? null;
  const tableBlock = cell
    ? blocks.find((b) => b.table && b.index >= (start ?? 0) && b.index <= (end ?? start ?? 0)) ?? null
    : null;
  // the stored cell is marked only where the table's text there is the cited cell's text
  const mark =
    cell && tableBlock?.table && norm(tableBlock.table.rows[cell.row_number - 1]?.[cell.column_number - 1]) === norm(cell.text)
      ? { row: cell.row_number - 1, column: cell.column_number - 1 }
      : null;
  const paragraph =
    s?.paragraph_no != null
      ? s.paragraph_end != null && s.paragraph_end !== s.paragraph_no
        ? `פסקאות ${s.paragraph_no}–${s.paragraph_end}`
        : `פסקה ${s.paragraph_no}`
      : null;
  const file = fileUrl(anchor.document_id, anchor.version_id);

  return (
    <div className="source-body structured-view" data-testid="structured-view">
      {s && s.section_path.length > 0 && (
        <nav className="structured-path" aria-label="נתיב הסעיף" data-testid="structured-path">
          {s.section_path.map((x, i) => (
            <span key={i}>
              {i > 0 && " › "}
              <bdi>{x}</bdi>
            </span>
          ))}
        </nav>
      )}
      {before && (
        <div className="blk structured-context" dir="auto">
          {before.text}
        </div>
      )}
      {cell && tableBlock?.table && mark ? (
        <div className="blk table" data-testid="structured-table">
          <TableView table={tableBlock.table} mark={mark} />
        </div>
      ) : cell ? (
        <div className="blk" data-cited="true" data-testid="structured-cell-fallback">
          <span className="meta">
            {cell.row_label && (
              <>
                שורה «<bdi>{cell.row_label}</bdi>»{" "}
              </>
            )}
            {cell.column_header && (
              <>
                עמודה «<bdi>{cell.column_header}</bdi>»
              </>
            )}
          </span>
          <mark data-testid="structured-cell">
            <bdi>{cell.text}</bdi>
          </mark>
        </div>
      ) : (
        <div className="blk" data-cited="true" data-testid="structured-paragraph">
          {(paragraph || s?.label) && <span className="meta">{[s?.label, paragraph].filter(Boolean).join(" · ")}</span>}
          <span dir="auto">{marked(s?.text ?? "", s?.highlight ?? null)}</span>
        </div>
      )}
      {after && (
        <div className="blk structured-context" dir="auto">
          {after.text}
        </div>
      )}
      <p>
        <a href={file} target="_blank" rel="noreferrer">
          הורדת הקובץ המקורי
        </a>
      </p>
    </div>
  );
}
