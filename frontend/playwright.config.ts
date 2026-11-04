import { defineConfig } from "@playwright/test";

// Uses the Chrome installed on the machine. a11y: built SPA with a mocked API. sign-in: `npm run e2e:sign-in`
// against real identity-service and gateway on the test infrastructure (e2e/start-stack.sh).
export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:4173", channel: "chrome", ignoreHTTPSErrors: true },
  projects: [
    { name: "a11y", testMatch: "a11y.spec.ts" },
    { name: "sign-in", testMatch: "sign-in.spec.ts" },
  ],
  webServer: process.env.E2E_STACK
    ? { command: "sh e2e/start-stack.sh", url: "http://localhost:4173/api/health/live", reuseExistingServer: false, timeout: 180_000, stdout: "pipe" }
    : { command: "npm run build && npx vite preview --port 4173 --strictPort", url: "http://localhost:4173", reuseExistingServer: false, timeout: 120_000 },
});
