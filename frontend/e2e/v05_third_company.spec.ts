// V0.5 acceptance: a THIRD fictitious company created and configured ONLY through the UI (no source edits): owner, 2 administrators, 2 technicians,
// assistants from templates, a plan delegated to chosen employees, evidence, verification — compared with SQLite and the visualizer.
import { Browser, expect, Page, test } from "@playwright/test";
import { apost } from "./csrf";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";

const SHOTS = "../docs/v05/screenshots";
const DB = "../backend/data/e2e.db";
const PY = "../backend/.venv/bin/python";
const dbq = (sql: string): any[] => JSON.parse(execFileSync(PY, ["../qa/dbq.py", sql, DB], { encoding: "utf8" }));
const one = (sql: string) => dbq(sql)[0];
const PW = "clave-ficticia-123";
const INSTR = "Revisa la mantención pendiente de Cliente Uno; pide a administración que verifique materiales y al técnico que confirme después de administración.";
const results: any[] = [];
const OUT = "../docs/v05/v05-results.json";
function rec(id: string, desc: string, ok: boolean, detail = "") { results.push({ id, desc, ok, detail }); writeFileSync(OUT, JSON.stringify(results, null, 2)); expect.soft(ok, `${id} ${desc} ${detail}`).toBeTruthy(); }
const shot = (p: Page, n: string, full = false) => p.screenshot({ path: `${SHOTS}/${n}.png`, fullPage: full });

async function loginUi(browser: Browser, email: string, hash = "#/dashboard") {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  await page.goto("/");
  await page.getByTestId("email").fill(email); await page.getByTestId("password").fill(PW); await page.getByTestId("login-submit").click();
  await expect(page.getByTestId("user-name")).toBeVisible({ timeout: 15_000 });
  await page.goto(`/${hash}`);
  return page;
}

