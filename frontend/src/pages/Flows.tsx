import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, Edge, Handle, MarkerType, Node, NodeProps, Position, ReactFlow } from "@xyflow/react";
import { api, Ev, Me, money } from "../api";
import { Chip, Empty, fmtTime, ModeChip, PageHeader, StatusChip } from "../ui";
import { usePoll } from "./Dashboard";

type St = "idle" | "running" | "waiting" | "blocked" | "failed" | "done";
const NODES: Record<string, { label: string; sub: string; x: number; y: number; kind: "agent" | "human" | "external" }> = {
  customer: { label: "Cliente", sub: "Chat de atención (prueba)", x: 0, y: 150, kind: "external" },
  reception: { label: "Agente de recepción", sub: "Agente externo", x: 340, y: 150, kind: "agent" },
  coordinator: { label: "Coordinador central", sub: "Clasifica · delega · revisa", x: 680, y: 150, kind: "agent" },
  admin_assistant: { label: "Asistente administrativo", sub: "Presupuestos · reservas", x: 1040, y: 10, kind: "agent" },
  tech_assistant: { label: "Asistente del técnico", sub: "Órdenes · materiales", x: 1040, y: 290, kind: "agent" },
  owner_assistant: { label: "Asistente del dueño", sub: "Aprobaciones · estado", x: 680, y: 380, kind: "agent" },
  owner_human: { label: "Dueño/a (humano)", sub: "Decide con su sesión", x: 340, y: 380, kind: "human" },
  admin_human: { label: "Administración (humana)", sub: "Completa tareas con evidencia", x: 1040, y: -170, kind: "human" },
  tech_human: { label: "Técnico/a (humano)", sub: "Informa material · confirma recepción", x: 1040, y: 480, kind: "human" },
};
const HUMAN_OF: Record<string, string> = { owner: "owner_human", admin: "admin_human", technician: "tech_human" };
const ASSISTANT_OF: Record<string, string> = { owner_human: "owner_assistant", admin_human: "admin_assistant", tech_human: "tech_assistant" };
const HUMAN_ROLE: Record<string, string> = { owner_human: "owner", admin_human: "admin", tech_human: "technician" };
const EDGES: [string, string, string, string][] = [ // a, b, handle on a, handle on b
  ["customer", "reception", "r", "l"], ["reception", "coordinator", "r", "l"], ["coordinator", "admin_assistant", "r", "l"],
  ["admin_assistant", "tech_assistant", "b", "t"], ["coordinator", "owner_assistant", "b", "t"], ["owner_assistant", "owner_human", "l", "r"],
  ["admin_assistant", "admin_human", "t", "b"], ["tech_assistant", "tech_human", "b", "t"], ["coordinator", "tech_assistant", "r", "l"],
];
const key = (a: string, b: string) => [a, b].sort().join("|");
const COLORS: Record<St, { ring: string; bg: string; dot: string; text: string }> = {
  idle: { ring: "#e2e8f0", bg: "#fff", dot: "#cbd5e1", text: "En espera" },
  running: { ring: "#4f46e5", bg: "#eef2ff", dot: "#4f46e5", text: "Trabajando" },
  waiting: { ring: "#d97706", bg: "#fffbeb", dot: "#d97706", text: "Esperando a la persona" },
  blocked: { ring: "#e11d48", bg: "#fff1f2", dot: "#e11d48", text: "Bloqueado" },
  failed: { ring: "#be123c", bg: "#fff1f2", dot: "#be123c", text: "Falló" },
  done: { ring: "#10b981", bg: "#ecfdf5", dot: "#10b981", text: "Completó su parte" },
};

type Derived = { status: Record<string, St>; edges: Record<string, { from: string; to: string; task: string; n: number }>; lastTool: Record<string, { name: string; access: string; mode: string; summary: string }>; tools: Record<string, number>; denied: Record<string, number>; note: Record<string, string> };

