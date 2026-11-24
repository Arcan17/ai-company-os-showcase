import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: process.env.BUILD_DIR ?? "dist" },   // BUILD_DIR=dist-v05 builds the branch UI WITHOUT touching the dist served by the running lab/demo servers
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8731" } },
});
