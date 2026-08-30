import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/livez": "http://127.0.0.1:8000",
      "/readyz": "http://127.0.0.1:8000",
      "/metrics": "http://127.0.0.1:8000",
      "/docs": "http://127.0.0.1:8000",
      "/redoc": "http://127.0.0.1:8000",
      "/openapi.json": "http://127.0.0.1:8000"
    }
  },
  build: {
    outDir: "dist",
    sourcemap: false
  }
});
