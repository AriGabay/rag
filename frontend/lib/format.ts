// Display formatting. Decimal strings are formatted directly (Intl accepts exact numeric strings),
// so no float arithmetic touches money or area. Nothing here aggregates values.

import type { ChatUnit } from "./chatTypes";
import type {
  AbstentionKind,
  ClaimKind,
  CoverageRegion,
  FactCoverage,
  Provider,
  ProviderMode,
  ProviderStatus,
  VersionStatus,
} from "./types";

/** A unit beside its file name: its title (or that it has none) and its pages, e.g. "מתחם הגפן א · עמוד 1". */
export function unitLabel(unit: ChatUnit | null | undefined): string {
  if (!unit) return "";
  const [first, last] = unit.pages;
  const pages = first == null ? "" : first === last || last == null ? `עמוד ${first}` : `עמודים ${first}–${last}`;
  return [unit.title || "ללא כותרת", pages].filter(Boolean).join(" · ");
}

const DECIMAL_RE = /^[-+]?\d+(\.\d+)?$/;

const plainFmt = new Intl.NumberFormat("he-IL", { maximumFractionDigits: 2 });
const wholeFmt = new Intl.NumberFormat("he-IL", { maximumFractionDigits: 0 });

export function formatDecimal(value: string | number | null | undefined, opts?: { whole?: boolean }): string {
  if (value === null || value === undefined || value === "") return "—";
  const fmt = opts?.whole ? wholeFmt : plainFmt;
  if (typeof value === "number") return Number.isFinite(value) ? fmt.format(value) : "—";
  const s = value.trim();
  if (!DECIMAL_RE.test(s)) return s;
  return fmt.format(s as Intl.StringNumericLiteral);
}

export function formatMoney(value: string | null | undefined, currency = "ILS"): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = formatDecimal(value, { whole: true });
  if (n === "—") return n;
  return currency === "ILS" ? `${n} ₪` : `${n} ${currency}`;
}

export function formatPricePerSqm(value: string | null | undefined, currency = "ILS"): string {
  const m = formatMoney(value, currency);
  return m === "—" ? m : `${m} למ״ר`;
}

export function formatArea(value: string | null | undefined): string {
  const n = formatDecimal(value);
  return n === "—" ? n : `${n} מ״ר`;
}

/** ISO YYYY-MM-DD → DD/MM/YYYY without going through Date (avoids timezone shifts). */
export function formatIsoDate(value: string | null | undefined): string {
  if (!value) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!m) return value;
  return `${m[3]}/${m[2]}/${m[1]}`;
}

const dateTimeFmt = new Intl.DateTimeFormat("he-IL", { dateStyle: "short", timeStyle: "short" });

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : dateTimeFmt.format(d);
}

export const VERSION_STATUS_LABEL: Record<VersionStatus, string> = {
  pending: "ממתין לעיבוד",
  processing: "בעיבוד",
  ready: "מוכן",
  needs_review: "דורש בדיקה",
  failed: "נכשל",
  superseded: "הוחלף בגרסה חדשה",
};

/** The stages of reading a document (``pdf_windows.STAGE_*``, ``pipeline.STAGE_*``), by the key the server reports. */
export const INGEST_STAGE_LABEL: Record<string, string> = {
  reading_pages: "קריאת עמודים",
  checking_fonts: "בדיקת גופנים",
  layout: "סידור כיווני הטקסט ומספרי העמודים",
  reading_regions: "קריאת תמונות ואזורים",
  assembling: "בניית מבנה המסמך",
  saving: "שמירת הקריאה",
  embedding: "בניית אינדקס החיפוש",
  publishing: "פרסום המסמך",
};

/** What a version being processed shows (R3): nothing before it reports; the stage alone before the pages in the file
 * are known; "pages read N of M" while pages are read; the stage name during the steps over the whole document. */
