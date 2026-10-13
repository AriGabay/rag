import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { anchor, answerWith, DOC, NOW, openAnswer, source, TITLE, VER, type Json } from "./answer-fixtures";
import { citationChip, login, sourceViewer, USERS } from "./helpers";

// Round 10 U10: the kind chosen at upload, what a standard states about itself with the source of each value, the
// admin's kind change, and a standard's citation. Every payload is fixed and synthetic (invented standard numbers,
// titles and quotes); the specs check how the screens show it, not what the server derives. Only the office B admin
// logs in, as in the other UI specs, and nothing is uploaded or changed on the server: the requests that would are
// answered here.

const GROUP = { id: "00000000-0000-0000-0000-0000000000b1", name: "קבוצה סינתטית" };
const STD_DOC = "00000000-0000-0000-0000-0000000010a1";
const PENDING_DOC = "00000000-0000-0000-0000-0000000010a2";
const COPY_DOC = "00000000-0000-0000-0000-0000000010a3";
const APPRAISAL_DOC = "00000000-0000-0000-0000-0000000010a4";
const LISTING = /\/api\/documents(\?.*)?$/;

const STD = {
  number: "77.3",
  title: "תקן סינתטי לבדיקת ממשק",
  edition: "גרסה 2.0",
  effective: "05.10.2025",
  section: "2.1",
  section_title: "גישת ההשוואה",
};
const STD_LABEL = "תקן 77.3 (גרסה 2.0) — סעיף 2.1";
const QUOTE = "תקן 77.3 (סינתטי) — גרסה 2.0, בתוקף מיום 05.10.2025";

function field(value: string | null, page = 1, quote = QUOTE): Json {
  return value === null
    ? { value: null, status: "unknown", method: null, page: null, block: null, quote: null }
    : { value, status: "stated", method: "pattern", page, block: 2, quote };
}

function profile(extra: Json = {}): Json {
  return {
    number: field("77.3", 1, "תקן 77.3 (סינתטי)"),
    title: field("תקן סינתטי לבדיקת ממשק", 1, "תקן סינתטי לבדיקת ממשק"),
    issuer: field(null),
    edition: field("גרסה 2.0", 1, "גרסה 2.0"),
    published: field(null),
    effective: field("05.10.2025", 2, "בתוקף מיום 05.10.2025"),
    scope: field(null),
    amends: { ...field(null), standard: null, relation: null },
    ...extra,
  };
}

const NO_COPIES = { same_text_as: null, copies: [] };

function version(filename: string): Json {
  return { id: VER, version_no: 1, filename, status: "ready", status_reason: null, page_count: 4, pages_incomplete: null,
    records_total: 0, records_needing_review: 0, is_current: true, created_at: NOW, processed_at: NOW,
    mime_type: "application/pdf", retriable: false };
}

function row(id: string, title: string, kind: string, professional?: Json): Json {
  return { id, title, source_kind: kind, group: GROUP, deleted: false, created_at: NOW, current_version: version(`${id}.pdf`),
    versions_count: 1, ...(professional ? { professional } : {}) };
}

function documents(): Json[] {
  return [
    row(STD_DOC, "תקן סינתטי 77.3", "standard", {
      state: "ready", profile: profile(), duplicates: NO_COPIES,
      editions: { number: "77.3", order_known: false, related: [],
        editions: [
          { document_id: STD_DOC, title: "תקן סינתטי 77.3", edition: "גרסה 2.0", published: null, effective: "05.10.2025", current: true },
          { document_id: COPY_DOC, title: "תקן סינתטי 77.3 (נוסח קודם)", edition: "גרסה 1.0", published: null, effective: null, current: false },
        ] },
    }),
    row(PENDING_DOC, "מסמך מקצועי סינתטי בחילוץ", "professional", {
      state: "pending", profile: null, editions: null, duplicates: NO_COPIES,
    }),
    row(COPY_DOC, "תקן סינתטי 77.3 (עותק)", "standard", {
      state: "ready", profile: profile(), editions: null,
      duplicates: { same_text_as: { document_id: STD_DOC, title: "תקן סינתטי 77.3" }, copies: [] },
    }),
    row(APPRAISAL_DOC, "שומה סינתטית לבדיקת סוגים", "appraisal"),
  ];
}

/** Serves the documents list (and one document's details, and its kind change when `patch` is given) from fixed
 * payloads. The admin session is the real one; `employee` makes the session an employee's. */
