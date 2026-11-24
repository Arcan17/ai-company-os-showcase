import { useState } from "react";
import { api, Me, money } from "../api";
import { usePoll } from "../pages/Dashboard";
import { DetailLink } from "./Detail";
import { requestStatus, STAGES, stageIndex } from "./copy";

const clean = (s: string) => s.replace(/_/g, " ").replace(/\$\s?(\d{1,3})[,.](\d{3})/g, "$$$1.$2");

export function Progress({ status }: { status: string }) {
  const at = stageIndex(status);
  return <ol className="flex flex-wrap gap-1.5 text-xs" data-testid="s-progress">{STAGES.map((s, i) =>
    <li key={s} className={`rounded-full px-2.5 py-1 border ${i < at ? "bg-emerald-50 text-emerald-700 border-emerald-200" : i === at ? "bg-amber-50 text-amber-800 border-amber-200 font-semibold" : "bg-white/40 text-slate-400 border-white/70"}`}>{s}</li>)}</ol>;
}

export default function OwnerHome({ me }: { me: Me }) {
  const [d, reload] = usePoll<any>("/dashboard", 3000);
  const [rq] = usePoll<any>("/requests", 4000);
  const [busy, setBusy] = useState(false); const [msg, setMsg] = useState("");
  const decide = async (id: string, decision: "approved" | "rejected") => {
    setBusy(true); setMsg("");
    try { await api(`/approvals/${id}/decide`, { body: { decision, comment: decision === "rejected" ? "Rechazado desde la vista simple" : "" } }); setMsg(decision === "approved" ? "Listo, aprobado. El equipo sigue con el trabajo." : "Rechazado. Quedó registrado."); reload(); }
    catch (e: any) { setMsg("No se pudo: " + e.message); } finally { setBusy(false); }
  };
  if (!d) return <div className="text-slate-400">Cargando…</div>;
  const pending: any[] = d.pending_approvals ?? [];
  const first = pending[0];
  const active = (rq?.requests ?? []).filter((r: any) => r.status !== "cerrada");
  return (
    <div className="max-w-2xl mx-auto" data-testid="simple-home">
      <h1 className="h-title">Hola, {me.name.split(" ")[0]}</h1>
      <p className="text-sm text-slate-500 mt-0.5 mb-6">{me.company.name} · esto es lo que necesita de ti hoy.</p>
      {first ? (
        <div className="card p-6 mb-4" data-testid="s-decision">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-amber-700">Necesita tu decisión</div>
          <div className="text-lg font-semibold mt-1">{clean(first.summary)}</div>
          <div className="text-4xl font-semibold tracking-tight mt-2">{money(first.amount)}</div>
          <p className="text-sm text-slate-500 mt-2">Nada se envía al cliente hasta que decidas.</p>
          <div className="flex gap-2 mt-4"><button className="btn btn-ok" disabled={busy} data-testid="s-approve" onClick={() => decide(first.id, "approved")}>Aprobar</button>
            <button className="btn btn-danger" disabled={busy} data-testid="s-reject" onClick={() => decide(first.id, "rejected")}>Rechazar</button></div>
          {pending.length > 1 && <p className="text-xs text-slate-400 mt-3">Y {pending.length - 1} más esperando después de esta.</p>}
        </div>
      ) : (
        <div className="card p-6 mb-4" data-testid="s-nothing"><div className="text-lg font-semibold">Nada requiere tu decisión hoy</div><p className="text-sm text-slate-500 mt-1">Cuando llegue algo que necesite tu visto bueno aparecerá aquí.</p></div>
      )}
      {msg && <div className="text-sm text-emerald-700 mb-3" data-testid="s-msg" role="status">{msg}</div>}
      <div className="card p-5">
        <div className="font-semibold mb-3">Trabajos en curso</div>
        {active.length === 0 ? <p className="text-sm text-slate-400">No hay trabajos en curso.</p> : active.slice(0, 3).map((r: any) => (
          <div key={r.id} className="border-t first:border-0 border-white/60 py-3" data-testid="s-active"><div className="text-sm font-medium">{r.customer}</div>
            <div className="text-xs text-slate-500 mb-2">{requestStatus(r.status)}</div><Progress status={r.status} /></div>))}
      </div>
      <div className="mt-4 text-right"><DetailLink to="/dashboard" /></div>
    </div>
  );
}
