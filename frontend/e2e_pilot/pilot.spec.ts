// V0.7 P1 (Issue #19, AC2/AC4) en un NAVEGADOR REAL: lo que las pruebas de la API no pueden demostrar es que Chromium ACEPTE la cookie `__Host-` y que la SPA siga funcionando con ella.
import { expect, test } from "@playwright/test";

const PW = "clave-ficticia-123";

test("V0.7 P1 — piloto: sin demostración, cookie __Host- aceptada por el navegador, cabeceras y sesión completa", async ({ page, context, request }) => {
  // 1) la pantalla de entrada no anuncia nada de demostración y el backend lo niega (404), no solo lo esconde
  await page.goto("/");
  await expect(page.getByTestId("login-submit")).toBeVisible();
  await expect(page.getByTestId("demo-accounts")).toHaveCount(0);
  await expect(page.getByTestId("new-company-link")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("demo1234");
  for (const p of ["/api/auth/demo-accounts", "/demo", "/demo-login/owner", "/api/demo/info", "/api/setup/info"]) expect((await request.get(p)).status(), p).toBe(404);
  expect((await request.post("/api/demo/reset-scenarios", { data: { confirm: "REINICIAR" } })).status()).toBe(404);
  const bad = await request.post("/api/auth/login", { data: { email: "dueno@climasur.test", password: "demo1234" } });
  expect(bad.status(), "las cuentas de demostración no existen").toBe(401);

  // 2) cabeceras de seguridad en una respuesta de la API y en la página
  for (const url of ["/api/health", "/"]) {
    const h = (await request.get(url)).headers();
    expect(h["strict-transport-security"], url).toContain("max-age=31536000");
    expect(h["x-content-type-options"], url).toBe("nosniff");
    expect(h["x-frame-options"], url).toBe("DENY");
    expect(h["content-security-policy"], url).toBe("frame-ancestors 'none'");
    expect(h["content-security-policy-report-only"], url).toContain("default-src 'self'");
  }
  expect((await request.get("/api/health")).headers()["cache-control"]).toBe("no-store");

  // 3) inicio de sesión real: el navegador guarda la cookie con prefijo y la SPA funciona
  await page.getByTestId("email").fill("ana@piloto.test");
  await page.getByTestId("password").fill(PW);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-name")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("user-name")).toHaveText("Ana Prueba");
  const jar = await context.cookies();
  const c = jar.find(x => x.name === "__Host-aicos_session");
  expect(c, `cookies del navegador: ${jar.map(x => x.name)}`).toBeTruthy();
  expect(c!.secure).toBe(true); expect(c!.httpOnly).toBe(true); expect(c!.path).toBe("/"); expect(c!.sameSite).toBe("Lax");
  expect(jar.some(x => x.name === "aicos_session")).toBe(false);
  expect((await page.request.get("/api/auth/me")).status()).toBe(200);
  await page.goto("/#/agentes");                       // una pantalla con datos de la empresa: las lecturas autenticadas funcionan con la cookie __Host-
  await expect(page.getByTestId("company-name")).toHaveText("Empresa Piloto Ficticia");

  // 4) cierre de sesión: una escritura con CSRF desde la SPA, y el navegador ACEPTA el borrado de la cookie __Host-
  await page.getByTestId("logout").click();
  await expect(page.getByTestId("login-submit")).toBeVisible({ timeout: 15_000 });
  expect((await context.cookies()).some(x => x.name === "__Host-aicos_session"), "la cookie sigue viva tras cerrar sesión").toBe(false);
  expect((await page.request.get("/api/auth/me")).status()).toBe(401);
});

test("V0.7 P2 — piloto: una operación sensible con la autenticación vencida pide la contraseña, se completa al confirmarla y se cancela sin efectos", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("email").fill("ana@piloto.test");
  await page.getByTestId("password").fill(PW);
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-name")).toBeVisible({ timeout: 15_000 });
  const me = await (await page.request.get("/api/auth/me")).json();
  expect(me.step_up).toMatchObject({ enforced: true, max_age_seconds: 3 });
  expect(me.auth_time).toBeTruthy();

  await page.goto("/#/config");
  await expect(page.getByTestId("add-employee-form")).toBeVisible();
  await page.waitForTimeout(4_000);                                       // la ventana del piloto en este E2E es de 3 s: la autenticación ya venció
  await page.getByTestId("e-name").fill("Beto Técnico");
  await page.getByTestId("e-email").fill("beto@piloto.test");
  await page.getByTestId("e-submit").click();
  await expect(page.getByTestId("reauth-dialog")).toBeVisible();          // el servidor respondió 401 reauth_required y la SPA abrió el cuadro
  await expect(page.getByTestId("e-credentials")).toHaveCount(0);
  await page.getByTestId("reauth-password").fill("incorrecta-123");
  await page.getByTestId("reauth-submit").click();
  await expect(page.getByTestId("reauth-error")).toBeVisible();           // una contraseña equivocada NO reautentica
  await expect(page.getByTestId("e-credentials")).toHaveCount(0);
  await page.getByTestId("reauth-password").fill(PW);
  await page.getByTestId("reauth-submit").click();
  await expect(page.getByTestId("reauth-dialog")).toHaveCount(0);
  await expect(page.getByTestId("e-credentials")).toBeVisible({ timeout: 15_000 });   // la MISMA operación se reintentó y se completó

  await page.waitForTimeout(4_000);                                       // vuelve a vencer: ahora el usuario cancela
  await page.getByTestId("e-name").fill("Carla Cancelada");
  await page.getByTestId("e-email").fill("carla@piloto.test");
  await page.getByTestId("e-submit").click();
  await expect(page.getByTestId("reauth-dialog")).toBeVisible();
  await page.getByTestId("reauth-cancel").click();
  await expect(page.getByTestId("reauth-dialog")).toHaveCount(0);
  await expect(page.getByTestId("cfg-error")).toContainText("confirmar tu identidad");
  const emails = (await (await page.request.get("/api/config/overview")).json()).employees.map((e: any) => e.email);
  expect(emails).toContain("beto@piloto.test");
  expect(emails).not.toContain("carla@piloto.test");                      // cancelar no dejó ningún efecto
});
