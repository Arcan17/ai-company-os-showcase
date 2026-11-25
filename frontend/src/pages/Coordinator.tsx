import { useState } from "react";
import { api, Me } from "../api";
import { Chip, Empty, fmtDate, PageHeader, StatusChip } from "../ui";
import { usePoll } from "./Dashboard";

const EXAMPLES = [
  "Revisa las mantenciones pendientes y asigna a administración que verifique materiales para la visita de Marta; quiero confirmación del técnico",
  "Pide a Javiera que confirme los materiales del mantenimiento de Marta y avísame si falta algo antes de agendar",
  "Revisa las solicitudes pendientes",
  "Compra repuestos y avisa por WhatsApp a los clientes",
];
const STEP_LABEL: Record<string, string> = { planned: "Planificado", waiting_dependency: "En espera de otro paso", waiting_human: "Esperando a una persona", done: "Hecho y verificado", cancelled: "Cancelado", blocked: "Bloqueado", failed: "Falló" };
const STEP_TONE: Record<string, "slate" | "amber" | "green" | "red"> = { planned: "slate", waiting_dependency: "slate", waiting_human: "amber", done: "green", cancelled: "red", blocked: "red", failed: "red" };
const ROLE: Record<string, string> = { admin: "administración", technician: "técnico" };
const newId = () => "web-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);

