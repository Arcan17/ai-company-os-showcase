import { expect, test } from "@playwright/test";

// Dos visitantes SIMULTÁNEOS en la demo pública: cada uno con su espacio; lo que hace uno no existe para el otro. Recorrido guiado y aviso visibles.
async function enter(page: any, who: string) {
  await page.goto("/");
  await expect(page.getByTestId("public-demo-banner")).toContainText("DEMO PÚBLICA");
  await page.getByTestId(`demo-${who}`).click();
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-name")).toBeVisible();
}

test("dos visitantes a la vez: cada uno abre con su decisión de ejemplo, el recorrido guiado funciona y sus espacios están aislados", async ({ browser }) => {
  const a = await (await browser.newContext()).newPage();
  const b = await (await browser.newContext()).newPage();
  await enter(a, "dueno@climasur.test"); await enter(b, "dueno@climasur.test");

  // primera impresión (spec 006): cada visitante abre con SU decisión pendiente de ejemplo, no con una pantalla vacía
  await expect(a.getByTestId("s-decision")).toContainText("$63.000");
  await expect(b.getByTestId("s-decision")).toContainText("$63.000");

  // recorrido guiado de 4 pasos leyendo el estado: el paso 1 (ver la decisión) ya está cumplido; se puede cerrar y reabrir
  await expect(a.getByTestId("tour")).toBeVisible();
  await expect(a.getByTestId("tour")).toContainText("paso 2 de 4");
  await a.getByTestId("tour-close").click();
  await expect(a.getByTestId("tour")).toHaveCount(0);
  await a.getByTestId("tour-open").click();
  await expect(a.getByTestId("tour")).toBeVisible();

  // A aprueba SU decisión (la persona, no el recorrido); la de B sigue pendiente: espacios aislados
  await a.getByTestId("s-approve").click();
  await expect(a.getByTestId("tour-title")).toContainText("qué quedó registrado", { timeout: 15_000 });
  await b.reload();
  await expect(b.getByTestId("s-decision")).toBeVisible();                       // jamás vio lo que hizo A
  await a.reload();
  await expect(a.getByTestId("s-nothing")).toBeVisible();
  await a.screenshot({ path: "../docs/v09/screenshots/01-demo-publica.png" });
  await a.context().close(); await b.context().close();
});

test("sin sesión de visitante la interfaz y la salud responden y la API crea un espacio propio", async ({ request }) => {
  expect((await request.get("/api/health")).ok()).toBeTruthy();
  const info = await (await request.get("/api/public-demo/info")).json();
  expect(info.public_demo).toBe(true);
  expect(info.capacity).toBeGreaterThan(0);
});
