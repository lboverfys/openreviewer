import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  cacheDir:
    process.env.OPENREVIEWER_VITE_CACHE_DIR ??
    "D:/rubbish/zhongjian/caches/node/openreviewer/vite",
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
  },
});
