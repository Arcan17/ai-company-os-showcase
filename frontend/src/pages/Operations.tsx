import { useEffect, useState } from "react";
import { api, Me, money } from "../api";
import { Chip, Empty, fmtDate, PageHeader, StatusChip } from "../ui";
import { usePoll } from "./Dashboard";
import CustomerData from "./CustomerData";

function Detail({ id, onClose, me }: { id: string; onClose: () => void; me: Me }) {
  const [d, setD] = useState<any>(null);
  const load = () => api(`/requests/${id}`).then(setD);
  useEffect(() => { load(); }, [id]);
  if (!d) return null;
  const r = d.request;
  return (
    <div className="card p-5 mt-4" data-testid="request-detail">
      <div className="flex items-start justify-between"><div><div className="text-xs font-mono text-slate-400">{r.id}</div><div className="font-semibold text-lg">{d.customer.name}</div>
        <div className="text-sm text-slate-500">{d.customer.phone} · {d.customer.email}</div></div><div className="flex gap-2 items-center"><StatusChip s={r.status} /><button className="btn btn-ghost" onClick={onClose}>Cerrar</button></div></div>
      <p className="mt-3 text-sm bg-slate-50 border border-slate-200 rounded-lg p-3">{r.description}</p>
      <div className="grid md:grid-cols-2 gap-4 mt-4 text-sm">
        <div><div className="font-semibold mb-1">Presupuesto</div>{d.quotes.length === 0 ? <Empty>Sin presupuesto</Empty> : d.quotes.map((q: any) => (
          <div key={q.kind} className="mb-2 rounded-lg border border-slate-200 p-2.5"><div className="flex justify-between"><Chip tone="indigo">{q.kind}</Chip><Chip tone={q.status === "aprobado" ? "green" : q.status === "rechazado" ? "red" : "amber"}>{q.status}</Chip></div>
            {q.lines.map((l: any, i: number) => <div key={i} className="flex justify-between text-xs mt-1"><span>{l.name}</span><span>{money(l.unit * l.qty)}</span></div>)}
            <div className="flex justify-between font-semibold mt-1 border-t border-slate-100 pt-1"><span>Total</span><span>{money(q.total)}</span></div></div>))}</div>
        <div><div className="font-semibold mb-1">Visita</div>{d.visit ? (
          <div className="rounded-lg border border-slate-200 p-2.5"><div>{fmtDate(d.visit.slot_start)}</div><div className="mt-1" data-testid="visit-status">{d.visit.customer_confirmed ? <Chip tone="green">Confirmada por el cliente</Chip> : <Chip tone="amber">Propuesta · NO confirmada por el cliente</Chip>}</div></div>) : <Empty>Sin visita</Empty>}
          <div className="font-semibold mt-3 mb-1">Orden de trabajo</div>{d.work_order ? <Chip>{d.work_order.status}</Chip> : <Empty>Sin orden</Empty>}</div>
        <div><div className="font-semibold mb-1">Historial del cliente</div>{d.history.length === 0 ? <Empty>Sin solicitudes previas</Empty> : d.history.map((h: any) => <div key={h.id} className="text-xs border-t first:border-0 border-slate-100 py-1.5">{h.description} <span className="text-slate-400">· {h.status}</span></div>)}</div>
        <div><div className="font-semibold mb-1">Tareas y resultados</div>{d.tasks.map((t: any, i: number) => <div key={i} className="text-xs py-1">☐ {t.title} <Chip>{t.status}</Chip></div>)}{d.results.map((x: any, i: number) => <div key={i} className="text-xs py-1 text-emerald-700">✔ {x.summary}</div>)}</div>
      </div>
      <CustomerData me={me} customer={d.customer} onChanged={load} />
      <div className="mt-4"><div className="font-semibold text-sm mb-1">Mensajes</div>{d.messages.map((m: any, i: number) => (
        <div key={i} className="text-xs border-t first:border-0 border-slate-100 py-1.5"><b>{m.sender}</b> {m.status === "preparado_no_enviado" && <Chip tone="amber">preparado · no enviado (simulado)</Chip>}<div className="text-slate-600">{m.body}</div></div>))}</div>
      <div className="mt-3">{d.runs.map((u: any) => <a key={u.id} className="btn btn-ghost mr-2" href={`#/flows/${u.id}`}>Ver ejecución en el visualizador →</a>)}</div>
    </div>
  );
}

