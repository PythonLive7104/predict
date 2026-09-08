import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev-only proxy so the SPA and the API share an origin and no CORS or
    // cookie juggling is needed while developing.
    proxy: {
      "/api": { target: "http://127.0.0.1:8811", changeOrigin: true },
    },
  },
});