function derive(evs: Ev[]): Derived {
  const d: Derived = { status: {}, edges: {}, lastTool: {}, tools: {}, denied: {}, note: {} };
  for (const k of Object.keys(NODES)) d.status[k] = "idle";
  const settle = (to: St, from: St[] = ["running"]) => { for (const k of Object.keys(d.status)) if (from.includes(d.status[k])) d.status[k] = to; };
  const hop = (a: string, b: string, task: string) => { const k = key(a, b); const e = d.edges[k]; d.edges[k] = { from: a, to: b, task, n: (e?.n ?? 0) + 1 }; };
  for (const e of evs) {
    const a = e.agent ?? "";
    switch (e.type) {
      case "plan.received": d.status.owner_human = "done"; hop("owner_human", "owner_assistant", "instrucción del dueño"); hop("owner_assistant", "coordinator", "pedido de plan"); d.note.owner_human = e.title; break;
      case "plan.proposed": d.status.coordinator = "running"; d.note.coordinator = e.title; break;
      case "plan.validated": case "plan.needs_clarification": d.status.owner_human = "waiting"; d.note.owner_human = e.type === "plan.validated" ? "Debe confirmar el plan (nada se ejecuta antes)" : e.title; d.note.coordinator = e.title; break;
      case "plan.blocked": d.status.coordinator = "blocked"; d.note.coordinator = e.title; settle("blocked", ["waiting"]); break;
      case "plan.clarified": d.status.owner_human = "done"; d.note.owner_human = e.title; break;
      case "plan.confirmed": d.status.owner_human = "done"; hop("owner_human", "owner_assistant", "CONFIRMÓ el plan"); hop("owner_assistant", "coordinator", "plan confirmado"); d.note.owner_human = e.title; break;
      case "plan.cancelled": d.status.coordinator = "blocked"; d.note.coordinator = e.title; for (const k of Object.keys(d.status)) if (d.status[k] === "waiting") d.status[k] = "idle"; break;
      case "plan.step_verified": d.status.coordinator = "running"; d.note.coordinator = e.title; break;
      case "plan.llm_rejected": d.note.coordinator = e.title; break;
      case "run.started": d.status.customer = "done"; hop("customer", "reception", "mensaje del cliente"); break;
      case "agent.activated": case "agent.recovering": d.status[a] = "running"; d.note[a] = e.title; break;
      case "agent.completed": d.status[a] = "done"; d.note[a] = e.title; break;
      case "agent.blocked": d.status[a] = "blocked"; d.note[a] = e.title; break;
      case "agent.waiting": d.status[a] = "waiting"; d.note[a] = e.title; break;
      case "agent.delegated": hop(a, e.data.to, e.data.task); break;
      case "approval.requested": d.status.owner_human = "waiting"; hop("owner_assistant", "owner_human", "solicitud de aprobación"); break;
      case "approval.decided": d.status.owner_human = "done"; d.status.owner_assistant = "done"; hop("owner_human", "owner_assistant", e.data.decision === "approved" ? "APROBÓ" : "RECHAZÓ"); d.note.owner_human = e.title; break;
      case "task.assigned": { const h = HUMAN_OF[e.data.assignee_role]; if (h) { d.status[h] = "waiting"; d.note[h] = e.title; hop(ASSISTANT_OF[h], h, "tarea asignada"); } break; }
      case "task.completed": { const h = Object.keys(ASSISTANT_OF).find(k => ASSISTANT_OF[k] === a); if (h) { d.status[h] = "done"; d.status[a] = "done"; d.note[h] = e.title; hop(h, a, "evidencia aportada"); } break; }
      case "run.waiting_human": { const h = HUMAN_OF[e.data.role]; if (h) d.status[h] = "waiting"; settle("done"); break; }
      case "customer.confirmed": d.status.customer = "done"; d.note.customer = e.title; break;
      case "run.waiting_approval": settle("done"); break;
      case "run.blocked": settle("done"); break;
      case "run.completed": settle("done"); break;
      case "run.failed": settle("failed"); d.status[a || "coordinator"] = "failed"; d.note[a || "coordinator"] = e.title; break;
      case "tool.called": d.tools[a] = (d.tools[a] ?? 0) + 1; break;
      case "tool.denied": d.denied[a] = (d.denied[a] ?? 0) + 1; break;
      case "tool.result": d.lastTool[a] = { name: e.data.tool, access: e.data.access, mode: e.mode ?? "real", summary: e.data.summary ?? "" }; break;
    }
  }
  return d;
}

