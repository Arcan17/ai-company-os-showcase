import { useState } from "react";
import { api, Me, ROLE_LABEL } from "../api";
import { Chip, Empty, PageHeader } from "../ui";
import { usePoll } from "./Dashboard";

const ROLES = [{ v: "admin", l: "Administración" }, { v: "technician", l: "Técnico/a" }];
const TPL_LABEL: Record<string, string> = { owner_assistant: "Asistente del dueño", admin_assistant: "Asistente administrativo", tech_assistant: "Asistente del técnico", reception: "Agente de recepción", coordinator: "Coordinador central" };

export default function Config({ me }: { me: Me }) {
  const [d, reload] = usePoll<any>("/config/overview", 4000);
  const [err, setErr] = useState(""); const [msg, setMsg] = useState("");
  const [f, setF] = useState({ name: "", email: "", role: "admin", password: "", assign: true });
  const [creds, setCreds] = useState<any>(null);
  const act = async (fn: () => Promise<any>, ok = "Hecho") => { setErr(""); setMsg(""); try { const r = await fn(); setMsg(ok); reload(); return r; } catch (e: any) { setErr(e.message); } };
  if (!d) return <div className="text-slate-400">Cargando…</div>;
  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const r = await act(() => api("/config/employees", { body: { name: f.name, email: f.email, role: f.role, password: f.password || null, assign_assistant: f.assign } }), "Empleado incorporado");
    if (r) { setCreds(r); setF({ name: "", email: "", role: f.role, password: "", assign: true }); }
  };
  return (
    <div>
      <PageHeader title="Empresa y equipo" sub={`Configura empleados y asistentes de ${d.company.name} sin tocar código. Solo el dueño puede hacerlo; todo queda auditado.`}
        right={<Chip tone="sky" title="No son agentes autónomos">Plantillas · roles lógicos</Chip>} />
      {err && <div className="card p-3 mb-3 border-rose-300 bg-rose-50 text-sm text-rose-800" role="alert" data-testid="cfg-error">{err}</div>}
      {msg && !err && <div className="card p-3 mb-3 border-emerald-200 bg-emerald-50 text-sm text-emerald-900" data-testid="cfg-msg">{msg}</div>}
      <div className="grid 2xl:grid-cols-[1fr_340px] gap-4">
        <div className="space-y-4 min-w-0">
          <div className="card p-4">
            <div className="font-semibold mb-1">Empresa</div>
            <div className="text-sm text-slate-600" data-testid="cfg-company"><b>{d.company.name}</b> · <code>{d.company.slug}</code> · {d.company.industry}</div>
          </div>
          <div className="card p-4">
            <div className="font-semibold mb-2">Empleados ({d.employees.length})</div>
            <div className="overflow-x-auto"><table className="w-full text-sm [&_td]:pr-3 [&_th]:pr-3 [&_[data-testid=emp-status]_.chip]:whitespace-normal [&_[data-testid=emp-status]_.chip]:rounded-xl" data-testid="cfg-employees"><thead className="text-left text-xs text-slate-400"><tr><th className="pb-2">Persona</th><th>Rol</th><th>Asistente personal</th><th>Tareas abiertas</th><th>Estado</th><th></th></tr></thead>
              <tbody>{d.employees.map((u: any) => (
                <tr key={u.id} className="border-t border-slate-100 align-top" data-testid="emp-row" data-name={u.name}>
                  <td className="py-2"><div className="font-medium">{u.name}</div><div className="text-xs text-slate-400">{u.email}</div></td>
                  <td><Chip tone="indigo">{ROLE_LABEL[u.role]}</Chip></td>
                  <td>{u.assistants.length === 0 ? (u.role !== "owner" || true) && <span className="text-xs text-amber-700">Sin asistente{u.active && <> · <button className="underline font-semibold" data-testid="emp-assign" onClick={() => act(() => api("/config/assignments", { body: { user_id: u.id, template: u.role === "admin" ? "admin_assistant" : u.role === "technician" ? "tech_assistant" : "owner_assistant" } }), "Asistente asignado")}>asignar</button></>}</span>
                    : u.assistants.map((a: any) => <div key={a.agent_id} className="flex flex-wrap items-center gap-x-1.5"><Chip tone={a.enabled ? "green" : "slate"}>{TPL_LABEL[a.template] ?? a.template}</Chip>
                      {!a.compatible ? <span className="text-[11px] text-slate-400" data-testid="emp-assistant-obsolete" title="Es la plantilla de su rol anterior: no se puede activar">plantilla de su rol anterior</span> : <button className="text-xs underline text-slate-600" data-testid="emp-assistant-toggle" onClick={() => act(() => api(`/config/agents/${a.agent_id}/enabled`, { body: { enabled: !a.enabled } }), a.enabled ? "Asistente desactivado" : "Asistente activado")}>{a.enabled ? "desactivar" : "activar"}</button>}</div>)}</td>
                  <td>{u.open_tasks}</td>
                  <td><span data-testid="emp-status">{u.active ? (u.eligible || u.role === "owner" ? <Chip tone="green">Activo{u.role !== "owner" ? " · puede recibir trabajo" : ""}</Chip> : <Chip tone="amber" title="No puede recibir trabajo del coordinador hasta tener un asistente activo">Activo · sin asistente activo</Chip>) : <Chip tone="red">Desactivado</Chip>}</span></td>
                  <td className="whitespace-nowrap text-xs">{u.role !== "owner" && <>
                    <button className="underline mr-2" data-testid="emp-active-toggle" onClick={() => act(() => api(`/config/employees/${u.id}/active`, { body: { active: !u.active } }), u.active ? "Empleado desactivado" : "Empleado activado")}>{u.active ? "Desactivar" : "Activar"}</button>
                    <select className="input !py-0.5 !text-xs !w-auto" value={u.role} aria-label={`Rol de ${u.name}`} data-testid="emp-role" onChange={e => act(() => api(`/config/employees/${u.id}/role`, { body: { role: e.target.value } }), "Rol actualizado")}>
                      {ROLES.map(r => <option key={r.v} value={r.v}>{r.l}</option>)}</select></>}</td>
                </tr>))}</tbody></table></div>
            <p className="text-[11px] text-slate-400 mt-2">Solo reciben trabajo del coordinador los empleados <b>activos</b> con un asistente personal <b>activo</b>. No se puede desactivar ni cambiar de rol a quien tiene tareas abiertas.</p>
          </div>
          <div className="card p-4">
            <div className="font-semibold mb-2">Agentes compartidos de la empresa</div>
            {d.shared_agents.map((a: any) => <div key={a.agent_id} className="flex items-center justify-between border-t first:border-0 border-slate-100 py-1.5 text-sm" data-testid="shared-agent"><span>{a.name}</span>
              <span className="flex items-center gap-2"><Chip tone={a.enabled ? "green" : "red"}>{a.enabled ? "Activo" : "Desactivado"}</Chip>
                <button className="text-xs underline" onClick={() => act(() => api(`/config/agents/${a.agent_id}/enabled`, { body: { enabled: !a.enabled } }))}>{a.enabled ? "desactivar" : "activar"}</button></span></div>)}
          </div>
          <div className="card p-4">
            <div className="font-semibold mb-1">Plantillas de agentes</div>
            <p className="text-xs text-slate-500 mb-3">Las plantillas están definidas en el código; aquí solo se <b>asignan</b> y se activan. Un asistente personal actúa con los permisos de la plantilla <b>∩</b> los de su empleado: configurarlo no otorga permisos nuevos. Las capacidades salen del registro de herramientas.</p>
            {d.templates.map((t: any) => (
              <details key={t.key} className="border-t first:border-0 border-slate-100 py-2" data-testid="tpl">
                <summary className="cursor-pointer text-sm flex items-center gap-2"><b>{t.name}</b><Chip tone={t.scope === "personal" ? "indigo" : "slate"}>{t.scope === "personal" ? `personal · ${ROLE_LABEL[t.role]}` : "compartido"}</Chip><span className="text-xs text-slate-400">{t.effective_permissions.length} permisos · {t.capabilities.length} herramientas</span></summary>
                <p className="text-xs text-slate-500 mt-1">{t.description} {t.note}</p>
                <div className="flex flex-wrap gap-1 mt-2">{t.capabilities.map((c: any) => <Chip key={c.name} tone={c.access === "write" ? "red" : "slate"}>{c.name}</Chip>)}</div>
              </details>))}
          </div>
        </div>
        <div className="space-y-4">
          <form onSubmit={add} className="card p-4 space-y-2" data-testid="add-employee-form">
            <div className="font-semibold">Incorporar empleado</div>
            <div><label className="label" htmlFor="e-name">Nombre</label><input id="e-name" className="input" data-testid="e-name" value={f.name} onChange={e => setF({ ...f, name: e.target.value })} /></div>
            <div><label className="label" htmlFor="e-email">Correo</label><input id="e-email" className="input" data-testid="e-email" value={f.email} onChange={e => setF({ ...f, email: e.target.value })} /></div>
            <div><label className="label" htmlFor="e-role">Rol</label><select id="e-role" className="input" data-testid="e-role" value={f.role} onChange={e => setF({ ...f, role: e.target.value })}>{ROLES.map(r => <option key={r.v} value={r.v}>{r.l}</option>)}</select></div>
            <div><label className="label" htmlFor="e-pass">Contraseña inicial (opcional)</label><input id="e-pass" type="password" className="input" data-testid="e-pass" placeholder="vacío = se genera una" value={f.password} onChange={e => setF({ ...f, password: e.target.value })} autoComplete="new-password" /></div>
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" data-testid="e-assign" checked={f.assign} onChange={e => setF({ ...f, assign: e.target.checked })} />Asignar su asistente personal ahora</label>
            <button className="btn btn-primary w-full" data-testid="e-submit" disabled={f.name.trim().length < 2 || !f.email.includes("@")}>Incorporar</button>
            <p className="text-[11px] text-slate-400">Empresa ficticia de entorno local: no uses datos ni correos reales.</p>
          </form>
          {creds && <div className="card p-4 border-amber-300 bg-amber-50 text-sm" data-testid="e-credentials"><div className="font-semibold text-amber-900">{creds.name} incorporado/a</div>
            <div className="text-xs text-amber-900 mt-1">Correo: <code>{creds.email}</code>{creds.password && <><br />Contraseña generada (solo se muestra ahora): <code data-testid="e-generated-password">{creds.password}</code></>}</div>
            <button className="text-xs underline mt-2" onClick={() => setCreds(null)}>Ocultar</button></div>}
          <div className="card p-4 text-xs text-slate-500">Alta solo en entorno <b>local/demo</b>. Usuarios reales, MFA, CSRF, TLS y PostgreSQL son una fase posterior. Cada cambio queda en la auditoría de la empresa.</div>
        </div>
      </div>
    </div>
  );
}
