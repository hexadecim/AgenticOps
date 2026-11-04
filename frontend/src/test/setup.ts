import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

class SilentEventSource {
  addEventListener() {}
  removeEventListener() {}
  close() {}
  onopen = null;
  onerror = null;
}
vi.stubGlobal("EventSource", SilentEventSource);
