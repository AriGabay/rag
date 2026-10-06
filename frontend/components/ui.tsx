"use client";

import { useEffect, useId, useRef } from "react";
import { versionStatusLabel } from "@/lib/format";

/** Isolates numbers, money, dates, addresses and block/parcel values so they never reverse in RTL. */
export function B({ children }: { children: React.ReactNode }) {
  return <bdi>{children}</bdi>;
}

const STATUS_CLASS: Record<string, string> = {
  ready: "badge-ok",
  processing: "badge-info",
  pending: "badge-info",
  needs_review: "badge-warn",
  failed: "badge-danger",
  superseded: "",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const label = versionStatusLabel(status);
  return (
    <span className={`badge ${STATUS_CLASS[status ?? ""] ?? ""}`} role="status" aria-label={`סטטוס: ${label}`}>
      {label}
    </span>
  );
}

export function ErrorAlert({ message, onRetry }: { message: string | null; onRetry?: () => void }) {
  if (!message) return null;
  return (
    <div className="alert alert-error row" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          נסו שוב
        </button>
      )}
    </div>
  );
}

export function Notice({ kind, children }: { kind: "ok" | "info" | "warn"; children: React.ReactNode }) {
  return (
    <div className={`alert alert-${kind}`} role="status">
      {children}
    </div>
  );
}

/** Modal built on the native <dialog> element (focus trapping and Escape handled by the browser). */
export function Dialog({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId} onClose={onClose}>
      <h2 id={titleId}>{title}</h2>
      {children}
    </dialog>
  );
}
