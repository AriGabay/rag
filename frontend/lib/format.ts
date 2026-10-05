// Display formatting. Decimal strings are formatted directly (Intl accepts exact numeric strings),
// so no float arithmetic touches money or area. Nothing here aggregates values.

import type { EffectiveProvider, Provider, VersionStatus } from "./types";

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
  mock: "מודל מדומה",
  cloud: "מודל ענן",
  extractive: "חילוץ מהמקורות",
};

export const EFFECTIVE_PROVIDER_LABEL: Record<EffectiveProvider, string> = {
  cloud: "מודל ענן פעיל",
  enabled_no_key: "מופעל, אך לא הוגדר מפתח בשרת — המערכת עונה ללא מודל ענן",
  demo_mock: "מצב דמו: מודל מדומה בלבד",
  extractive: "תשובות מחולצות מהמקורות בלבד (ללא מודל ענן)",
};

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
