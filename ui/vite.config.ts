import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In production the API serves ui/dist from the same origin, so no proxy is
// needed there. In development Vite runs on 5173 and forwards API paths to
// uvicorn on 8080.
const API = "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/health": API,
      "/faults": API,
      "/stage": API,
      "/webhook": API,
      "/stream": { target: API, changeOrigin: true, ws: false },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
