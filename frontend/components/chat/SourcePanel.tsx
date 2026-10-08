"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ApiError, chatApi, errorMessage, isAbortError, safeApiUrl } from "@/lib/api";
import type { ChatSource, SourceBlock, SourceBlocks } from "@/lib/chatTypes";
import "./chat.css";

const WINDOW = 10;
const STATUS_NOTE: Record<string, string> = {
  read_uncertain: "נקרא מתמונה בקריאה לא ודאית",
  unread: "התמונה לא נקראה",
  no_text: "תמונה ללא טקסט",
  decorative: "",
};
const SOURCE_NOTE: Record<string, string> = {
  emf: "טבלה מתוך תמונה וקטורית (נקראה במדויק)",
  vision: "תוכן מתוך תמונה (קריאה חזותית)",
  ocr: "תוכן מתוך תמונה (OCR)",
  word_table: "טבלת Word",
};
const KIND_LABEL: Record<SourceBlock["kind"], string> = {
  image: "תמונה",
  table: "טבלה",
  paragraph: "פסקה",
  heading: "כותרת",
  textbox: "תיבת טקסט",
};
const REGION_STATUS: Record<string, string> = {
  read: "נקרא",
  read_uncertain: "קריאה לא ודאית",
  unread: "לא נקרא",
  no_text: "ללא טקסט",
  decorative: "עיטור",
};
const MSG_IMAGE_UNAVAILABLE = "התמונה אינה זמינה";
const MSG_REVOKED = "המקור אינו זמין עוד (ייתכן שהמסמך נמחק או שההרשאה אליו הוסרה).";
const MSG_STALE =
  "המסמך עובד מחדש אחרי שניתנה התשובה, ולכן המיקום המדויק של הציטוט אינו זמין. מוצג הטקסט כפי שצוטט בתשובה.";
/** The reading a citation is bound to, as the blocks request carries it: an answer's source always checks it ("none"
 * for an answer from before readings had ids); a source from elsewhere (the review screen) opens the current one. */
function citedReading(source: ChatSource): string | undefined {
  if (source.reading_id === undefined) return undefined;
  return source.reading_id ?? "none";
}

/** Where a block can be opened as an image: PDF pictures and tables, blocks not fully read, and the cited ones. */
function hasRegionMarker(block: SourceBlock, cited: boolean): boolean {
  if (!block.region_url || !block.page) return false;
  return cited || block.kind === "image" || block.kind === "table" || block.status === "unread" || block.status === "read_uncertain";
}

/** The cited place in its document: the blocks around it, the cited ones highlighted, tables as tables, and a
 * picture next to what was read from it. A DOCX is located by section and paragraph; nothing invents pages. */
