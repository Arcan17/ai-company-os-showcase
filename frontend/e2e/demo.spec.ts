import { Browser, expect, Page, test } from "@playwright/test";
import { apost, newCtx } from "./csrf";

const SHOTS = "../docs/screenshots";
let n = 0;
const uniq = () => `e2e-${Date.now().toString(36)}-${n++}`;

async function login(page: Page, email: string) {
  const r = await apost(page, "/api/auth/login", { data: { email, password: "demo1234" } });
  expect(r.ok()).toBeTruthy();
}
async function sendScenario(page: Page, slug: string, id: string) {
  await page.goto(`/#/cliente/${slug}`);
  await page.getByTestId("scenario-" + id).click();
  await page.getByTestId("send-chat").click();
  await expect(page.getByTestId("conversation")).toBeVisible();
}
const status = (page: Page, node: string) => page.getByTestId("node-" + node).getAttribute("data-status");

test.describe.configure({ mode: "serial" });

test("login: real auth, demo accounts clearly labelled, bad password rejected", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("demo-accounts")).toContainText("DEMOSTRACIÓN");
  await page.getByTestId("email").fill("dueno@climasur.test");
  await page.getByTestId("password").fill("incorrecta");
  await page.getByTestId("login-submit").click();
  await expect(page.getByRole("alert")).toContainText("credenciales inválidas");
  await page.screenshot({ path: `${SHOTS}/01-login.png` });
  await page.getByTestId("demo-dueno@climasur.test").click();
  await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-name")).toHaveText("Carolina Vega");
});

test("scenario A: full journey is visible in the graph, human approval, proposal NOT confirmed", async ({ page }) => {
  await login(page, "dueno@climasur.test");
  await sendScenario(page, "clima-sur", "A");
  await page.getByTestId("open-run").click();
  // the run pauses for HUMAN approval; the graph shows who did what from persisted events
  await expect(page.getByTestId("approval-card")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("approval-card")).toContainText("$63.000");
  expect(await status(page, "reception")).toBe("done");
  expect(await status(page, "admin_assistant")).toBe("done");
  expect(await status(page, "owner_human")).toBe("waiting");
  await page.getByTestId("node-admin_assistant").click();
  const ev = page.getByTestId("inspector-event").filter({ hasText: "quotes.prepare" }).last();
  await ev.locator("summary").click();
  await expect(page.getByTestId("inspector")).toContainText("part_skus"); // real tool input
  await expect(page.getByTestId("inspector")).toContainText("GAS-R410");
  await expect(page.getByTestId("inspector")).toContainText("BD escritura");
  await page.screenshot({ path: `${SHOTS}/02-flow-waiting-approval.png` });
  // step-by-step replay: rewinding hides later states
  const total = Number((await page.getByTestId("step-counter").textContent())!.split("/")[1]);
  await page.getByTestId("scrubber").fill("2");
  await expect(page.getByTestId("step-counter")).toHaveText(`2/${total}`);
  expect(await status(page, "admin_assistant")).toBe("idle");
  await page.getByRole("button", { name: "En vivo" }).click();
  await page.getByTestId("approve").click();
  await expect(page.getByTestId("event-feed")).toContainText("Propuesta preparada", { timeout: 20_000 });
  expect(await status(page, "tech_assistant")).toBe("done");
  expect(await status(page, "owner_human")).toBe("done");
  await expect(page.getByTestId("event-feed")).toContainText("NO está confirmada");
  await page.screenshot({ path: `${SHOTS}/03-flow-completed.png` });
  // operations: visit is PROPOSED, not confirmed
  await page.goto("/#/operations");
  await page.getByTestId("request-row").filter({ hasText: "Marta Rojas" }).first().click();
  await expect(page.getByTestId("visit-status")).toContainText("NO confirmada");
  await page.screenshot({ path: `${SHOTS}/04-operations-detail.png` });
});

