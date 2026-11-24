import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

// V0.9 (spec 004, fase B) — la DEMO PÚBLICA (`AICOS_PUBLIC_DEMO=1`) en un navegador real: un espacio aislado por visitante. Su propio puerto y su propio directorio de datos (ignorado por Git).
const cached = `${homedir()}/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const executablePath = process.env.PW_CHROMIUM ?? (existsSync(cached) ? cached : undefined);
export const PUBLIC_PORT = 8751;
export default defineConfig({
  testDir: "./e2e_public", timeout: 90_000, workers: 1, fullyParallel: false, reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${PUBLIC_PORT}`, screenshot: "off", trace: "off", launchOptions: { executablePath }, viewport: { width: 1440, height: 900 } },
  webServer: {
    command: `rm -rf ../backend/data/e2e_public && cd ../backend && AICOS_PUBLIC_DEMO=1 AICOS_PUBLIC_DEMO_DIR=data/e2e_public AICOS_STATIC_DIR=${process.cwd()}/${process.env.AICOS_UI_DIR ?? "dist-v05"} AICOS_STEP_DELAY=0.12 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${PUBLIC_PORT} --no-proxy-headers`,
    url: `http://127.0.0.1:${PUBLIC_PORT}/api/health`, reuseExistingServer: false, timeout: 60_000,
  },
});