export function ingestProgressText(p: { stage: string; pages_done: number | null; pages_total: number | null } | null | undefined): string | null {
  if (!p || !p.stage) return null;
  const stage = INGEST_STAGE_LABEL[p.stage] ?? p.stage;
  if (p.stage === "reading_pages" && p.pages_total != null && p.pages_done != null) {
    return `נקראו ${p.pages_done} מתוך ${p.pages_total} עמודים`;
  }
  return stage;
}

export function versionStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return VERSION_STATUS_LABEL[status as VersionStatus] ?? status;
}

export const PROVIDER_LABEL: Record<Provider, string> = {
  template: "תבנית",
  mock: "מודל מדומה (דמו)",
  cloud: "מודל ענן",
  extractive: "חילוץ מהמקורות",
};

export const PROVIDER_MODE_LABEL: Record<ProviderMode, string> = {
  cloud: "מודל ענן פעיל",
  error: "תקלה בספק המודל — התשובות מורכבות מקטעי המקור בלבד",
  demo: "מצב דמו: מודל מדומה בלבד, לא מודל אמיתי",
  limited: "מצב מוגבל: תשובות מקטעי המקור בלבד (ללא מודל ענן)",
};

export const PROVIDER_MODE_BADGE: Record<ProviderMode, string> = {
  cloud: "badge-ok",
  error: "badge-danger",
  demo: "badge-demo",
  limited: "badge-info",
};

/** Hebrew explanation per connection-test status; each status has its own message. */
export const PROVIDER_STATUS_LABEL: Record<ProviderStatus, string> = {
  ok: "החיבור תקין: הספק החזיר תשובה מובנית תקינה",
  missing_key: "לא נמצא בשרת מפתח עבור הספק שנבחר — לא נשלחה בקשה",
  auth: "הספק דחה את המפתח (שגיאת הרשאה)",
  model_unavailable: "המודל שנבחר אינו זמין עבור המפתח",
  timeout: "הספק לא הגיב בזמן",
  rate_limited: "חריגה ממגבלת קצב הבקשות של הספק",
  quota: "מכסת השימוש אצל הספק נוצלה",
  refusal: "הספק סירב לבקשת הבדיקה",
  incomplete: "תשובת הספק נקטעה לפני סיומה",
  invalid: "תשובת הספק לא תאמה את המבנה הנדרש",
  error: "תקלה כללית בפנייה לספק",
  reacknowledge_required:
    "השימוש בענן אושר בעבר עבור ספק אחר. לא יישלח דבר לספק הנוכחי עד לאישור מחדש (הפעלת שימוש בענן ואישור).",
};

export function providerStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return PROVIDER_STATUS_LABEL[status as ProviderStatus] ?? status;
}

export const DATA_KIND_OPTIONS: { value: string; label: string }[] = [
  { value: "transaction_price", label: "מחירי עסקאות" },
  { value: "appraised_value", label: "שווי שמאי" },
  { value: "asking_price", label: "מחיר מבוקש" },
  { value: "adjusted_comparable", label: "מחיר השוואה מתואם" },
];

export const DATE_FIELD_OPTIONS: { value: string; label: string }[] = [
  { value: "transaction_date", label: "תאריך עסקה" },
  { value: "valuation_date", label: "מועד קובע" },
  { value: "report_date", label: "תאריך הדוח" },
];

export function dataKindLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return DATA_KIND_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export const VERIFICATION_LABEL: Record<string, string> = {
  human_verified: "אומת על ידי אדם",
  auto_extracted: "חולץ אוטומטית — ממתין לאימות",
  extracted: "חולץ מהמסמך",
  computed: "חושב במערכת",
  missing: "חסר במסמך",
  verified: "מאומת",
  approved: "מאומת",
  corrected: "תוקן",
  pending: "ממתין לאימות",
  awaiting_verification: "ממתין לאימות",
  unverified: "לא מאומת",
  needs_review: "דורש בדיקה",
  rejected: "נדחה",
};

