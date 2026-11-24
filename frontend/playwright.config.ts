import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

// Reuse the Chromium already cached on this Mac (Playwright 1.65 asks for r1255; r1234 is installed) to avoid a download.
const cached = `${homedir()}/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const executablePath = process.env.PW_CHROMIUM ?? (existsSync(cached) ? cached : undefined);
const PORT = 8741;

export default defineConfig({
  testDir: "./e2e", testIgnore: ["audit*.spec.ts", "demo_commercial*.spec.ts"],  // the QA audit has its own config (playwright.audit.config.ts: isolated server on a DB copy)
  timeout: 60_000,
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  use: { storageState: { cookies: [], origins: [{ origin: `http://127.0.0.1:${PORT}`, localStorage: [{ name: "aicos.mode.default", value: "complete" }] }] }, baseURL: `http://127.0.0.1:${PORT}`, screenshot: "off", trace: "off", launchOptions: { executablePath }, viewport: { width: 1440, height: 900 } },
  webServer: {
    command: `rm -f ../backend/data/e2e.db* && cd ../backend && AICOS_STATIC_DIR=${process.cwd()}/${process.env.AICOS_UI_DIR ?? "dist-v05"} AICOS_ALLOW_COMPANY_CREATION=1 AICOS_DB_URL=sqlite:///data/e2e.db AICOS_STEP_DELAY=0.12 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${PORT} --no-proxy-headers`,
    url: `http://127.0.0.1:${PORT}/api/auth/demo-accounts`, reuseExistingServer: false, timeout: 60_000,
  },
});
