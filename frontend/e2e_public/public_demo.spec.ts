import { expect, test } from "@playwright/test";

// Dos visitantes SIMULTÁNEOS en la demo pública: cada uno con su espacio; lo que hace uno no existe para el otro. Recorrido guiado y aviso visibles.
async function enter(page: any, who: string) {
  await page.goto("/");
  await expect(page.getByTestId("public-demo-banner")).toContainText("DEMO PÚBLICA");
  await page.getByTestId(`demo-${who}`).click();
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-name")).toBeVisible();
}

test("dos visitantes a la vez: el recorrido guiado funciona y sus espacios están aislados", async ({ browser }) => {
  const a = await (await browser.newContext()).newPage();
  const b = await (await browser.newContext()).newPage();
  await enter(a, "dueno@climasur.test"); await enter(b, "dueno@climasur.test");

  // recorrido guiado: 5 pasos, se puede cerrar y reabrir
  await expect(a.getByTestId("tour")).toBeVisible();
  await expect(a.getByTestId("tour")).toContainText("paso 1 de 5");
  for (let i = 0; i < 4; i++) await a.getByTestId("tour-next").click();
  await expect(a.getByTestId("tour-title")).toContainText("modo Completo");
  await a.getByTestId("tour-close").click();
  await expect(a.getByTestId("tour")).toHaveCount(0);
  await a.getByTestId("tour-open").click();
  await expect(a.getByTestId("tour")).toBeVisible();

  // A hace llegar una solicitud y la aprueba (en la vista simple); B no ve nada de eso
  await a.getByTestId("mode-complete").click();
  await a.goto("/#/cliente/clima-sur");
  await a.getByTestId("scenario-A").click(); await a.getByTestId("send-chat").click();
  await expect(a.getByTestId("conversation")).toBeVisible();
  await a.goto("/#/dashboard");
  await expect(a.getByTestId("kpi-approvals")).toHaveText("1", { timeout: 20_000 });
  await b.goto("/#/dashboard"); await b.reload();
  await expect(b.getByTestId("s-nothing")).toBeVisible();                       // B (vista simple) sigue en «Nada requiere tu decisión hoy»: jamás vio la solicitud de A
  await a.screenshot({ path: "../docs/v09/screenshots/01-demo-publica.png" });
  await a.context().close(); await b.context().close();
});

test("sin sesión de visitante la interfaz y la salud responden y la API crea un espacio propio", async ({ request }) => {
  expect((await request.get("/api/health")).ok()).toBeTruthy();
  const info = await (await request.get("/api/public-demo/info")).json();
  expect(info.public_demo).toBe(true);
  expect(info.capacity).toBeGreaterThan(0);
});
