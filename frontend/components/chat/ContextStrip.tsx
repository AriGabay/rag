"use client";

import { B } from "@/components/ui";
import type { ContextChip } from "@/lib/types";

/**
 * The conversation context as the server holds it (R17): one removable chip per item. Removing a chip sends a
 * condition edit (`remove`); nothing here is kept as client-side state.
 */
export function ContextStrip({
  chips,
  disabled,
  onRemove,
}: {
  chips: ContextChip[];
  disabled: boolean;
  onRemove: (chip: ContextChip) => void;
}) {
  if (chips.length === 0) return null;
  return (
    <div className="small row" role="region" aria-label="הקשר השיחה" style={{ alignItems: "baseline" }}>
      <strong>תנאים שאושרו בשיחה:</strong>
      <ul className="row" style={{ listStyle: "none", margin: 0, padding: 0, gap: 6 }}>
        {chips.map((c) => (
          <li
            key={c.key}
            className="badge badge-info row"
            style={{ gap: 4, whiteSpace: "normal", overflowWrap: "anywhere", fontWeight: 500 }}
          >
            <span>
              {c.label}: <B>{c.value}</B>
            </span>
            <button
              type="button"
              className="btn-link"
              style={{ textDecoration: "none", fontWeight: 700, paddingInline: 4 }}
              disabled={disabled}
              aria-label={`הסרת התנאי ${c.label}: ${c.value}`}
              title="הסרה"
              onClick={() => onRemove(c)}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