const AgentNode = memo(({ id, data }: NodeProps) => {
  const d = data as { st: St; meta: (typeof NODES)[string]; tool?: Derived["lastTool"][string]; tools: number; denied: number; note?: string; selected: boolean };
  const c = COLORS[d.st];
  return (
    <div data-testid={"node-" + id} data-status={d.st} className={`rounded-2xl border-2 px-3.5 py-3 w-[220px] transition-colors ${d.st === "running" ? "node-running" : ""}`}
         style={{ borderColor: d.selected ? "#0f172a" : c.ring, background: c.bg, borderStyle: d.meta.kind === "external" ? "dashed" : "solid" }}>
      {["l", "r", "t", "b"].map(h => <Handle key={"s" + h} id={"s-" + h} type="source" position={{ l: Position.Left, r: Position.Right, t: Position.Top, b: Position.Bottom }[h]!} style={{ opacity: 0 }} />)}
      {["l", "r", "t", "b"].map(h => <Handle key={"t" + h} id={"t-" + h} type="target" position={{ l: Position.Left, r: Position.Right, t: Position.Top, b: Position.Bottom }[h]!} style={{ opacity: 0 }} />)}
      <div className="flex items-center gap-2"><span className="h-2.5 w-2.5 rounded-full" style={{ background: c.dot }} />
        <span className="font-semibold text-[13px] leading-tight">{d.meta.label}</span></div>
      <div className="text-[11px] text-slate-500 mt-0.5">{d.meta.sub}</div>
      <div className="text-[11px] font-semibold mt-1.5" style={{ color: c.dot === "#cbd5e1" ? "#94a3b8" : c.dot }}>{c.text}</div>
      {d.tool && <div className="mt-1.5 rounded-md bg-white/80 border border-slate-200 px-1.5 py-1 text-[10.5px] leading-tight">
        <span className={`font-bold ${d.tool.access === "write" ? "text-rose-600" : "text-emerald-600"}`}>{d.tool.access === "write" ? "BD ✎" : "BD 👁"}</span> <code>{d.tool.name}</code></div>}
      {(d.tools > 0 || d.denied > 0) && <div className="mt-1 text-[10px] text-slate-400">{d.tools} herramienta(s){d.denied > 0 && <b className="text-rose-600"> · {d.denied} denegada(s)</b>}</div>}
    </div>
  );
});
const nodeTypes = { agent: AgentNode };

// What is PERSISTED in the database for this run, filtered to the selected node (not an animation)
function RealState({ sel, run }: { sel: string; run: any }) {
  if (!run) return null;
  const human = HUMAN_ROLE[sel]; const agent = ASSISTANT_OF[sel] ?? sel;
  const dels = (run.delegations ?? []).filter((x: any) => x.from_agent === agent || x.to_agent === agent);
  const tasks = (run.tasks ?? []).filter((t: any) => human ? t.assignee_role === human : t.assignee_agent === agent);
  const cms = (run.commitments ?? []).filter((c: any) => human ? c.owner_role === human : false);
  const aps = sel === "owner_human" || sel === "owner_assistant" ? run.approvals ?? [] : [];
  const res = sel === "coordinator" ? run.results ?? [] : [];
  const mats = sel === "admin_assistant" || sel === "tech_assistant" ? run.materials ?? [] : [];
  const drafts = sel === "reception" ? run.drafts ?? [] : [];
  if (![dels, tasks, cms, aps, res, mats, drafts].some(a => a.length)) return null;
  const Row = ({ children }: { children: React.ReactNode }) => <div className="text-[11px] border-t first:border-0 border-slate-100 py-1">{children}</div>;
  return (
    <div className="mt-3 rounded-lg bg-slate-50 border border-slate-200 p-2.5" data-testid="real-state">
      <div className="text-[11px] font-bold text-slate-500 mb-1">DATOS REALES EN LA BASE (esta ejecución)</div>
      {dels.map((x: any, i: number) => <Row key={"d" + i}>↪ <b>{x.from_agent}</b> → <b>{x.to_agent}</b>: {x.task}</Row>)}
      {tasks.map((t: any) => <Row key={t.id}>📌 {t.title} <Chip tone={t.status === "hecha" ? "green" : t.status === "cancelada" ? "red" : "amber"}>{t.status}</Chip>{t.evidence && <div className="text-slate-500">Evidencia: {t.evidence}</div>}</Row>)}
      {cms.map((c: any, i: number) => <Row key={"c" + i}>📅 {c.title} · plazo {c.due_at.slice(0, 16).replace("T", " ")} <Chip tone={c.status === "cumplido" ? "green" : c.status === "cancelado" ? "red" : "amber"}>{c.status}</Chip></Row>)}
      {aps.map((a: any) => <Row key={a.id}>⚖ {a.subject} · {money(a.amount)} <Chip tone={a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "amber"}>{a.status}</Chip>{a.decided_by && <> · decidió <b>{a.decided_by}</b></>}</Row>)}
      {mats.map((m: any, i: number) => <Row key={"m" + i}>📦 {m.sku} x{m.qty} · {money(m.cost ?? 0)} <Chip>{m.status}</Chip></Row>)}
      {res.map((r: any, i: number) => <Row key={"r" + i}>🏁 {r.kind}: {r.summary}</Row>)}
      {drafts.map((d: any, i: number) => <Row key={"dr" + i}>✉ <Chip tone="amber">{d.status}</Chip> {d.body.slice(0, 90)}…</Row>)}
    </div>
  );
}

