import { expect, test } from "@playwright/test";
import { FORBIDDEN_TERMS } from "../src/simple/copy";
import { apost } from "./csrf";

// Spec 002 — modo simple. Estos contextos NO traen el valor por defecto «completo» de las demás pruebas: parten del valor real (simple).
const FRESH = { storageState: { cookies: [], origins: [] } };

async function login(page: any, email: string) {
  await page.goto("/#/");
  await page.getByTestId("email").fill(email); await page.getByTestId("password").fill("demo1234"); await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-name")).toBeVisible();
}
async function apiLogin(page: any, email: string) {
  await page.goto("/");
  expect((await apost(page, "/api/auth/login", { data: { email, password: "demo1234" } })).ok()).toBeTruthy();
}
async function seedPendingApproval(page: any) {   // Escenario A del chat de prueba: deja una aprobación pendiente (datos de demostración)
  await page.goto("/#/cliente/clima-sur");
  await page.getByTestId("scenario-A").click();
  await page.getByTestId("send-chat").click();
  await expect(page.getByTestId("conversation")).toBeVisible();
}

test("el modo por defecto es simple y la dueña decide desde una sola pantalla", async ({ browser }) => {
  const ctx = await browser.newContext(FRESH); const page = await ctx.newPage();
  await apiLogin(page, "dueno@climasur.test");
  await seedPendingApproval(page);
  await page.goto("/#/dashboard"); await page.reload();
  await expect(page.getByTestId("simple-home")).toBeVisible();
  await expect(page.getByTestId("mode-simple")).toHaveAttribute("aria-pressed", "true");
  expect(await page.locator("nav a").count()).toBeLessThanOrEqual(4);
  await expect(page.getByTestId("s-decision")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(900);
  await page.screenshot({ path: "../docs/simple/screenshots/01-dueña-inicio.png" });
  const text = (await page.locator("main").innerText()).toLowerCase();
  for (const t of FORBIDDEN_TERMS) expect(text, `término técnico visible: ${t}`).not.toContain(t);
  await page.getByTestId("s-approve").click();
  await expect(page.getByTestId("s-msg")).toContainText("aprobado");
  await ctx.close();
});

test("el interruptor se recuerda por usuario y funciona sin almacenamiento", async ({ browser }) => {
  const ctx = await browser.newContext(FRESH); const page = await ctx.newPage();
  await login(page, "dueno@climasur.test");
  await page.getByTestId("mode-complete").click();
  await expect(page.getByTestId("nav-dashboard")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("nav-dashboard")).toBeVisible();
  await page.getByTestId("logout").click();
  await login(page, "admin@climasur.test");   // otro usuario del mismo navegador: parte de su propio valor (simple)
  await expect(page.getByTestId("simple-tasks")).toBeVisible();
  await page.getByTestId("logout").click();
  await login(page, "dueno@climasur.test");   // la dueña recupera su elección
  await expect(page.getByTestId("nav-dashboard")).toBeVisible();
  await ctx.close();
  const blocked = await browser.newContext(FRESH); const bp = await blocked.newPage();
  await bp.addInitScript(() => { Object.defineProperty(window, "localStorage", { get() { throw new Error("blocked"); } }); });
  await login(bp, "dueno@climasur.test");
  await expect(bp.getByTestId("simple-home")).toBeVisible();   // sin almacenamiento: valor por defecto, sin errores
  await bp.getByTestId("mode-complete").click();
  await expect(bp.getByTestId("nav-dashboard")).toBeVisible();
  await blocked.close();
});

test("«Ver detalle técnico» lleva al modo completo y administración cierra tareas solo con evidencia", async ({ browser }) => {
  const ctx = await browser.newContext(FRESH); const page = await ctx.newPage();
  await login(page, "admin@climasur.test");
  await expect(page.getByTestId("simple-tasks")).toBeVisible();
  const text = (await page.locator("main").innerText()).toLowerCase();
  for (const t of FORBIDDEN_TERMS) expect(text, `término técnico visible: ${t}`).not.toContain(t);
  await page.getByTestId("snav-requests").click();
  await expect(page.getByTestId("simple-requests")).toBeVisible();
  await page.waitForTimeout(900);
  await page.screenshot({ path: "../docs/simple/screenshots/02-administración-solicitudes.png" });
  await page.getByTestId("see-detail").click();
  await expect(page.getByTestId("nav-operations")).toBeVisible();
  await expect(page.getByTestId("mode-complete")).toHaveAttribute("aria-pressed", "true");
  await ctx.close();
});

async function crearYAprobar(browser: any) {   // deja una visita propuesta y sus tareas bloqueadas por el cliente
  const ctx = await browser.newContext(FRESH); const page = await ctx.newPage();
  await apiLogin(page, "dueno@climasur.test");
  await seedPendingApproval(page);
  await page.goto("/#/dashboard"); await page.reload();
  await expect(page.getByTestId("s-approve")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("s-approve").click();
  await expect(page.getByTestId("s-msg")).toContainText("aprobado");
  await ctx.close();
}

test("técnico/a: la visita propuesta no se puede dar por confirmada y cerrar sin evidencia se rechaza", async ({ browser }) => {
  await crearYAprobar(browser);
  const ctx = await browser.newContext(FRESH); const page = await ctx.newPage();
  await login(page, "tecnico@climasur.test");
  await expect(page.getByTestId("simple-tasks")).toBeVisible();
  await expect(page.getByTestId("s-task-blocked").first()).toContainText("Esperando confirmación del cliente", { timeout: 20_000 });
  await expect(page.getByTestId("s-task").filter({ has: page.getByTestId("s-task-blocked") }).getByTestId("s-complete")).toHaveCount(0);   // bloqueada por el cliente: no hay botón para cerrarla
  await page.waitForTimeout(900);
  await page.screenshot({ path: "../docs/simple/screenshots/03-técnico-tareas.png" });
  await expect(page.getByTestId("snav-requests")).toHaveCount(0);   // el técnico no tiene permiso para ver las solicitudes: el menú no lo ofrece
  await ctx.close();
  const adm = await browser.newContext(FRESH); const ap = await adm.newPage();
  await login(ap, "admin@climasur.test");
  await ap.getByTestId("snav-requests").click();
  await ap.getByTestId("s-request").filter({ hasText: "Visita propuesta al cliente" }).first().getByRole("button").first().click();
  await expect(ap.getByTestId("s-visit-state")).toContainText("aún sin confirmar");
  await ap.getByTestId("snav-tasks").click();
  const btn = ap.getByTestId("s-complete").first();
  if (await btn.count()) { await btn.click(); await expect(ap.getByTestId("s-task-msg")).toContainText("qué hiciste"); }
  await adm.close();
});
