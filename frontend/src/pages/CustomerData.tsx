import { useState } from "react";
import { api, Me } from "../api";
import { fmtDate } from "../ui";

/** Spec 003: control de los datos de un cliente (solo la dueña): quién los vio, exportarlos y anonimizarlos. */
export default function CustomerData({ me, customer, onChanged }: { me: Me; customer: { id: string; name: string }; onChanged: () => void }) {
  const [log, setLog] = useState<any[] | null>(null);
  const [msg, setMsg] = useState(""); const [confirming, setConfirming] = useState(false); const [typed, setTyped] = useState("");
  if (!me.permissions.includes("customers.access_log")) return null;
  const fail = (e: any) => setMsg("No se pudo: " + e.message);
  const showLog = async () => { setMsg(""); try { setLog((await api(`/customers/${customer.id}/access-log`)).accesses); } catch (e) { fail(e); } };
  const exportData = async () => {
    setMsg("");
    try {
      const d = await api(`/customers/${customer.id}/export`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: "application/json" }));
      const a = document.createElement("a"); a.href = url; a.download = `datos-cliente-${customer.id}.json`; a.click(); URL.revokeObjectURL(url);
      setMsg("Descargaste los datos de este cliente. La descarga quedó registrada.");
    } catch (e) { fail(e); }
  };
  const anonymize = async () => {
    setMsg("");
    try { await api(`/customers/${customer.id}/anonymize`, { body: { confirm_name: typed } }); setConfirming(false); setTyped(""); setMsg("Listo: los datos personales de este cliente quedaron anonimizados. Los montos y fechas se conservaron."); onChanged(); }
    catch (e) { fail(e); }
  };
  return (
    <div className="mt-4 rounded-2xl border border-white/70 bg-white/40 p-4" data-testid="customer-data">
      <div className="font-semibold text-sm">Datos de este cliente</div>
      <p className="text-xs text-slate-500 mt-0.5">Solo tú puedes ver quién consultó estos datos, descargarlos o anonimizarlos.</p>
      <div className="flex flex-wrap gap-2 mt-3">
        <button className="btn btn-ghost" data-testid="cd-log" onClick={showLog}>¿Quién vio estos datos?</button>
        <button className="btn btn-ghost" data-testid="cd-export" onClick={exportData}>Descargar datos</button>
        <button className="btn btn-danger" data-testid="cd-anonymize" onClick={() => { setConfirming(c => !c); setMsg(""); }}>Anonimizar datos</button>
      </div>
      {confirming && (
        <div className="mt-3 text-sm" data-testid="cd-confirm">
          <p className="text-slate-600">Se borrarán el nombre, el teléfono, el correo, la dirección y los mensajes de este cliente en todo el sistema. Se conservan montos, fechas y estados. <b>No se puede deshacer.</b></p>
          <label className="text-xs text-slate-500 block mt-2" htmlFor="cd-typed">Para confirmar, escribe el nombre exacto: {customer.name}</label>
          <div className="flex gap-2 mt-1"><input id="cd-typed" className="input" data-testid="cd-typed" value={typed} onChange={e => setTyped(e.target.value)} />
            <button className="btn btn-danger" data-testid="cd-confirm-go" onClick={anonymize}>Confirmar</button></div>
        </div>)}
      {msg && <div className="text-sm mt-3 text-slate-700" data-testid="cd-msg" role="status">{msg}</div>}
      {log && (
        <div className="mt-3 text-sm" data-testid="cd-log-list">
          {log.length === 0 ? <p className="text-slate-400">Nadie ha consultado este cliente todavía.</p> : log.map((a, i) => (
            <div key={i} className="flex justify-between border-t first:border-0 border-white/60 py-1.5"><span>{a.actor ?? "Persona eliminada"} <span className="text-xs text-slate-400">· {a.role === "owner" ? "dueño/a" : a.role === "admin" ? "administración" : a.role ?? ""}</span></span><span className="text-xs text-slate-400">{fmtDate(a.ts)}</span></div>))}
        </div>)}
    </div>
  );
}
