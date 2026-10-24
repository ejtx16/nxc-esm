import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Vite dev server proxies /api to the FastAPI backend (uvicorn on :8000).
// Set API_TARGET to point at a backend on another port.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Let the dev server answer on the sandbox and preview addresses
    // (5173-<sandbox-id>.e2b.app, <session>--frontend.previews.kosuke.ai).
    // A leading dot allows every subdomain, so a new sandbox or session ID keeps working.
    // Vite 5 ignores __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS, so the hosts are listed here.
    allowedHosts: [".e2b.app", ".previews.kosuke.ai"],
    proxy: {
      "/api": { target: process.env.API_TARGET ?? "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
