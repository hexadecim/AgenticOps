import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it } from "vitest";
import { queue, user } from "./test/fixtures";
import { mockApi, renderAt } from "./test/render";

describe("access by role", () => {
  it("sends a signed-out person to sign-in", async () => {
    mockApi(null);
    renderAt("/approvals");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("refuses an admin page to other roles", async () => {
    mockApi(user("l1_analyst"));
    renderAt("/admin/users");
    expect(await screen.findByRole("alert")).toHaveTextContent("Your role cannot open this page");
  });

  it("opens an admin page for an admin", async () => {
    mockApi(user("admin"));
    renderAt("/admin/users");
    expect(await screen.findByText("max@example.test")).toBeInTheDocument();
  });

  it("opens the legal pages without signing in", async () => {
    mockApi(null);
    renderAt("/legal/privacy");
    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Sign in" })).not.toBeInTheDocument();
  });
});

describe("approvals", () => {
  it("lists what is waiting and filters by gate", async () => {
    mockApi(user("l1_analyst"));
    renderAt("/approvals");
    expect(await screen.findByText("Uploads fail at site Berlin")).toBeInTheDocument();
    expect(screen.getByText("Restart upload-worker")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /Fix/ }));
    expect(screen.queryByText("Uploads fail at site Berlin")).not.toBeInTheDocument();
    expect(screen.getByText("Restart upload-worker")).toBeInTheDocument();
  });

  it.each([
    ["ap-ticket", /ticket/i],
    ["ap-fix", /restart_service|Restart/],
    ["ap-message", /Re: Uploads fail|Resolution/],
  ])("opens the decision screen of %s", async (id, text) => {
    mockApi(user("support_agent"));
    renderAt(`/approvals/${id}`);
    await waitFor(() => expect(screen.getAllByText(text).length).toBeGreaterThan(0));
  });

  it("says so when the queue is longer than the page", async () => {
    const long = { items: queue.items, stats: { ...queue.stats, waiting: 5000 } };
    mockApi(user("l1_analyst"), (path) => (path === "/approvals" ? { status: 200, body: long } : undefined));
    renderAt("/approvals");
    expect(await screen.findByRole("status")).toHaveTextContent("Showing the 3 waiting longest of 5000");
  });

  it("does not offer to send a message while a blocking check fails", async () => {
    mockApi(user("l1_analyst"));
    renderAt("/approvals/ap-message");
    expect(await screen.findByText("No promises or ETAs")).toBeInTheDocument();
    const send = await screen.findByRole("button", { name: /Approve and send/ });
    expect(send).toBeDisabled();
  });

  it("never shows a real address: only tokens reach the screen", async () => {
    mockApi(user("l1_analyst"));
    const { container } = renderAt("/approvals/ap-ticket");
    await screen.findAllByText(/\[PERSON_1\]/);
    expect(container.textContent).not.toMatch(/@/);
  });
});

describe("data requests", () => {
  it("shows the token and never asks the server for the address", async () => {
    const calls = mockApi(user("compliance_lead"));
    const { container } = renderAt("/privacy");
    expect(await screen.findByText("[EMAIL_1]")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/@/);
    expect(calls.every((c) => !/address/.test(c.path))).toBe(true);
  });

  it("states the retention schedule", async () => {
    mockApi(user("compliance_lead"));
    renderAt("/privacy");
    const row = (await screen.findByText("Raw emails")).closest("tr, li, div");
    expect(within(row as HTMLElement).getByText("30 days")).toBeInTheDocument();
  });
});

describe("audit log", () => {
  it("shows entries with people by role", async () => {
    mockApi(user("compliance_lead"));
    renderAt("/audit");
    expect(await screen.findByText("Approved ticket")).toBeInTheDocument();
    expect(screen.getByText("L1 Analyst")).toBeInTheDocument();
  });

  it("sends the search to the server, not to the browser's address bar", async () => {
    const calls = mockApi(user("compliance_lead"));
    renderAt("/audit");
    await screen.findByText("Approved ticket");
    await userEvent.type(screen.getByRole("searchbox"), "KAN");
    await waitFor(() => expect(calls.some((c) => c.path.includes("q=KAN"))).toBe(true));
  });
});

describe("structure for assistive technology (jsdom; colours are checked in e2e/a11y.spec.ts)", () => {
  it.each(["/sign-in", "/legal/privacy"])("%s has no violations", async (path) => {
    mockApi(null);
    const { container } = renderAt(path);
    await screen.findAllByRole("heading");
    const results = await axe.run(container, { rules: { "color-contrast": { enabled: false }, region: { enabled: false } } });
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
