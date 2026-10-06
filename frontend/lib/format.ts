// Display formatting. Decimal strings are formatted directly (Intl accepts exact numeric strings),
// so no float arithmetic touches money or area. Nothing here aggregates values.

import type { Provider, ProviderMode, ProviderStatus, VersionStatus } from "./types";

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
