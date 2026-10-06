"use client";

import { useState } from "react";
import { B, ErrorAlert } from "@/components/ui";
import { api, errorMessage, safeApiUrl } from "@/lib/api";
import { dataKindLabel, formatArea, formatIsoDate, formatMoney } from "@/lib/format";
import type { DedupReviewItem, TransactionSummary } from "@/lib/types";

function Side({ title, t }: { title: string; t: TransactionSummary }) {
  return (
    <section className="card stack" aria-label={title}>
      <h3>{title}</h3>
      <dl className="kv">
        <dt>סוג נתון</dt>
        <dd>{dataKindLabel(t.data_kind)}</dd>
        <dt>כתובת</dt>
        <dd>
          <bdi>{t.address ?? "—"}</bdi>
        </dd>
        <dt>מחיר</dt>
        <dd>
          <B>{formatMoney(t.price)}</B>
        </dd>
        <dt>שטח</dt>
        <dd>
          <B>{formatArea(t.area)}</B>
          {t.area_type && <span className="muted"> ({t.area_type})</span>}
        </dd>
        <dt>תאריך עסקה</dt>
        <dd>
          <B>{formatIsoDate(t.transaction_date)}</B>
        </dd>
      </dl>
      <div>
        <strong>מקורות</strong>
        <ul className="sources-list">
          {t.sources.map((s, i) => {
            const href = safeApiUrl(s.url);
            return (
              <li key={`${s.evidence_id}-${i}`}>
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    {s.title}
                  </a>
                ) : (
                  s.title
                )}
                {s.page_list.length > 0 && (
                  <span className="small muted">
                    {" "}
                    עמוד <B>{s.page_list.join(", ")}</B>
                  </span>
                )}
                {s.section && <span className="small muted"> · סעיף: {s.section}</span>}
                {s.row !== null && s.row !== undefined && (
                  <span className="small muted">
                    {" "}
                    · שורה <B>{s.row}</B>
                  </span>
                )}
                {s.snippet && <div className="snippet">{s.snippet}</div>}
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}

export function DedupView({ item, onResolved }: { item: DedupReviewItem; onResolved: (message: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(fn: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onResolved(message);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <h2>חשד לכפילות</h2>
      {item.reason && <p>{item.reason}</p>}
      <div className="compare">
        <Side title="רשומה א׳" t={item.a} />
        <Side title="רשומה ב׳" t={item.b} />
      </div>
      <ErrorAlert message={error} />
      <div className="row">
        <button
          type="button"
          className="btn btn-primary"
          disabled={busy}
          onClick={() => void act(() => api.merge(item.id), "הרשומות מוזגו לעסקה אחת.")}
        >
          מזג
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy}
          onClick={() => void act(() => api.keepSeparate(item.id), "הרשומות נשארו נפרדות.")}
        >
          השאר נפרד
        </button>
      </div>
    </div>
  );
}
