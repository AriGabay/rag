"use client";

import { useEffect, useId, useRef, useState } from "react";
import { api, errorMessage, fileUrl } from "@/lib/api";
import type { Group, UploadResult } from "@/lib/types";
import { useSession } from "./AppShell";
import { ErrorAlert } from "./ui";

const MAX_FILES = 20;
const ACCEPT = ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";

const RESULT_LABEL: Record<UploadResult["status"], string> = {
  accepted: "התקבל לעיבוד",
  duplicate: "כבר הועלה",
  rejected: "נדחה",
};

const RESULT_CLASS: Record<UploadResult["status"], string> = {
  accepted: "badge-ok",
  duplicate: "badge-info",
  rejected: "badge-danger",
};

export function UploadResults({
  results,
  onOpenDocument,
}: {
  results: UploadResult[];
  onOpenDocument: (id: string) => void;
}) {
  if (results.length === 0) return null;
  return (
    <section aria-label="תוצאות ההעלאה">
      <h3>תוצאות ההעלאה</h3>
      <ul className="sources-list">
        {results.map((r, i) => (
          <li key={`${r.filename}-${i}`} className="row">
            <span className={`badge ${RESULT_CLASS[r.status]}`} role="status" aria-label={`${r.filename}: ${RESULT_LABEL[r.status]}`}>
              {RESULT_LABEL[r.status]}
            </span>
            <bdi>{r.filename}</bdi>
            {r.reason && <span className="muted">— {r.reason}</span>}
            {r.status === "duplicate" && r.document_id && (
              <>
                <button type="button" className="btn-link" onClick={() => onOpenDocument(r.document_id as string)}>
                  למסמך הקיים
                </button>
                {r.version_id && (
                  <a href={fileUrl(r.document_id, r.version_id)} target="_blank" rel="noopener noreferrer">
                    פתיחת הגרסה הקיימת
                  </a>
                )}
              </>
            )}
            {r.status === "accepted" && r.document_id && (
              <button type="button" className="btn-link" onClick={() => onOpenDocument(r.document_id as string)}>
                פרטים
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function UploadPanel({
  onUploaded,
  onOpenDocument,
}: {
  onUploaded: () => void;
  onOpenDocument: (id: string) => void;
}) {
  const me = useSession();
  const isAdmin = me?.user.role === "admin";
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [groups, setGroups] = useState<Group[]>(me?.groups ?? []);
  const [groupId, setGroupId] = useState<string>(me?.groups[0]?.id ?? "");
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<UploadResult[]>([]);

  // Admins may upload into any office group, not only their own memberships.
  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    api
      .groups()
      .then((res) => {
        if (cancelled) return;
        setGroups(res.groups.map((g) => ({ id: g.id, name: g.name })));
        setGroupId((cur) => cur || res.groups[0]?.id || "");
      })
      .catch(() => {
        // Fall back to the session's groups.
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin]);

  function addFiles(list: FileList | null) {
    if (!list) return;
    const next = [...files, ...Array.from(list)];
    if (next.length > MAX_FILES) {
      setError(`ניתן להעלות עד ${MAX_FILES} קבצים בכל פעם. נבחרו רק ${MAX_FILES} הראשונים.`);
      setFiles(next.slice(0, MAX_FILES));
    } else {
      setError(null);
      setFiles(next);
    }
  }

  async function upload() {
    if (files.length === 0) {
      setError("לא נבחרו קבצים.");
      return;
    }
    if (!groupId) {
      setError("יש לבחור קבוצת הרשאה.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.upload(files, groupId);
      setResults(res.results);
      setFiles([]);
      if (inputRef.current) inputRef.current.value = "";
      onUploaded();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card stack" aria-labelledby={`${inputId}-title`}>
      <h2 id={`${inputId}-title`}>העלאת מסמכים</h2>
      <div className="row">
        <div className="field">
          <label htmlFor={`${inputId}-group`}>קבוצת הרשאה</label>
          <select
            id={`${inputId}-group`}
            className="input"
            value={groupId}
            onChange={(e) => setGroupId(e.target.value)}
          >
            {groups.length === 0 && <option value="">אין קבוצות זמינות</option>}
            {groups.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div
        className={`dropzone${dragging ? " active" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(e.dataTransfer.files);
        }}
      >
        <p>
          גררו לכאן קובצי PDF או DOCX (עד <bdi>{MAX_FILES}</bdi> בכל פעם), או בחרו קבצים:
        </p>
        <label htmlFor={inputId} className="visually-hidden">
          בחירת קבצים להעלאה
        </label>
        <input
          ref={inputRef}
          id={inputId}
          type="file"
          multiple
          accept={ACCEPT}
          onChange={(e) => addFiles(e.target.files)}
        />
      </div>
      {files.length > 0 && (
        <div>
          <p>
            נבחרו <bdi>{files.length}</bdi> קבצים:
          </p>
          <ul>
            {files.map((f, i) => (
              <li key={`${f.name}-${i}`}>
                <bdi>{f.name}</bdi>{" "}
                <button
                  type="button"
                  className="btn-link"
                  aria-label={`הסרת ${f.name}`}
                  onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                >
                  הסרה
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <ErrorAlert message={error} />
      <div className="row">
        <button type="button" className="btn btn-primary" onClick={upload} disabled={busy || files.length === 0}>
          {busy ? "מעלה..." : "העלאה"}
        </button>
      </div>
      <UploadResults results={results} onOpenDocument={onOpenDocument} />
    </section>
  );
}
