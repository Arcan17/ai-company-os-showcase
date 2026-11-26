import { expect, test } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";

// Recorrido GRABABLE de la demo (spec 006): la dueña decide, ve la trazabilidad, prueba una orden prohibida y —si hay IA local— ve a la IA proponer un plan.
// Con RECORD_VIDEO=1 lleva pausas y escribe `marks.json` (cuándo empieza cada escena) para cortar el video; sin él corre rápido (prueba de CI).
// Todo es real: el mismo servidor y la misma interfaz; solo se agregan un cursor y rótulos en pantalla, y pausas para que se pueda leer.
const FAST = process.env.RECORD_VIDEO !== "1";
const IA = process.env.RECORD_IA === "1";
const NOCAP = process.env.REC_NOCAPTION === "1"; // sin rótulos en pantalla: para montar el video aparte (rótulos propios)
const OUT = process.env.REC_OUT ?? "../grabaciones/raw";
const SHOTS = process.env.REC_SHOTS ?? "../grabaciones/capturas";

const OVERLAY = () => {
  const mk = () => {
    if (document.getElementById("rec-cursor")) return;
    const c = document.createElement("div"); c.id = "rec-cursor";
    c.style.cssText = "position:fixed;z-index:2147483647;width:24px;height:24px;border-radius:50%;background:rgba(99,102,241,.30);border:2px solid #4f46e5;pointer-events:none;transform:translate(-50%,-50%);left:-60px;top:-60px;transition:transform .12s ease";
    const k = document.createElement("div"); k.id = "rec-caption";
    k.style.cssText = "position:fixed;z-index:2147483646;left:50%;bottom:24px;transform:translateX(-50%);max-width:min(70vw,860px);padding:11px 20px;border-radius:14px;background:rgba(15,23,42,.90);color:#fff;font:600 17px/1.3 system-ui,sans-serif;text-align:center;pointer-events:none;display:none;box-shadow:0 6px 24px rgba(0,0,0,.25)";
    document.documentElement.append(c, k);
    addEventListener("mousemove", e => { c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px"; }, true);
    addEventListener("mousedown", () => { c.style.transform = "translate(-50%,-50%) scale(.65)"; }, true);
    addEventListener("mouseup", () => { c.style.transform = "translate(-50%,-50%) scale(1)"; }, true);
  };
  if (document.readyState === "loading") addEventListener("DOMContentLoaded", mk); else mk();
};

test("recorrido de la demo: decidir, trazabilidad, lo prohibido y la IA", async ({ page }) => {
  const marks: { escena: string; epoch: number }[] = [];
  // Captura de cuadros por el protocolo del navegador (CDP screencast): no descarga nada; solo emite un cuadro cuando la pantalla cambia, así que se guarda cuándo ocurrió cada uno.
  const frames: { f: string; t: number }[] = []; let cdp: any = null;
  if (!FAST) {
    mkdirSync(`${OUT}/frames`, { recursive: true });
    cdp = await page.context().newCDPSession(page);
    cdp.on("Page.screencastFrame", async (e: any) => {
      const f = `frames/f${String(frames.length).padStart(6, "0")}.jpg`;
      writeFileSync(`${OUT}/${f}`, Buffer.from(e.data, "base64")); frames.push({ f, t: e.metadata.timestamp });
      await cdp.send("Page.screencastFrameAck", { sessionId: e.sessionId }).catch(() => {});
    });
    await cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, maxWidth: 1280, maxHeight: 800, everyNthFrame: 1 });
  }
  const pause = (ms: number) => (FAST ? Promise.resolve() : page.waitForTimeout(ms));
  const caption = (t: string) => NOCAP ? Promise.resolve() : page.evaluate(x => { const e = document.getElementById("rec-caption"); if (!e) return; if (!x) { e.style.display = "none"; return; } e.textContent = x; e.style.display = "block"; }, t);
  const scene = async (name: string, text: string) => { marks.push({ escena: name, epoch: Date.now() / 1000 }); await caption(text); };
  const aim = async (loc: any) => { await loc.scrollIntoViewIfNeeded(); const b = await loc.boundingBox(); if (b) await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: FAST ? 1 : 30 }); await pause(350); };
  const tap = async (loc: any) => { await aim(loc); await loc.click(); };
  const shot = async (n: string) => { if (FAST) return; mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: `${SHOTS}/${n}.png` }); };
  await page.addInitScript(OVERLAY);
  await page.addInitScript(() => { try { localStorage.setItem("aicos.tour", "closed"); } catch { /* sin almacenamiento */ } });   // el recorrido guiado se muestra aparte; aquí se graba el producto

  // 1 · entrada
  await page.goto("/");
  await scene("01-entrada", "Entrada con cuentas ficticias de demostración (datos 100 % de prueba)");
  await pause(2500);
  await tap(page.getByTestId("demo-dueno@climasur.test")); await pause(700);
  await tap(page.getByTestId("login-submit"));

  // 2 · la dueña abre y ya hay una decisión (el producto en uso, no una pantalla vacía)
  await expect(page.getByTestId("s-decision")).toContainText("$63.000");
  await scene("02-decision", "Al abrir, el producto ya está en uso: una solicitud espera la decisión de la dueña");
  await pause(3500); await shot("01-decision-pendiente");
  await caption("Resultados: solo cifras que el sistema ya registró; sin datos dice «aún sin datos»");
  await aim(page.getByTestId("results-card")); await pause(3500);

  // 3 · aprueba una persona
  await scene("03-aprobar", "Ningún agente puede aprobar gastos: decide una persona");
  await pause(2200);
  await tap(page.getByTestId("s-approve"));
  await expect(page.getByTestId("results-decided")).toHaveText("1", { timeout: 20_000 });
  await caption("Quedó registrado: decisiones tomadas = 1 (la cifra sale del registro real)");
  await pause(3500); await shot("02-aprobada");

  // 4 · trazabilidad en el visualizador
  await scene("04-trazabilidad", "Modo Completo: cada paso queda registrado, con sus datos reales");
  await tap(page.getByTestId("mode-complete")); await pause(900);
  await page.evaluate(() => { location.hash = "#/flows"; });
  await expect(page.getByTestId("run-item").first()).toBeVisible({ timeout: 20_000 });
  await tap(page.getByTestId("run-item").first());
  const nodes = page.locator('[data-testid^="node-"]');
  await expect(nodes.first()).toBeVisible({ timeout: 20_000 });
  await pause(2500);
  await tap(nodes.nth(Math.min(2, (await nodes.count()) - 1)));
  await expect(page.getByTestId("inspector")).toBeVisible();
  await caption("Cada nodo es una fila real: qué herramienta se usó, con qué datos y qué devolvió");
  await pause(4500); await shot("03-visualizador");

  // 5 · lo prohibido
  await page.evaluate(() => { location.hash = "#/coordinator"; });
  await expect(page.getByTestId("plan-input")).toBeVisible();
  await scene("05-prohibido", "Pídele algo que no puede hacer: comprar y avisar por WhatsApp");
  await tap(page.getByTestId("plan-input"));
  await page.getByTestId("plan-input").pressSequentially("Compra repuestos y avisa por WhatsApp a los clientes", { delay: FAST ? 0 : 38 });
  await pause(500); await tap(page.getByTestId("plan-submit"));
  await expect(page.getByTestId("plan-status").first()).toContainText(/bloquead/i, { timeout: 30_000 });
  await caption("Lo prohibido se bloquea: comprar y enviar mensajes no existen en su catálogo");
  await pause(4500); await shot("04-bloqueado");

  // 6 · un plan real (con IA local si se grabó con --ia)
  const instruccion = "Pide a administración que verifique los materiales de la visita de Marta Rojas y quiero confirmación del técnico";
  await scene("06-plan", IA ? "Un plan de verdad: la IA propone, el motor valida" : "Un plan de verdad: el motor arma el plan y lo valida");
  await page.getByTestId("plan-input").fill(""); await tap(page.getByTestId("plan-input"));
  await page.getByTestId("plan-input").pressSequentially(instruccion, { delay: FAST ? 0 : 30 });
  await pause(500); await tap(page.getByTestId("plan-submit"));
  if (IA) { await expect(page.getByTestId("plan-thinking")).toBeVisible({ timeout: 15_000 }); await caption("La IA local está pensando… luego el motor valida el plan"); }
  await expect(page.getByText(IA ? /Propuesto por IA/ : /Reglas deterministas/).first()).toBeVisible({ timeout: IA ? 180_000 : 30_000 });   // quién propuso el plan
  await expect(page.getByTestId("plan-status").first()).toContainText(/confirmaci/i);
  await caption(IA ? "Propuesto por IA · validado por el motor · y solo se ejecuta si una persona lo confirma" : "Validado por el motor · y solo se ejecuta si una persona lo confirma");
  await pause(5000); await shot("05-plan");

  // 7 · cierre
  await scene("07-cierre", "Datos ficticios · WhatsApp y correo simulados · aprobación humana siempre");
  await pause(3500);
  await caption("");
  if (cdp && frames.length > 1) {
    await cdp.send("Page.stopScreencast").catch(() => {});
    const t0 = frames[0].t, fin = Date.now() / 1000;
    // lista para `ffmpeg -f concat`: cada cuadro dura hasta el siguiente (la pantalla quieta no emite cuadros)
    const lista = frames.map((x, i) => `file '${x.f}'\nduration ${(((frames[i + 1]?.t ?? t0 + (fin - t0)) - x.t) || 0.04).toFixed(3)}`).join("\n") + `\nfile '${frames[frames.length - 1].f}'\n`;
    writeFileSync(`${OUT}/frames.txt`, lista);
    writeFileSync(`${OUT}/marks.json`, JSON.stringify({ duracion: fin - t0, ia: IA, escenas: marks.map(m => ({ escena: m.escena, desde: Math.max(0, m.epoch - t0) })) }, null, 1));
  }
});