export function SourcePanel({ source, onClose }: { source: ChatSource; onClose: () => void }) {
  const [data, setData] = useState<SourceBlocks | null>(null);
  const [error, setError] = useState<string | null>(null);
  // the document became unavailable while the panel was open: nothing read from it is shown any more
  const [revoked, setRevoked] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
  }, [source.id]);

  const reading = citedReading(source);

  useEffect(() => {
    // the parent keys this panel by source, so a new source starts from empty state
    const ctrl = new AbortController();
    const start = source.block_start;
    const end = source.block_end ?? start;
    // a listing of documents has no place in a document: its text is all there is
    if (start == null || !source.document_id || !source.version_id) return () => ctrl.abort();
    chatApi
      .blocks(
        source.document_id,
        source.version_id,
        Math.max(0, start - WINDOW),
        (end ?? start) + WINDOW,
        ctrl.signal,
        reading,
      )
      .then(setData)
      .catch((err: unknown) => {
        if (!isAbortError(err)) setError(errorMessage(err));
      });
    return () => ctrl.abort();
  }, [source.document_id, source.version_id, source.block_start, source.block_end, reading]);

  useEffect(() => {
    if (!data) return;
    const el = bodyRef.current?.querySelector('[data-cited="true"]');
    if (el && "scrollIntoView" in el) (el as HTMLElement).scrollIntoView({ block: "center" });
  }, [data]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const lo = source.block_start ?? -1;
  const hi = source.block_end ?? lo;
  const file = safeApiUrl(data?.file_url ?? null);
  const onRevoked = () => {
    setRevoked(true);
    setData(null);
  };

  if (revoked) {
    return (
      <aside className="source-panel" aria-label="תצוגת מקור">
        <header>
          <div className="t">
            <strong>{source.title}</strong>
          </div>
          <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="סגירת תצוגת המקור">
            ✕
          </button>
        </header>
        <div className="source-body">
          <div className="alert alert-error" role="alert">
            {MSG_REVOKED}
          </div>
        </div>
      </aside>
    );
  }

  return (
    <aside className="source-panel" aria-label="תצוגת מקור">
      <header>
        <div className="t">
          <strong>{source.title}</strong>
          <span>{source.location}</span>
          {data && !data.is_current && <span> · גרסה קודמת של המסמך</span>}
        </div>
        <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="סגירת תצוגת המקור">
          ✕
        </button>
      </header>
      <div className="source-body" ref={bodyRef}>
        <div className="source-cited" dir="auto">
          {source.text}
        </div>
        {error && <div className="alert alert-error">{error}</div>}
        {source.kind === "listing" ? (
          <p className="muted">רשימת המסמכים שהוחזרה בחיפוש בתור הזה (לא קטע ממסמך).</p>
        ) : (
          source.block_start == null &&
          !error && <p className="muted">למקור הזה אין מיקום מפורט במסמך; מוצג הקטע שצוטט.</p>
        )}
        {source.block_start != null && !data && !error && <p className="muted">טוען את המסמך…</p>}
        {data?.stale && (
          <p className="muted" role="note">
            {MSG_STALE}
          </p>
        )}
        {data?.blocks.map((b) => (
          <Block
            key={b.index}
            block={b}
            cited={b.index >= lo && b.index <= hi}
            documentId={data.document_id}
            versionId={data.version_id}
            onRevoked={onRevoked}
          />
        ))}
        {file && (
          <p>
            <a href={file} target="_blank" rel="noreferrer">
              הורדת הקובץ המקורי
            </a>
          </p>
        )}
      </div>
    </aside>
  );
}

