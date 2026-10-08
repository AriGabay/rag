import { expect, test } from "@playwright/test";
import { deleteOfficeBDocsByTitle, fixture, login, setOfficeBCloud, shot, USERS } from "./helpers";

// The reading report on the documents screen (U6), office B only: a synthetic PDF whose raster table cannot be read
// without the vision model (office B's cloud use is off) is shown as partly read, and its per-page details name the
// page, the region and the reason. Office A data is never modified.
const FILE = "regions/R1_synthetic_raster_table.pdf";
const TITLE = "R1 synthetic raster table";

test.describe.configure({ mode: "serial" });

test.describe("reading report (office B)", () => {
  test.beforeAll(async ({ request }) => {
    // Without the vision model the table region stays unread, whatever OCR is installed.
    await setOfficeBCloud(request, false);
    // Upload dedup is by content hash, so a leftover copy from an earlier run is removed first.
    await deleteOfficeBDocsByTitle(request, TITLE);
  });

  test("a partly read PDF shows the partial badge and its per-page reasons, opened with the keyboard", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    await login(page, USERS.adminB);
    await page.goto("/documents");

    await page.getByLabel("בחירת קבצים להעלאה").setInputFiles(fixture(FILE));
    await page.getByRole("button", { name: "העלאה", exact: true }).click();
    const results = page.getByRole("region", { name: "תוצאות ההעלאה" });
    await expect(results.getByRole("status", { name: /R1_synthetic_raster_table\.pdf: התקבל לעיבוד/ })).toBeVisible();

    // Admins also see logically deleted copies from earlier runs (badge "נמחק"); only the live row counts.
    const row = page
      .getByRole("table", { name: "רשימת מסמכים" })
      .getByRole("row")
      .filter({ hasText: TITLE })
      .filter({ hasNotText: "נמחק" });
    await expect(row).toHaveCount(1);
    await expect(row.getByRole("status", { name: /^סטטוס: (מוכן|דורש בדיקה)$/ })).toBeVisible({ timeout: 240_000 });

    // The badge, not a hover tooltip: the reasons are in a list the user opens.
    await expect(row.getByText("נקרא חלקית", { exact: true })).toBeVisible();
    await expect(row.getByText("נקרא במלואו", { exact: true })).toHaveCount(0);
    const toggle = row.getByRole("button", { name: "פירוט לפי עמוד" });
    const details = row.getByRole("list", { name: "פירוט הקריאה לפי עמוד" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(details).toBeHidden();

    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(details).toBeVisible();
    const page1 = details.getByRole("listitem").filter({ hasText: "עמוד 1" }).first();
    await expect(page1).toBeVisible();
    // the table region: its kind, its status and a Hebrew reason (OCR-dependent wording, so only its presence)
    await expect(page1).toContainText(/תמונה – לא נקרא/);
    await expect(page1.getByRole("listitem").first()).toContainText(/:\s*\S+/);
    await page.screenshot({ path: shot("documents-reading-details.png"), fullPage: true });

    await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(details).toBeHidden();
  });

  test("the unread region opens as an image for the office's user, through the page's session", async ({ page }) => {
    await login(page, USERS.adminB);
    const { documents } = (await (await page.request.get(`/api/documents?q=${encodeURIComponent(TITLE)}`)).json()) as {
      documents: { id: string; title: string; deleted: boolean; current_version: { id: string } | null }[];
    };
    const doc = documents.find((d) => d.title === TITLE && !d.deleted);
    expect(doc?.current_version, "the document uploaded by the previous test").toBeTruthy();
    const base = `/api/documents/${doc!.id}/versions/${doc!.current_version!.id}`;
    const { blocks } = (await (await page.request.get(`${base}/blocks`)).json()) as {
      blocks: { index: number; status: string; region_url?: string }[];
    };
    const unread = blocks.find((b) => b.status === "unread" && b.region_url);
    expect(unread, "an unread region with a region view").toBeTruthy();
    const image = await page.request.get(unread!.region_url!);
    expect(image.status()).toBe(200);
    expect(image.headers()["content-type"]).toBe("image/png");
    expect((await page.request.get(`${base}/pages/999/image`)).status()).toBe(404);
  });
});
