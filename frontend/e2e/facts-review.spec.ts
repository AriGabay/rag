import { expect, test, type APIRequestContext } from "@playwright/test";
import { apiLogin, login, shot, USERS } from "./helpers";

// U11 / R16: the "extracted facts" tab of the review screen. Facts exist only after an extraction ran on the
// stack, so the test looks one up through the API first and skips with a reason when there is none. Approving
// changes that fact's status for good, so the test acts in office B only: office A holds the data the real-model
// evaluation reads, and an approval there would turn an unreviewed (possibly wrong) value into a verified one.

interface ListedFact {
  id: string;
  status: string;
  quote: string;
  page: number | null;
  url: string;
  document: { id: string; title: string };
}

interface FactGroup {
  attribute: { label: string; unit_options: { code: string }[] };
  documents: { facts: ListedFact[] }[];
}

async function firstReviewableFact(
  request: APIRequestContext,
  email: string,
): Promise<{ fact: ListedFact; attribute: FactGroup["attribute"] } | null> {
  await apiLogin(request, email);
  const res = await request.get("/api/review/facts");
  expect(res.ok(), `facts list: ${res.status()}`).toBeTruthy();
  const { attributes } = (await res.json()) as { attributes: FactGroup[] };
  await request.post("/api/auth/logout");
  for (const group of attributes) {
    for (const doc of group.documents) {
      // A fact with a page and a unique quote is easy to find on screen.
      const fact = doc.facts.find((f) => f.page !== null) ?? doc.facts[0];
      if (fact) return { fact, attribute: group.attribute };
    }
  }
  return null;
}

test.describe("Facts review", () => {
  test("the facts tab lists a fact with its quote and page link, and approving it updates the list", async ({
    page,
    request,
  }) => {
    const user: string = USERS.adminB;
    const found = await firstReviewableFact(request, user);
    test.skip(!found, "no needs_review or auto_validated fact in office B (office A is never written by this test)");
    const { fact, attribute } = found!;

    await login(page, user);
    await page.goto("/review");
    await page.getByRole("tab", { name: "עובדות (מנוע קודם)" }).click();
    const panel = page.getByRole("tabpanel", { name: "עובדות (מנוע קודם)" });
    const docSection = panel.getByRole("region", { name: `${attribute.label} — ${fact.document.title}` });
    const row = docSection.getByRole("article").filter({ hasText: fact.quote }).first();
    await expect(row).toBeVisible();
    await expect(row.getByText(fact.quote)).toBeVisible();
    if (fact.page !== null) {
      const link = row.getByRole("link", { name: new RegExp(`עמוד\\s*${fact.page}\\s*במקור`) });
      await expect(link).toHaveAttribute("href", new RegExp(`#page=${fact.page}$`));
    }

    // The correction form validates inline in Hebrew and offers only units of the attribute's dimension.
    if (attribute.unit_options.length > 0) {
      await row.getByRole("button", { name: "תיקון", exact: true }).click();
      const form = row.getByRole("form");
      await expect(form.getByLabel("יחידה").locator("option")).toHaveCount(attribute.unit_options.length);
      await form.getByLabel("ערך מתוקן").fill("abc");
      await form.getByRole("button", { name: "שמירת התיקון" }).click();
      await expect(form.getByText("יש להזין מספר תקין")).toBeVisible();
      await form.getByRole("button", { name: "ביטול" }).click();
    }
    await page.screenshot({ path: shot("review-facts-tab.png"), fullPage: true });

    await row.getByRole("button", { name: "אישור", exact: true }).click();
    await expect(panel.getByRole("status").filter({ hasText: "הערך אושר." })).toBeVisible();
    await expect(docSection.getByRole("article").filter({ hasText: fact.quote })).toHaveCount(0);

    await apiLogin(request, user);
    const detail = await (await request.get(`/api/review/facts/${fact.id}`)).json();
    expect(detail.status).toBe("verified");
    await request.post("/api/auth/logout");
  });

  test("an unknown or invisible fact is a 404 for an employee", async ({ request }) => {
    await apiLogin(request, USERS.dana);
    const missing = "00000000-0000-0000-0000-000000000000";
    expect((await request.get(`/api/review/facts/${missing}`)).status()).toBe(404);
    expect((await request.post(`/api/review/facts/${missing}/approve`, { data: {} })).status()).toBe(404);
    await request.post("/api/auth/logout");
  });
});