function Block({
  block,
  cited,
  documentId,
  versionId,
  onRevoked,
}: {
  block: SourceBlock;
  cited: boolean;
  documentId: string;
  versionId: string;
  onRevoked: () => void;
}) {
  const [regionOpen, setRegionOpen] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);
  const regionId = useId();
  const meta = [
    block.section_path.length ? block.section_path.join(" › ") : null,
    block.paragraph_no ? `פסקה ${block.paragraph_no}` : null,
    block.page ? `עמוד ${block.page}` : null,
    block.kind === "image" ? `תמונה ${block.media ?? ""}`.trim() : null,
    SOURCE_NOTE[block.source] ?? null,
    STATUS_NOTE[block.status] || null,
    block.original_text ? "טקסט מתוקן (מיפוי גופן פגום תוקן לפי צורת האותיות)" : null,
  ].filter(Boolean);
  const showMeta = cited || block.kind === "image" || block.kind === "table" || !!block.original_text;
  const marker = hasRegionMarker(block, cited);
  const kind = KIND_LABEL[block.kind] ?? block.kind;
  const status = REGION_STATUS[block.status] ?? block.status;
  return (
    <div className={`blk ${block.kind}`} data-cited={cited ? "true" : "false"}>
      {showMeta && meta.length > 0 && <span className="meta">{meta.join(" · ")}</span>}
      {marker && (
        <button
          type="button"
          className="region-marker"
          aria-expanded={regionOpen}
          aria-controls={regionId}
          aria-label={`אזור במסמך: עמוד ${block.page}, ${kind}, ${status}`}
          onClick={() => setRegionOpen((o) => !o)}
        >
          {regionOpen ? "הסתרת האזור" : "הצגת האזור"} · עמוד <bdi>{block.page}</bdi> · {kind} · {status}
        </button>
      )}
      {marker && regionOpen && (
        <div id={regionId}>
          <RegionView block={block} documentId={documentId} versionId={versionId} onRevoked={onRevoked} />
        </div>
      )}
      {block.media_url && block.status !== "decorative" && safeApiUrl(block.media_url) && (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={safeApiUrl(block.media_url) ?? ""} alt={block.note ?? "תמונה מתוך המסמך"} loading="lazy" />
      )}
      {block.table ? (
        <TableView table={block.table} />
      ) : block.text ? (
        <span dir="auto">{block.text}</span>
      ) : block.note ? (
        <span className="muted">{block.note}</span>
      ) : null}
      {block.original_text && (
        <div className="original-toggle">
          <button
            type="button"
            className="btn-link"
            aria-pressed={showOriginal}
            onClick={() => setShowOriginal((o) => !o)}
          >
            הטקסט המקורי כפי שחולץ
          </button>
          {showOriginal && (
            <div className="original-text">
              <span className="meta">הטקסט המקורי (לפני התיקון):</span>
              <span dir="auto">{block.original_text}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

type RegionState = "loading" | "loaded" | "checking" | "unavailable";

/** A PDF region as an image. An unread region shows why it was not read instead. When the image cannot be
 * shown, the block is fetched again (the server checks permission again): its extracted text is the fallback, and
 * a document no longer visible shows nothing from it. */
function RegionView({
  block,
  documentId,
  versionId,
  onRevoked,
}: {
  block: SourceBlock;
  documentId: string;
  versionId: string;
  onRevoked: () => void;
}) {
  const [state, setState] = useState<RegionState>("loading");
  const [fallback, setFallback] = useState<SourceBlock | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => () => ctrl.current?.abort(), []);
  const url = safeApiUrl(block.region_url);
  const kind = KIND_LABEL[block.kind] ?? block.kind;

  if (block.status === "unread") {
    return (
      <p className="region-note" role="note">
        האזור לא נקרא: <bdi>{block.note || "לא נרשמה סיבה"}</bdi>
      </p>
    );
  }

  const onError = () => {
    setState("checking");
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    chatApi
      .blocks(documentId, versionId, block.index, block.index, c.signal)
      .then((res) => {
        setFallback(res.blocks.find((b) => b.index === block.index) ?? null);
        setState("unavailable");
      })
      .catch((err: unknown) => {
        if (isAbortError(err)) return;
        if (err instanceof ApiError && err.status === 404) onRevoked();
        else setState("unavailable");
      });
  };

  return (
    <div className="region-view">
      {(state === "loading" || state === "checking") && (
        <p className="muted" role="status">
          טוען את התמונה…
        </p>
      )}
      {url && state !== "unavailable" && state !== "checking" && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={url}
          alt={`${kind} בעמוד ${block.page} של המסמך`}
          hidden={state !== "loaded"}
          onLoad={() => setState("loaded")}
          onError={onError}
        />
      )}
      {(!url || state === "unavailable") && (
        <div role="note">
          <p className="region-note">{MSG_IMAGE_UNAVAILABLE}</p>
          {fallback && (fallback.text || fallback.note) && (
            <p className="muted">
              הטקסט שחולץ מהאזור: <span dir="auto">{fallback.text || fallback.note}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function TableView({ table }: { table: NonNullable<SourceBlock["table"]> }) {
  const headers = table.headers ?? [];
  return (
    <div>
      {table.title?.map((t, i) => (
        <div key={i}>
          <strong>{t}</strong>
        </div>
      ))}
      <div style={{ overflowX: "auto" }}>
        <table>
          {headers.some(Boolean) && (
            <thead>
              <tr>
                {headers.map((h, i) => (
                  <th key={i}>{h}</th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {table.rows.map((r, i) => (
              <tr key={i}>
                {r.map((c, j) => (
                  <td key={j} dir="auto">
                    {c}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {table.notes?.map((n, i) => (
        <div key={i} className="small">
          {n}
        </div>
      ))}
    </div>
  );
}
