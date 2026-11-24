import { useEffect, useState } from "react";
import { api, Me, money, STATUS_LABEL } from "../api";
import { Chip, Empty, fmtDate, PageHeader, StatusChip } from "../ui";

export function usePoll<T>(path: string, ms = 3000): [T | null, () => void] {
  const [d, setD] = useState<T | null>(null);
  const load = () => api<T>(path).then(setD).catch(() => {});
  useEffect(() => { load(); const t = setInterval(load, ms); return () => clearInterval(t); }, [path]);
  return [d, load];
}

const Kpi = ({ label, value, tone, id, sub }: { label: string; value: number | string; tone?: string; id: string; sub?: string }) => (
  <div className="card p-4"><div className="text-xs font-semibold text-slate-500">{label}</div><div className={`text-3xl font-bold mt-1 ${tone ?? ""}`} data-testid={id}>{value}</div>
    {sub && <div className="text-[11px] text-slate-400 mt-0.5" data-testid={id + "-sub"}>{sub}</div>}</div>
);

// «Qué hacer» for a commitment: who must act and how (the table used to show only a red «vencido»)
const commitmentAction = (c: any, canProcess: boolean) => {
  if (c.status === "cumplido") return "Cumplido: nada que hacer.";
  if (c.status === "cancelado") return "Cancelado: nada que hacer.";
  if (c.overdue) return c.status === "vencido"
    ? `${c.owner} debe actuar: el recordatorio interno ya está en su lista de tareas. Confirma con el cliente o ciérralo.`
    : `Venció y aún no se procesó: ${canProcess ? "pulsa «Procesar vencimientos» para crear el recordatorio interno." : "pídele a administración o al dueño que procese los vencimientos."}`;
  return `${c.owner} debe cumplirlo antes de ${fmtDate(c.due_at)}.`;
};

