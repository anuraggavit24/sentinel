import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser only ever talks to http://localhost:5173. Requests to /api are forwarded to the
// FastAPI service, so the session cookie is first-party and WebAuthn sees a single origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.API_URL ?? "http://localhost:8000",
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
