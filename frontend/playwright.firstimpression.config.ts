import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

// V0.11 (spec 006) — la primera impresión de la demo en un navegador real: datos de ejemplo cargados (AICOS_DEMO_EXAMPLE=1) y recorrido guiado. Su propio puerto y su propia base (ignorada por Git).
const cached = `${homedir()}/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const executablePath = process.env.PW_CHROMIUM ?? (existsSync(cached) ? cached : undefined);
export const FIRST_PORT = 8752;
export default defineConfig({
  testDir: "./e2e_first", timeout: 90_000, workers: 1, fullyParallel: false, reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${FIRST_PORT}`, screenshot: "off", trace: "off", launchOptions: { executablePath }, viewport: { width: 1440, height: 900 } },
  webServer: {
    command: `rm -f ../backend/data/e2e_first.db* && cd ../backend && AICOS_DEMO_EXAMPLE=1 AICOS_STATIC_DIR=${process.cwd()}/${process.env.AICOS_UI_DIR ?? "dist-v05"} AICOS_DB_URL=sqlite:///data/e2e_first.db AICOS_STEP_DELAY=0.12 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${FIRST_PORT} --no-proxy-headers`,
    url: `http://127.0.0.1:${FIRST_PORT}/api/health`, reuseExistingServer: false, timeout: 60_000,
  },
});
