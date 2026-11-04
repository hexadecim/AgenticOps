import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the SPA and the gateway share an origin through this proxy, so the
// SameSite=Strict session cookie works exactly as it does behind nginx in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.GATEWAY_URL ?? "http://localhost:8000", changeOrigin: false } },
  },
  preview: {
    proxy: { "/api": { target: process.env.GATEWAY_URL ?? "http://localhost:8000", changeOrigin: false } },
  },
});