export function verificationLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return VERIFICATION_LABEL[value] ?? value;
}

export const NUMERIC_FIELDS = new Set(["area", "price", "price_per_sqm_stated", "rooms"]);
export const DATE_FIELDS = new Set(["transaction_date", "valuation_date", "report_date"]);

// ---------- Chat answers ----------

/** Short badge text for the mode an answer was produced in (the admin screen uses PROVIDER_MODE_LABEL). */
export const ANSWER_MODE_LABEL: Record<ProviderMode, string> = {
  cloud: "מודל ענן",
  limited: "מצב מוגבל",
  demo: "דמו",
  error: "תקלה בספק המודל",
};

export const ANSWER_MODE_DESCRIPTION: Record<ProviderMode, string> = {
  cloud: "התשובה הורכבה בעזרת מודל ענן ונבדקה מול המקורות",
  limited: "מצב מוגבל: מודל הענן כבוי, והתשובה מבוססת על קטעי המקור בלבד",
  demo: "תשובת דמו: מודל מדומה, לא מודל אמיתי",
  error: "ספק המודל אינו זמין, והתשובה מבוססת על קטעי המקור בלבד",
};

export const CLAIM_KIND_LABEL: Record<ClaimKind, string> = {
  explicit: "נאמר במסמך",
  inferred: "הסקה",
  computed: "חושב במערכת",
};

export const CLAIM_KIND_BADGE: Record<ClaimKind, string> = {
  explicit: "badge-ok",
  inferred: "badge-warn",
  computed: "badge-info",
};

/** Each abstention kind has its own heading (R22). */
export const ABSTENTION_HEADING: Record<AbstentionKind, string> = {
  not_found: "לא נמצא מידע רלוונטי במסמכים",
  not_stated: "המידע אינו נאמר במפורש במסמכים",
  not_extracted_or_verified: "הנתון טרם חולץ או טרם אומת",
  insufficient_permission_scope: "לא נמצא מידע במסמכים שבהרשאתכם",
};

export function abstentionHeading(kind: string | null | undefined): string {
  return (kind && ABSTENTION_HEADING[kind as AbstentionKind]) || "לא ניתן לענות על סמך המסמכים";
}

/** A block or region kind of a document reading (the documents page and the source panel). */
export const BLOCK_KIND_LABEL: Record<CoverageRegion["kind"], string> = {
  image: "תמונה",
  table: "טבלה",
  paragraph: "פסקה",
  heading: "כותרת",
  textbox: "תיבת טקסט",
};

/** Canonical unit codes of computed attributes (backend templates.UNIT_LABELS). */
export const UNIT_LABEL: Record<string, string> = {
  sqm: "מ״ר",
  ILS: "₪",
  "ILS/sqm": "₪ למ״ר",
  room: "חדרים",
  m: "מ׳",
  m3: "מ״ק",
  percent: "%",
  month: "חודשים",
};

export const OPERATION_LABEL: Record<string, string> = {
  count: "מספר הערכים",
  sum: "סכום",
  mean: "ממוצע",
  weighted_mean: "ממוצע משוקלל",
  median: "חציון",
  min: "ערך מינימלי",
  max: "ערך מקסימלי",
  range: "טווח",
  values: "ערכים",
};

/** A decimal string with its unit label ("12.5 מ״ר", "40 %"). Counts carry no unit. */
export function formatQuantity(value: string | null | undefined, unit: string | null | undefined): string {
  const n = formatDecimal(value);
  if (n === "—") return n;
  const label = unit ? (UNIT_LABEL[unit] ?? unit) : "";
  return label ? `${n} ${label}` : n;
}

