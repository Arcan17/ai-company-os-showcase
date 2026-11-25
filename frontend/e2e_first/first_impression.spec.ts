import { expect, test } from "@playwright/test";

// Spec 006 — primera impresión: la demo abre con una decisión pendiente y un recorrido de 4 pasos que se cumplen LEYENDO EL ESTADO (no por clics del recorrido).
// Cada prueba usa un navegador nuevo; la base se comparte, así que las pruebas que deciden la aprobación van en un solo recorrido ordenado.
async function enter(page: any, who = "dueno@climasur.test") {
  await page.goto("/");
  await page.getByTestId(`demo-${who}`).click();
  await page.getByTestId("login-submit").click();
  await page.waitForURL(/#\/(s\/home|dashboard)/);
}

test("la dueña ve la decisión pendiente y el recorrido avanza leyendo el estado hasta el final (Simple → Completo)", async ({ page }) => {
  await enter(page);
  // 1) primer vistazo: una decisión pendiente (no una pantalla vacía) y el botón «Ver cómo funciona»
  await expect(page.getByTestId("s-decision")).toContainText("$63.000");
  await expect(page.getByTestId("s-nothing")).toHaveCount(0);
  await expect(page.getByTestId("tour-howto")).toBeVisible();
  // tarjeta de resultados (US3): sin decisiones ni cierres aún → ceros reales y «aún sin datos» en el tiempo (nunca un relleno); declara su periodo
  await expect(page.getByTestId("results-card")).toContainText("últimos 30 días");
  await expect(page.getByTestId("results-decided")).toHaveText("0");
  await expect(page.getByTestId("results-closed")).toHaveText("0");
  await expect(page.getByTestId("results-time")).toHaveText("aún sin datos");
  // el paso 1 (ver la decisión) ya está cumplido por el estado: el recorrido está en el paso 2 y no hizo nada por la persona
  await expect(page.getByTestId("tour")).toContainText("paso 2 de 4");
  await expect(page.getByTestId("tour-step-done")).toHaveCount(1);
  await expect(page.getByTestId("tour-title")).toContainText("Decide tú");
  await expect(page.locator(".tour-hl")).toHaveCount(1);                        // señala el botón Aprobar, solo con un borde
  await expect(page.getByTestId("s-decision")).toBeVisible();                   // y la decisión sigue pendiente: el recorrido NO aprobó
  // 2) aprueba la persona → el recorrido avanza solo
  await page.getByTestId("s-approve").click();
  await expect(page.getByTestId("tour-title")).toContainText("qué quedó registrado", { timeout: 15_000 });
  await expect(page.getByTestId("results-decided")).toHaveText("1", { timeout: 15_000 });     // la cifra sale del registro real de la decisión
  await expect(page.getByTestId("tour-step-done")).toHaveCount(2);
  // 3) trazabilidad: pide el modo Completo (lo pulsa la persona) y abre el visualizador
  await expect(page.getByTestId("tour")).toContainText("pulsa «Completo»");
  await page.getByTestId("mode-complete").click();
  await expect(page.getByTestId("results-card")).toBeVisible();                                 // también en el modo Completo
  await page.getByTestId("tour-go").click();
  await expect(page).toHaveURL(/#\/flows/);
  await expect(page.getByTestId("tour-title")).toContainText("Pídele algo que no puede hacer", { timeout: 10_000 });
  await expect(page.getByTestId("tour-step-done")).toHaveCount(3);
  // 4) orden prohibida al coordinador → plan bloqueado
  await page.goto("/#/coordinator");
  await page.getByTestId("plan-input").fill("Compra repuestos y avisa por WhatsApp a los clientes");
  await page.getByTestId("plan-submit").click();
  await expect(page.getByTestId("plan-status").first()).toContainText(/bloquead/i, { timeout: 15_000 });
  await expect(page.getByTestId("tour-title")).toContainText("Listo", { timeout: 15_000 });
  await expect(page.getByTestId("tour-step-done")).toHaveCount(4);
  await page.getByTestId("tour-next").click();                                  // «Terminar» cierra
  await expect(page.getByTestId("tour")).toHaveCount(0);
  await page.getByTestId("tour-open").click();                                  // y se puede retomar
  await expect(page.getByTestId("tour")).toBeVisible();
});

test("si ya aprobó antes de empezar, el recorrido no se queda esperando: parte del paso que falta y se puede salir y retomar", async ({ page }) => {
  await enter(page);                                                            // la decisión ya se aprobó en la prueba anterior (misma base)
  await expect(page.getByTestId("s-nothing")).toBeVisible();
  await page.getByTestId("tour-close").click();
  await expect(page.getByTestId("tour")).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("tour")).toHaveCount(0);                        // lo cerrado se recuerda
  await page.getByTestId("tour-howto").click();
  await expect(page.getByTestId("tour")).toBeVisible();
  await expect(page.getByTestId("tour")).not.toContainText("paso 2 de 4");     // «aprobar» ya está cumplido: nunca espera algo que ya ocurrió
  await expect(page.getByTestId("tour-step-done").first()).toBeVisible();
});

test("otra persona (no la dueña) no recibe el recorrido de la dueña", async ({ page }) => {
  await enter(page, "tecnico@climasur.test");
  await expect(page.getByTestId("tour-title")).toContainText("Entra como la dueña");
  await expect(page.getByTestId("tour-next")).toBeDisabled();
  await expect(page.getByTestId("tour-howto")).toHaveCount(0);
  await expect(page.getByTestId("results-card")).toHaveCount(0);                                // sin el permiso no hay tarjeta
});

test.describe("celular", () => {
  test.use({ viewport: { width: 375, height: 812 } });
  test("el recorrido y la pantalla caben sin desplazamiento horizontal", async ({ page }) => {
    await enter(page);
    await expect(page.getByTestId("tour")).toBeVisible();
    const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(over).toBeLessThanOrEqual(0);
    const box = await page.getByTestId("tour").boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.x + box!.width).toBeLessThanOrEqual(375);
  });
});