function ManualRequest({ onCreated }: { onCreated: (id: string) => void }) {
  const [f, setF] = useState({ customer_name: "", phone: "", description: "" }); const [msg, setMsg] = useState(""); const [err, setErr] = useState(""); const [open, setOpen] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setErr(""); setMsg("");
    try { const r = await api("/requests/manual", { body: { customer_name: f.customer_name, phone: f.phone || null, description: f.description, client_request_id: "web-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36) } });
      setMsg(`Solicitud ${r.request_id} registrada y clasificada`); setF({ customer_name: "", phone: "", description: "" }); onCreated(r.request_id); } catch (x: any) { setErr(x.message); }
  };
  return (
    <div className="card p-4 mb-4">
      <button className="font-semibold text-left w-full flex items-center justify-between" data-testid="mr-toggle" onClick={() => setOpen(o => !o)}><span>Nueva solicitud (registro manual)</span><span className="text-xs text-slate-400">{open ? "ocultar" : "mostrar"}</span></button>
      {open && <form onSubmit={submit} className="mt-3 grid md:grid-cols-[1fr_1fr_2fr_auto] gap-2 items-end" data-testid="mr-form">
        <div><label className="label" htmlFor="mr-name">Cliente</label><input id="mr-name" className="input" data-testid="mr-name" value={f.customer_name} onChange={e => setF({ ...f, customer_name: e.target.value })} /></div>
        <div><label className="label" htmlFor="mr-phone">Teléfono (opcional)</label><input id="mr-phone" className="input" data-testid="mr-phone" value={f.phone} onChange={e => setF({ ...f, phone: e.target.value })} /></div>
        <div><label className="label" htmlFor="mr-desc">Qué necesita</label><input id="mr-desc" className="input" data-testid="mr-desc" value={f.description} onChange={e => setF({ ...f, description: e.target.value })} /></div>
        <button className="btn btn-primary" data-testid="mr-submit" disabled={f.customer_name.trim().length < 2 || f.description.trim().length < 5}>Registrar</button>
      </form>}
      {msg && <div className="text-sm text-emerald-700 mt-2" data-testid="mr-result">{msg}</div>}{err && <div className="text-sm text-rose-600 mt-2" role="alert">{err}</div>}
      <p className="text-[11px] text-slate-400 mt-2">Útil en una empresa recién configurada. Es una solicitud interna FICTICIA: no se envía nada al cliente.</p>
    </div>
  );
}

const HISTORIC = ["cerrada", "ejecucion_fallida", "presupuesto_rechazado"];

export default function Operations({ me, reqId }: { me: Me; reqId?: string }) {
  const can = me.permissions.includes("requests.read");
  const [reqs] = usePoll<any>(can ? "/requests" : "/work-orders", 3500);
  const [cust] = usePoll<any>(me.permissions.includes("customers.read") ? "/customers" : "/work-orders", 20000);
  const [sel, setSel] = useState<string | null>(reqId ?? null);
  const [flt, setFlt] = useState<"active" | "closed" | "all">("active");
  useEffect(() => { if (reqId) setSel(reqId); }, [reqId]);
  const all: any[] = reqs?.requests ?? [];
  const nClosed = all.filter(r => HISTORIC.includes(r.status)).length;
  const shown = all.filter(r => flt === "all" || (flt === "closed") === HISTORIC.includes(r.status));
  return (
    <div>
      <PageHeader title="Clientes y operaciones" sub="Solicitudes, historial de interacciones, estado de trabajos y resultados." />
      {!can && <div className="card p-4 mb-4 text-sm text-slate-600">Tu rol (técnico) solo ve sus órdenes de trabajo asignadas; el detalle de clientes y solicitudes requiere permisos de administración.</div>}
      {me.permissions.includes("requests.create_manual") && <ManualRequest onCreated={(id) => setSel(id)} />}
      <div className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3"><div className="font-semibold">{can ? "Solicitudes" : "Mis órdenes de trabajo"}</div>
          {can && <div className="flex gap-1.5" role="tablist">{([["active", `Activas (${all.length - nClosed})`], ["closed", `Históricas: cerradas, rechazadas o fallidas (${nClosed})`], ["all", "Todas"]] as const).map(([k, l]) => (
            <button key={k} role="tab" aria-selected={flt === k} data-testid={"req-filter-" + k} onClick={() => setFlt(k)} className={`chip cursor-pointer ${flt === k ? "bg-brand-50 border-brand-600 text-brand-700" : "bg-white border-slate-200 text-slate-600"}`}>{l}</button>))}</div>}</div>
        <div className="overflow-x-auto"><table className="w-full text-sm" data-testid="requests-table"><thead className="text-left text-xs text-slate-400"><tr><th className="pb-2">ID</th><th>Cliente</th><th>Descripción</th><th>Estado</th></tr></thead><tbody>
          {can ? shown.map((r: any) => (
            <tr key={r.id} onClick={() => setSel(r.id)} className="border-t border-slate-100 hover:bg-slate-50 cursor-pointer" data-testid="request-row"><td className="py-2 font-mono text-xs">{r.id}</td><td>{r.customer}</td><td className="max-w-xs truncate">{r.description}</td><td><StatusChip s={r.status} /></td></tr>))
            : (reqs?.work_orders ?? []).map((w: any) => <tr key={w.request_id} className="border-t border-slate-100"><td className="py-2 font-mono text-xs">{w.request_id}</td><td>{w.customer}</td><td>{w.description}</td><td><Chip>{w.status}</Chip></td></tr>)}
        </tbody></table></div>
      </div>
      {sel && can && <Detail id={sel} me={me} onClose={() => setSel(null)} />}
      {me.permissions.includes("customers.read") && (
        <div className="card p-4 mt-4"><div className="font-semibold mb-3">Clientes</div>
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-2">{(cust?.customers ?? []).map((c: any) => (
            <div key={c.id} className="rounded-lg border border-slate-200 p-3 text-sm"><div className="font-semibold">{c.name}</div><div className="text-xs text-slate-500">{c.phone}</div><div className="text-xs text-slate-400">{c.email}</div></div>))}</div></div>)}
    </div>
  );
}
