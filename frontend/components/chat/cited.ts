import type { ChatAnswer, ChatAssumption, ChatSource, ChatStandard, ChatValue, ViewerTarget } from "@/lib/chatTypes";
import { type ValueStatus, chatValueStatus, measurementValueStatus, standardLabel } from "@/lib/format";
import { documentAnchor } from "./SourceViewer";

/** What a citation is looked up in: an answer, or the records the diagnostics route returned for a removal. */
export type CitedRecords = Pick<ChatAnswer, "sources" | "values" | "measurements">;

// an answer's source is bound to the reading it was read from (null: an answer from before reading ids)
function withReading(src: ChatSource): ChatSource {
  return { ...src, reading_id: src.reading_id ?? null };
}

/** The source a citation opens: a passage itself, or the passage of the value or measurement it cites; null for a
 * calculation (C#) or an assumption (A#), which have no place in a document (`citedView` opens their own views). */
export function citedSource(answer: CitedRecords, id: string): ChatSource | null {
  const direct = answer.sources.find((s) => s.id === id);
  if (direct) return withReading(direct);
  // a value opens the passage it was verified in
  const v = answer.values?.find((x) => x.id === id);
  if (v) {
    const src = answer.sources.find((s) => s.id === v.source_id);
    return src ? withReading(src) : null;
  }
  const m = answer.measurements.find((x) => x.id === id);
  if (m) {
    return {
      id: m.id,
      document_id: m.document_id,
      version_id: m.version_id,
      title: m.title,
      section: m.section,
      location: m.section ? `סעיף "${m.section}"` : "נתון כמותי",
      kind: "measurement",
      text: m.quote,
      block_start: m.block_index,
      block_end: m.block_index,
      table_index: m.table_index,
      page_list: null,
      chunk_id: null,
      reading_id: m.reading_id ?? null,
    };
  }
  return null;
}

/** The standard a source cites, from the snapshot first (it outlives the reading), else the source itself; null for an
 * appraisal and for answers from before round 10. */
export function sourceStandard(src: Pick<ChatSource, "anchor" | "standard">): ChatStandard | null {
  return src.anchor?.standard ?? src.standard ?? null;
}

/** What a standard's citation says of its identity beside the title: "תקן 98.1 (גרסה 2.0) — סעיף 2.1"; null for an
 * appraisal's source. */
export function sourceStandardLabel(src: Pick<ChatSource, "anchor" | "standard">): string | null {
  return standardLabel(sourceStandard(src));
}

/** A standard's chip title and sources-list entry: its identity ("תקן 98.1 (גרסה 2.0) — סעיף 2.1"); the document's own
 * title leads only when it states neither a number nor a name. Null for an appraisal's source. */
export function standardChipTitle(src: ChatSource): string | null {
  const label = sourceStandardLabel(src);
  if (!label) return null;
  const std = sourceStandard(src);
  return std?.number || std?.title ? label : `${src.title} — ${label}`;
}

/** A value's status (V#): checked automatically in its passage, uncertain, or unread (never a person's decision). */
export function valueStatusOf(answer: CitedRecords, v: ChatValue): ValueStatus {
  const src = answer.sources.find((s) => s.id === v.source_id);
  return chatValueStatus(v.certainty, src?.status, v.reading);
}

/** What the source viewer opens for a citation: its passage (the text view) and its anchor (the page or structured
 * view), with a value's or measurement's status; null for an id with no place in a document (C#, A#). */
export function citedTarget(answer: CitedRecords, id: string): ViewerTarget | null {
  const source = citedSource(answer, id);
  if (!source) return null;
  const s = answer.sources.find((x) => x.id === id);
  if (s) return { id, source, anchor: documentAnchor(s.anchor) };
  const v = answer.values?.find((x) => x.id === id);
  if (v) {
    // the cell or quoted words it was taken from; else the passage it was verified in
    const passage = answer.sources.find((x) => x.id === v.source_id);
    return {
      id,
      source,
      anchor: documentAnchor(v.anchor) ?? documentAnchor(passage?.anchor),
      valueStatus: valueStatusOf(answer, v),
    };
  }
  const m = answer.measurements.find((x) => x.id === id);
  if (m) return { id, source, anchor: documentAnchor(m.anchor), valueStatus: measurementValueStatus(m) };
  return null;
}

export function assumptionText(a: ChatAssumption): string {
  return a.unit === "percent" && !a.value_text.includes("%") ? `${a.value_text}%` : a.value_text;
}