function Json({ v }: { v: unknown }) {
  return <pre className="bg-slate-900 text-slate-100 rounded-lg p-2.5 text-[11px] leading-snug overflow-auto max-h-56 scroll-thin">{JSON.stringify(v, null, 2)}</pre>;
}
const ICON: Record<string, string> = { "tool.called": "🔧", "tool.result": "🗄", "tool.denied": "⛔", "tool.error": "⚠", "agent.delegated": "↪", "agent.activated": "▶", "agent.completed": "✓", "agent.blocked": "⏸", "agent.waiting": "⏳", "approval.requested": "✋", "approval.decided": "⚖", "run.started": "●", "run.completed": "🏁", "run.waiting_approval": "⏳", "task.assigned": "📌", "task.completed": "✅", "commitment.created": "📅", "commitment.closed": "🔒", "commitment.reassigned": "↔", "reminder.created": "⏰", "followup.drafted": "✉", "run.waiting_human": "👤", "run.blocked": "⏸", "run.failed": "✕", "run.resolved": "↻", "customer.confirmed": "✔", "agent.recovering": "↻", "plan.received": "💬", "plan.proposed": "🧠", "plan.validated": "🛡", "plan.needs_clarification": "❓", "plan.blocked": "⛔", "plan.clarified": "☑", "plan.confirmed": "✋", "plan.cancelled": "✕", "plan.step_verified": "🔎", "plan.llm_rejected": "✂", "task.queued": "⏸" };

// Honest stage label for plan events: what KIND of thing each step is (LLM proposal vs validated plan vs executed action vs human wait vs backed result vs simulation)
const STAGE = (e: Ev): { label: string; tone: "violet" | "sky" | "green" | "red" | "amber" | "slate" } | null => {
  if (e.type === "plan.proposed") return { label: "Propuesta del LLM (sin validar)", tone: "violet" };
  if (e.type === "plan.llm_rejected") return { label: "Descartado por el motor", tone: "amber" };
  if (e.type === "plan.validated") return { label: "Plan validado", tone: "sky" };
  if (e.type === "plan.confirmed" || e.type === "plan.clarified") return { label: "Decisión humana", tone: "green" };
  if (e.type === "tool.result" && e.data.access === "write") return { label: "Acción ejecutada", tone: "red" };
  if (e.type === "run.waiting_human" || e.type === "task.queued") return { label: "Espera de humano", tone: "amber" };
  if (e.type === "plan.step_verified" || (e.type === "run.completed" && e.data?.summary)) return { label: "Resultado respaldado", tone: "green" };
  if (e.mode === "simulated") return { label: "Simulación", tone: "amber" };
  return null;
};

