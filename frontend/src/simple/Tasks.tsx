import { useState } from "react";
import { api, Me } from "../api";
import { usePoll } from "../pages/Dashboard";
import { DetailLink } from "./Detail";

export default function Tasks({ me }: { me: Me }) {
  const [t, reload] = usePoll<any>("/tasks", 4000);
  const [ev, setEv] = useState<Record<string, string>>({}); const [msg, setMsg] = useState("");
  const done = async (id: string) => {
    if (!(ev[id] ?? "").trim()) { setMsg("Cuéntanos con tus palabras qué hiciste para poder cerrarla."); return; }
    try { await api(`/tasks/${id}/complete`, { body: { evidence: ev[id] } }); setMsg("Tarea cerrada. Quedó registrado lo que hiciste."); setEv(e => ({ ...e, [id]: "" })); reload(); }
    catch (e: any) { setMsg("No se pudo cerrar: " + e.message); }
  };
  const tasks: any[] = (t?.tasks ?? []).filter((x: any) => x.kind !== "recordatorio");
  const waiting: any[] = t?.waiting ?? [];
  const canClose = me.permissions.includes("tasks.complete");
  return (
    <div className="max-w-2xl mx-auto" data-testid="simple-tasks">
      <h1 className="h-title">Mis tareas</h1>
      <p className="text-sm text-slate-500 mt-0.5 mb-6">{me.role === "technician" ? "Tus visitas y pedidos de material." : "Lo que tienes que dejar listo."}</p>
      {msg && <div className="text-sm mb-3 text-emerald-700" data-testid="s-task-msg" role="status">{msg}</div>}
      {t && tasks.length === 0 && waiting.length === 0 && <div className="card p-6 text-sm text-slate-500" data-testid="s-no-tasks">No tienes tareas pendientes.</div>}
      {tasks.map(x => (
        <div key={x.id} className="card p-5 mb-3" data-testid="s-task">
          <div className="font-medium">{x.title}</div>
          {x.blocked_reason ? <p className="text-sm text-amber-800 mt-2" data-testid="s-task-blocked">Esperando confirmación del cliente. Todavía no se puede cerrar.</p>
            : canClose && <>
              <label className="text-xs text-slate-500 block mt-3" htmlFor={"ev-" + x.id}>¿Qué hiciste? (obligatorio para cerrarla)</label>
              <input id={"ev-" + x.id} className="input mt-1" data-testid="s-evidence" placeholder="Ej.: verifiqué el stock de gas" value={ev[x.id] ?? ""} onChange={e => setEv(v => ({ ...v, [x.id]: e.target.value }))} />
              <button className="btn btn-primary mt-3" data-testid="s-complete" onClick={() => done(x.id)}>Marcar como lista</button></>}
        </div>))}
      {waiting.map(x => <div key={x.id} className="card p-4 mb-3 text-sm text-slate-500" data-testid="s-waiting">{x.title} · esperando a {x.waiting_for ?? "otra persona"}</div>)}
      <div className="mt-4 text-right"><DetailLink to="/assistant" /></div>
    </div>
  );
}
