import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  cacheDir:
    process.env.OPENREVIEWER_VITE_CACHE_DIR ?? ".vite-cache",
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target:
          process.env.OPENREVIEWER_DEV_API_URL ?? "http://127.0.0.1:18090",
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "node",
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary"],
      thresholds: {
        lines: 60,
        functions: 45,
        branches: 50,
        statements: 55,
      },
    },
  },
});
