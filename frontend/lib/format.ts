// Display formatting. Decimal strings are formatted directly (Intl accepts exact numeric strings),
// so no float arithmetic touches money or area. Nothing here aggregates values.

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