export default function Flows({ me, runId }: { me: Me; runId?: string }) {
  const [runs, reloadRuns] = usePoll<any>("/runs", 3000);
  const [plans, reloadPlans] = usePoll<any>(me.permissions.includes("plans.create") ? "/plans" : "/plans", 3000);
  const isPlan = !!runId && runId.startsWith("pln_"); const base = isPlan ? "plans" : "runs";
  const [agents] = usePoll<any>("/agents", 30000);
  const [events, setEvents] = useState<Ev[]>([]);
  const [run, setRun] = useState<any>(null);
  const [idx, setIdx] = useState(0);
  const [live, setLive] = useState(true);
  const [sel, setSel] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [conn, setConn] = useState<"live" | "closed">("closed");
  const [busy, setBusy] = useState(false); const [err, setErr] = useState("");
  const feedEnd = useRef<HTMLDivElement>(null);

  const loadRun = useCallback(() => { if (runId) api(`/${runId.startsWith("pln_") ? "plans" : "runs"}/${runId}`).then(setRun).catch(() => setRun(null)); }, [runId]);
  useEffect(() => {
    setEvents([]); setIdx(0); setLive(true); setSel(null); setRun(null); setErr(""); setPlaying(false);
    if (!runId) return;
    loadRun();
    const es = new EventSource(`/api/${runId.startsWith("pln_") ? "plans" : "runs"}/${runId}/stream`);
    es.onopen = () => setConn("live");
    es.onerror = () => setConn("closed");
    es.addEventListener("run-event", (m: MessageEvent) => {
      const ev: Ev = JSON.parse(m.data);
      setEvents(prev => prev.some(p => p.seq === ev.seq) ? prev : [...prev, ev]);
      loadRun(); reloadRuns(); reloadPlans();
    });
    return () => es.close();
  }, [runId]);
  useEffect(() => { const t = setInterval(loadRun, 3000); return () => clearInterval(t); }, [loadRun]);
  useEffect(() => { if (live) setIdx(events.length); }, [events.length, live]);
  useEffect(() => { if (live) feedEnd.current?.scrollIntoView({ block: "nearest" }); }, [events.length, live]);
  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => setIdx(i => { if (i >= events.length) { setPlaying(false); setLive(true); return i; } return i + 1; }), 380);
    return () => clearInterval(t);
  }, [playing, events.length]);

  const shown = useMemo(() => events.slice(0, idx), [events, idx]);
  const d = useMemo(() => derive(shown), [shown]);

  const nodes: Node[] = useMemo(() => Object.entries(NODES).map(([id, m]) => ({
    id, type: "agent", position: { x: m.x, y: m.y }, draggable: true, data: { st: d.status[id], meta: m, tool: d.lastTool[id], tools: d.tools[id] ?? 0, denied: d.denied[id] ?? 0, selected: sel === id },
  })), [d, sel]);
  const edges: Edge[] = useMemo(() => EDGES.map(([a, b, ha, hb]) => {
    const e = d.edges[key(a, b)];
    const forward = !e || (e.from === a);
    const animated = !!e && d.status[e.to] === "running";
    const color = e ? (animated ? "#4f46e5" : "#818cf8") : "#cbd5e1";
    return { id: key(a, b), source: a, target: b, sourceHandle: "s-" + ha, targetHandle: "t-" + hb, animated, type: "default",
      label: e ? `${e.n > 1 ? e.n + "× " : ""}${e.task.length > 26 ? e.task.slice(0, 25) + "…" : e.task}` : undefined,
      labelStyle: { fontSize: 10, fill: "#334155", fontWeight: 600 }, labelBgStyle: { fill: "#fff", fillOpacity: 0.95 }, labelBgPadding: [4, 2] as [number, number], labelBgBorderRadius: 4,
      style: { stroke: color, strokeWidth: e ? 2.5 : 1.5 },
      markerEnd: e && forward ? { type: MarkerType.ArrowClosed, color } : undefined, markerStart: e && !forward ? { type: MarkerType.ArrowClosed, color } : undefined };
  }), [d]);

  const dur = useMemo(() => { const m: Record<number, number> = {}; const open: Record<string, Ev> = {};
    for (const e of events) { const k = (e.agent ?? "") + e.data?.tool; if (e.type === "tool.called") open[k] = e; else if (e.type === "tool.result" && open[k]) { m[e.seq] = new Date(e.ts).getTime() - new Date(open[k].ts).getTime(); } } return m; }, [events]);
  const nodeEvents = useMemo(() => !sel ? [] : shown.filter(e => e.agent === sel || (sel === "customer" && e.type === "run.started") || (sel === "owner_human" && (e.type.startsWith("approval") || e.type.startsWith("plan."))) || (sel in ASSISTANT_OF && (e.agent === ASSISTANT_OF[sel] && (e.type.startsWith("task.") || e.type === "run.waiting_human")))), [shown, sel]);
  const canDecide = me.permissions.includes("approvals.decide");
  const canResolve = me.permissions.includes("runs.resolve");
  const pendingAp = run?.approvals?.find((a: any) => a.status === "pendiente");
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); setErr(""); try { await fn(); loadRun(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const agentMeta = sel ? agents?.agents?.find((a: any) => a.role === sel) : null;

  return (
    <div>
      <PageHeader title="Visualizador de flujos" sub="Cada nodo y cada flecha representan eventos REALES del motor, guardados en la base de datos y transmitidos por SSE."
        right={run && <div className="flex flex-wrap items-center gap-2"><StatusChip s={run.status} />{isPlan && <Chip tone="indigo">Escenario E · plan</Chip>}<Chip tone={conn === "live" ? "green" : "slate"}>{conn === "live" ? "● SSE conectado" : "SSE cerrado"}</Chip>
          <Chip tone={run.provider?.startsWith("ollama") || run.provider?.startsWith("llm") ? "violet" : "sky"} title="Motor de decisiones">{run.provider?.startsWith("ollama") || run.provider?.startsWith("llm") ? "LLM local (propone; el motor valida)" : "Motor determinista"}</Chip></div>} />
      <div className="card p-3 mb-3">
        <div className="flex gap-2 overflow-x-auto scroll-thin pb-1" data-testid="run-list">
          {(plans?.plans ?? []).map((pl: any) => (
            <a key={pl.id} href={`#/flows/${pl.id}`} data-testid="plan-run-item" className={`shrink-0 rounded-lg border px-3 py-1.5 text-xs ${pl.id === runId ? "border-brand-600 bg-brand-50" : "border-slate-200 hover:bg-slate-50"}`}>
              <div className="font-semibold">Plan del coordinador <Chip tone="indigo">E</Chip></div><div className="text-slate-400 truncate max-w-[160px]">{pl.summary ?? pl.text ?? pl.id}</div><div className="mt-1"><StatusChip s={pl.status} /></div></a>))}
          {(runs?.runs ?? []).length === 0 && (plans?.plans ?? []).length === 0 && <span className="text-sm text-slate-400 px-2">Sin ejecuciones aún. Envía un mensaje desde el <a className="text-brand-700 font-semibold" href={`#/cliente/${me.company.slug}`}>chat de cliente</a>.</span>}
          {(runs?.runs ?? []).map((r: any) => (
            <a key={r.id} href={`#/flows/${r.id}`} data-testid="run-item" className={`shrink-0 rounded-lg border px-3 py-1.5 text-xs ${r.id === runId ? "border-brand-600 bg-brand-50" : "border-slate-200 hover:bg-slate-50"}`}>
              <div className="font-semibold">{r.customer ?? "Nuevo caso"} {r.scenario === "D" && <Chip tone="violet">D</Chip>}</div><div className="text-slate-400 truncate max-w-[160px]">{r.description ?? r.id}</div><div className="mt-1"><StatusChip s={r.status} /></div></a>))}
        </div>
      </div>
      {!runId ? <div className="card"><Empty>Selecciona una ejecución para ver su recorrido.</Empty></div> : (
        <div className="grid xl:grid-cols-[1fr_380px] gap-3">
          <div className="space-y-3 min-w-0">
            {run?.status === "waiting_approval" && pendingAp && (
              <div className="card p-4 border-amber-300 bg-amber-50" data-testid="approval-card">
                <div className="flex flex-wrap items-center justify-between gap-3"><div><div className="font-semibold text-amber-900">Aprobación humana requerida · {pendingAp.subject}</div>
                  <div className="text-sm text-amber-800">{pendingAp.summary}</div><div className="text-2xl font-bold text-amber-900 mt-1">{money(pendingAp.amount)}</div></div>
                  {canDecide ? <div className="flex gap-2"><button className="btn btn-ok" disabled={busy} data-testid="approve" onClick={() => act(() => api(`/approvals/${pendingAp.id}/decide`, { body: { decision: "approved" } }))}>Aprobar</button>
                    <button className="btn btn-danger" disabled={busy} data-testid="reject" onClick={() => act(() => api(`/approvals/${pendingAp.id}/decide`, { body: { decision: "rejected", comment: "Rechazado desde el visualizador" } }))}>Rechazar</button></div>
                    : <Chip tone="amber">Solo el dueño puede decidir</Chip>}</div></div>)}
            {run?.status === "blocked" && (
              <div className="card p-4 border-rose-300 bg-rose-50" data-testid="blocked-card"><div className="font-semibold text-rose-900">Ejecución bloqueada</div>
                <div className="text-sm text-rose-800 mb-2">{[...events].reverse().find(e => e.type === "run.blocked")?.title}</div>
                {canResolve ? <div className="flex flex-wrap gap-2">{(run.alternatives ?? []).map((a: any) => (
                  <button key={a.id} className="btn btn-ghost" disabled={busy} data-testid={"resolve-" + a.id} onClick={() => act(() => api(`/runs/${runId}/resolve`, { body: { action: a.id } }))}>{a.label}</button>))}</div>
                  : <Chip tone="red">Requiere decisión de administración o dueño</Chip>}</div>)}
            {err && <div className="text-sm text-rose-600" role="alert">{err}</div>}
            {isPlan && run?.steps && (
              <div className="card p-3" data-testid="plan-panel"><div className="flex items-center justify-between mb-1.5"><div className="font-semibold text-sm">Plan {run.id}</div>
                <a className="text-xs font-semibold text-brand-700" href={`#/coordinator/${run.id}`}>Gestionar en «Coordinador» →</a></div>
                <ol className="flex flex-wrap gap-1.5">{run.steps.map((st: any) => (
                  <li key={st.seq} data-testid="plan-panel-step" data-status={st.status} className="rounded-lg border border-slate-200 px-2 py-1 text-[11px]"><b>{st.seq}.</b> {st.label}{st.assignee ? ` → ${st.assignee}` : ""}{" "}
                    <Chip tone={st.status === "done" ? "green" : st.status === "waiting_human" ? "amber" : st.status === "cancelled" ? "red" : "slate"}>{({ planned: "planificado", waiting_dependency: "en espera", waiting_human: "espera humana", done: "hecho", cancelled: "cancelado" } as any)[st.status] ?? st.status}</Chip></li>))}</ol>
                {run.result_summary && <pre className="mt-2 text-[11px] whitespace-pre-wrap font-sans text-emerald-900 bg-emerald-50 border border-emerald-200 rounded p-2" data-testid="plan-panel-result">{run.result_summary}</pre>}
              </div>)}
            <div className="card overflow-hidden relative h-[540px]" data-testid="flow-canvas">
              <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.12 }} nodesConnectable={false} onNodeClick={(_, n) => setSel(n.id)} onPaneClick={() => setSel(null)} minZoom={0.3}>
                <Background gap={22} color="#e5e7eb" /><Controls showInteractive={false} />
              </ReactFlow>
              <div className="absolute left-3 bottom-3 flex flex-wrap gap-1.5 pointer-events-none">
                {(["running", "waiting", "blocked", "done"] as St[]).map(s => <span key={s} className="chip bg-white/90 border-slate-200 text-slate-600"><span className="h-2 w-2 rounded-full" style={{ background: COLORS[s].dot }} />{COLORS[s].text}</span>)}
              </div>
            </div>
            <div className="card p-3">
              <div className="flex items-center gap-3">
                <button className="btn btn-ghost" data-testid="play" onClick={() => { if (idx >= events.length) setIdx(0); setLive(false); setPlaying(p => !p); }}>{playing ? "❚❚ Pausa" : "▶ Reproducir"}</button>
                <input type="range" min={0} max={events.length} value={idx} className="flex-1 accent-indigo-600" data-testid="scrubber" aria-label="Paso de la ejecución"
                       onChange={e => { setIdx(+e.target.value); setLive(+e.target.value === events.length); setPlaying(false); }} />
                <span className="text-xs text-slate-500 tabular-nums w-16 text-right" data-testid="step-counter">{idx}/{events.length}</span>
                <button className={`btn ${live ? "btn-primary" : "btn-ghost"}`} onClick={() => { setLive(true); setIdx(events.length); setPlaying(false); }}>En vivo</button>
              </div>
            </div>
            <div className="card p-3">
              <div className="font-semibold text-sm mb-2">Registro de eventos <span className="text-slate-400 font-normal">({events.length})</span></div>
              <div className="max-h-72 overflow-auto scroll-thin" data-testid="event-feed">
                {events.map((e, i) => (
                  <div key={e.seq} onClick={() => { setSel(e.agent && NODES[e.agent] ? e.agent : sel); setLive(false); setIdx(i + 1); setPlaying(false); }} data-testid="event-row" data-type={e.type}
                       className={`ev-in flex items-center gap-2 text-xs py-1 px-1.5 rounded cursor-pointer ${i + 1 === idx ? "bg-indigo-50" : i + 1 > idx ? "opacity-35" : "hover:bg-slate-50"}`}>
                    <span className="text-slate-400 tabular-nums w-14 shrink-0">{fmtTime(e.ts)}</span><span className="w-4 text-center">{ICON[e.type] ?? "•"}</span>
                    <span className="truncate flex-1 min-w-0">{e.title}</span>{e.type === "tool.result" && <span className="hidden sm:inline-flex"><Chip tone={e.data.access === "write" ? "red" : "slate"}>BD {e.data.access === "write" ? "escritura" : "lectura"}</Chip></span>}
                    {(e.type.startsWith("tool.") || e.type === "run.completed") && <span className="hidden sm:inline-flex"><ModeChip m={e.mode} /></span>}{STAGE(e) && <span data-testid="stage-chip"><Chip tone={STAGE(e)!.tone}>{STAGE(e)!.label}</Chip></span>}</div>))}
                <div ref={feedEnd} />
              </div>
            </div>
          </div>
          <aside className="card p-4 h-fit xl:sticky xl:top-0" data-testid="inspector">
            {!sel ? <div><div className="font-semibold mb-1">Inspector</div><Empty>Haz clic en un nodo (o en un evento) para ver su actividad, entradas, salidas y resultados.</Empty></div> : (
              <div>
                <div className="flex items-start justify-between"><div><div className="font-semibold">{NODES[sel].label}</div><div className="text-xs text-slate-500">{NODES[sel].sub}</div></div>
                  <Chip tone={d.status[sel] === "blocked" || d.status[sel] === "failed" ? "red" : d.status[sel] === "done" ? "green" : d.status[sel] === "waiting" ? "amber" : "indigo"}>{COLORS[d.status[sel]].text}</Chip></div>
                {d.note[sel] && <p className="text-xs text-slate-600 mt-2">{d.note[sel]}</p>}
                {agentMeta && <details className="mt-2"><summary className="text-xs font-semibold text-brand-700 cursor-pointer">Permisos efectivos del agente ({agentMeta.permissions.length})</summary>
                  <div className="flex flex-wrap gap-1 mt-1">{agentMeta.permissions.map((p: string) => <Chip key={p}>{p}</Chip>)}</div></details>}
                <RealState sel={sel} run={run} />
                <div className="font-semibold text-sm mt-4 mb-2">Actividad ({nodeEvents.length})</div>
                <div className="space-y-2 max-h-[520px] overflow-auto scroll-thin" data-testid="inspector-events">
                  {nodeEvents.length === 0 && <Empty>Sin actividad todavía</Empty>}
                  {nodeEvents.map(e => (
                    <details key={e.seq} className="rounded-lg border border-slate-200 px-2.5 py-1.5" data-testid="inspector-event">
                      <summary className="text-xs cursor-pointer flex items-center gap-1.5"><span>{ICON[e.type] ?? "•"}</span><span className="flex-1">{e.title}</span>{dur[e.seq] !== undefined && <span className="text-[10px] text-slate-400">{dur[e.seq]} ms</span>}{e.type.startsWith("tool.") && <ModeChip m={e.mode} />}</summary>
                      <div className="mt-2 space-y-2">
                        {e.data.input !== undefined && <div><div className="text-[11px] font-semibold text-slate-500 mb-0.5">Entrada</div><Json v={e.data.input} /></div>}
                        {e.data.output !== undefined && <div><div className="text-[11px] font-semibold text-slate-500 mb-0.5">Salida {e.data.access && `(BD ${e.data.access === "write" ? "escritura" : "lectura"})`}</div><Json v={e.data.output} /></div>}
                        {e.data.input === undefined && e.data.output === undefined && Object.keys(e.data).length > 0 && <Json v={e.data} />}
                      </div></details>))}
                </div>
              </div>)}
          </aside>
        </div>)}
    </div>
  );
}
