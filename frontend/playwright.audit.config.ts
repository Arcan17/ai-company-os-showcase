import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

// QA audit: runs against an ISOLATED server (:8742) started by qa/start_audit_server.sh on a COPY of the real database.
const cached = `${homedir()}/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const executablePath = process.env.PW_CHROMIUM ?? (existsSync(cached) ? cached : undefined);
export default defineConfig({
  testDir: "./e2e", testMatch: "audit*.spec.ts", timeout: 180_000, workers: 1, fullyParallel: false, reporter: [["list"]],
  use: { baseURL: "http://127.0.0.1:8742", screenshot: "off", trace: "off", launchOptions: { executablePath }, viewport: { width: 1440, height: 900 } },
});
