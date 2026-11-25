import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

// Kit de grabación (spec 006): recorre la demo REAL con rótulos y cursor visibles y, con RECORD_VIDEO=1, captura los cuadros para el video (`qa/grabar_demo.sh` los une y corta por escenas).
// Sin RECORD_VIDEO el recorrido corre rápido y sin video: sirve de prueba en el CI para que el guion de la demo no se rompa en silencio.
// Su propio puerto y su propia base (ignorada por Git). Si se define AICOS_LLM_PROVIDER=ollama (qa/grabar_demo.sh --ia), el servidor usa ese modelo.
const cached = `${homedir()}/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const executablePath = process.env.PW_CHROMIUM ?? (existsSync(cached) ? cached : undefined);
export const REC_PORT = 8753;
const video = process.env.RECORD_VIDEO === "1";
export default defineConfig({
  testDir: "./record", outputDir: process.env.REC_OUT ?? "../grabaciones/raw", timeout: 600_000, workers: 1, fullyParallel: false, reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${REC_PORT}`, screenshot: "off", trace: "off", launchOptions: { executablePath }, viewport: { width: 1280, height: 800 },
         video: "off" },       // el video NO usa el ffmpeg descargable de Playwright: la prueba captura los cuadros por el protocolo del navegador y qa/grabar_demo.sh los une con el ffmpeg del sistema
  webServer: {
    command: `rm -f ../backend/data/e2e_record.db* && cd ../backend && AICOS_DEMO_EXAMPLE=1 AICOS_STATIC_DIR=${process.cwd()}/${process.env.AICOS_UI_DIR ?? "dist-v05"} AICOS_DB_URL=sqlite:///data/e2e_record.db AICOS_STEP_DELAY=${video ? "0.45" : "0.12"} .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${REC_PORT} --no-proxy-headers`,
    url: `http://127.0.0.1:${REC_PORT}/api/health`, reuseExistingServer: false, timeout: 60_000,
  },
});
