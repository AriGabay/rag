"use client";

import Link from "next/link";
import { useCallback, useId, useRef, useState } from "react";
import { useSession } from "@/components/AppShell";
import { B, Dialog, ErrorAlert, Notice, StatusBadge } from "@/components/ui";
import { UploadPanel, UploadResults } from "@/components/UploadPanel";
import { api, errorMessage, fileUrl } from "@/lib/api";
import { useApi, useInterval } from "@/lib/useApi";
import { formatTimestamp, VERSION_STATUS_LABEL } from "@/lib/format";
import type { DocumentDetail, DocumentSummary, SearchResult, UploadResult, Version } from "@/lib/types";

const POLL_MS = 3000;

function isActive(v: Version | null | undefined): boolean {
  return v?.status === "pending" || v?.status === "processing";
}

function incompletePages(v: Version): React.ReactNode {
  const p = v.pages_incomplete;
  if (p === null || p === undefined) return null;
  if (Array.isArray(p)) {
    if (p.length === 0) return null;
    return (
      <span className="small muted">
        {" "}
        (עמודים שלא חולצו במלואם: <B>{p.join(", ")}</B>)
      </span>
    );
  }
  if (p === 0) return null;
  return (
    <span className="small muted">
      {" "}
      (<B>{p}</B> עמודים שלא חולצו במלואם)
    </span>
  );
}

function StatusCell({ v }: { v: Version | null }) {
  if (!v) return <span className="muted">—</span>;
  return (
    <div>
      <StatusBadge status={v.status} />
      {v.status_reason && (v.status === "failed" || v.status === "needs_review") && (
        <div className="small" style={{ color: v.status === "failed" ? "var(--danger)" : "var(--warn)" }}>
          {v.status_reason}
        </div>
      )}
    </div>
  );
}