/** The extraction coverage counts on one line; zero counts other than scope and found are left out. */
export function factCoverageLine(c: FactCoverage): string {
  const parts = [`${c.in_scope} מסמכים בתחום`, `נמצא ערך ב-${c.found}`];
  const optional: [number, string][] = [
    [c.not_stated, `${c.not_stated} ללא הנתון`],
    [c.partial_scan, `${c.partial_scan} נקרא חלקית`],
    [c.not_yet_extracted, `${c.not_yet_extracted} טרם חולצו`],
    [c.pending, `${c.pending} בחילוץ`],
    [c.failed, `${c.failed} שחילוצם נכשל`],
    [c.awaiting_review, `${c.awaiting_review} ערכים ממתינים לבדיקה`],
    [c.conflicts, `${c.conflicts} ערכים סותרים`],
    [c.unknown_metadata, `${c.unknown_metadata} ללא נתוני סינון`],
  ];
  for (const [n, text] of optional) if (n > 0) parts.push(text);
  return parts.join(" · ");
}

// ---------- Reprocessing and value statuses (KTD9, R25, R27) ----------

/** A background job's state. ``kept_previous``: a reprocess that kept the current reading (no admin action needed). */
export const JOB_STATUS_LABEL: Record<string, string> = {
  queued: "בתור",
  running: "רץ",
  done: "הסתיים",
  failed: "נכשל",
  kept_previous: "נשמרה הקריאה הקודמת",
};

/** Why a reprocess kept a version's current reading (``ingestion.reprocess_kept``). */
export interface KeptPrevious {
  reason: string | null;
  attempts?: number | null;
  at?: string | null;
}

export const KEPT_PREVIOUS_TITLE = "נשמרה הקריאה הקודמת — המסמך זמין כרגיל";

/** The reason a reprocess kept the current reading, on one line, with the attempts when it was retried. */
export function keptPreviousText(k: KeptPrevious): string {
  const reason = k.reason?.trim() || "לא נרשמה סיבה";
  const attempts = k.attempts && k.attempts > 1 ? ` (אחרי ${k.attempts} ניסיונות)` : "";
  return `${reason}${attempts}`;
}

/** The four statuses a value or measurement is shown with (R25). Each has its own text and icon, never colour alone,
 * and "checked by a person" is shown only when a person's review is recorded. */
export type ValueStatus = "auto_verified" | "human_verified" | "uncertain" | "unread";

export const VALUE_STATUS: Record<ValueStatus, { icon: string; label: string; description: string }> = {
  auto_verified: {
    icon: "✓",
    label: "נבדק אוטומטית",
    description: "הערך נמצא במקור ועבר את בדיקות המקור והמשמעות; לא נבדק על ידי אדם",
  },
  human_verified: {
    icon: "✎",
    label: "אומת או תוקן על ידי אדם",
    description: "אדם בדק את הערך ואישר או תיקן אותו",
  },
  uncertain: {
    icon: "?",
    label: "לא ודאי",
    description: "הערך נקרא בקריאה לא ודאית או שלא אומת מול הקריאה הנוכחית של המסמך",
  },
  unread: {
    icon: "∅",
    label: "לא נקרא",
    description: "האזור במסמך לא נקרא, ולכן אין לו ערך",
  },
};

/** A stored measurement's status (M#): a person's decision only when one was recorded (verified, corrected); a
 * value whose place was lost on reprocessing, or one extraction flagged, is uncertain; otherwise checked
 * automatically. */
export function measurementValueStatus(m: { status: string; anchor_lost?: boolean | null }): ValueStatus {
  if (m.anchor_lost) return "uncertain";
  if (m.status === "verified" || m.status === "corrected") return "human_verified";
  if (m.status === "needs_review") return "uncertain";
  return "auto_verified";
}

/** A value taken in chat (V#): checked automatically when it was verified in its source, uncertain when it was only
 * asserted or its source was read uncertainly, unread when its region was not read. Never a person's decision. */
