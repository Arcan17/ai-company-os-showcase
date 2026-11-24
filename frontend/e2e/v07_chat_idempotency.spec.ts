// Issue #20 en un NAVEGADOR REAL: el widget del chat público genera el id del mensaje y el secreto de reintento con el generador criptográfico del navegador (128 bits, no Math.random)
// y un reintento con el MISMO par recupera la MISMA conversación sin duplicar nada.
import { expect, test } from "@playwright/test";

test("Issue #20 — el chat público envía un id y un secreto de 128 bits y el reintento recupera la misma conversación", async ({ page }) => {
  const posts: any[] = [], answers: any[] = [];
  page.on("request", r => { if (r.method() === "POST" && r.url().includes("/api/public/clima-sur/chat")) posts.push(r.postDataJSON()); });
  page.on("response", async r => { if (r.request().method() === "POST" && r.url().includes("/api/public/clima-sur/chat")) { try { answers.push(await r.json()); } catch { /* la página se cerró antes de leer el cuerpo */ } } });
  await page.goto("/#/cliente/clima-sur");
  await page.getByTestId("send-chat").click();
  await expect(page.getByTestId("resend-chat")).toBeVisible();
  await page.getByTestId("resend-chat").click();
  await expect.poll(() => answers.length, { timeout: 15_000 }).toBe(2);
  const [a, b] = posts;
  expect(a.client_msg_id).toMatch(/^web-[A-Za-z0-9_-]{22}$/);                 // 16 bytes del CSPRNG en base64url (antes: 8 caracteres de Math.random)
  expect(a.retry_secret).toMatch(/^[A-Za-z0-9_-]{22}$/);
  expect(a.retry_secret).not.toBe(a.client_msg_id.slice(4));                   // el secreto NO es el id
  expect(b).toEqual(a);                                                        // el reintento lleva el MISMO par
  expect(answers[0].deduplicated).toBe(false); expect(answers[1].deduplicated).toBe(true);
  expect(answers[1].run_id).toBe(answers[0].run_id); expect(answers[1].conversation_token).toBe(answers[0].conversation_token);
  // otro escenario = otro mensaje: id y secreto NUEVOS
  await page.getByTestId("scenario-B").click();
  await page.getByTestId("send-chat").click();
  await expect.poll(() => posts.length, { timeout: 15_000 }).toBe(3);
  expect(posts[2].client_msg_id).not.toBe(a.client_msg_id); expect(posts[2].retry_secret).not.toBe(a.retry_secret);
});
