import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Vite dev server proxies /api to the FastAPI backend (uvicorn on :8000).
// Set API_TARGET to point at a backend on another port.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.API_TARGET ?? "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