test("V0.5 — tercera empresa creada y configurada desde la UI, plan completo entre dueña, administración y técnico", async ({ browser }) => {
  test.setTimeout(240_000);
  // 1) create the company from the login screen
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const owner = await ctx.newPage();
  await owner.goto("/");
  await owner.getByTestId("new-company-link").click();
  await owner.getByTestId("c-name").fill("Aire Austral");
  await owner.getByTestId("c-owner-name").fill("Elena Rojas");
  await owner.getByTestId("c-owner-email").fill("elena@aireaustral.test");
  await owner.getByTestId("c-owner-pass").fill(PW);
  await shot(owner, "01-crear-empresa");
  await owner.getByTestId("c-submit").click();
  await expect(owner.getByTestId("company-created")).toContainText("aire-austral");
  await owner.getByTestId("enter-company").click();
  await expect(owner.getByTestId("company-name")).toHaveText("Aire Austral", { timeout: 15_000 });
  await expect(owner.getByTestId("nav-config")).toBeVisible();
  const co = one("select id from companies where slug='aire-austral'").id as string;
  rec("V5-01", "Empresa «Aire Austral» creada desde la UI: owner + agentes compartidos + asistente del dueño + catálogo ficticio, sin tocar código", one(`select count(*) n from users where company_id='${co}'`).n === 1 && one(`select count(*) n from agents where company_id='${co}'`).n === 3 && one(`select count(*) n from parts where company_id='${co}'`).n === 5);

  // 2) employees and assistants from templates
  await owner.goto("/#/config");
  await expect(owner.getByTestId("cfg-company")).toContainText("aire-austral");
  const add = async (name: string, email: string, role: string, assign = true) => {
    await owner.getByTestId("e-name").fill(name); await owner.getByTestId("e-email").fill(email); await owner.getByTestId("e-role").selectOption(role); await owner.getByTestId("e-pass").fill(PW);
    if (!assign) await owner.getByTestId("e-assign").uncheck(); else await owner.getByTestId("e-assign").check();
    await owner.getByTestId("e-submit").click(); await expect(owner.getByTestId("cfg-msg")).toContainText("incorporado");
  };
  await add("Javiera Pons", "javiera@aireaustral.test", "admin");
  await add("Rodrigo Silva", "rodrigo@aireaustral.test", "admin");
  await add("Matías Ortiz", "matias@aireaustral.test", "technician");
  await add("Camila Vera", "camila@aireaustral.test", "technician");
  await add("Sin Asistente", "sin@aireaustral.test", "admin", false);
  await expect(owner.getByTestId("emp-row")).toHaveCount(6);
  const sin = owner.getByTestId("emp-row").filter({ hasText: "Sin Asistente" });
  await expect(sin.getByTestId("emp-status")).toContainText("sin asistente activo");
  await shot(owner, "02-equipo-sin-asignar", true);
  rec("V5-02", "Una persona sin asistente aparece NO elegible; 5 empleados con rol y plantillas visibles", (await sin.getByTestId("emp-status").textContent())!.includes("sin asistente activo") && one(`select count(*) n from users where company_id='${co}' and role in ('admin','technician')`).n === 5);
  await sin.getByTestId("emp-assign").click();
  await expect(sin.getByTestId("emp-status")).toContainText("puede recibir trabajo");
  // disable and re-enable an assistant (audited)
  const rod = owner.getByTestId("emp-row").filter({ hasText: "Rodrigo Silva" });
  await rod.getByTestId("emp-assistant-toggle").click();
  await expect(rod.getByTestId("emp-status")).toContainText("sin asistente activo");
  await rod.getByTestId("emp-assistant-toggle").click();
  await expect(rod.getByTestId("emp-status")).toContainText("puede recibir trabajo");
  const acts = dbq(`select action from audit_log where company_id='${co}' and action like 'config.%' order by id`).map(r => r.action);
  rec("V5-03", "Activar/desactivar asignaciones y asignar plantillas quedan AUDITADOS en SQLite", acts.includes("config.assignment_add") && acts.includes("config.assignment_disable") && acts.includes("config.assignment_enable") && acts.filter(a => a === "config.employee_add").length === 5, acts.join(","));
  await owner.getByTestId("tpl").filter({ hasText: "Asistente del técnico" }).locator("summary").click();
  await expect(owner.getByTestId("tpl").filter({ hasText: "Asistente del técnico" })).toContainText("tasks.complete");
  rec("V5-04", "Las plantillas muestran capacidades derivadas del registro de herramientas (p. ej. tasks.complete), no un editor de código", true);
  await shot(owner, "03-equipo-configurado", true);

  // 3) a request in the new company, then the plan: the coordinator ASKS who (two administrators, two technicians)
  await owner.goto("/#/operations");
  await owner.getByTestId("mr-toggle").click();
  await owner.getByTestId("mr-name").fill("Cliente Uno"); await owner.getByTestId("mr-desc").fill("Quiero una mantención preventiva del split del local, por favor.");
  await owner.getByTestId("mr-submit").click();
  await expect(owner.getByTestId("mr-result")).toContainText("clasificada");
  await owner.goto("/#/coordinator");
  await owner.getByTestId("plan-input").fill(INSTR);
  await owner.getByTestId("plan-submit").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Necesita aclaración", { timeout: 20_000 });
  const opts = (await owner.getByTestId("clarify-option").allTextContents()).join("|");
  rec("V5-05", "Con 2+ administradores elegibles el coordinador PREGUNTA (muestra carga de trabajo) y no toma «el primero»; la persona sin asistente reactivada también aparece", /Javiera Pons/.test(opts) && /Rodrigo Silva/.test(opts) && /Sin Asistente/.test(opts) && /tarea\(s\) abierta\(s\)/.test(opts), opts);
  await shot(owner, "04-coordinador-pregunta-administracion");
  await owner.getByTestId("clarify-option").filter({ hasText: "Rodrigo Silva" }).click();
  await expect(owner.getByTestId("clarification")).toContainText("técnic");
  await owner.getByTestId("clarify-option").filter({ hasText: "Camila Vera" }).click();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando tu confirmación", { timeout: 20_000 });
  const PLAN = owner.url().split("/").pop()!;
  const rodId = one("select id from users where email='rodrigo@aireaustral.test'").id, camId = one("select id from users where email='camila@aireaustral.test'").id, javId = one("select id from users where email='javiera@aireaustral.test'").id;
  const st = dbq(`select seq,action,assignee_user_id from plan_steps where plan_id='${PLAN}' order by seq`);
  rec("V5-06", "Plan propuesto con los responsables ELEGIDOS por la dueña (Rodrigo y Camila), 0 tareas antes de confirmar", st[1].assignee_user_id === rodId && st[2].assignee_user_id === camId && one(`select count(*) n from tasks where run_id='${PLAN}'`).n === 0);
  await shot(owner, "05-plan-propuesto-empresa3");
  await owner.getByTestId("plan-confirm").dblclick();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando a una persona", { timeout: 25_000 });
  await expect(owner.getByTestId("plan-waiting-on")).toContainText("Rodrigo Silva");
  await expect.poll(() => dbq(`select id from tasks where run_id='${PLAN}'`).length, { timeout: 15_000 }).toBe(2);
  const tasks = dbq(`select id,assignee_user_id,status from tasks where run_id='${PLAN}' order by created_at`);
  rec("V5-07", "Confirmar (doble clic): exactamente 2 tareas y 2 delegaciones, a Rodrigo (pendiente) y Camila (en espera)", tasks.length === 2 && one(`select count(*) n from delegations where run_id='${PLAN}'`).n === 2 && tasks[0].assignee_user_id === rodId && tasks[0].status === "pendiente" && tasks[1].assignee_user_id === camId && tasks[1].status === "en_espera");

  // 4) three independent sessions: the chosen administrator, the OTHER administrator (sees nothing), the chosen technician
  const rodP = await loginUi(browser, "rodrigo@aireaustral.test", "#/assistant");
  const javP = await loginUi(browser, "javiera@aireaustral.test", "#/assistant");
  const camP = await loginUi(browser, "camila@aireaustral.test", "#/assistant");
  await expect(rodP.getByTestId("my-task").filter({ hasText: "Verificar los materiales" })).toBeVisible({ timeout: 20_000 });
  await javP.waitForTimeout(1500);
  rec("V5-08", "La OTRA administradora (Javiera) NO recibe la tarea; el técnico elegido la ve «en espera»", (await javP.getByTestId("my-task").filter({ hasText: "Verificar los materiales" }).count()) === 0 && (await camP.getByTestId("my-task-queued").count()) >= 1);
  const wrong = await apost(javP, `/api/tasks/${tasks[0].id}/complete`, { data: { evidence: "Intento cerrar la tarea de Rodrigo siendo otra persona" } });
  rec("V5-09", "Otra administradora no puede cerrar la tarea de Rodrigo (400); nada cambia", wrong.status() === 400 && one(`select status s from tasks where id='${tasks[0].id}'`).s === "pendiente");
  const stock = await (async () => { const n = await rodP.getByTestId("assistant-reply").count(); await rodP.getByTestId("assistant-input").fill("¿Cuánto stock hay de GAS-R410?"); await rodP.getByTestId("assistant-send").click(); await expect(rodP.getByTestId("assistant-reply")).toHaveCount(n + 1); return (await rodP.getByTestId("assistant-reply").last().textContent()) ?? ""; })();
  rec("V5-10", "El asistente de Rodrigo responde el stock del catálogo de SU empresa (GAS-R410: 12 u.)", /GAS-R410/.test(stock) && /12 u\./.test(stock), stock.slice(0, 80));
  const EV_A = "Revisé el inventario de Aire Austral: gas R410 y kit de mantención completos para la visita.";
  await rodP.getByTestId("my-task").filter({ hasText: "Verificar los materiales" }).getByTestId("evidence-input").fill(EV_A);
  await rodP.getByTestId("my-task").filter({ hasText: "Verificar los materiales" }).getByTestId("complete-task").click();
  await expect(rodP.getByTestId("task-msg")).toContainText("Tarea completada");
  await shot(rodP, "06-rodrigo-asistente");
  await camP.reload();
  await expect(camP.getByTestId("my-task").filter({ hasText: "Confirmar con tu evidencia" })).toBeVisible({ timeout: 25_000 });
  const EV_T = "Confirmado por Camila: llevo todo lo verificado por Rodrigo y puedo realizar la visita.";
  await camP.getByTestId("my-task").filter({ hasText: "Confirmar con tu evidencia" }).getByTestId("evidence-input").fill(EV_T);
  await camP.getByTestId("my-task").filter({ hasText: "Confirmar con tu evidencia" }).getByTestId("complete-task").click();
  await expect(camP.getByTestId("task-msg")).toContainText("Tarea completada");
  await shot(camP, "07-camila-asistente");

  // 5) verification: UI = SQLite = visualizer events
  await expect(owner.getByTestId("plan-status")).toContainText("Completada", { timeout: 30_000 });
  const t = dbq(`select assignee_user_id,status,evidence,completed_by from tasks where run_id='${PLAN}' order by created_at`);
  rec("V5-11", "Plan `completed`: ambas tareas `hecha`, cerradas por SUS responsables con la evidencia escrita", one(`select status s from plans where id='${PLAN}'`).s === "completed" && t[0].completed_by === rodId && t[0].evidence === EV_A && t[1].completed_by === camId && t[1].evidence === EV_T);
  await shot(owner, "08-plan-completado-empresa3");
  await owner.getByTestId("plan-open-flow").click();
  const evs = dbq(`select type from events where run_id='${PLAN}' order by seq`).map(e => e.type);
  await expect.poll(async () => owner.getByTestId("event-row").count(), { timeout: 20_000 }).toBe(evs.length);
  const ui = await owner.getByTestId("event-row").evaluateAll(els => els.map(e => e.getAttribute("data-type")));
  rec("V5-12", `Visualizador: los ${evs.length} eventos mostrados = filas de \`events\` (mismo orden); 2 delegaciones y 2 cierres con evidencia`, JSON.stringify(ui) === JSON.stringify(evs) && evs.filter(e => e === "agent.delegated").length === 2 && evs.filter(e => e === "task.completed").length === 2);
  await owner.getByTestId("node-admin_human").click();
  await expect(owner.getByTestId("real-state")).toContainText("Evidencia: Revisé el inventario de Aire Austral");
  await shot(owner, "09-visualizador-empresa3");

  // 6) safety in the new company: forbidden action blocked; isolation from the seeded companies
  await owner.goto("/#/coordinator");
  await owner.getByTestId("plan-input").fill("Compra repuestos y avisa a todos por WhatsApp.");
  await owner.getByTestId("plan-submit").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Bloqueada", { timeout: 20_000 });
  const bad = owner.url().split("/").pop()!;
  rec("V5-13", "Acción prohibida en la empresa nueva: plan BLOQUEADO, 0 pasos, 0 tareas", one(`select status s from plans where id='${bad}'`).s === "blocked" && one(`select count(*) n from plan_steps where plan_id='${bad}'`).n === 0);
  const clima = await loginUi(browser, "dueno@climasur.test", "#/dashboard").catch(async () => null);
  const climaCtx = await browser.newContext(); const cp = await climaCtx.newPage();
  await apost(cp, "/api/auth/login", { data: { email: "dueno@climasur.test", password: "demo1234" } });
  const seen = await cp.request.get(`/api/plans/${PLAN}`); const cfg = await cp.request.get("/api/config/overview").then(r => r.json());
  rec("V5-14", "Clima Sur no ve el plan de Aire Austral (404) ni sus empleados; sus 3 empleados y 5 agentes siguen intactos", seen.status() === 404 && cfg.employees.length === 3 && cfg.employees.every((e: any) => !e.email.includes("aireaustral")) && one("select count(*) n from agents a join companies c on c.id=a.company_id where c.slug='clima-sur'").n === 5);
  const hacker = await javP.request.get("/api/config/overview");
  rec("V5-15", "Una administradora no puede abrir la configuración de la empresa (403)", hacker.status() === 403, String(hacker.status()));
  // 7) PR #6 review: after a role change the previous role's assistant is shown as obsolete and cannot be re-activated
  await owner.goto("/#/config");
  const mat = owner.getByTestId("emp-row").filter({ hasText: "Matías Ortiz" });
  await mat.getByTestId("emp-role").selectOption("admin");
  await expect(mat.getByTestId("emp-assistant-obsolete")).toBeVisible({ timeout: 15_000 });
  const matId = one("select id from users where email='matias@aireaustral.test'").id;
  const oldAgent = one(`select id,enabled from agents where user_id='${matId}' and role='tech_assistant'`);
  const reenable = await apost(owner, `/api/config/agents/${oldAgent.id}/enabled`, { data: { enabled: true } });
  rec("V5-16", "Tras cambiar de rol, la plantilla anterior se muestra obsoleta y NO se puede reactivar (400); sus franjas no se ofrecen para visitas", (await mat.getByTestId("emp-assistant-toggle").count()) === 1 && reenable.status() === 400 && one(`select enabled e from agents where id='${oldAgent.id}'`).e === 0);
  // 8) V0.6 — access: the UI wrote everything above WITH its CSRF token; a direct write without it is refused; «cerrar sesión en todos lados» kills every session of the user
  const naked = await owner.request.post(`/api/config/agents/${oldAgent.id}/enabled`, { data: { enabled: true } });
  const nakedBody = await naked.json();
  rec("V6-01", "Una escritura con la cookie de sesión pero SIN token CSRF se rechaza (403 csrf); la UI, que sí lo envía, escribió todo lo anterior", naked.status() === 403 && String(nakedBody.detail).startsWith("csrf"), `${naked.status()} ${nakedBody.detail}`);
  const foreign = await owner.request.post(`/api/config/agents/${oldAgent.id}/enabled`, { data: { enabled: true }, headers: { Origin: "http://evil.example", "X-CSRF-Token": await (async () => (await (await owner.request.get("/api/auth/me")).json()).csrf_token)() } });
  rec("V6-02", "Con token correcto pero Origin de otro sitio también se rechaza (403)", foreign.status() === 403, String(foreign.status()));
  const second = await browser.newContext(); const secondP = await second.newPage();
  const relog = await secondP.request.post("/api/auth/login", { data: { email: "elena@aireaustral.test", password: PW } });
  owner.once("dialog", d => d.accept());
  await owner.getByTestId("logout-all").click();
  await expect(owner.getByTestId("login-submit")).toBeVisible({ timeout: 15_000 });
  const after = await secondP.request.get("/api/auth/me");
  rec("V6-03", "«Cerrar sesión en todos lados» cierra también la OTRA sesión de la misma persona", relog.ok() && after.status() === 401, `login2=${relog.status()} me2=${after.status()}`);
  await second.close();
  void clima; void javId;
  for (const p of [owner, rodP, javP, camP]) await p.context().close();
  await climaCtx.close();
});
