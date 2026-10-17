import { expect, test } from "@playwright/test";
import { login, USERS } from "./helpers";
import type { FactReviewGroup } from "../lib/types";

// #39: approve/reject/correct carry the status and value the row showed; when the fact changed meanwhile the server
// answers 409 and the tab reloads the list with a notice. The facts API is mocked (only the login is real), so the
// stale state is forced deterministically.

const FACT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const STALE_DETAIL = "הערך השתנה בינתיים; טענו את הרשימה מחדש";

const GROUP: FactReviewGroup = {
  attribute: {
    id: "a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1",
    label: "שטח ממ״ד",
    value_type: "numeric",
    unit_dimension: "area",
    canonical_unit: "sqm",
    canonical_unit_label: "מ״ר",
    unit_options: [{ code: "sqm", label: "מ״ר" }],
    facts_version: 3,
  },
  documents: [
    {
      document: { id: "d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1", title: "דוח בדיקה" },
      version_id: "e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1",
      facts: [
        {
          id: FACT_ID,
          status: "needs_review",
          value: "12",
          unit: "sqm",
          unit_label: "מ״ר",
          original: { value_text: "12", unit: "sqm", unit_label: "מ״ר" },
          quote: "שטח הממ״ד 12 מ״ר",
          page: null,
          url: "",
          document: { id: "d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1", title: "דוח בדיקה" },
          version_id: "e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1",
          entity_role: "subject",
          entity_descriptor: null,
          review_note: null,
          reviewed_at: null,
          previous: [],
          conflicts: [],
        },
      ],
    },
  ],
};

const path = (url: string) => new URL(url).pathname;

for (const action of ["approve", "correct"] as const) {
  test(`a stale ${action} (409) reloads the facts list with a notice`, async ({ page }) => {
    let lists = 0;
    const sent: Record<string, unknown>[] = [];
    await page.route(
      (url) => path(url.toString()) === "/api/review/facts",
      async (route) => {
        lists += 1;
        await route.fulfill({ json: { attributes: lists === 1 ? [GROUP] : [] } });
      },
    );
    await page.route(
      (url) => path(url.toString()) === `/api/review/facts/${FACT_ID}/${action}`,
      async (route) => {
        sent.push(route.request().postDataJSON() as Record<string, unknown>);
        await route.fulfill({ status: 409, json: { detail: STALE_DETAIL } });
      },
    );

    await login(page, USERS.adminA);
    await page.goto("/review");
    await page.getByRole("tab", { name: "עובדות (מנוע קודם)" }).click();
    const panel = page.getByRole("tabpanel", { name: "עובדות (מנוע קודם)" });
    const row = panel.getByRole("article").filter({ hasText: "שטח הממ״ד 12 מ״ר" });
    await expect(row).toBeVisible();

    if (action === "approve") {
      await row.getByRole("button", { name: "אישור", exact: true }).click();
    } else {
      await row.getByRole("button", { name: "תיקון", exact: true }).click();
      await row.getByLabel("ערך מתוקן").fill("14");
      await row.getByRole("button", { name: "שמירת התיקון" }).click();
    }

    await expect(panel.getByRole("status").filter({ hasText: "הערך השתנה בינתיים; הרשימה נטענה מחדש." })).toBeVisible();
    await expect(row).toHaveCount(0);
    await expect(panel.getByText("אין עובדות שממתינות לבדיקה")).toBeVisible();
    expect(lists).toBe(2);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ expected_status: "needs_review", expected_value: "12" });
  });
}
