import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { user } from "../test/fixtures";
import { mockApi, renderAt } from "../test/render";

const challenge = { challenge_token: "ch", name: "Lena", mfa_enrolled: true, enrolment: null, password_change_required: false };

async function submitPassword() {
  await userEvent.type(await screen.findByLabelText("Work email"), "lena@example.test");
  await userEvent.type(screen.getByLabelText("Password"), "a long password");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("sign-in", () => {
  it("asks for the code after the password and then opens the app", async () => {
    let signedIn = false;
    const calls = mockApi(null, (path) => {
      if (path === "/auth/sign-in") return { status: 200, body: challenge };
      if (path === "/auth/sign-in/mfa") { signedIn = true; return { status: 200, body: { user: user("l1_analyst"), expires_in: 1800 } }; }
      if (signedIn && path === "/auth/me") return { status: 200, body: user("l1_analyst") };
      return undefined;
    });
    renderAt("/sign-in");
    await submitPassword();
    await userEvent.type(await screen.findByLabelText("Verification code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify and continue" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/auth/sign-in/mfa")).toBe(true));
    expect(calls.find((c) => c.path === "/auth/sign-in/mfa")?.body).toMatchObject({ challenge_token: "ch", code: "123456" });
    expect(await screen.findByRole("heading", { name: "Approvals" })).toBeInTheDocument();
  });

  it("clears the password from the form once it has been used", async () => {
    mockApi(null, (path) => (path === "/auth/sign-in" ? { status: 200, body: challenge } : undefined));
    renderAt("/sign-in");
    await submitPassword();
    await screen.findByLabelText("Verification code");
    await userEvent.click(screen.getByRole("button", { name: /different account/ }));
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("shows the server's message for a wrong password", async () => {
    mockApi(null, (path) => (path === "/auth/sign-in" ? { status: 401, body: { code: "invalid", message: "Email or password is wrong." } } : undefined));
    renderAt("/sign-in");
    await submitPassword();
    expect(await screen.findByRole("alert")).toHaveTextContent("Email or password is wrong.");
  });

  it("does not call the server without an email", async () => {
    const calls = mockApi(null);
    renderAt("/sign-in");
    await userEvent.type(await screen.findByLabelText("Password"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your work email");
    expect(calls.some((c) => c.path === "/auth/sign-in")).toBe(false);
  });

  it("refuses a code that is not six digits without calling the server", async () => {
    const calls = mockApi(null, (path) => (path === "/auth/sign-in" ? { status: 200, body: challenge } : undefined));
    renderAt("/sign-in");
    await submitPassword();
    await userEvent.type(await screen.findByLabelText("Verification code"), "12ab");
    await userEvent.click(screen.getByRole("button", { name: "Verify and continue" }));
    expect(screen.getByRole("alert")).toHaveTextContent("6-digit code");
    expect(calls.some((c) => c.path === "/auth/sign-in/mfa")).toBe(false);
  });

  it("offers enrolment on the first sign-in, with the key as a fallback", async () => {
    const enrolling = { ...challenge, mfa_enrolled: false, enrolment: { secret: "SYNTHETICSECRET", otpauth_uri: "otpauth://totp/x?secret=SYNTHETICSECRET" } };
    mockApi(null, (path) => (path === "/auth/sign-in" ? { status: 200, body: enrolling } : undefined));
    renderAt("/sign-in");
    await submitPassword();
    expect(await screen.findByRole("heading", { name: "Set up your authenticator" })).toBeInTheDocument();
    expect(screen.getByText("SYNTHETICSECRET")).toBeInTheDocument();
  });

  it("asks for a new password after a temporary one and refuses a mismatch", async () => {
    const calls = mockApi(null, (path) => (path === "/auth/sign-in" ? { status: 200, body: { ...challenge, password_change_required: true } } : undefined));
    renderAt("/sign-in");
    await submitPassword();
    await userEvent.type(await screen.findByLabelText("New password"), "one long password");
    await userEvent.type(screen.getByLabelText("Repeat new password"), "another long password");
    await userEvent.type(screen.getByLabelText("Verification code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify and continue" }));
    expect(screen.getByRole("alert")).toHaveTextContent("do not match");
    expect(calls.some((c) => c.path === "/auth/sign-in/mfa")).toBe(false);
  });

  it("goes back to the password when the challenge expired", async () => {
    mockApi(null, (path) => {
      if (path === "/auth/sign-in") return { status: 200, body: challenge };
      if (path === "/auth/sign-in/mfa") return { status: 401, body: { code: "challenge_expired", message: "Start again." } };
      return undefined;
    });
    renderAt("/sign-in");
    await submitPassword();
    await userEvent.type(await screen.findByLabelText("Verification code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify and continue" }));
    expect(await screen.findByLabelText("Work email")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Start again.");
  });

  it("explains a timeout", async () => {
    mockApi(null);
    renderAt("/sign-in?ended=timeout");
    expect(await screen.findByRole("alert")).toHaveTextContent("signed out after 30 minutes");
  });
});
