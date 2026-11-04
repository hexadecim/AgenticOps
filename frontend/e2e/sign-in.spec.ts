/** The one end-to-end sign-in: real browser, real gateway, real identity-service, real Postgres. Synthetic admin only. */
import { expect, test } from "@playwright/test";
import * as OTPAuth from "otpauth";

const EMAIL = "e2e-admin@opspilot.local";
const PASSWORD = "a-long-synthetic-e2e-passphrase";

test("first admin: password, authenticator enrolment, app, reload keeps the session, sign out ends it", async ({ page, context }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();

  await page.getByLabel("Work email").fill(EMAIL);
  await page.getByLabel("Password").fill("wrong password here");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();

  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Set up your authenticator" })).toBeVisible();
  await expect(page.getByAltText("QR code for your authenticator app")).toBeVisible();

  await page.getByText("Can't scan? Enter this key").click();
  const secret = (await page.locator(".secret").innerText()).trim();
  const code = new OTPAuth.TOTP({ secret: OTPAuth.Secret.fromBase32(secret) }).generate();

  await page.getByLabel("Verification code").fill(code);
  await page.getByRole("button", { name: "Verify and continue" }).click();
  await expect(page).toHaveURL(/\/admin\/users/);

  const session = (await context.cookies()).find((c) => c.name === "opspilot_session");
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Strict");
  expect(await page.evaluate(() => document.cookie)).not.toContain("opspilot_session");

  await page.reload();
  await expect(page).toHaveURL(/\/admin\/users/);

  await page.getByRole("button", { name: "Open menu" }).click();
  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
  await page.goto("/admin/users");
  await expect(page.getByRole("heading", { name: "Sign in", exact: true })).toBeVisible();
});