export default function Dashboard({ me }: { me: Me }) {
  const [d] = usePoll<any>("/dashboard");
  const [a] = usePoll<any>("/agents", 4000);
  const [att] = usePoll<any>("/attention", 3000);
  const [cm, reloadCm] = usePoll<any>(me.permissions.includes("commitments.read") ? "/commitments" : "/attention", 4000);
  const [msg, setMsg] = useState("");
  const canProcess = me.permissions.includes("commitments.process");
  const processDue = async () => {
    const asOf = new Date(Date.now() + 2 * 86400_000).toISOString();
    const r = await api("/commitments/process-due", { body: { as_of: asOf } });
    setMsg(`Reloj simulado +2 días: ${r.processed.length} compromiso(s) vencido(s) procesado(s); recordatorios internos y borradores creados (nada se envía).`); reloadCm();
  };
  const reset = async () => {
    if (prompt("Esto borra SOLO las ejecuciones de escenario de esta empresa (se hace copia de la base antes). Escribe REINICIAR para confirmar:") !== "REINICIAR") return;
    const r = await api("/demo/reset-scenarios", { body: { confirm: "REINICIAR" } }); setMsg(`Escenarios reiniciados: ${r.runs} ejecución(es), ${r.requests} solicitud(es).`);
  };
  if (!d) return <div className="text-slate-400">Cargando…</div>;
  const by = Object.entries(d.requests_by_status as Record<string, number>);
  const max = Math.max(1, ...by.map(([, n]) => n));
  const liveRoles = new Set<string>();
  const attItems: any[] = att?.items ?? [];
  const alerts: any[] = att ? d.alerts.filter((al: any) => !attItems.some(i => i.run_id === al.run_id && i.kind === al.kind)) : [];  // «Alertas» = what is NOT already in «Qué requiere mi atención»
  return (
    <div>
      <PageHeader title={`Estado de ${me.company.name}`} sub={me.company.industry}
        right={<Chip tone={d.provider_mode === "llm" ? "violet" : "sky"} title="Motor que toma las decisiones de clasificación">
          {d.provider_mode === "llm" ? `LLM local · ${d.provider}` : "Motor determinista de demostración (sin LLM)"}</Chip>} />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
        <Kpi id="kpi-requests" label="Solicitudes" value={d.total_requests} sub={`${d.active_requests} activas · ${d.historic_requests} históricas (cerradas/fallidas)`} />
        <Kpi id="kpi-active" label="Ejecuciones activas" value={d.active_runs} tone="text-brand-600" />
        <Kpi id="kpi-approvals" label="Aprobaciones pendientes" value={d.pending_approvals.length} tone={d.pending_approvals.length ? "text-amber-600" : ""} />
        <Kpi id="kpi-tasks" label="Tareas pendientes" value={d.pending_tasks.length} sub={`${d.pending_tasks.filter((t: any) => t.blocked_reason).length} bloqueadas por el cliente · ${(d.waiting_tasks ?? []).length} en espera de otro paso`} />
      </div>
      <div className="card p-4 mb-4" data-testid="attention">
        <div className="font-semibold mb-2">Qué requiere mi atención</div>
        {(att?.items ?? []).length === 0 ? <Empty>Nada pendiente para ti.</Empty> : (att.items as any[]).map((it, i) => (
          <a key={i} href={it.plan_id ? `#/coordinator/${it.plan_id}` : it.run_id ? `#/flows/${it.run_id}` : "#/assistant"} data-testid="attention-item"
             className={`flex items-center justify-between rounded-lg border px-3 py-2 text-sm mb-1.5 hover:brightness-95 ${it.severity === "high" ? "bg-amber-50 border-amber-200" : "bg-slate-50 border-slate-200"}`}>
            <span>{it.text}</span><span className="text-brand-700 font-semibold shrink-0 ml-3">Abrir →</span></a>))}
      </div>
      {msg && <div className="card p-3 mb-4 text-sm bg-emerald-50 border-emerald-200 text-emerald-900" data-testid="dash-msg">{msg}</div>}
      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card p-4 lg:col-span-2">
          <div className="font-semibold mb-3">Alertas de la empresa</div>
          {alerts.length === 0 ? <Empty>Sin alertas adicionales. Lo que requiere acción tuya está arriba, en «Qué requiere mi atención».</Empty> : (
            <ul className="space-y-2" data-testid="alerts">
              {alerts.map((al: any, i: number) => (
                <li key={i} className={`flex items-center justify-between rounded-lg border px-3 py-2 text-sm ${al.kind === "approval" ? "bg-amber-50 border-amber-200" : "bg-rose-50 border-rose-200"}`}>
                  <span>{al.text}</span><a className="text-brand-700 font-semibold" href={`#/flows/${al.run_id}`}>Abrir →</a>
                </li>))}
            </ul>)}
          <div className="flex items-center justify-between mt-6 mb-3"><div className="font-semibold">Compromisos</div>
            {canProcess && <button className="btn btn-ghost" onClick={processDue} data-testid="process-due" title="Simula que pasaron 2 días y genera recordatorios internos y borradores (sin enviar)">Procesar vencimientos (reloj +2 días)</button>}</div>
          {(cm?.commitments ?? []).length === 0 ? <Empty>Sin compromisos registrados.</Empty> : (
            <table className="w-full text-sm mb-4" data-testid="commitments"><thead className="text-left text-xs text-slate-400"><tr><th className="pb-2">Compromiso</th><th>Responsable</th><th>Plazo</th><th>Estado</th><th>Qué hacer</th></tr></thead>
              <tbody>{cm.commitments.map((c: any) => (
                <tr key={c.id} className="border-t border-slate-100"><td className="py-2">{c.title}</td><td>{c.owner}</td><td>{fmtDate(c.due_at)}</td>
                  <td><Chip tone={c.status === "cumplido" ? "green" : c.status === "cancelado" ? "slate" : c.overdue ? "red" : "amber"}>{c.overdue && c.status !== "cumplido" ? "vencido" : c.status}</Chip></td>
                  <td className="text-xs text-slate-600 max-w-[260px]" data-testid="commitment-action">{commitmentAction(c, canProcess)}{c.request_id && <> <a className="text-brand-700 font-semibold" href={`#/operations/${c.request_id}`}>Ver solicitud →</a></>}</td></tr>))}</tbody></table>)}
          <div className="font-semibold mb-3">Visitas</div>
          {d.commitments.length === 0 ? <Empty>Aún no hay visitas propuestas.</Empty> : (
            <table className="w-full text-sm"><thead className="text-left text-xs text-slate-400"><tr><th className="pb-2">Cliente</th><th>Fecha</th><th>Estado</th></tr></thead>
              <tbody>{d.commitments.map((c: any) => (
                <tr key={c.request_id} className="border-t border-slate-100"><td className="py-2">{c.customer}</td><td>{fmtDate(c.slot_start)}</td>
                  <td>{c.customer_confirmed ? <Chip tone="green">Confirmada por el cliente</Chip> : <Chip tone="amber">Propuesta · sin confirmar</Chip>}</td></tr>))}</tbody></table>)}
        </div>
        <div className="space-y-4">
          <div className="card p-4">
            <div className="font-semibold mb-3">Solicitudes por estado</div>
            {by.length === 0 ? <Empty>Sin solicitudes</Empty> : by.map(([k, n]) => (
              <div key={k} className="mb-2"><div className="flex justify-between text-xs mb-1"><span>{STATUS_LABEL[k] ?? k}</span><span className="font-semibold">{n}</span></div>
                <div className="h-2 rounded bg-slate-100"><div className="h-2 rounded bg-brand-500" style={{ width: `${(n / max) * 100}%` }} /></div></div>))}
          </div>
          <div className="card p-4">
            <div className="font-semibold mb-3">Agentes</div>
            {(a?.agents ?? []).map((g: any) => (
              <div key={g.id} className="flex items-center justify-between py-1.5 text-sm"><span>{g.name}</span>
                <span data-testid="agent-status"><Chip tone={g.availability === "disabled" ? "red" : g.availability === "working" ? "indigo" : "green"} title="Disponible = habilitado y listo para recibir trabajo">{g.availability_text}</Chip></span></div>))}
          </div>
        </div>
      </div>
      <div className="card p-4 mt-4">
        <div className="font-semibold mb-3">Tareas pendientes por persona</div>
        {d.pending_tasks.length === 0 && (d.waiting_tasks ?? []).length === 0 ? <Empty>Sin tareas pendientes</Empty> : Object.entries(d.pending_tasks.reduce((g: any, t: any) => { (g[t.blocked_reason && !t.assignee ? "Esperan al cliente (no accionables por el equipo)" : t.assignee ?? "Agente: " + (t.assignee_agent ?? "—")] ??= []).push(t); return g; }, {})).map(([who, ts]: any) => (
          <div key={who} className="mb-3"><div className="text-xs font-bold text-slate-500 mb-1">{who} ({ts.length})</div>
            {ts.map((t: any) => <div key={t.id} className="flex items-center justify-between gap-2 border-t border-slate-100 py-1.5 text-sm" data-testid="pending-task"><span className={t.blocked_reason ? "text-slate-500" : ""}>{t.title}</span>
              <span className="flex gap-1 shrink-0">{t.blocked_reason && <span data-testid="task-blocked"><Chip tone="amber" title={t.blocked_reason}>🔒 {t.blocked_reason.startsWith("Espera") ? "Espera al cliente" : "Hasta que el cliente confirme"}</Chip></span>}{t.kind && <Chip>{t.kind}</Chip>}</span></div>)}</div>))}
        {(d.waiting_tasks ?? []).length > 0 && <div className="mb-3"><div className="text-xs font-bold text-slate-500 mb-1">En espera de otro paso del plan ({d.waiting_tasks.length})</div>
          {d.waiting_tasks.map((t: any) => <div key={t.id} className="flex items-center justify-between gap-2 border-t border-slate-100 py-1.5 text-sm" data-testid="waiting-task"><span className="text-slate-500">{t.title}</span><span data-testid="task-waiting"><Chip tone="slate">{t.assignee ?? "—"} · aún no puede actuar</Chip></span></div>)}</div>}
        {me.role === "owner" && <div className="mt-4 pt-3 border-t border-slate-100"><button className="btn btn-danger" onClick={reset} data-testid="reset-scenarios">Reiniciar escenarios de prueba…</button>
          <span className="text-xs text-slate-400 ml-2">Solo esta empresa; confirmación explícita; copia previa de la base.</span></div>}
      </div>
      <div className="card p-4 mt-4">
        <div className="font-semibold mb-3">Ejecuciones recientes</div>
        {d.runs.length === 0 ? <Empty>Aún no hay ejecuciones. Prueba el <a className="text-brand-700 font-semibold" href={`#/cliente/${me.company.slug}`}>chat de cliente</a>.</Empty> :
          d.runs.slice(0, 6).map((r: any) => (
            <a key={r.id} href={`#/flows/${r.id}`} className="flex items-center justify-between border-t first:border-0 border-slate-100 py-2 text-sm hover:bg-slate-50 px-1 rounded">
              <span className="font-mono text-xs text-slate-500">{r.id}</span><StatusChip s={r.status} /></a>))}
      </div>
    </div>
  );
}
