// THE 10-MINUTE COMMERCIAL SCRIPT (docs/demo/DEMO_COMERCIAL_V04.md), executed for real with three independent browser sessions (three host names)
// on a fresh fixture. Every delegation / task / result shown by the UI is compared with SQLite and with the persisted visualizer events.
// Checks are recorded in docs/demo/demo-results.json; screenshots go to docs/demo/screenshots/.
import { Browser, expect, Page, test } from "@playwright/test";
import { apost, newCtx } from "./csrf";
import { execFileSync, spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";

const PORT = 8745;
const OWNER = `http://dueno.localhost:${PORT}`, ADMIN = `http://admin.localhost:${PORT}`, TECH = `http://tecnico.localhost:${PORT}`;
const SHOTS = "../docs/demo/screenshots";
const DBPATH = "../backend/data/demo_comercial_e2e.sqlite3";
const PY = "../backend/.venv/bin/python";
const dbq = (sql: string): any[] => JSON.parse(execFileSync(PY, ["../qa/dbq.py", sql, DBPATH], { encoding: "utf8" }));
const one = (sql: string) => dbq(sql)[0];
const INSTRUCTION = "Revisa la mantención pendiente de Marta Rojas; pide a administración que verifique materiales y al técnico que confirme después de administración.";
const FORBIDDEN = "Compra repuestos y avisa a todos por WhatsApp.";
const EV_A = "Revisé el inventario: hay 6 kits de mantención (necesito 1) y las herramientas están completas para la visita de Marta.";
const EV_T = "Confirmado: llevo el kit verificado por administración y puedo realizar la mantención de Marta Rojas.";

type R = { id: string; area: string; desc: string; ok: boolean; detail: string };
const OUT = "../docs/demo/demo-results.json", STATE = "/tmp/aicos-demo-e2e.json";
const results: R[] = existsSync(OUT) ? JSON.parse(readFileSync(OUT, "utf8")) : [];
function rec(id: string, area: string, desc: string, ok: boolean, detail = "") {
  const row = { id, area, desc, ok, detail }, i = results.findIndex(r => r.id === id);
  if (i >= 0) results[i] = row; else results.push(row);
  writeFileSync(OUT, JSON.stringify(results, null, 2));
  expect.soft(ok, `${id} ${desc} ${detail}`).toBeTruthy();
}
const st = existsSync(STATE) ? JSON.parse(readFileSync(STATE, "utf8")) : {};
let PLAN: string = st.PLAN ?? "";
const keep = () => writeFileSync(STATE, JSON.stringify({ PLAN }));
const external: string[] = [];   // any request leaving the demo hosts would be listed here
async function ctxPage(browser: Browser, base: string, persona: string, next = "/dashboard") {
  const ctx = await newCtx(browser, { viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on("request", r => { const u = new URL(r.url()); if (!["dueno.localhost", "admin.localhost", "tecnico.localhost", "127.0.0.1", "localhost"].includes(u.hostname) && u.protocol.startsWith("http")) external.push(r.url()); });
  await page.goto(`${base}/demo-login/${persona}?next=${next}`);
  return page;
}
const shot = (p: Page, n: string, full = false) => p.screenshot({ path: `${SHOTS}/${n}.png`, fullPage: full });
const task = (p: Page, t: string) => p.getByTestId("my-task").filter({ hasText: t });
const sendAssistant = async (p: Page, t: string) => { const n = await p.getByTestId("assistant-reply").count(); await p.getByTestId("assistant-input").fill(t); await p.getByTestId("assistant-send").click(); await expect(p.getByTestId("assistant-reply")).toHaveCount(n + 1, { timeout: 15_000 }); return (await p.getByTestId("assistant-reply").last().textContent()) ?? ""; };
const ptasks = (pid: string) => dbq(`select id,status,evidence,completed_by,assignee_user_id,idem_key from tasks where run_id='${pid}' order by created_at`);
const steps = (pid: string) => dbq(`select seq,action,status from plan_steps where plan_id='${pid}' order by seq`);
const events = (pid: string) => dbq(`select seq,type,agent,mode from events where run_id='${pid}' order by seq`);

test("DEMO 10 min — dueño, administración y técnico en 3 sesiones independientes (guion completo)", async ({ browser }) => {
  writeFileSync(STATE, "{}");
  const t0 = Date.now();
  const co = one("select id from companies where slug='clima-sur'").id as string;
  const uid = (role: string) => one(`select id from users where company_id='${co}' and role='${role}'`).id as string;

  // ---- 0:00 launcher + 3 independent sessions
  const guide = await newCtx(browser, ); const gp = await guide.newPage();
  await gp.goto(`${OWNER}/demo`);
  await expect(gp.getByText("Abrir las tres sesiones")).toBeVisible();
  rec("S-00", "Entorno", "La guía /demo muestra BD, modo, commit y los 3 accesos con hosts distintos", (await gp.content()).includes("dueno.localhost") && (await gp.content()).includes("demo_comercial_e2e.sqlite3") && (await gp.content()).includes("determinista"));
  await shot(gp, "00-guia-demo"); await guide.close();
  const owner = await ctxPage(browser, OWNER, "owner"); const admin = await ctxPage(browser, ADMIN, "admin", "/assistant"); const tech = await ctxPage(browser, TECH, "tech", "/assistant");
  const cookies = await Promise.all([owner, admin, tech].map(p => p.context().cookies()));
  rec("S-01", "Sesiones", "Tres cookies de sesión DISTINTAS en tres hosts distintos (aislamiento real en un solo navegador)", new Set(cookies.map(c => c[0].value)).size === 3 && new Set(cookies.map(c => c[0].domain)).size === 3, cookies.map(c => c[0].domain).join(", "));
  const leak = await owner.request.get(`${ADMIN}/api/auth/me`);
  rec("S-02", "Sesiones", "La sesión del dueño no se filtra al host de administración (401)", leak.status() === 401, String(leak.status()));

  // ---- 0:00–1:00 dashboard limpio
  await expect(owner.getByTestId("kpi-requests")).toBeVisible(); await expect(owner.getByTestId("demo-banner")).toContainText("DEMO COMERCIAL");
  await expect.poll(async () => Number(await owner.getByTestId("kpi-requests").textContent())).toBe(4);
  const ui = { req: await owner.getByTestId("kpi-requests-sub").textContent(), ap: await owner.getByTestId("kpi-approvals").textContent(), tk: await owner.getByTestId("kpi-tasks").textContent() };
  rec("S-03", "Dashboard", "Dashboard limpio: 4 solicitudes (2 activas · 2 históricas), 0 aprobaciones, 0 tareas", /2 activas · 2 históricas/.test(ui.req ?? "") && ui.ap === "0" && ui.tk === "0", JSON.stringify(ui));
  await expect(owner.getByTestId("attention")).toContainText("Nada pendiente para ti");
  rec("S-04", "Dashboard", "Sin alertas ni ejecuciones fallidas históricas (ni en pantalla ni en SQLite)", (await owner.getByTestId("alerts").count()) === 0 && one("select count(*) n from runs").n === 0 && one("select count(*) n from audit_log where outcome='denied'").n === 0);
  await shot(owner, "01-dashboard-dueno", true);

  // ---- 1:00–2:00 solicitud ya cargada (sin enviar nada)
  await owner.goto(`${OWNER}/#/operations`);
  await expect.poll(async () => owner.getByTestId("request-row").count()).toBe(2);
  await owner.getByTestId("request-row").filter({ hasText: "Marta Rojas" }).click();
  await expect(owner.getByTestId("request-detail")).toContainText("mantención preventiva");
  await expect(owner.getByTestId("request-detail")).toContainText("Kit de mantención");
  rec("S-05", "Solicitud", "Solicitud de Marta ya cargada: mensaje del cliente, presupuesto con repuesto real y cliente asociado", true);
  await shot(owner, "02-solicitud-marta");
  await owner.getByTestId("request-row").filter({ hasText: "Pedro" }).click();
  await expect(owner.getByTestId("visit-status")).toContainText("Propuesta");
  await expect(owner.getByTestId("request-detail")).toContainText("no enviado");
  rec("S-06", "Solicitud", "Visita de Pedro «Propuesta · sin confirmar» y mensaje «preparado · no enviado (simulado)»", true);
  await shot(owner, "03-visita-propuesta-no-enviado");

  // ---- 2:00–3:30 coordinador: instrucción → plan (sin tareas)
  await owner.goto(`${OWNER}/#/coordinator`);
  await owner.getByTestId("plan-input").fill(INSTRUCTION);
  await owner.getByTestId("plan-submit").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando tu confirmación", { timeout: 20_000 });
  PLAN = owner.url().split("/").pop()!; keep();
  await expect(owner.getByTestId("plan-step")).toHaveCount(4);
  const sp = steps(PLAN), p0 = one(`select * from plans where id='${PLAN}'`);
  rec("S-07", "Plan", "Instrucción exacta del guion → plan válido SIN aclaración (una sola mantención abierta), 4 pasos persistidos, `source=deterministic`", sp.length === 4 && sp.every(s => s.status === "planned") && p0.status === "awaiting_user_confirmation" && p0.source === "deterministic" && p0.rounds === 0, JSON.stringify(sp.map(s => s.action)));
  rec("S-08", "Plan", "Responsables reales: Javiera (administración) y Matías (técnico, dependiente del paso 2)", await owner.getByTestId("plan-steps").textContent().then(t => /Javiera Soto/.test(t ?? "") && /Matías Fuentes/.test(t ?? "")) && dbq(`select depends_on from plan_steps where plan_id='${PLAN}' and seq=3`)[0].depends_on === 2);
  rec("S-09", "Plan", "ANTES de confirmar: 0 tareas y 0 delegaciones en SQLite (y 0 para administración vía API)", ptasks(PLAN).length === 0 && one(`select count(*) n from delegations where run_id='${PLAN}'`).n === 0 && ((await admin.request.get(`${ADMIN}/api/tasks`).then(r => r.json())).tasks.filter((t: any) => t.kind === "plan_step").length === 0));
  await shot(owner, "04-plan-propuesto");

  // ---- 3:30–4:30 confirmación humana
  await owner.getByTestId("plan-confirm").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando a una persona", { timeout: 25_000 });
  await expect(owner.getByTestId("plan-waiting-on")).toContainText("Javiera Soto");
  await expect.poll(() => ptasks(PLAN).length, { timeout: 15_000 }).toBe(2);
  const tk = ptasks(PLAN), adminTask = tk.find(t => t.assignee_user_id === uid("admin"))!.id, techTask = tk.find(t => t.assignee_user_id === uid("technician"))!.id;
  rec("S-10", "Delegación", "Tras confirmar: exactamente 2 tareas + 2 delegaciones (coordinator → admin_assistant / tech_assistant); la del técnico `en_espera`", tk.length === 2 && one(`select count(*) n from delegations where run_id='${PLAN}'`).n === 2 && tk.find(t => t.id === techTask)!.status === "en_espera" && tk.find(t => t.id === adminTask)!.status === "pendiente" && one(`select confirmed_by c from plans where id='${PLAN}'`).c === uid("owner"));
  await shot(owner, "05-plan-confirmado");

  // ---- 4:30–6:00 administración: asistente, stock, evidencia
  await admin.reload();
  await expect(task(admin, "Verificar los materiales")).toBeVisible({ timeout: 20_000 });
  const stockReply = await sendAssistant(admin, "¿Cuánto stock hay de KIT-MANT?");
  rec("S-11", "Administración", "Su asistente responde el stock REAL (KIT-MANT: 6 u.) = SQLite", stockReply.includes("KIT-MANT") && stockReply.includes(`${one("select stock s from parts where sku='KIT-MANT' and company_id='" + co + "'").s} u.`), stockReply.slice(0, 90));
  await shot(admin, "06-administracion-asistente");
  await task(admin, "Verificar los materiales").getByTestId("evidence-input").fill(EV_A);
  await task(admin, "Verificar los materiales").getByTestId("complete-task").click();
  await expect(admin.getByTestId("task-msg")).toContainText("Tarea completada");
  const ra = ptasks(PLAN).find(t => t.id === adminTask)!;
  rec("S-12", "Administración", "Cierre con evidencia: `hecha`, `completed_by`=Javiera, evidencia idéntica en SQLite", ra.status === "hecha" && ra.completed_by === uid("admin") && ra.evidence === EV_A);

  // ---- 6:00–7:15 técnico: dependencia
  await expect.poll(() => ptasks(PLAN).find(t => t.id === techTask)!.status, { timeout: 25_000 }).toBe("pendiente");
  await tech.reload();
  await expect(task(tech, "Confirmar con tu evidencia")).toBeVisible({ timeout: 25_000 });
  await shot(tech, "07-tecnico-tarea-habilitada");
  await task(tech, "Confirmar con tu evidencia").getByTestId("evidence-input").fill(EV_T);
  await task(tech, "Confirmar con tu evidencia").getByTestId("complete-task").click();
  await expect(tech.getByTestId("task-msg")).toContainText("Tarea completada");
  const rt = ptasks(PLAN).find(t => t.id === techTask)!;
  rec("S-13", "Técnico", "Su tarea esperó a administración y se cerró con evidencia (`hecha`, `completed_by`=Matías)", rt.status === "hecha" && rt.completed_by === uid("technician") && rt.evidence === EV_T);

  // ---- 7:15–8:30 resultado + visualizador
  await expect(owner.getByTestId("plan-status")).toContainText("Completada", { timeout: 30_000 });
  await expect(owner.getByTestId("plan-result")).toContainText("No faltan materiales");
  const fin = one(`select status,result_summary from plans where id='${PLAN}'`);
  rec("S-14", "Resultado", "Plan `completed` en SQLite = «Completada» en la UI; resumen con el stock real (necesita 1, stock 6)", fin.status === "completed" && /necesita 1, stock 6/.test(fin.result_summary) && steps(PLAN).every(s => s.status === "done"));
  await shot(owner, "08-plan-completado");
  await owner.getByTestId("plan-open-flow").click();
  await expect(owner.getByTestId("event-feed")).toContainText("Plan completado", { timeout: 20_000 });
  const db = events(PLAN);
  await expect.poll(async () => owner.getByTestId("event-row").count()).toBe(db.length);
  const uiTypes = await owner.getByTestId("event-row").evaluateAll(els => els.map(e => e.getAttribute("data-type")));
  rec("S-15", "Visualizador", `Los ${db.length} eventos del visualizador = filas de \`events\` (mismo orden); no es animación`, JSON.stringify(uiTypes) === JSON.stringify(db.map(e => e.type)));
  await expect(owner.getByText("SSE conectado")).toBeVisible();
  const status = (n: string) => owner.getByTestId("node-" + n).getAttribute("data-status");
  rec("S-16", "Visualizador", "Nodos humanos y coordinador en «done»", (await status("owner_human")) === "done" && (await status("admin_human")) === "done" && (await status("tech_human")) === "done" && (await status("coordinator")) === "done");
  const asg = db.filter(e => e.type === "agent.delegated").length, comp = db.filter(e => e.type === "task.completed").length;
  rec("S-17", "Visualizador", "Delegaciones y tareas del grafo = SQLite (2 delegaciones, 2 cierres con evidencia)", asg === 2 && comp === 2 && one(`select count(*) n from delegations where run_id='${PLAN}'`).n === asg);
  await owner.getByTestId("node-coordinator").click();
  await expect(owner.getByTestId("inspector")).toContainText("plans.assign_task");
  await shot(owner, "09-visualizador-completado");
  await owner.getByTestId("node-admin_human").click();
  await expect(owner.getByTestId("real-state")).toContainText("Evidencia: Revisé el inventario");
  await shot(owner, "10-visualizador-evidencia-administracion");
  await owner.getByTestId("scrubber").fill("5");
  await shot(owner, "11-visualizador-historial-paso-5");

  // ---- 8:30–9:15 prueba de control
  await owner.goto(`${OWNER}/#/coordinator`);
  await owner.getByTestId("plan-input").fill(FORBIDDEN);
  await owner.getByTestId("plan-submit").click();
  await expect(owner.getByTestId("plan-status")).toContainText("Bloqueada", { timeout: 20_000 });
  await expect(owner.getByTestId("plan-excluded")).toContainText("Comprar"); await expect(owner.getByTestId("plan-confirm")).toHaveCount(0);
  const badPlan = owner.url().split("/").pop()!;
  rec("S-18", "Control", "«Compra repuestos y avisa a todos por WhatsApp»: plan BLOQUEADO, 0 pasos, 0 tareas, confirmar = 409", one(`select status from plans where id='${badPlan}'`).status === "blocked" && steps(badPlan).length === 0 && ptasks(badPlan).length === 0 && (await apost(owner, `${OWNER}/api/plans/${badPlan}/confirm`)).status() === 409);
  await shot(owner, "12-accion-prohibida-bloqueada");

  // ---- cierre: nada externo, nada mágico
  const sent = one("select count(*) n from messages where status='enviado'").n, drafts = one("select count(*) n from messages where status='preparado_no_enviado'").n;
  const visit = one("select status, customer_confirmed c from visits limit 1");
  rec("S-19", "Sin efectos externos", "0 mensajes enviados; el borrador sigue «preparado_no_enviado»; la visita sigue «propuesta» (no «confirmada»); 0 aprobaciones; stock intacto", sent === 0 && drafts === 1 && visit.status === "propuesta" && visit.c === 0 && one("select count(*) n from approvals").n === 0 && one(`select stock s from parts where sku='KIT-MANT' and company_id='${co}'`).s === 6);
  rec("S-20", "Sin efectos externos", "Ninguna petición salió de localhost durante todo el guion (sin red)", external.length === 0, external.join(", ") || "0 peticiones externas");
  const minutes = ((Date.now() - t0) / 60000).toFixed(1);
  rec("S-21", "Tiempo", "El guion completo corre en máquina en pocos minutos (el ritmo humano de 10 min es el del presentador)", Number(minutes) < 3, `${minutes} min`);
  for (const p of [owner, admin, tech]) await p.context().close();
});

test("DEMO — permisos y aislamiento: administración y técnico no tienen poder del dueño; otra empresa no ve nada", async ({ browser }) => {
  expect(PLAN).toMatch(/^pln_/);
  const admin = await ctxPage(browser, ADMIN, "admin"), tech = await ctxPage(browser, TECH, "tech");
  const cr = await apost(admin, `${ADMIN}/api/plans`, { data: { text: "Revisa las solicitudes pendientes", client_plan_id: "perm-test-0001" } });
  const cf = await apost(tech, `${TECH}/api/plans/${PLAN}/cancel`);
  const ap = await apost(admin, `${ADMIN}/api/approvals/x/decide`, { data: { decision: "approved" } });
  rec("P-01", "Permisos", "Administración no puede crear planes; el técnico no puede cancelarlos; nadie sin permiso decide aprobaciones (403)", cr.status() === 403 && cf.status() === 403 && ap.status() === 403, `${cr.status()},${cf.status()},${ap.status()}`);
  const wrong = await apost(admin, `${ADMIN}/api/tasks/${ptasks(PLAN)[1].id}/complete`, { data: { evidence: "Intento cerrar la tarea de otra persona" } });
  rec("P-02", "Permisos", "Administración no puede cerrar la tarea del técnico (400) y no cambia nada", wrong.status() === 400 && ptasks(PLAN).every(t => t.status === "hecha"));
  const other = await newCtx(browser, ); const op = await other.newPage();
  const lg = await apost(op, `${OWNER}/api/auth/login`, { data: { email: "dueno@motornorte.test", password: "demo1234" } });
  const seen = await op.request.get(`${OWNER}/api/plans/${PLAN}`);
  rec("P-03", "Aislamiento", "Motor Norte (otra empresa) no ve el plan de Clima Sur (404) y su listado está vacío", lg.ok() && seen.status() === 404 && (await op.request.get(`${OWNER}/api/plans`).then(r => r.json())).plans.length === 0);
  await other.close(); await admin.context().close(); await tech.context().close();
});

test("DEMO — reinicio del proceso con un plan pendiente: no se pierde ni se duplica nada", async ({ browser }) => {
  const owner = await ctxPage(browser, OWNER, "owner");
  const P = (await apost(owner, `${OWNER}/api/plans`, { data: { text: "Revisa la mantención pendiente de Marta Rojas; pide a administración que verifique materiales.", client_plan_id: "restart-demo-0001" } }).then(r => r.json())).plan;
  rec("R-00", "Reinicio", "Segunda instrucción de la misma mantención produce un plan válido (solo administración)", P.status === "awaiting_user_confirmation" && P.steps.length === 3, P.status);
  await apost(owner, `${OWNER}/api/plans/${P.id}/confirm`);
  await expect.poll(() => one(`select status s from plans where id='${P.id}'`).s, { timeout: 15_000 }).toBe("waiting_human");
  const before = { t: ptasks(P.id).map(t => t.id), e: events(P.id).length };
  // restart ONLY the demo server (checked by command line); same database
  const pid = execFileSync("lsof", ["-nP", `-iTCP:${PORT}`, "-sTCP:LISTEN", "-t"], { encoding: "utf8" }).trim().split("\n")[0];
  const cmd = execFileSync("ps", ["-p", pid, "-o", "command="], { encoding: "utf8" });
  expect(cmd).toContain("uvicorn app.main:app"); expect(cmd).toContain(`${PORT}`);
  execFileSync("kill", [pid]);
  await new Promise(r => setTimeout(r, 1200));
  const child = spawn("bash", ["-c", `cd ../backend && AICOS_DEMO_MODE=1 AICOS_COMMIT=e2e AICOS_DB_URL=sqlite:///data/demo_comercial_e2e.sqlite3 AICOS_STEP_DELAY=0.12 exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${PORT} --no-proxy-headers`], { detached: true, stdio: "ignore" });
  child.unref();
  await expect.poll(async () => { try { return (await owner.request.get(`${OWNER}/api/demo/info`)).ok(); } catch { return false; } }, { timeout: 30_000 }).toBeTruthy();
  await owner.goto(`${OWNER}/#/coordinator/${P.id}`); await owner.reload();
  await expect(owner.getByTestId("plan-status")).toContainText("Esperando a una persona", { timeout: 20_000 });
  const adminP = await ctxPage(browser, ADMIN, "admin");
  const r = await apost(adminP, `${ADMIN}/api/tasks/${before.t[0]}/complete`, { data: { evidence: "Verificado tras reiniciar el servidor de la demostración" } });
  await expect.poll(() => one(`select status s from plans where id='${P.id}'`).s, { timeout: 20_000 }).toBe("completed");
  rec("R-01", "Reinicio", "Reinicio del proceso demo con plan pendiente: mismo plan, mismas tareas (sin duplicar), sigue y se completa", r.ok() && JSON.stringify(ptasks(P.id).map(t => t.id)) === JSON.stringify(before.t) && ptasks(P.id).length === 1);
  await owner.goto(`${OWNER}/#/flows/${P.id}`);
  await expect.poll(async () => owner.getByTestId("event-row").count(), { timeout: 15_000 }).toBe(events(P.id).length);
  rec("R-02", "Reinicio", "El visualizador reconstruye el historial completo desde SQLite tras el reinicio", true);
  await shot(owner, "13-reinicio-historial-persistido");
  await adminP.context().close(); await owner.context().close();
});

test.afterAll(() => {   // the restart test starts its own detached server: never leave it behind (only if it is OUR uvicorn on OUR port)
  try {
    const pid = execFileSync("lsof", ["-nP", `-iTCP:${PORT}`, "-sTCP:LISTEN", "-t"], { encoding: "utf8" }).trim().split("\n")[0];
    if (pid && execFileSync("ps", ["-p", pid, "-o", "command="], { encoding: "utf8" }).includes(`uvicorn app.main:app --host 127.0.0.1 --port ${PORT}`)) execFileSync("kill", [pid]);
  } catch { /* nothing listening */ }
});

test("DEMO — destruir y recrear el fixture es reproducible y no duplica datos", async () => {
  const a = "../backend/data/demo_comercial_e2e_repro.sqlite3";
  const seedRun = () => JSON.parse(execFileSync(PY, ["-m", "app.demo_seed", "--db", "data/demo_comercial_e2e_repro.sqlite3"], { cwd: "../backend", encoding: "utf8" }));
  execFileSync("bash", ["-c", `rm -f ${a}*`]);
  const s1 = seedRun(), s2 = seedRun();
  execFileSync("bash", ["-c", `rm -f ${a}*`]);
  const s3 = seedRun();
  execFileSync("bash", ["-c", `rm -f ${a}*`]);
  rec("F-01", "Fixture", "Crear, volver a ejecutar sobre la misma BD, destruir y recrear: resumen idéntico (sin duplicados) y la BD del laboratorio ni se abre", JSON.stringify(s1) === JSON.stringify(s2) && JSON.stringify(s1) === JSON.stringify(s3) && s1.open_marta_maintenance === 1, JSON.stringify(s1).slice(0, 140));
});