async function serveDocuments(
  page: Page,
  opts: { docs?: Json[]; employee?: boolean; patch?: (route: Route, body: { source_kind: string }) => Promise<void> } = {},
): Promise<{ docs: Json[]; patches: string[] }> {
  const docs = opts.docs ?? documents();
  const patches: string[] = [];
  if (opts.employee) {
    await page.route("**/api/auth/me", async (route) => {
      const real = await route.fetch();
      const me = await real.json();
      await route.fulfill({ json: { ...me, user: { ...me.user, role: "employee", can_upload: false } } });
    });
  }
  await page.route(LISTING, (route) => route.fulfill({ json: { documents: docs } }));
  await page.route(/\/api\/documents\/[0-9a-f-]{36}$/, async (route) => {
    const request = route.request();
    const id = request.url().split("/").pop();
    if (request.method() === "PATCH" && opts.patch) {
      const body = request.postDataJSON() as { source_kind: string };
      patches.push(body.source_kind);
      await opts.patch(route, body);
      return;
    }
    const doc = docs.find((d) => d.id === id);
    await route.fulfill(doc ? { json: { ...doc, versions: [version(`${id}.pdf`)] } } : { status: 404, json: { detail: "לא נמצא" } });
  });
  return { docs, patches };
}

function listRow(page: Page, title: string) {
  return page.getByRole("table", { name: "רשימת מסמכים" }).getByRole("row").filter({ hasText: title });
}

