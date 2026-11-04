import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import { App } from "../App";
import type { User } from "../api/types";
import { SessionProvider } from "../auth/session";
import { respond } from "./fixtures";

type Handler = (path: string, init: RequestInit) => { status: number; body?: unknown } | undefined;

/** Replaces fetch: `handler` first, then the GET fixtures. `me` null: not signed in. */
export function mockApi(me: User | null, handler: Handler = () => undefined) {
  const calls: { path: string; method: string; body: unknown; headers: Record<string, string> }[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const path = String(input).replace(/^\/api/, "");
    const method = init.method ?? "GET";
    calls.push({ path, method, body: init.body ? JSON.parse(String(init.body)) : undefined, headers: (init.headers ?? {}) as Record<string, string> });
    const handled = handler(path, init);
    if (handled) return new Response(handled.body === undefined ? null : JSON.stringify(handled.body), { status: handled.status });
    if (path === "/auth/me" && !me) return new Response(JSON.stringify({ code: "unauthorized", message: "Not signed in" }), { status: 401 });
    const body = me && method === "GET" ? respond(path, me) : undefined;
    if (body === undefined) return new Response(JSON.stringify({ code: "not_found", message: `No fixture: ${path}` }), { status: 404 });
    return new Response(JSON.stringify(body), { status: 200 });
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

export function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <SessionProvider><App /></SessionProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