export function chatValueStatus(certainty: string | null | undefined, sourceStatus?: string | null,
                                reading?: string | null): ValueStatus {
  if (sourceStatus === "unread") return "unread";
  if (certainty !== "verified") return "uncertain";
  // a value whose own region a focused re-read confirmed stays settled inside a source read uncertainly elsewhere
  if (sourceStatus === "uncertain_reading" && reading !== "clear") return "uncertain";
  return "auto_verified";
}

/** "✓ נבדק אוטומטית": the status with its icon, for a line of text. */
export function valueStatusText(s: ValueStatus): string {
  return `${VALUE_STATUS[s].icon} ${VALUE_STATUS[s].label}`;
}

// --- gaps and removals in an answer (round 7 U8) ----------------------------------------------------------------

/** One label table for what the answer UI says of a component's gap and of a removed claim: fixed Hebrew per key,
 * never the model's words. The gap reasons match the server's (`coverage.REASONS`). A claim that could not be checked
 * is said to be unchecked, never wrong. */
export const GAP_REASON_LABEL: Record<string, string> = {
  not_located: "לא אותר בחיפושים שבוצעו",
  not_in_part_read: "לא מופיע בחלק שנקרא ונבדק",
  document_not_read: "המסמך שנשאל עליו לא נקרא",
  region_not_read: "האזור הרלוונטי לא נקרא במלואו",
  limit_reached: "לא אותר במסגרת מגבלות השאלה",
  unit_not_located: "השומה שנשאלה עליה לא אותרה",
  not_verifiable: "נמצא, אך לא ניתן לאמת או להכריע",
  sources_conflict: "המקורות סותרים",
  detail_missing: "חסר פרט בבקשה",
  calculation_incomplete: "החישוב לא הושלם",
  tool_failure: "תקלה בכלי או בשירות המודל",
  instruction_not_met: "הוראה שלא קוימה",
  removed: "הוסר באימות",
};

export const FAILURE_KIND_LABEL: Record<string, string> = {
  absent_from_source: "המקורות שנבדקו אינם מציינים זאת",
  wrong_subject: "שויך לנכס או לצד אחר מזה שבמקור",
  wrong_unit: "יחידה, תקופה, בסיס שטח או מע״מ שאינם כבמקור",
  uncertain_reading: "נשען על קריאה לא ודאית של המקור",
  contradicts_source: "סותר את המקור",
  wrong_calculation: "חישוב שגוי או על קלטים שגויים",
  invalid_citation: "ציטט מקור שלא נבדק בתור הזה",
  not_checked: "לא ניתן היה לבדוק מול המקורות",
};

/** A component's status in the shown answer (older answers: missing, undeterminable). */
export const COMPONENT_STATUS_LABEL: Record<string, string> = {
  full: "ניתן במלואו",
  partial: "ניתן בחלקו",
  not_answered: "לא ניתן",
  needs_clarification: "ממתין להבהרה",
  not_relevant: "לא רלוונטי",
  missing: "חסר",
  undeterminable: "לא ניתן להכריע",
};

export const COMPONENT_KIND_LABEL: Record<string, string> = {
  information: "",
  calculation: "חישוב",
  instruction: "הוראה",
  assumption: "הנחה",
  clarification: "הבהרה",
};

export function gapReasonText(reason: string | null | undefined): string {
  return reason ? GAP_REASON_LABEL[reason] ?? "" : "";
}

/** A failure kind's label; an unknown kind reads as removed in verification, never as a specific fault. */
export function failureKindText(kind: string | null | undefined): string {
  return (kind && FAILURE_KIND_LABEL[kind]) || "הוסרה באימות";
}

// --- what verification did to a claim (round 8 U8, R23): the action, in words --------------------------------------

/** What was done to a claim: taken out of the text (`removed`), held back because its check did not complete
 * (`withheld`), a part of its sentence cut (`cut`), rewritten, kept with a qualifier, or given a citation. */
export type ChangeAction = "removed" | "withheld" | "cut" | "rewritten" | "qualified" | "cited";