function DocumentPanel({
  id,
  onClose,
  onChanged,
}: {
  id: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const me = useSession();
  const canUpload = !!me && (me.user.role === "admin" || me.user.can_upload);
  const inputId = useId();
  const versionInput = useRef<HTMLInputElement>(null);
  const fetchDoc = useCallback(() => api.document(id), [id]);
  const { data: doc, error: loadError, reload } = useApi<DocumentDetail>(fetchDoc);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<UploadResult[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleted, setDeleted] = useState(false);

  const polling = !!doc && doc.versions.some((v) => isActive(v));
  useInterval(reload, POLL_MS, polling);

  async function uploadVersion(list: FileList | null) {
    if (!doc || !list || list.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.upload([list[0]], doc.group.id, doc.id);
      setResults(res.results);
      reload();
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
      if (versionInput.current) versionInput.current.value = "";
    }
  }

  async function doDelete() {
    setConfirmDelete(false);
    setBusy(true);
    try {
      await api.deleteDocument(id);
      setDeleted(true);
      onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card stack" aria-label="פרטי מסמך">
      <div className="row">
        <h2 style={{ margin: 0 }}>{doc ? doc.title : loadError ? "פרטי מסמך" : "טוען..."}</h2>
        <span className="spacer" />
        <button type="button" className="btn" onClick={onClose}>
          סגירה
        </button>
      </div>
      <ErrorAlert message={loadError} onRetry={reload} />
      <ErrorAlert message={error} />
      {deleted && <Notice kind="ok">המסמך נמחק ולא ישמש עוד בתשובות.</Notice>}
      {doc && (
        <>
          <div className="row small">
            <span>
              קבוצה: <strong>{doc.group.name}</strong>
            </span>
            <span>
              נוצר: <B>{formatTimestamp(doc.created_at)}</B>
            </span>
            {doc.deleted && <span className="badge badge-danger">נמחק</span>}
            {polling && <span className="muted">מתעדכן אוטומטית...</span>}
          </div>
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">גרסאות המסמך</caption>
              <thead>
                <tr>
                  <th scope="col">גרסה</th>
                  <th scope="col">קובץ</th>
                  <th scope="col">סטטוס</th>
                  <th scope="col">עמודים</th>
                  <th scope="col">רשומות</th>
                  <th scope="col">לבדיקה</th>
                  <th scope="col">הועלה</th>
                  <th scope="col">עובד</th>
                </tr>
              </thead>
              <tbody>
                {doc.versions.map((v) => (
                  <tr key={v.id}>
                    <td>
                      <B>{v.version_no}</B>
                      {v.is_current && <span className="badge badge-info"> נוכחית</span>}
                    </td>
                    <td>
                      <a href={fileUrl(doc.id, v.id)} target="_blank" rel="noopener noreferrer">
                        <bdi>{v.filename}</bdi>
                      </a>
                    </td>
                    <td>
                      <StatusCell v={v} />
                    </td>
                    <td>
                      <B>{v.page_count ?? "—"}</B>
                      {incompletePages(v)}
                    </td>
                    <td>
                      <B>{v.records_total ?? "—"}</B>
                    </td>
                    <td>
                      <B>{v.records_needing_review ?? "—"}</B>
                    </td>
                    <td>
                      <B>{formatTimestamp(v.created_at)}</B>
                    </td>
                    <td>
                      <B>{formatTimestamp(v.processed_at)}</B>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="row">
            <Link className="btn" href={`/review?document_id=${encodeURIComponent(doc.id)}`}>
              לבדיקת הנתונים של המסמך
            </Link>
            {canUpload && !doc.deleted && (
              <>
                <label htmlFor={inputId}>העלאת גרסה חדשה:</label>
                <input
                  ref={versionInput}
                  id={inputId}
                  type="file"
                  accept=".pdf,.docx"
                  disabled={busy}
                  onChange={(e) => void uploadVersion(e.target.files)}
                />
              </>
            )}
            {canUpload && !doc.deleted && !deleted && (
              <button type="button" className="btn btn-danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
                מחיקת המסמך
              </button>
            )}
          </div>
          <UploadResults results={results} onOpenDocument={reload} />
        </>
      )}
      <Dialog open={confirmDelete} title="מחיקת מסמך" onClose={() => setConfirmDelete(false)}>
        <p>המסמך והעובדות שחולצו ממנו יוסרו מהחיפוש ומהתשובות. להמשיך?</p>
        <div className="row">
          <button type="button" className="btn btn-danger" onClick={() => void doDelete()}>
            מחיקה
          </button>
          <button type="button" className="btn" onClick={() => setConfirmDelete(false)}>
            ביטול
          </button>
        </div>
      </Dialog>
    </section>
  );
}

function DocumentsTab({
  docs,
  error,
  reload,
  selected,
  onSelect,
  onSearch,
}: {
  docs: DocumentSummary[] | null;
  error: string | null;
  reload: () => void;
  selected: string | null;
  onSelect: (id: string) => void;
  onSearch: (q: string, status: string) => void;
}) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const polling = !!docs && docs.some((d) => isActive(d.current_version));
  useInterval(reload, POLL_MS, polling);

  return (
    <div className="stack">
      <form
        className="row"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          onSearch(q.trim(), status);
        }}
      >
        <label htmlFor="doc-q" className="visually-hidden">
          חיפוש לפי כותרת
        </label>
        <input
          id="doc-q"
          className="input"
          placeholder="חיפוש לפי כותרת או פרטי מסמך"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <label htmlFor="doc-status" className="visually-hidden">
          סינון לפי סטטוס
        </label>
        <select id="doc-status" className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">כל הסטטוסים</option>
          {Object.entries(VERSION_STATUS_LABEL).map(([k, label]) => (
            <option key={k} value={k}>
              {label}
            </option>
          ))}
        </select>
        <button type="submit" className="btn">
          חיפוש
        </button>
        {polling && <span className="small muted">מתעדכן אוטומטית כל 3 שניות...</span>}
      </form>
      <ErrorAlert message={error} onRetry={reload} />
      {docs === null && !error && <p className="muted">טוען...</p>}
      {docs && docs.length === 0 && <p className="muted">לא נמצאו מסמכים.</p>}
      {docs && docs.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">רשימת מסמכים</caption>
            <thead>
              <tr>
                <th scope="col">כותרת</th>
                <th scope="col">קבוצה</th>
                <th scope="col">סטטוס</th>
                <th scope="col">גרסה</th>
                <th scope="col">עמודים</th>
                <th scope="col">רשומות</th>
                <th scope="col">לבדיקה</th>
                <th scope="col">נוצר</th>
              </tr>
            </thead>
            <tbody>
              {docs.map((d) => {
                const v = d.current_version;
                return (
                  <tr key={d.id} className={d.id === selected ? "selected" : undefined}>
                    <td>
                      <button type="button" className="btn-link" onClick={() => onSelect(d.id)}>
                        {d.title}
                      </button>
                      {d.deleted && <span className="badge badge-danger"> נמחק</span>}
                    </td>
                    <td>{d.group.name}</td>
                    <td>
                      <StatusCell v={v} />
                    </td>
                    <td>
                      <B>{v?.version_no ?? "—"}</B>
                      {d.versions_count > 1 && (
                        <span className="small muted">
                          {" "}
                          (מתוך <B>{d.versions_count}</B>)
                        </span>
                      )}
                    </td>
                    <td>
                      <B>{v?.page_count ?? "—"}</B>
                    </td>
                    <td>
                      <B>{v?.records_total ?? "—"}</B>
                    </td>
                    <td>
                      <B>{v?.records_needing_review ?? "—"}</B>
                    </td>
                    <td>
                      <B>{formatTimestamp(d.created_at)}</B>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ContentSearchTab() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(e?: React.FormEvent) {
    e?.preventDefault();
    const term = q.trim();
    if (!term) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.search(term, 20);
      setResults(res.results);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <form className="row" role="search" onSubmit={run}>
        <label htmlFor="content-q" className="visually-hidden">
          חיפוש בתוכן המסמכים
        </label>
        <input
          id="content-q"
          className="input"
          style={{ flex: 1 }}
          placeholder="חיפוש בתוכן המסמכים"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button type="submit" className="btn btn-primary" disabled={busy || !q.trim()}>
          {busy ? "מחפש..." : "חיפוש"}
        </button>
      </form>
      <ErrorAlert message={error} onRetry={() => void run()} />
      {results && results.length === 0 && <p className="muted">לא נמצאו תוצאות.</p>}
      {results && results.length > 0 && (
        <ul className="sources-list" aria-label="תוצאות חיפוש">
          {results.map((r) => (
            <li key={r.chunk_id}>
              <div className="row">
                <a href={fileUrl(r.document_id, r.version_id, r.page_list[0])} target="_blank" rel="noopener noreferrer">
                  {r.title}
                </a>
                {r.section && <span className="small muted">סעיף: {r.section}</span>}
                {r.page_list.length > 0 && (
                  <span className="small">
                    עמודים:{" "}
                    {r.page_list.map((p, i) => (
                      <span key={p}>
                        {i > 0 && ", "}
                        <a href={fileUrl(r.document_id, r.version_id, p)} target="_blank" rel="noopener noreferrer">
                          <bdi>{p}</bdi>
                        </a>
                      </span>
                    ))}
                  </span>
                )}
              </div>
              <div className="snippet">{r.snippet}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function DocumentsPage() {
  const me = useSession();
  const canUpload = !!me && (me.user.role === "admin" || me.user.can_upload);
  const [tab, setTab] = useState<"docs" | "content">("docs");
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState({ q: "", status: "" });
  const fetchDocs = useCallback(() => api.documents(query.q || undefined, query.status || undefined), [query]);
  const docsApi = useApi(fetchDocs);
  const refresh = docsApi.reload;

  return (
    <div className="stack">
      <h1>מסמכים</h1>
      {canUpload && (
        <UploadPanel
          onUploaded={refresh}
          onOpenDocument={(id) => {
            setTab("docs");
            setSelected(id);
          }}
        />
      )}
      {selected && (
        <DocumentPanel key={selected} id={selected} onClose={() => setSelected(null)} onChanged={refresh} />
      )}
      <section className="card">
        <div className="tabs" role="tablist" aria-label="תצוגת מסמכים">
          <button
            type="button"
            role="tab"
            id="tab-docs"
            aria-selected={tab === "docs"}
            aria-controls="panel-docs"
            onClick={() => setTab("docs")}
          >
            רשימת מסמכים
          </button>
          <button
            type="button"
            role="tab"
            id="tab-content"
            aria-selected={tab === "content"}
            aria-controls="panel-content"
            onClick={() => setTab("content")}
          >
            חיפוש בתוכן
          </button>
        </div>
        {tab === "docs" ? (
          <div role="tabpanel" id="panel-docs" aria-labelledby="tab-docs">
            <DocumentsTab
              docs={docsApi.data?.documents ?? null}
              error={docsApi.error}
              reload={refresh}
              selected={selected}
              onSelect={setSelected}
              onSearch={(q, status) => setQuery({ q, status })}
            />
          </div>
        ) : (
          <div role="tabpanel" id="panel-content" aria-labelledby="tab-content">
            <ContentSearchTab />
          </div>
        )}
      </section>
    </div>
  );
}