test.describe("standard citations", () => {
  test("a standard source shows its number, edition and section on the chip and in the viewer header", async ({ page }) => {
    const markdown = "יש להשוות לפי הוראות התקן [S1].\n\nשווי הנכס בשומה הוא 9,500 ₪ [S2].";
    const { id, reply } = await openAnswer(
      page,
      answerWith({
        markdown,
        status: "answered",
        gaps: [],
        components: [],
        sources: [
          source("S1", {
            title: "תקן 77.3 (גרסה 2.0, בתוקף מ-05.10.2025)", location: "סעיף 2.1, עמוד 3", source_kind: "standard",
            standard: STD, anchor: { ...anchor(), v: 2, standard: STD },
          }),
          source("S2", { text: "שיעור ההיוון החודשי הוא 0.6%.", source_kind: "appraisal", standard: null }),
        ],
        documents: [{ document_id: DOC, title: TITLE }],
      }),
    );

    const chip = citationChip(reply, markdown, "S1");
    await expect(chip).toHaveAttribute("title", STD_LABEL);
    await expect(chip).toHaveAttribute("aria-label", `מקור 1: ${STD_LABEL}`);
    // an appraisal's chip keeps its title and location
    await expect(citationChip(reply, markdown, "S2")).toHaveAttribute("title", `${TITLE} — עמוד 3`);

    await chip.click();
    const viewer = sourceViewer(page);
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-standard")).toHaveText(STD_LABEL);
    await page.keyboard.press("Escape");

    await citationChip(reply, markdown, "S2").click();
    await expect(sourceViewer(page)).toBeVisible();
    await expect(sourceViewer(page).getByTestId("viewer-standard")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

test.describe("upload by kind", () => {
  async function uploadWith(page: Page, choose?: string): Promise<string> {
    await login(page, USERS.adminB);
    await serveDocuments(page);
    let body = "";
    await page.route(LISTING, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      body = (await route.request().postDataBuffer())?.toString("utf8") ?? "";
      await route.fulfill({ json: { results: [{ filename: "synthetic.pdf", status: "accepted", document_id: STD_DOC, version_id: VER }] } });
    });
    await page.goto("/documents");
    const panel = page.getByRole("region", { name: "העלאת מסמכים" });
    const kinds = panel.getByRole("radiogroup");
    await expect(kinds.getByRole("radio")).toHaveCount(3);
    await expect(kinds.getByRole("radio", { name: "שומה" })).toBeChecked();
    await expect(panel.getByText("הסוג נקבע כאן ואינו נלמד משם הקובץ.")).toBeVisible();
    if (choose) await kinds.getByRole("radio", { name: choose }).check();
    await page.getByLabel("בחירת קבצים להעלאה").setInputFiles({
      name: "synthetic.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 synthetic"),
    });
    await expect(panel.getByTestId("upload-kind-summary")).toContainText(choose ?? "שומה");
    await panel.getByRole("button", { name: "העלאה", exact: true }).click();
    await expect(page.getByRole("region", { name: "תוצאות ההעלאה" })).toBeVisible();
    return body;
  }

  test("a file is uploaded as an appraisal unless another kind is chosen", async ({ page }) => {
    const body = await uploadWith(page);
    expect(body).toMatch(/name="source_kind"\r\n\r\nappraisal\r\n/);
  });

  test("the chosen kind is sent with the batch", async ({ page }) => {
    const body = await uploadWith(page, "תקן שמאות");
    expect(body).toMatch(/name="source_kind"\r\n\r\nstandard\r\n/);
  });

  test("a professional document is sent as one", async ({ page }) => {
    const body = await uploadWith(page, "מסמך מקצועי");
    expect(body).toMatch(/name="source_kind"\r\n\r\nprofessional\r\n/);
  });
});

test.describe("documents screen", () => {
  test("the list shows the kind badge, what a standard states, 'unknown', its editions and its copies; pending is not unknown", async ({
    page,
  }) => {
    await login(page, USERS.adminB);
    await serveDocuments(page);
    await page.goto("/documents");

    const standard = listRow(page, "תקן סינתטי 77.3").filter({ hasNotText: "(עותק)" }).first();
    await expect(standard.getByTestId("kind-badge")).toHaveText("תקן שמאות");
    await expect(standard.getByTestId("professional-number")).toContainText("77.3");
    await expect(standard.getByTestId("professional-edition")).toContainText("גרסה 2.0");
    await expect(standard.getByTestId("professional-effective")).toContainText("05.10.2025");
    // the finished extraction did not find the publication date
    await expect(standard.getByTestId("professional-published")).toContainText("לא ידוע");
    // editions: this one and the earlier; the stated dates do not order them
    await expect(standard.getByTestId("professional-editions")).toContainText("תקן סינתטי 77.3 (נוסח קודם)");
    await expect(standard.getByTestId("professional-editions")).toContainText("סדר הנוסחים לא ידוע");

    const copy = listRow(page, "תקן סינתטי 77.3 (עותק)");
    await expect(copy.getByTestId("professional-same-text")).toContainText("אותו נוסח כמו תקן סינתטי 77.3");

    // derivation still pending: its own text, and no "unknown" value
    const pending = listRow(page, "מסמך מקצועי סינתטי בחילוץ");
    await expect(pending.getByTestId("kind-badge")).toHaveText("מסמך מקצועי");
    await expect(pending.getByTestId("professional-pending")).toHaveText("הנתונים בחילוץ");
    await expect(pending).not.toContainText("לא ידוע");

    // an appraisal shows its badge and no standard block
    const appraisal = listRow(page, "שומה סינתטית לבדיקת סוגים");
    await expect(appraisal.getByTestId("kind-badge")).toHaveText("שומה");
    await expect(appraisal.getByTestId("professional")).toHaveCount(0);
  });

  test("the kind filter keeps the chosen kind", async ({ page }) => {
    await login(page, USERS.adminB);
    await serveDocuments(page);
    await page.goto("/documents");
    await page.getByLabel("סינון לפי סוג").selectOption("professional");
    await expect(listRow(page, "מסמך מקצועי סינתטי בחילוץ")).toHaveCount(1);
    await expect(listRow(page, "תקן סינתטי 77.3")).toHaveCount(0);
    await expect(listRow(page, "שומה סינתטית")).toHaveCount(0);
    await page.getByLabel("סינון לפי סוג").selectOption("");
    await expect(listRow(page, "שומה סינתטית")).toHaveCount(1);
  });

  test("a stated value's source opens with the keyboard (Enter and Space) and by a click, with its page and quote", async ({
    page,
  }) => {
    await login(page, USERS.adminB);
    await serveDocuments(page);
    await page.goto("/documents");

    const standard = listRow(page, "תקן סינתטי 77.3").filter({ hasNotText: "(עותק)" }).first();
    const toggle = standard.getByRole("button", { name: "מקור מספר" });
    const quote = standard.getByTestId("professional-number").locator(".source-quote");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(quote).toBeHidden();

    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(quote).toBeVisible();
    await expect(quote).toContainText("עמוד 1");
    await expect(quote).toContainText("תקן 77.3 (סינתטי)");

    await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(quote).toBeHidden();

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(quote).toBeVisible();
    // the effective date's source is on another page
    await standard.getByRole("button", { name: "מקור תאריך תחילה" }).click();
    await expect(standard.getByTestId("professional-effective").locator(".source-quote")).toContainText("עמוד 2");
  });

  test("the mobile viewport has no horizontal scroll", async ({ page }) => {
    await login(page, USERS.adminB);
    await serveDocuments(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto("/documents");
    await expect(listRow(page, "תקן סינתטי 77.3").first()).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    // and with a standard's details open
    await listRow(page, "תקן סינתטי 77.3").first().getByRole("button", { name: "תקן סינתטי 77.3", exact: true }).click();
    await expect(page.getByRole("region", { name: "פרטי מסמך" })).toBeVisible();
    const overflowOpen = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflowOpen).toBeLessThanOrEqual(0);
  });
});

test.describe("kind change", () => {
  async function openStandard(page: Page): Promise<Locator> {
    await page.goto("/documents");
    await listRow(page, "תקן סינתטי 77.3").filter({ hasNotText: "(עותק)" }).first()
      .getByRole("button", { name: "תקן סינתטי 77.3", exact: true }).click();
    const panel = page.getByRole("region", { name: "פרטי מסמך" });
    await expect(panel.getByTestId("kind-badge")).toHaveText("תקן שמאות");
    return panel;
  }

  test("a refused change shows the server's reason and keeps the badge", async ({ page }) => {
    const reason = "לא ניתן להשלים את עיבוד השומה מהקריאה השמורה של המסמך, ולכן סוג המקור לא שונה";
    await login(page, USERS.adminB);
    const served = await serveDocuments(page, {
      patch: (route) => route.fulfill({ status: 409, json: { detail: reason } }),
    });
    const panel = await openStandard(page);

    await panel.getByLabel("שינוי סוג המסמך:").selectOption("appraisal");
    await panel.getByRole("button", { name: "שינוי סוג" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("לא יימחק דבר והמסמך לא ייקרא מחדש");
    await dialog.getByRole("button", { name: "אישור השינוי" }).click();

    await expect(panel.getByRole("alert")).toContainText(reason);
    await expect(panel.getByTestId("kind-badge")).toHaveText("תקן שמאות");
    expect(served.patches).toEqual(["appraisal"]);
  });

  test("the change asks first, shows a busy state, and the badge changes after it", async ({ page }) => {
    await login(page, USERS.adminB);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const served = await serveDocuments(page, {
      patch: async (route, body) => {
        await gate;
        const doc = served.docs.find((d) => d.id === STD_DOC)!;
        doc.source_kind = body.source_kind;
        await route.fulfill({ json: { id: STD_DOC, source_kind: body.source_kind } });
      },
    });
    const panel = await openStandard(page);

    await panel.getByLabel("שינוי סוג המסמך:").selectOption("professional");
    await panel.getByRole("button", { name: "שינוי סוג" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("לא יימחק דבר והמסמך לא ייקרא מחדש");
    // cancelling changes nothing
    await dialog.getByRole("button", { name: "ביטול" }).click();
    expect(served.patches).toEqual([]);

    await panel.getByRole("button", { name: "שינוי סוג" }).click();
    await dialog.getByRole("button", { name: "אישור השינוי" }).click();
    await expect(dialog.getByRole("button", { name: "משנה..." }).first()).toBeDisabled();
    await expect(panel.getByTestId("kind-badge")).toHaveText("תקן שמאות");
    release();

    await expect(panel.getByTestId("kind-badge")).toHaveText("מסמך מקצועי");
    expect(served.patches).toEqual(["professional"]);
    await expect(listRow(page, "תקן סינתטי 77.3").filter({ hasNotText: "(עותק)" }).first().getByTestId("kind-badge"))
      .toHaveText("מסמך מקצועי");
  });

  test("an employee does not see the control", async ({ page }) => {
    await login(page, USERS.adminB);
    await serveDocuments(page, { employee: true });
    await page.goto("/documents");
    await listRow(page, "תקן סינתטי 77.3").filter({ hasNotText: "(עותק)" }).first()
      .getByRole("button", { name: "תקן סינתטי 77.3", exact: true }).click();
    const panel = page.getByRole("region", { name: "פרטי מסמך" });
    await expect(panel.getByTestId("kind-badge")).toHaveText("תקן שמאות");
    await expect(panel.getByTestId("kind-control")).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "שינוי סוג" })).toHaveCount(0);
  });
});
