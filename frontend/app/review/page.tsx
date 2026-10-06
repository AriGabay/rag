"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useState } from "react";
import { DedupView } from "@/components/review/DedupView";
import { RecordView } from "@/components/review/RecordView";
import { B, ErrorAlert, Notice } from "@/components/ui";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { dataKindLabel, formatArea, formatIsoDate, formatMoney } from "@/lib/format";
import type { RecordDetail, ReviewItem } from "@/lib/types";

function QueueItem({ item, selected, onSelect }: { item: ReviewItem; selected: boolean; onSelect: () => void }) {
  if (item.kind === "dedup") {
    return (
      <button type="button" aria-current={selected ? "true" : undefined} onClick={onSelect}>
        <div className="row">
          <span className="badge badge-info">חשד לכפילות</span>
        </div>
        <div className="small">
          <bdi>{item.a.address ?? "—"}</bdi> / <bdi>{item.b.address ?? "—"}</bdi>
        </div>
        {item.reason && <div className="small muted">{item.reason}</div>}
      </button>
    );
  }
  const s = item.summary;
  return (
    <button type="button" aria-current={selected ? "true" : undefined} onClick={onSelect}>
      <div>{item.document.title}</div>
      <div className="small">
        {dataKindLabel(s.data_kind)} · <bdi>{[s.address, s.neighborhood, s.city].filter(Boolean).join(", ") || "—"}</bdi>
      </div>
      <div className="small muted">
        <B>{formatMoney(s.price)}</B> · <B>{formatArea(s.area)}</B> ·{" "}
        <B>{formatIsoDate(s.transaction_date ?? s.valuation_date)}</B>
        {item.page_no !== null && (
          <>
            {" "}
            · עמוד <B>{item.page_no}</B>
          </>
        )}
      </div>
      <div className="row">
        {item.flags.conflict && <span className="badge badge-warn">סתירה</span>}
        {item.flags.missing_critical.length > 0 && <span className="badge badge-danger">חסרים שדות</span>}
        {item.flags.ocr && <span className="badge">OCR</span>}
      </div>
    </button>
  );
}

function ReviewInner() {
  const params = useSearchParams();
  const documentId = params.get("document_id") ?? undefined;
  const fetchQueue = useCallback(() => api.reviewQueue(documentId), [documentId]);
  const queue = useApi(fetchQueue);
  const items: ReviewItem[] | null = queue.data?.items ?? null;
  const queueError = queue.error;
  const loadQueue = queue.reload;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedKind, setSelectedKind] = useState<ReviewItem["kind"] | null>(null);
  const [record, setRecord] = useState<RecordDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const selected = items?.find((i) => i.id === selectedId) ?? null;

  async function select(item: ReviewItem) {
    setSelectedId(item.id);
    setSelectedKind(item.kind);
    setNotice(null);
    setDetailError(null);
    setRecord(null);
    if (item.kind === "record") {
      try {
        setRecord(await api.record(item.id));
      } catch (err) {
        setDetailError(errorMessage(err));
      }
    }
  }

  return (
    <div className="stack">
      <div className="row">
        <h1 style={{ margin: 0 }}>בדיקת נתונים</h1>
        {documentId && (
          <>
            <span className="badge badge-info">מסונן למסמך אחד</span>
            <Link href="/review">הצג את כל התור</Link>
          </>
        )}
      </div>
      {notice && <Notice kind="ok">{notice}</Notice>}
      <div className="split">
        <aside className="card stack" aria-label="תור הבדיקה">
          <div className="row">
            <h2 style={{ margin: 0 }}>תור הבדיקה</h2>
            {items && (
              <span className="badge">
                <bdi>{items.length}</bdi>
              </span>
            )}
          </div>
          <ErrorAlert message={queueError} onRetry={loadQueue} />
          {items === null && !queueError && <p className="muted">טוען...</p>}
          {items && items.length === 0 && <p className="muted">אין פריטים הממתינים לבדיקה.</p>}
          {items && items.length > 0 && (
            <ul className="conv-list">
              {items.map((it) => (
                <li key={`${it.kind}-${it.id}`}>
                  <QueueItem item={it} selected={it.id === selectedId} onSelect={() => void select(it)} />
                </li>
              ))}
            </ul>
          )}
        </aside>
        <section aria-label="פרטי הפריט" className="stack">
          <ErrorAlert message={detailError} />
          {!selectedKind && !detailError && <p className="muted">בחרו פריט מהתור כדי לבדוק אותו מול המקור.</p>}
          {selectedKind === "record" && !record && !detailError && <p className="muted">טוען...</p>}
          {selectedKind === "record" && record && (
            <RecordView
              key={record.id}
              record={record}
              onUpdated={(r) => {
                setRecord(r);
                loadQueue();
              }}
            />
          )}
          {selected?.kind === "dedup" && (
            <DedupView
              key={selected.id}
              item={selected}
              onResolved={(message) => {
                setNotice(message);
                setSelectedId(null);
                setSelectedKind(null);
                loadQueue();
              }}
            />
          )}
        </section>
      </div>
    </div>
  );
}

export default function ReviewPage() {
  return (
    <Suspense fallback={<p className="muted">טוען...</p>}>
      <ReviewInner />
    </Suspense>
  );
}
