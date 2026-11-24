import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { homedir } from "node:os";

// V0.7 P2: la ventana de autenticación reciente es de 3 s (AICOS_REAUTH_MAX_AGE_SECONDS, válido 1–900 en el piloto) para poder probar el cuadro de reautenticación sin esperar 15 minutos.
// V0.7 P1 — el MODO PILOTO (`AICOS_ENV=pilot`) en un navegador real: base nueva con UNA empresa ficticia dada de alta por la CLI del operador (no la siembra de demostración), su propio puerto y su
// propia base (backend/data/e2e_pilot.db, ignorada por Git). Chromium acepta cookies `Secure` y `__Host-` en http://127.0.0.1 (origen «potencialmente confiable»); el piloto real va siempre con HTTPS.
const cached = `${homedir()}/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell`;
const executablePath = process.env.PW_CHROMIUM ?? (existsSync(cached) ? cached : undefined);
export const PILOT_PORT = 8747;
export default defineConfig({
  testDir: "./e2e_pilot", timeout: 90_000, workers: 1, fullyParallel: false, reporter: [["list"]],
  use: { storageState: { cookies: [], origins: [{ origin: `http://127.0.0.1:${PILOT_PORT}`, localStorage: [{ name: "aicos.mode.default", value: "complete" }] }] }, baseURL: `http://127.0.0.1:${PILOT_PORT}`, screenshot: "off", trace: "off", launchOptions: { executablePath }, viewport: { width: 1440, height: 900 } },
  webServer: {
    command: `rm -f ../backend/data/e2e_pilot.db* && cd ../backend && .venv/bin/python -m app.company_admin create --db data/e2e_pilot.db --name "Empresa Piloto Ficticia" --slug piloto-ficticio --owner-name "Ana Prueba" --owner-email ana@piloto.test --owner-password clave-ficticia-123 >/dev/null && AICOS_ENV=pilot AICOS_REAUTH_MAX_AGE_SECONDS=3 AICOS_COOKIE_SECURE=1 AICOS_TRUSTED_ORIGINS=127.0.0.1:${PILOT_PORT} AICOS_STATIC_DIR=${process.cwd()}/${process.env.AICOS_UI_DIR ?? "dist-v05"} AICOS_DB_URL=sqlite:///data/e2e_pilot.db AICOS_STEP_DELAY=0.12 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${PILOT_PORT} --no-proxy-headers`,
    url: `http://127.0.0.1:${PILOT_PORT}/api/health`, reuseExistingServer: false, timeout: 60_000,
  },
});