/** The actions that change what the answer says; the notice above the details counts only these. */
export const CONTENT_ACTIONS: readonly ChangeAction[] = ["removed", "cut", "rewritten", "withheld"];

export const CHANGE_ACTION_LABEL: Record<ChangeAction, string> = {
  removed: "הוסרה",
  withheld: "לא נבדקה עד הסוף",
  cut: "חלק מהמשפט הושמט",
  rewritten: "נוסחה מחדש",
  qualified: "נשארה עם סייג",
  cited: "צורף ציטוט",
};

/** The action of a summary row: its own, else (a row stored before round 8) a claim the check could not complete was
 * withheld and any other was removed. */
export function rowChangeAction(row: { action?: string; failure_kind: string }): ChangeAction {
  if (row.action === "removed" || row.action === "withheld" || row.action === "cut") return row.action;
  return row.failure_kind === "not_checked" ? "withheld" : "removed";
}

/** Why a withheld claim is not in the answer: the check did not complete — never that the claim was wrong or absent. */
export const WITHHELD_CAUSE_TEXT: Record<string, string> = {
  check_failed: "הבדיקה לא הושלמה בגלל תקלה בבדיקה. הטענה לא נמצאה שגויה ולא נמצאה חסרה.",
  omitted: "הבדיקה לא החזירה תשובה לטענה זו, ולכן לא ניתן היה להשלים אותה. הטענה לא נמצאה שגויה.",
  not_classified: "הבדיקה לא סיווגה את הטענה כטענה לבדיקה, ולכן היא לא אומתה. הטענה לא נמצאה שגויה.",
  time_limit: "הבדיקה לא הושלמה במגבלת הזמן. הטענה לא נמצאה שגויה ולא נמצאה חסרה.",
};
export const WITHHELD_DEFAULT_TEXT = "הבדיקה לא הושלמה. הטענה לא נמצאה שגויה ולא נמצאה חסרה.";

/** The cut marker the server writes in the answer's text: «[N: חלק שלא אומת הושמט]». */
export const CUT_MARKER = /«\[(\d+): חלק שלא אומת הושמט\]»/g;

/** The cut marker for the Nth cut, exactly as the server writes it: «[N: חלק שלא אומת הושמט]». */
export function cutMarkerText(n: number | string): string {
  return `«[${n}: חלק שלא אומת הושמט]»`;
}

/** The default notice's sentence for the content-affecting actions it counts (`counts` by action), without the verb
 * "removed" when nothing was removed. Empty when none was. */
export function contentActionsText(counts: Partial<Record<ChangeAction, number>>): string {
  const n = (a: ChangeAction) => counts[a] ?? 0;
  const parts: string[] = [];
  if (n("removed")) parts.push(n("removed") === 1 ? "האימות הסיר טענה אחת" : `האימות הסיר ${n("removed")} טענות`);
  if (n("cut")) {
    parts.push(
      n("cut") === 1
        ? "הושמט חלק שלא אומת ממשפט אחד"
        : `הושמטו חלקים שלא אומתו מ-${n("cut")} משפטים`,
    );
  }
  if (n("rewritten")) parts.push(n("rewritten") === 1 ? "טענה אחת נוסחה מחדש" : `${n("rewritten")} טענות נוסחו מחדש`);
  if (n("withheld")) {
    parts.push(
      n("withheld") === 1
        ? "טענה אחת לא הוצגה כי בדיקתה לא הושלמה"
        : `${n("withheld")} טענות לא הוצגו כי בדיקתן לא הושלמה`,
    );
  }
  return parts.join("; ");
}

/** A claim's draft or remaining text as plain words for the details view: Markdown emphasis and code marks
 * (`**`, `__`, backticks) are dropped, the words themselves are kept exactly. */
export function plainClaimText(text: string): string {
  return text.replace(/\*\*|__|`/g, "");
}
