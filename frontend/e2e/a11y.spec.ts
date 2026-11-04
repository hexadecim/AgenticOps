/** Accessibility audit (WCAG 2.1 AA, axe-core) of every screen, in Chrome, in both themes. API answers are the
 * synthetic fixtures, so every screen shows content. Contrast is checked here because jsdom cannot. */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { respond, user } from "../src/test/fixtures";
import type { Role } from "../src/api/types";

const SCREENS: { path: string; role: Role | null; ready: string }[] = [
  { path: "/sign-in", role: null, ready: "Sign in" },
  { path: "/legal/privacy", role: null, ready: "Privacy" },
  { path: "/legal/imprint", role: null, ready: "Imprint" },
  { path: "/legal/accessibility", role: null, ready: "Accessibility" },
  { path: "/approvals", role: "support_agent", ready: "Uploads fail at site Berlin" },
  { path: "/approvals/ap-ticket", role: "support_agent", ready: "Uploads fail" },
  { path: "/approvals/ap-fix", role: "support_agent", ready: "upload-worker" },
  { path: "/approvals/ap-message", role: "support_agent", ready: "No promises or ETAs" },
  { path: "/dashboard", role: "support_manager", ready: "KAN-1" },
  { path: "/audit", role: "compliance_lead", ready: "Approved ticket" },
  { path: "/privacy", role: "compliance_lead", ready: "[EMAIL_1]" },
  { path: "/admin/users", role: "admin", ready: "max@example.test" },
  { path: "/admin/integrations", role: "admin", ready: "Language model" },
  { path: "/admin/system", role: "admin", ready: "ollama" },
];

async function mockApi(page: Page, role: Role | null) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, "");
    if (path === "/events") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    if (!role) return route.fulfill({ status: 401, json: { code: "unauthorized", message: "Not signed in" } });
    const body = respond(path + url.search, user(role));
    return body === undefined
      ? route.fulfill({ status: 404, json: { code: "not_found", message: path } })
      : route.fulfill({ status: 200, json: body });
  });
}

for (const theme of ["light", "dark"] as const) {
  for (const screen of SCREENS) {
    test(`${screen.path} (${theme})`, async ({ page }) => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.addInitScript((t) => localStorage.setItem("opspilot-theme", t), theme);
      await mockApi(page, screen.role);
      await page.goto(screen.path);
      await expect(page.getByText(screen.ready).first()).toBeVisible();
      await page.waitForTimeout(400);
      expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      const found = results.violations.map((v) => ({
        rule: v.id,
        impact: v.impact,
        nodes: v.nodes.slice(0, 4).map((n) => `${n.target.join(" ")} :: ${n.failureSummary?.split("\n")[1]?.trim() ?? ""}`),
      }));
      expect(found).toEqual([]);
    });
  }
}

test("the audit does find faults: a planted low-contrast text and an unlabelled field fail", async ({ page }) => {
  await mockApi(page, null);
  await page.goto("/sign-in");
  await expect(page.getByText("Sign in").first()).toBeVisible();
  await page.evaluate(() => {
    const main = document.querySelector("main");
    main?.insertAdjacentHTML(
      "beforeend",
      '<p style="color:#ccc;background:#fff">planted faint text</p><input type="text" id="planted" />',
    );
  });
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const rules = results.violations.map((v) => v.id);
  expect(rules).toContain("color-contrast");
  expect(rules).toContain("label");
});

for (const screen of SCREENS) {
  test(`${screen.path} reflows at 320 px without sideways scrolling (WCAG 1.4.10)`, async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 640 });
    await mockApi(page, screen.role);
    await page.goto(screen.path);
    await expect(page.getByText(screen.ready).first()).toBeAttached();
    await page.waitForTimeout(300);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test(`${screen.path}: keyboard focus is visible on every stop (WCAG 2.4.7)`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await mockApi(page, screen.role);
    await page.goto(screen.path);
    await expect(page.getByText(screen.ready).first()).toBeVisible();
    const invisible: string[] = [];
    let stops = 0;
    for (let i = 0; i < 25; i += 1) {
      await page.keyboard.press("Tab");
      const stop = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        if (!el || el === document.body) return null;
        const style = getComputedStyle(el);
        const hasRing =
          (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0) ||
          (style.boxShadow !== "none" && style.boxShadow !== "");
        return { name: `${el.tagName} ${el.textContent?.trim().slice(0, 30) ?? ""}`, hasRing };
      });
      if (!stop) break;
      stops += 1;
      if (!stop.hasRing) invisible.push(stop.name);
    }
    expect(stops).toBeGreaterThan(0);
    expect(invisible).toEqual([]);
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`the open menu has no violations (${theme})`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript((t) => localStorage.setItem("opspilot-theme", t), theme);
    await mockApi(page, "admin");
    await page.goto("/admin/users");
    await page.getByRole("button", { name: "Open menu" }).click();
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
    await page.waitForTimeout(400);
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.map((v) => `${v.id}: ${v.nodes[0]?.target.join(" ")}`)).toEqual([]);
  });
}

test("the closed menu cannot take keyboard focus (it is off screen)", async ({ page }) => {
  await mockApi(page, "admin");
  await page.goto("/admin/users");
  await expect(page.getByText("max@example.test")).toBeVisible();
  const hidden: string[] = [];
  for (let i = 0; i < 30; i += 1) {
    await page.keyboard.press("Tab");
    const inDrawer = await page.evaluate(() => {
      const el = document.activeElement;
      return el?.closest("#op-drawer") ? (el.textContent ?? "") : null;
    });
    if (inDrawer !== null) hidden.push(inDrawer);
  }
  expect(hidden).toEqual([]);
});

test("the menu opens and closes with the keyboard, and Escape closes it", async ({ page }) => {
  await mockApi(page, "admin");
  await page.goto("/admin/users");
  const opener = page.getByRole("button", { name: "Open menu" });
  await opener.focus();
  await page.keyboard.press("Enter");
  await expect(opener).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(opener).toHaveAttribute("aria-expanded", "false");
  await expect(opener).toBeFocused();
});
