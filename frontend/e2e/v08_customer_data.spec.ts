import { expect, test } from "@playwright/test";
import { apost, newCtx } from "./csrf";

// Spec 003 — control de datos de clientes desde la ficha (solo la dueña). La anonimización completa (barrido de todas las tablas) la prueba pytest; aquí se prueba la interfaz.
test("la dueña ve quién consultó los datos, los descarga y la anonimización pide el nombre exacto y se niega con trabajo activo", async ({ browser }) => {
  const ctx = await newCtx(browser, { viewport: { width: 1440, height: 900 } }); const page = await ctx.newPage();
  await page.goto("/");
  expect((await apost(page, "/api/auth/login", { data: { email: "dueno@climasur.test", password: "demo1234" } })).ok()).toBeTruthy();
  await page.goto("/#/cliente/clima-sur"); await page.reload();
  await page.getByTestId("scenario-A").click(); await page.getByTestId("send-chat").click();
  await expect(page.getByTestId("conversation")).toBeVisible();
  await page.goto("/#/operations"); await page.reload();
  await page.getByTestId("request-row").first().click();
  await expect(page.getByTestId("customer-data")).toBeVisible();

  await page.getByTestId("cd-log").click();
  await expect(page.getByTestId("cd-log-list")).toContainText("Carolina Vega");

  const [dl] = await Promise.all([page.waitForEvent("download"), page.getByTestId("cd-export").click()]);
  expect(dl.suggestedFilename()).toMatch(/^datos-cliente-.*\.json$/);
  await expect(page.getByTestId("cd-msg")).toContainText("Descargaste los datos");

  await page.getByTestId("cd-anonymize").click();
  await expect(page.getByTestId("cd-confirm")).toBeVisible();
  await page.getByTestId("cd-typed").fill("otro nombre");
  await page.getByTestId("cd-confirm-go").click();
  await expect(page.getByTestId("cd-msg")).toContainText("nombre exacto");
  const name = (await page.getByTestId("cd-confirm").locator("label").innerText()).split(": ").pop()!.trim();
  await page.getByTestId("cd-typed").fill(name);
  await page.getByTestId("cd-confirm-go").click();
  await expect(page.getByTestId("cd-msg")).toContainText("trabajo activo");     // la solicitud recién creada sigue en curso: no se anonimiza
  await page.waitForTimeout(600);
  await page.screenshot({ path: "../docs/v08/screenshots/01-datos-del-cliente.png" });
  await ctx.close();
});
