import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

// Commercial-demo script, run against a FRESH fixture copy (backend/data/demo_comercial_e2e.sqlite3, gitignored) on its own port.
// Three browser contexts use three different HOST NAMES (dueno./admin./tecnico.localhost) so their cookies cannot mix.
const cached = `${homedir()}/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const executablePath = process.env.PW_CHROMIUM ?? (existsSync(cached) ? cached : undefined);
export const DEMO_PORT = 8745;
export default defineConfig({
  testDir: "./e2e", testMatch: "demo_commercial*.spec.ts", timeout: 180_000, workers: 1, fullyParallel: false, reporter: [["list"]],
  use: { storageState: { cookies: [], origins: [{ origin: `http://127.0.0.1:${DEMO_PORT}`, localStorage: [{ name: "aicos.mode.default", value: "complete" }] }] }, baseURL: `http://127.0.0.1:${DEMO_PORT}`, screenshot: "off", trace: "off", launchOptions: { executablePath }, viewport: { width: 1440, height: 900 } },
  webServer: {
    command: `rm -f ../backend/data/demo_comercial_e2e.sqlite3* && cd ../backend && .venv/bin/python -m app.demo_seed --db data/demo_comercial_e2e.sqlite3 >/dev/null && AICOS_STATIC_DIR=${process.cwd()}/${process.env.AICOS_UI_DIR ?? "dist-v05"} AICOS_DEMO_MODE=1 AICOS_COMMIT=e2e AICOS_DB_URL=sqlite:///data/demo_comercial_e2e.sqlite3 AICOS_STEP_DELAY=0.12 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${DEMO_PORT} --no-proxy-headers`,
    url: `http://127.0.0.1:${DEMO_PORT}/api/demo/info`, reuseExistingServer: false, timeout: 90_000,
  },
});