export default function Coordinator({ me, planId }: { me: Me; planId?: string }) {
  const [text, setText] = useState(""); const [busy, setBusy] = useState(false); const [err, setErr] = useState("");
  const [list, reloadList] = usePoll<any>("/plans", 3000);
  const [plan, reloadPlan] = usePoll<any>(planId ? `/plans/${planId}` : "/plans/none", 2000);
  const go = (id: string) => { location.hash = `#/coordinator/${id}`; };
  const run = async (fn: () => Promise<any>) => { setBusy(true); setErr(""); try { return await fn(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); reloadList(); reloadPlan(); } };
  const propose = (t: string) => run(async () => { const r = await api("/plans", { body: { text: t, client_plan_id: newId() } }); setText(""); go(r.plan_id); });
  const p = planId && plan?.id === planId ? plan : null;
  const canConfirm = me.permissions.includes("plans.confirm");
  const active = p && ["awaiting_user_confirmation", "needs_clarification", "running", "waiting_human"].includes(p.status);

  return (
    <div>
      <PageHeader title="Pedir trabajo al coordinador" sub="Escribe una instrucción nueva. El coordinador prepara un plan con datos reales, TÚ lo confirmas, y solo entonces delega a administración y técnico."
        right={<Chip tone="sky" title="No son agentes autónomos">Roles lógicos · no agentes plenamente autónomos</Chip>} />
      <div className="grid grid-cols-1 xl:grid-cols-[360px_1fr] gap-4">
        <div className="space-y-4 min-w-0">
          <div className="card p-4">
            <form onSubmit={e => { e.preventDefault(); if (text.trim().length >= 5) propose(text); }}>
              <textarea className="input min-h-[110px]" maxLength={600} data-testid="plan-input" placeholder="Ej.: Pide a Javiera que confirme los materiales del mantenimiento de Marta…" value={text} onChange={e => setText(e.target.value)} />
              <div className="flex items-center justify-between mt-2"><span className="text-[11px] text-slate-400">{text.length}/600 · máx. 6 pasos por plan</span>
                <button className="btn btn-primary" disabled={busy || text.trim().length < 5} data-testid="plan-submit">Proponer plan</button></div>
            </form>
            <div className="mt-3 text-[11px] font-semibold text-slate-500">Ejemplos</div>
            <div className="flex flex-col gap-1.5 mt-1">{EXAMPLES.map((x, i) => <button key={i} onClick={() => setText(x)} data-testid={"example-" + i}
              className="text-left text-xs rounded-lg border border-slate-200 px-2.5 py-1.5 hover:bg-brand-50 text-slate-600">{x}</button>)}</div>
            {err && <div className="text-sm text-rose-600 mt-2" role="alert" data-testid="plan-error">{err}</div>}
          </div>
          <div className="card p-4">
            <div className="font-semibold mb-2">Planes recientes</div>
            {(list?.plans ?? []).length === 0 ? <Empty>Aún no hay planes</Empty> : list.plans.map((x: any) => (
              <a key={x.id} href={`#/coordinator/${x.id}`} data-testid="plan-item" className={`block rounded-lg border px-3 py-2 mb-1.5 text-xs ${x.id === planId ? "border-brand-600 bg-brand-50" : "border-slate-200 hover:bg-slate-50"}`}>
                <div className="truncate font-medium">{x.summary ?? x.text ?? x.id}</div><div className="mt-1 flex items-center gap-1.5"><StatusChip s={x.status} /><span className="text-slate-400">{fmtDate(x.created_at)}</span></div></a>))}
          </div>
        </div>
        <div className="min-w-0">
          {!p ? <div className="card"><Empty>{planId ? "Cargando plan…" : "Propón un plan o abre uno de la lista."}</Empty></div> : (
            <div className="card p-5 space-y-4" data-testid="plan-card" data-status={p.status}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div><div className="text-xs text-slate-400">{p.id} · {p.created_by}</div><div className="font-semibold mt-0.5" data-testid="plan-text">«{p.text}»</div></div>
                <div className="flex items-center gap-1.5"><span data-testid="plan-status"><StatusChip s={p.status} /></span>
                  <Chip tone={p.source?.startsWith("llm") ? "violet" : p.fallback ? "amber" : "sky"} title={p.fallback ? "Había un modelo de IA configurado, pero no se pudo usar" : "Quién propuso el plan"} data-testid="plan-source">{p.source?.startsWith("llm") ? `Propuesto por IA (${p.source.slice(4)}) · validado por el motor` : p.fallback ? `Por reglas: ${p.fallback_label}` : "Reglas deterministas (sin LLM)"}</Chip></div>
              </div>
              {p.status_reason && <div className={`text-sm rounded-lg border px-3 py-2 ${p.status === "blocked" || p.status === "failed" ? "bg-rose-50 border-rose-200 text-rose-900" : "bg-amber-50 border-amber-200 text-amber-900"}`} data-testid="plan-reason">{p.status_reason}</div>}
              {["waiting_human", "running"].includes(p.status) && (() => {
                const now = p.steps.filter((x: any) => x.status === "waiting_human"), later = p.steps.filter((x: any) => x.status === "waiting_dependency");
                if (!now.length) return null;
                return <div className="rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 text-sm text-amber-900" data-testid="plan-waiting-on">
                  <b>Esperando a: {now.map((x: any) => `${x.assignee} (${ROLE[x.assignee_role] ?? x.assignee_role})`).join(", ")}</b> — debe(n) completar su tarea desde «Mi asistente» con evidencia.
                  {later.length > 0 && <span> Después: {later.map((x: any) => x.assignee).join(", ")}.</span>}</div>; })()}
              {p.excluded?.length > 0 && <div className="rounded-lg bg-rose-50 border border-rose-200 px-3 py-2 text-sm text-rose-900" data-testid="plan-excluded">
                <b>NO se ejecutará (fuera del alcance del coordinador):</b><ul className="list-disc ml-5">{p.excluded.map((e: any, i: number) => <li key={i}>{e.action}</li>)}</ul></div>}
              {p.ignored?.length > 0 && <div className="rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 text-sm text-amber-900" data-testid="plan-ignored">
                <b>Estas partes de tu instrucción no tienen un paso en el plan y NO se harán:</b><ul className="list-disc ml-5">{p.ignored.map((e: string, i: number) => <li key={i}>«{e}»</li>)}</ul></div>}
              {p.notes?.map((x: string, i: number) => <div key={i} className="text-xs text-slate-500">ℹ {x}</div>)}
              {p.clarification && p.status === "needs_clarification" && (
                <div className="rounded-lg bg-amber-50 border border-amber-200 p-3" data-testid="clarification">
                  <div className="font-semibold text-amber-900 text-sm">{p.clarification.question} <span className="font-normal text-amber-700">(ronda {p.rounds + 1} de {p.max_rounds})</span></div>
                  <div className="flex flex-wrap gap-2 mt-2">{p.clarification.options.map((o: any) => (
                    <button key={o.id} className="btn btn-ghost" disabled={busy} data-testid="clarify-option" onClick={() => run(() => api(`/plans/${p.id}/clarify`, { body: { [p.clarification.field]: o.id } }))}>{o.label}</button>))}
                    {p.clarification.options.length === 0 && <span className="text-xs text-amber-800">Sin opciones: escribe una instrucción nueva con más datos.</span>}</div></div>)}
              {p.status === "blocked" && p.clarification?.suggestions && (
                <div><div className="text-xs font-semibold text-slate-500 mb-1">Lo que sí puedo hacer</div><div className="flex flex-col gap-1">{p.clarification.suggestions.map((x: string, i: number) => <button key={i} className="text-left text-xs rounded-lg border border-slate-200 px-2.5 py-1.5 hover:bg-brand-50" onClick={() => setText(x)}>{x}</button>)}</div></div>)}
              {p.steps.length > 0 && (
                <div>
                  <div className="text-sm font-semibold mb-2">Plan ({p.steps.length} pasos){p.status === "awaiting_user_confirmation" && <span className="font-normal text-amber-700"> · todavía no se creó ninguna tarea</span>}</div>
                  <ol className="space-y-2" data-testid="plan-steps">{p.steps.map((s: any) => (
                    <li key={s.seq} className="rounded-xl border border-slate-200 p-3" data-testid="plan-step" data-action={s.action} data-status={s.status}>
                      <div className="flex flex-wrap items-center gap-1.5"><span className="font-bold text-slate-400">{s.seq}</span><span className="font-medium text-sm">{s.label}</span>
                        <Chip tone={s.access === "read" ? "slate" : "red"}>{s.access === "read" ? "lectura" : "escritura vía herramienta"}</Chip>
                        {s.assignee && <Chip tone="indigo">→ {s.assignee}</Chip>}{s.depends_on && <Chip>depende del paso {s.depends_on}</Chip>}
                        <span className="ml-auto"><Chip tone={STEP_TONE[s.status] ?? "slate"}>{STEP_LABEL[s.status] ?? s.status}</Chip></span></div>
                      <div className="text-xs text-slate-500 mt-1">{s.title}</div>
                      <div className="text-[10px] text-slate-400 mt-1">agente: {s.agent} · permiso: {s.permission} · origen: {s.origin}</div>
                      {s.evidence && <div className="text-xs mt-1.5 rounded bg-emerald-50 border border-emerald-200 px-2 py-1 text-emerald-900" data-testid="step-evidence">Evidencia de {s.assignee}: {s.evidence}</div>}
                      {s.result?.shortages?.length > 0 && <div className="text-xs mt-1 text-rose-700 font-semibold">Faltan: {s.result.shortages.map((x: any) => `${x.sku} (necesita ${x.needed}, hay ${x.stock})`).join(", ")}</div>}
                    </li>))}</ol>
                </div>)}
              {p.result_summary && <div className="rounded-xl border-2 border-emerald-300 bg-emerald-50 p-3" data-testid="plan-result"><div className="font-semibold text-emerald-900 text-sm">🏁 Resultado respaldado por el registro</div>
                <pre className="text-xs whitespace-pre-wrap text-emerald-900 mt-1 font-sans">{p.result_summary}</pre></div>}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                {p.status === "awaiting_user_confirmation" && canConfirm && <button className="btn btn-ok" disabled={busy} data-testid="plan-confirm" onClick={() => run(() => api(`/plans/${p.id}/confirm`, { method: "POST", body: {} }))}>Confirmar plan</button>}
                {active && canConfirm && <button className="btn btn-danger" disabled={busy} data-testid="plan-cancel" onClick={() => run(() => api(`/plans/${p.id}/cancel`, { method: "POST", body: {} }))}>Cancelar plan</button>}
                <a className="btn btn-ghost" href={`#/flows/${p.id}`} data-testid="plan-open-flow">Ver en el visualizador →</a>
                <span className="text-[11px] text-slate-400 ml-auto">La confirmación y las aprobaciones son siempre acciones humanas.</span>
              </div>
            </div>)}
        </div>
      </div>
    </div>
  );
}
