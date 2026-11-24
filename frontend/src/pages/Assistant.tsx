import { useEffect, useRef, useState } from "react";
import { api, Me, ROLE_LABEL } from "../api";
import { Chip, Empty, PageHeader } from "../ui";
import { usePoll } from "./Dashboard";

const SUGGEST: Record<string, string[]> = {
  owner: ["¿Qué requiere mi atención?", "¿Cómo vamos? Estado de la empresa", "¿Qué aprobaciones tengo pendientes?", "Compromisos", "Mis notas"],
  admin: ["¿Qué requiere mi atención?", "Tareas pendientes", "Compromisos", "Solicitudes", "Muéstrame las notas de Carolina"],
  technician: ["¿Qué requiere mi atención?", "Mis trabajos asignados", "Necesito dos cilindros de gas para la visita de Marta", "Compromisos", "Dame las notas de Javiera"],
};

export default function Assistant({ me }: { me: Me }) {
  const [msgs, setMsgs] = useState<any[]>([]); const [text, setText] = useState(""); const [busy, setBusy] = useState(false);
  const [tasks, reloadTasks] = usePoll<any>("/tasks", 4000);
  const [wo] = usePoll<any>("/work-orders", 5000);
  const [cms] = usePoll<any>("/commitments", 5000);
  const [ev, setEv] = useState<Record<string, string>>({}); const [tmsg, setTmsg] = useState<string>("");
  const completeTask = async (id: string) => {
    try { const r = await api(`/tasks/${id}/complete`, { body: { evidence: ev[id] ?? "" } }); setTmsg(r.effect ? `Hecho: ${r.effect}` : "Tarea completada"); reloadTasks(); }
    catch (e: any) { setTmsg("No se pudo: " + e.message); }
  };
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { api("/assistant/history").then(h => setMsgs(h.messages.map((m: any) => ({ who: m.sender === "user" ? "me" : "bot", text: m.body, calls: m.status && m.sender !== "user" ? JSON.parse(m.status) : [] })))); }, []);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs]);
  const send = async (t: string) => {
    if (!t.trim() || busy) return; setBusy(true); setText("");
    setMsgs(m => [...m, { who: "me", text: t }]);
    try { const r = await api("/assistant/chat", { body: { message: t } }); setMsgs(m => [...m, { who: "bot", text: r.reply, calls: r.tool_calls }]); reloadTasks(); }
    finally { setBusy(false); }
  };
  return (
    <div>
      <PageHeader title="Mi asistente" sub={`Asistente personal de ${me.name} · actúa solo con los permisos de ${ROLE_LABEL[me.role]}.`} />
      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card lg:col-span-2 flex flex-col h-[70vh]">
          <div className="flex-1 overflow-auto p-4 space-y-3 scroll-thin" data-testid="assistant-log">
            {msgs.length === 0 && <Empty>Pregúntame por tu estado, tareas o trabajos. Prueba una sugerencia de abajo.</Empty>}
            {msgs.map((m, i) => m.who === "me"
              ? <div key={i} className="flex justify-end"><div className="bg-brand-600 text-white rounded-2xl rounded-br-sm px-3.5 py-2 max-w-[85%] text-sm whitespace-pre-line">{m.text}</div></div>
              : <div key={i} className="flex justify-start"><div className="bg-slate-100 rounded-2xl rounded-bl-sm px-3.5 py-2 max-w-[85%] text-sm" data-testid="assistant-reply">
                  <div className="whitespace-pre-line">{m.text}</div>
                  {m.calls?.length > 0 && <div className="mt-2 flex flex-wrap gap-1">{m.calls.map((c: any, j: number) => <Chip key={j} tone={c.ok ? "green" : "red"} title={c.summary}>{c.ok ? "✓" : "✕"} {c.tool}</Chip>)}</div>}
                </div></div>)}
            <div ref={end} />
          </div>
          <div className="border-t border-slate-100 p-3">
            <div className="flex flex-wrap gap-1.5 mb-2">{SUGGEST[me.role].map(s => <button key={s} onClick={() => send(s)} className="chip bg-slate-100 border-slate-200 text-slate-600 hover:bg-brand-50 hover:text-brand-700 cursor-pointer">{s}</button>)}</div>
            <form onSubmit={e => { e.preventDefault(); send(text); }} className="flex gap-2">
              <input className="input" data-testid="assistant-input" placeholder="Escribe a tu asistente…" value={text} onChange={e => setText(e.target.value)} />
              <button className="btn btn-primary" disabled={busy} data-testid="assistant-send">Enviar</button>
            </form>
          </div>
        </div>
        <div className="space-y-4">
          <div className="card p-4"><div className="font-semibold mb-2">Mis tareas y recordatorios</div>
            {tmsg && <div className="text-xs text-emerald-700 mb-2" data-testid="task-msg">{tmsg}</div>}
            {(tasks?.tasks ?? []).length === 0 ? <Empty>Nada pendiente</Empty> : tasks.tasks.map((t: any) => (
              <div key={t.id} className="border-t first:border-0 border-slate-100 py-2 text-sm" data-testid="my-task">
                <div>{t.title}</div><div className="flex flex-wrap gap-1 mt-1">{t.kind && <Chip tone="indigo">{t.kind}</Chip>}{t.blocked_reason && <span data-testid="my-task-blocked" className="text-[11px] rounded-md bg-amber-50 border border-amber-200 text-amber-800 px-1.5 py-0.5 max-w-full">🔒 {t.blocked_reason}</span>}<code className="text-[10px] text-slate-400">{t.id}</code></div>
                {me.permissions.includes("tasks.complete") && t.kind !== "recordatorio" && !t.blocked_reason && (
                  <div className="mt-1.5 flex gap-1"><input className="input !py-1 !text-xs" placeholder="Evidencia (mín. 10 caracteres)" value={ev[t.id] ?? ""} onChange={e => setEv({ ...ev, [t.id]: e.target.value })} data-testid="evidence-input" />
                    <button className="btn btn-ghost !py-1 !text-xs" onClick={() => completeTask(t.id)} data-testid="complete-task">Completar</button></div>)}
              </div>))}</div>
          {(tasks?.waiting ?? []).length > 0 && <div className="card p-4" data-testid="my-tasks-queued"><div className="font-semibold mb-1">En espera de otra persona</div>
            <p className="text-[11px] text-slate-400 mb-2">Ya existen, pero aún no puedes actuar: se habilitan solas cuando la otra persona termine.</p>
            {tasks.waiting.map((t: any) => <div key={t.id} className="border-t first:border-0 border-slate-100 py-2 text-sm text-slate-600" data-testid="my-task-queued"><div>{t.title}</div>
              <div className="mt-1"><Chip tone="amber">en espera de {t.waiting_for ?? "otro paso del plan"}</Chip></div></div>)}</div>}
          <div className="card p-4"><div className="font-semibold mb-2">Mis compromisos</div>
            {(cms?.commitments ?? []).length === 0 ? <Empty>Sin compromisos</Empty> : cms.commitments.slice(0, 8).map((c: any) => (
              <div key={c.id} className="border-t first:border-0 border-slate-100 py-1.5 text-xs">{c.title}<div className="text-slate-400">{c.owner} · {c.due_at.slice(0, 16).replace("T", " ")} · <b className={c.overdue ? "text-rose-600" : ""}>{c.overdue && c.status !== "cumplido" ? "vencido" : c.status}</b></div></div>))}</div>
          {me.role === "technician" && (
            <div className="card p-4"><div className="font-semibold mb-2">Mis trabajos</div>
              {(wo?.work_orders ?? []).length === 0 ? <Empty>Sin trabajos asignados</Empty> : wo.work_orders.map((w: any) => (
                <div key={w.request_id} className="border-t first:border-0 border-slate-100 py-2 text-sm"><div className="font-medium">{w.customer}</div><div className="text-xs text-slate-500">{w.description}</div>
                  <div className="mt-1 flex gap-1"><Chip>{w.status}</Chip><code className="text-[11px] text-slate-400">{w.request_id}</code></div></div>))}
              <p className="text-[11px] text-slate-400 mt-2">Prueba: «marcar &lt;id&gt; en camino», «solicitar material GAS-R410 para &lt;id&gt;», «reportar problema en &lt;id&gt;: …»</p></div>)}
          <div className="card p-4 text-xs text-slate-500">Tu asistente <b>no puede</b> leer las notas privadas de otros empleados ni aprobar presupuestos. Pídele «las notas de Javiera» para comprobarlo.</div>
        </div>
      </div>
    </div>
  );
}
