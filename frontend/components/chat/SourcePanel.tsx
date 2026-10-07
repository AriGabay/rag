"use client";

import { useEffect, useRef, useState } from "react";
import { chatApi, errorMessage, isAbortError, safeApiUrl } from "@/lib/api";
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

/** The cited place in its document: the blocks around it, the cited ones highlighted, tables as tables, and a
 * picture next to what was read from it. A DOCX is located by section and paragraph; nothing invents pages. */
export function SourcePanel({ source, onClose }: { source: ChatSource; onClose: () => void }) {
  const [data, setData] = useState<SourceBlocks | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
  }, [source.id]);

  useEffect(() => {
    // the parent keys this panel by source, so a new source starts from empty state
    const ctrl = new AbortController();
    const start = source.block_start;
    const end = source.block_end ?? start;
    // a listing of documents has no place in a document: its text is all there is
    if (start == null || !source.document_id || !source.version_id) return () => ctrl.abort();
    chatApi
      .blocks(source.document_id, source.version_id, Math.max(0, start - WINDOW), (end ?? start) + WINDOW, ctrl.signal)
      .then(setData)
      .catch((err: unknown) => {
        if (!isAbortError(err)) setError(errorMessage(err));
      });
    return () => ctrl.abort();
  }, [source.document_id, source.version_id, source.block_start, source.block_end]);

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
        {data?.blocks.map((b) => (
          <Block key={b.index} block={b} cited={b.index >= lo && b.index <= hi} />
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

function Block({ block, cited }: { block: SourceBlock; cited: boolean }) {
  const meta = [
    block.section_path.length ? block.section_path.join(" › ") : null,
    block.paragraph_no ? `פסקה ${block.paragraph_no}` : null,
    block.page ? `עמוד ${block.page}` : null,
    block.kind === "image" ? `תמונה ${block.media ?? ""}`.trim() : null,
    SOURCE_NOTE[block.source] ?? null,
    STATUS_NOTE[block.status] || null,
  ].filter(Boolean);
  const showMeta = cited || block.kind === "image" || block.kind === "table";
  return (
    <div className={`blk ${block.kind}`} data-cited={cited ? "true" : "false"}>
      {showMeta && meta.length > 0 && <span className="meta">{meta.join(" · ")}</span>}
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
