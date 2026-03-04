import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/dashboard-static/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/status": "http://127.0.0.1:8000",
      "/portfolio_history": "http://127.0.0.1:8000",
      "/leaderboard": "http://127.0.0.1:8000",
      "/trades": "http://127.0.0.1:8000",
      "/positions": "http://127.0.0.1:8000",
      "/control": "http://127.0.0.1:8000",
      "/metrics": "http://127.0.0.1:8000",
      "/pnl_status": "http://127.0.0.1:8000",
      "/discovery": "http://127.0.0.1:8000",
    },
  },
});