test("customer really confirms; retry with same message id does not duplicate", async ({ page }) => {
  await login(page, "dueno@climasur.test");
  await sendScenario(page, "clima-sur", "A");
  const before = await page.request.get("/api/runs").then(r => r.json());
  await page.getByTestId("resend-chat").click(); // same client_msg_id
  await page.getByTestId("resend-chat").click();
  const after = await page.request.get("/api/runs").then(r => r.json());
  expect(after.runs.length).toBe(before.runs.length); // idempotent: no new run
  await expect(page.getByTestId("customer-confirm")).toHaveCount(0); // nothing to confirm until approved
  await expect.poll(async () => (await page.request.get("/api/dashboard").then(r => r.json())).pending_approvals.length, { timeout: 20_000 }).toBe(1);
  const ap = (await page.request.get("/api/dashboard").then(r => r.json())).pending_approvals;
  for (const a of ap) await apost(page, `/api/approvals/${a.id}/decide`, { data: { decision: "approved" } });
  await expect(page.getByTestId("customer-confirm")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("customer-confirm").click();
  await expect(page.getByTestId("customer-confirm")).toContainText("confirmada por el cliente");
  await page.goto("/#/dashboard");
  await expect(page.getByText("Confirmada por el cliente").first()).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/05-dashboard.png` });
});

test("scenario B: missing part → blocked → alternative → approval → completed", async ({ page }) => {
  await login(page, "dueno@climasur.test");
  await sendScenario(page, "clima-sur", "B");
  await page.getByTestId("open-run").click();
  await expect(page.getByTestId("blocked-card")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("blocked-card")).toContainText("PLACA-INV");
  expect(await status(page, "admin_assistant")).toBe("blocked");
  await page.screenshot({ path: `${SHOTS}/06-flow-blocked.png` });
  await page.getByTestId("resolve-use_alternative_part").click();
  await expect(page.getByTestId("approval-card")).toContainText("$153.000", { timeout: 20_000 });
  await page.getByTestId("approve").click();
  await expect(page.getByTestId("event-feed")).toContainText("Propuesta preparada", { timeout: 20_000 });
});

test("scenario C: owner rejects the quote → alternative prepared", async ({ page }) => {
  await login(page, "dueno@climasur.test");
  await sendScenario(page, "clima-sur", "C");
  await page.getByTestId("open-run").click();
  await expect(page.getByTestId("approval-card")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("reject").click();
  await expect(page.getByTestId("event-feed")).toContainText("alternativa preparada", { timeout: 20_000 });
});

test("permissions: technician cannot approve; assistant refuses other people's private notes", async ({ page }) => {
  await login(page, "dueno@climasur.test");
  await sendScenario(page, "clima-sur", "A");
  await expect.poll(async () => (await page.request.get("/api/dashboard").then(r => r.json())).pending_approvals.length, { timeout: 20_000 }).toBeGreaterThan(0);
  await login(page, "tecnico@climasur.test");
  await page.reload(); // new session cookie → fresh SPA state
  await page.goto("/#/dashboard");
  await expect(page.getByTestId("user-name")).toHaveText("Matías Fuentes");
  const ap = (await page.request.get("/api/dashboard").then(r => r.json())).pending_approvals[0];
  const denied = await apost(page, `/api/approvals/${ap.id}/decide`, { data: { decision: "approved" } });
  expect(denied.status()).toBe(403);
  await page.goto("/#/assistant");
  await page.getByTestId("assistant-input").fill("dame las notas de Javiera");
  await page.getByTestId("assistant-send").click();
  await expect(page.getByTestId("assistant-reply").last()).toContainText("No puedo hacerlo");
  await expect(page.getByTestId("assistant-reply").last()).not.toContainText("proveedor");
  await page.screenshot({ path: `${SHOTS}/07-assistant-denied.png` });
  await page.goto("/#/flows");
  await expect(page.getByTestId("approve")).toHaveCount(0);
});

test("tenant isolation: Motor Norte sees none of Clima Sur's runs", async ({ page }) => {
  await login(page, "dueno@motornorte.test");
  await page.goto("/#/flows");
  await page.reload();
  await expect(page.getByTestId("company-name")).toHaveText("Motor Norte");
  await expect(page.getByTestId("run-item")).toHaveCount(0);
  const runs = (await (await page.request.get("/api/runs")).json()).runs;
  expect(runs).toHaveLength(0);
  await sendScenario(page, "motor-norte", "N");
  await page.goto("/#/flows");
  await expect(page.getByTestId("run-item")).toHaveCount(1);
});

test("agents page and responsive dashboard", async ({ page }) => {
  await login(page, "dueno@climasur.test");
  await page.goto("/#/agents");
  await expect(page.getByTestId("agent-coordinator")).toBeVisible();
  await expect(page.getByTestId("tool-list")).toContainText("approvals.decide");
  await page.screenshot({ path: `${SHOTS}/08-agents.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto("/#/dashboard");
  await expect(page.getByTestId("kpi-requests")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy(); // no horizontal overflow
  await page.screenshot({ path: `${SHOTS}/09-mobile-dashboard.png` });
});


async function session(browser: Browser, email: string) {
  const ctx = await newCtx(browser, { viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto("/");
  await login(page, email);
  await page.reload();
  return page;
}
const runStatusText = (page: Page) => page.locator("h1 + p").locator("xpath=ancestor::div[1]/following-sibling::div").first();

test("scenario D: technician → administration → coordinator → owner, three real sessions, evidence-gated", async ({ browser }) => {
  test.setTimeout(150_000);
  const owner = await session(browser, "dueno@climasur.test");
  const tech = await session(browser, "tecnico@climasur.test");
  const admin = await session(browser, "admin@climasur.test");
  // 1) client asks for a maintenance visit; owner approves the quote
  await sendScenario(owner, "clima-sur", "D");
  await owner.getByTestId("open-run").click();
  await expect(owner.getByTestId("approval-card")).toContainText("$35.000", { timeout: 25_000 });
  await owner.getByTestId("approve").click();
  await expect(owner.getByTestId("event-feed")).toContainText("debe informar materiales", { timeout: 25_000 });
  expect(await status(owner, "tech_human")).toBe("waiting");
  await owner.screenshot({ path: `${SHOTS}/10-d-waiting-technician.png` });
  // 2) the technician sees it in HIS session and asks for material through his assistant
  await tech.goto("/#/dashboard");
  await expect(tech.getByTestId("attention")).toContainText("espera tu informe de material", { timeout: 15_000 });
  await tech.goto("/#/assistant");
  await tech.getByTestId("assistant-input").fill("necesito dos cilindros de gas para la visita de Marta antes de ir");
  await tech.getByTestId("assistant-send").click();
  await expect(tech.getByTestId("assistant-reply").last()).toContainText("pedí el material a administración", { timeout: 30_000 });
  await expect(tech.getByTestId("assistant-reply").last()).toContainText("Javiera Soto");
  // 3) coordinator asks the owner for the spend; nothing moves without the human decision
  await expect(owner.getByTestId("approval-card")).toContainText("$36.000", { timeout: 30_000 });
  expect(await status(owner, "admin_human")).toBe("waiting");
  await admin.goto("/#/assistant");
  await expect(admin.getByTestId("my-task").filter({ hasText: "Preparar y entregar" })).toBeVisible({ timeout: 15_000 });
  await admin.getByTestId("evidence-input").first().fill("ya lo entregué en bodega");
  await admin.getByTestId("complete-task").first().click();
  await expect(admin.getByTestId("task-msg")).toContainText("aprobación económica"); // rejected: no approval yet → not "resolved"
  await owner.screenshot({ path: `${SHOTS}/11-d-material-approval.png` });
  await owner.getByTestId("approve").click();
  await expect(owner.getByTestId("event-feed")).toContainText("debe entregar el material", { timeout: 30_000 });
  // 4) administration completes WITH evidence → real stock movement; 5) technician confirms reception
  await admin.getByTestId("evidence-input").first().fill("entregué 2 cilindros de gas al técnico en bodega");
  await admin.getByTestId("complete-task").first().click();
  await expect(admin.getByTestId("task-msg")).toContainText("stock GAS-R410 −2");
  const inv = await apost(admin, "/api/tools/inventory.check/invoke", { data: { args: { skus: ["GAS-R410"] } } }).then(r => r.json());
  expect(inv.parts[0].stock).toBe(10);
  await tech.reload();
  await tech.goto("/#/assistant");
  await expect(tech.getByTestId("my-task").filter({ hasText: "Confirmar recepción" })).toBeVisible({ timeout: 20_000 });
  await tech.getByTestId("evidence-input").first().fill("recibí los 2 cilindros y revisé los sellos");
  await tech.getByTestId("complete-task").first().click();
  await expect(tech.getByTestId("task-msg")).toContainText("material recibido");
  // 6) the visualizer shows the whole thing from persisted events
  await expect(owner.getByTestId("event-feed")).toContainText("Material entregado y recibido", { timeout: 30_000 });
  expect(await status(owner, "admin_human")).toBe("done");
  expect(await status(owner, "tech_human")).toBe("done");
  await owner.getByTestId("node-tech_assistant").click();
  await expect(owner.getByTestId("real-state")).toContainText("tech_assistant");
  await expect(owner.getByTestId("inspector")).toContainText("collab.request_material");
  await owner.screenshot({ path: `${SHOTS}/12-d-completed-inspector.png` });
  await owner.getByTestId("node-admin_human").click();
  await expect(owner.getByTestId("real-state")).toContainText("Evidencia: entregué 2 cilindros");
  // 7) commitments: simulated clock creates internal reminders + an unsent draft, idempotently
  await owner.goto("/#/dashboard");
  await owner.getByTestId("process-due").click();
  await expect(owner.getByTestId("dash-msg")).toContainText("procesado");
  await owner.getByTestId("process-due").click();
  await expect(owner.getByTestId("dash-msg")).toContainText("0 compromiso(s)");
  await owner.screenshot({ path: `${SHOTS}/13-d-dashboard-commitments.png`, fullPage: true });
  const run = (await owner.request.get("/api/runs").then(r => r.json())).runs.find((r: any) => r.scenario === "D");
  const detail = await owner.request.get(`/api/runs/${run.id}`).then(r => r.json());
  expect(detail.status).toBe("completed");
  expect(detail.drafts.every((d: any) => d.status !== "enviado")).toBeTruthy();
  for (const p of [owner, tech, admin]) await p.context().close();
});

// ---------------------------------------------------------------- V0.4 — conversational coordinator (scenario E)
async function openRequest(page: Page, name: string, phone: string, message: string) {
  const r = await apost(page, "/api/public/clima-sur/chat", { data: { message, name, phone, client_msg_id: uniq() } });
  expect(r.ok()).toBeTruthy();
}
const planTask = (page: Page, text: string) => page.getByTestId("my-task").filter({ hasText: text });

test("scenario E1: owner instruction → visible plan → human confirms → admin + technician answer with evidence → verified result", async ({ browser }) => {
  test.setTimeout(150_000);
  const owner = await session(browser, "dueno@climasur.test");
  const admin = await session(browser, "admin@climasur.test");
  const tech = await session(browser, "tecnico@climasur.test");
  await openRequest(owner, "Oficina Andes SpA", "+56 2 5555 0103", "Quiero una mantención preventiva de los equipos de la oficina, por favor.");
  await expect.poll(async () => (await owner.request.get("/api/requests").then(r => r.json())).requests.filter((x: any) => x.customer === "Oficina Andes SpA" && x.status !== "cerrada").length, { timeout: 20_000 }).toBeGreaterThan(0);
  // 1) the owner gives NEW work in natural language
  await owner.goto("/#/coordinator");
  await owner.getByTestId("plan-input").fill("Revisa las mantenciones pendientes y asigna a administración que verifique materiales para la visita de Oficina Andes; quiero confirmación del técnico");
  await owner.getByTestId("plan-submit").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando tu confirmación", { timeout: 20_000 });
  await expect(owner.getByTestId("plan-step")).toHaveCount(4);
  await expect(owner.getByTestId("plan-steps")).toContainText("Javiera Soto");
  await expect(owner.getByTestId("plan-steps")).toContainText("Matías Fuentes");
  // 2) nothing exists yet for the employees (the plan is only a proposal)
  expect((await admin.request.get("/api/tasks").then(r => r.json())).tasks.filter((t: any) => t.kind === "plan_step")).toHaveLength(0);
  await owner.screenshot({ path: `${SHOTS}/14-e-plan-proposal.png` });
  // 3) the HUMAN confirms; only then delegations/tasks are created
  await owner.getByTestId("plan-confirm").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando a una persona", { timeout: 20_000 });
  await admin.goto("/#/assistant");
  await expect(planTask(admin, "Verificar los materiales")).toBeVisible({ timeout: 20_000 });
  await tech.goto("/#/assistant");
  await expect(planTask(tech, "Confirmar con tu evidencia")).toHaveCount(0); // the technician's task waits for administration
  // 4) administration answers from ITS assistant, with evidence
  await admin.getByTestId("evidence-input").first().fill("ok");
  await admin.getByTestId("complete-task").first().click();
  await expect(admin.getByTestId("task-msg")).toContainText("No se pudo"); // evidence too short → rejected, plan still waiting
  await admin.getByTestId("evidence-input").first().fill("Revisé el inventario: kit de mantención completo y gas R410 con 12 unidades");
  await admin.getByTestId("complete-task").first().click();
  await expect(admin.getByTestId("task-msg")).toContainText("Tarea completada");
  // 5) dependency met → the technician's task appears → answers with evidence
  await expect(planTask(tech, "Confirmar con tu evidencia")).toBeVisible({ timeout: 25_000 });
  await tech.getByTestId("evidence-input").first().fill("Confirmado: llevo el kit y puedo hacer la visita sin material adicional");
  await tech.getByTestId("complete-task").first().click();
  await expect(tech.getByTestId("task-msg")).toContainText("Tarea completada");
  // 6) the coordinator verified the REGISTRY (not a claim) and reported
  await expect(owner.getByTestId("plan-status")).toContainText("Completada", { timeout: 25_000 });
  await expect(owner.getByTestId("plan-result")).toContainText("No faltan materiales");
  await expect(owner.getByTestId("plan-result")).toContainText("Revisé el inventario");
  await expect(owner.getByTestId("step-evidence").first()).toContainText("Javiera Soto");
  await owner.screenshot({ path: `${SHOTS}/15-e-plan-completed.png` });
  // 7) the visualizer replays the real persisted events of the plan, labelled by kind
  await owner.getByTestId("plan-open-flow").click();
  await expect(owner.getByTestId("plan-panel")).toContainText("hecho");
  await expect(owner.getByTestId("event-feed")).toContainText("Plan completado", { timeout: 20_000 });
  expect(await status(owner, "admin_human")).toBe("done");
  expect(await status(owner, "tech_human")).toBe("done");
  expect(await status(owner, "owner_human")).toBe("done");
  const stages = await owner.getByTestId("stage-chip").allTextContents();
  for (const s of ["Plan validado", "Decisión humana", "Acción ejecutada", "Espera de humano", "Resultado respaldado"]) expect(stages.join("|")).toContain(s);
  await owner.getByTestId("node-coordinator").click();
  await expect(owner.getByTestId("inspector")).toContainText("plans.assign_task");
  await owner.screenshot({ path: `${SHOTS}/16-e-flow-plan.png` });
  // 8) no stock moved, nobody was messaged, scenario D data is untouched
  const stock = await apost(admin, "/api/tools/inventory.check/invoke", { data: { args: { skus: ["GAS-R410"] } } }).then(r => r.json());
  expect(stock.parts[0].stock).toBe(10); // 12 − the 2 delivered by scenario D earlier; the coordinator changed nothing
  for (const p of [owner, tech, admin]) await p.context().close();
});

test("scenario E2: dangerous or ambiguous instructions never run; clarification, cancel and tenant isolation", async ({ browser }) => {
  test.setTimeout(120_000);
  const owner = await session(browser, "dueno@climasur.test");
  const norte = await session(browser, "dueno@motornorte.test");
  await owner.goto("/#/coordinator");
  // dangerous: purchase + external message → blocked, nothing proposed
  await owner.getByTestId("plan-input").fill("Compra repuestos y avisa por WhatsApp a los clientes");
  await owner.getByTestId("plan-submit").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Bloqueada", { timeout: 20_000 });
  await expect(owner.getByTestId("plan-excluded")).toContainText("Comprar");
  await expect(owner.getByTestId("plan-excluded")).toContainText("mensajes");
  await expect(owner.getByTestId("plan-confirm")).toHaveCount(0);
  await expect(owner.getByTestId("plan-step")).toHaveCount(0);
  await owner.screenshot({ path: `${SHOTS}/17-e2-blocked.png` });
  // ambiguous: Marta has several open requests → the coordinator asks instead of guessing
  await openRequest(owner, "Marta Rojas", "+56 9 5555 0101", "Necesito una mantención preventiva del split del dormitorio.");
  await openRequest(owner, "Marta Rojas", "+56 9 5555 0101", "Necesito otra mantención preventiva para el split de la cocina.");
  await owner.getByTestId("plan-input").fill("Pide a Javiera que confirme los materiales del mantenimiento de Marta y avísame si falta algo antes de agendar");
  await owner.getByTestId("plan-submit").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Necesita aclaración", { timeout: 20_000 });
  expect(await owner.getByTestId("clarify-option").count()).toBeGreaterThan(1);
  await owner.goto("/#/dashboard");
  await expect(owner.getByTestId("attention")).toContainText("necesita una aclaración", { timeout: 15_000 });
  await owner.goBack();
  await owner.getByTestId("clarify-option").first().click();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando tu confirmación", { timeout: 20_000 });
  await expect(owner.getByTestId("plan-excluded")).toHaveCount(0); // «antes de agendar» is a precondition, not an order
  const planId = (await owner.getByTestId("plan-card").locator("div.text-xs").first().textContent())!.split(" ")[0];
  // another tenant cannot see, confirm or cancel it
  expect((await norte.request.get(`/api/plans/${planId}`)).status()).toBe(404);
  expect((await apost(norte, `/api/plans/${planId}/confirm`)).status()).toBe(404);
  // cancelling cancels the unexecuted steps and creates nothing
  await owner.getByTestId("plan-cancel").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Cancelado", { timeout: 15_000 });
  const tasks = await owner.request.get(`/api/plans/${planId}`).then(r => r.json());
  expect(tasks.tasks).toHaveLength(0);
  for (const p of [owner, norte]) await p.context().close();
});
