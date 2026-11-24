import { useEffect, useState } from "react";
import { api, Me, money } from "../api";
import { usePoll } from "../pages/Dashboard";
import { DetailLink, VisitState } from "./Detail";
import { Progress } from "./OwnerHome";
import { requestStatus } from "./copy";

function One({ id }: { id: string }) {
  const [d, setD] = useState<any>(null);
  useEffect(() => { api(`/requests/${id}`).then(setD).catch(() => setD(false)); }, [id]);
  if (d === false) return <p className="text-sm text-slate-400 mt-3">No tienes acceso al detalle de esta solicitud.</p>;
  if (!d) return <p className="text-sm text-slate-400 mt-3">Cargando…</p>;
  const q = d.quotes?.[0];
  return (
    <div className="mt-3 text-sm space-y-2" data-testid="s-request-detail">
      <p className="bg-white/50 border border-white/70 rounded-xl p-3">{d.request.description}</p>
      <div className="flex justify-between"><span className="text-slate-500">Presupuesto</span><span>{q ? `${money(q.total)} · ${q.status === "aprobado" ? "aprobado" : "pendiente"}` : "Aún sin presupuesto"}</span></div>
      <div className="flex justify-between items-center"><span className="text-slate-500">Visita</span><VisitState visit={d.visit} /></div>
      {(d.messages ?? []).some((m: any) => m.status === "preparado_no_enviado") && <p className="text-xs text-amber-800">Hay un mensaje al cliente preparado pero no enviado (simulado).</p>}
    </div>);
}

export default function Requests({ me }: { me: Me }) {
  const [rq] = usePoll<any>("/requests", 4000);
  const [open, setOpen] = useState<string | null>(null);
  const list: any[] = rq?.requests ?? [];
  return (
    <div className="max-w-2xl mx-auto" data-testid="simple-requests">
      <h1 className="h-title">Solicitudes</h1>
      <p className="text-sm text-slate-500 mt-0.5 mb-6">Lo que han pedido tus clientes y en qué etapa va cada trabajo.</p>
      {rq && list.length === 0 && <div className="card p-6 text-sm text-slate-500">Aún no hay solicitudes.</div>}
      {list.map(r => (
        <div key={r.id} className="card p-4 mb-3" data-testid="s-request">
          <button className="w-full text-left" onClick={() => setOpen(open === r.id ? null : r.id)} aria-expanded={open === r.id}>
            <div className="flex justify-between gap-3"><span className="font-medium">{r.customer}</span><span className="text-xs text-slate-500">{requestStatus(r.status)}</span></div>
            <div className="text-sm text-slate-500 truncate mt-0.5">{r.description}</div>
          </button>
          <div className="mt-2"><Progress status={r.status} /></div>
          {open === r.id && <One id={r.id} />}
        </div>))}
      <div className="mt-4 text-right"><DetailLink to="/operations" /></div>
    </div>
  );
}
