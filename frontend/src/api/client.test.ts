import { describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";

function respondWith(status: number, body?: unknown) {
  const mock = vi.fn(async () => new Response(body === undefined ? null : JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("api client", () => {
  it("marks state-changing calls so the gateway accepts them, and only those", async () => {
    const mock = respondWith(200, {});
    await api("/thing", { method: "POST", body: { a: 1 } });
    await api("/thing");
    const [post, get] = mock.mock.calls as unknown as [[string, RequestInit], [string, RequestInit]];
    expect((post[1].headers as Record<string, string>)["X-OpsPilot-Client"]).toBe("web");
    expect((get[1].headers as Record<string, string>)["X-OpsPilot-Client"]).toBeUndefined();
  });

  it("sends the session cookie to its own origin only", async () => {
    const mock = respondWith(200, {});
    await api("/thing");
    expect((mock.mock.calls as unknown as [string, RequestInit][])[0]![1].credentials).toBe("same-origin");
  });

  it("turns an error response into an ApiError with the server's words", async () => {
    respondWith(409, { code: "store_down", message: "A store cannot be asked now." });
    const error = await api("/thing").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "store_down", message: "A store cannot be asked now." });
  });

  it("says when OpsPilot cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("failed"); }));
    await expect(api("/thing")).rejects.toMatchObject({ code: "network" });
  });

  it("reports 503 as unavailable", async () => {
    respondWith(503, { code: "unavailable", message: "Paused" });
    const error = (await api("/thing").catch((e: unknown) => e)) as ApiError;
    expect(error.isUnavailable).toBe(true);
  });
});
