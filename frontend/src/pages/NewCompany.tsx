import { useEffect, useState } from "react";
import { api } from "../api";
import { Chip } from "../ui";

// Local/demo only (the server answers 404 unless AICOS_ALLOW_COMPANY_CREATION=1 and the request comes from this machine). Not a public sign-up.
export default function NewCompany({ onDone }: { onDone: () => void }) {
  const [info, setInfo] = useState<any>(null); const [err, setErr] = useState(""); const [res, setRes] = useState<any>(null); const [busy, setBusy] = useState(false);
  const [f, setF] = useState({ name: "", preset: "hvac", owner_name: "", owner_email: "", owner_password: "" });
  useEffect(() => { api("/setup/info").then(setInfo).catch(() => setInfo({ company_creation: false })); }, []);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setErr(""); setBusy(true);
    try { setRes(await api("/setup/companies", { body: { name: f.name, preset: f.preset, owner_name: f.owner_name, owner_email: f.owner_email, owner_password: f.owner_password || null } })); }
    catch (x: any) { setErr(x.message); } finally { setBusy(false); }
  };
  const enter = async () => { try { await api("/auth/login", { body: { email: res.owner_email, password: res.owner_password ?? f.owner_password } }); onDone(); } catch (x: any) { setErr(x.message); } };
  if (!info) return <div className="p-10 text-slate-400">Cargando…</div>;
  return (
    <div className="min-h-full flex items-center justify-center p-6">
      <div className="w-full max-w-lg">
        <a href="#/" className="text-xs text-brand-700 font-semibold">← Volver al inicio de sesión</a>
        <h1 className="h-title mt-2">Crear una empresa ficticia</h1>
        <p className="text-sm text-slate-500 mt-1 mb-4">Entorno <b>local de pruebas</b>: crea una empresa con su dueño, sin tocar código. Luego, como dueño/a, añade empleados y asistentes en «Empresa y equipo».</p>
        {!info.company_creation ? <div className="card p-4 text-sm" data-testid="creation-off"><Chip tone="amber">Desactivado</Chip> La creación de empresas está apagada en este servidor (es una acción local; se activa con <code>AICOS_ALLOW_COMPANY_CREATION=1</code> o con <code>python -m app.company_admin</code>).</div> : res ? (
          <div className="card p-4 space-y-2" data-testid="company-created"><Chip tone="green">Empresa creada</Chip>
            <div className="text-sm"><b>{res.name}</b> · <code>{res.slug}</code></div>
            <div className="text-sm">Dueño/a: <code>{res.owner_email}</code>{res.owner_password && <><br />Contraseña generada (solo se muestra ahora): <code data-testid="owner-generated-password">{res.owner_password}</code></>}</div>
            <p className="text-xs text-slate-500">{res.note}</p>
            {err && <div className="text-sm text-rose-600" role="alert">{err}</div>}
            <button className="btn btn-primary" data-testid="enter-company" onClick={enter}>Entrar como dueño/a</button></div>
        ) : (
          <form onSubmit={submit} className="card p-4 space-y-3" data-testid="new-company-form">
            <div><label className="label" htmlFor="c-name">Nombre de la empresa (ficticia)</label><input id="c-name" className="input" data-testid="c-name" value={f.name} onChange={e => setF({ ...f, name: e.target.value })} /></div>
            <div><label className="label" htmlFor="c-preset">Plantilla de industria</label><select id="c-preset" className="input" data-testid="c-preset" value={f.preset} onChange={e => setF({ ...f, preset: e.target.value })}>{info.presets.map((p: any) => <option key={p.key} value={p.key}>{p.label}</option>)}</select>
              <p className="text-[11px] text-slate-400 mt-1">Carga un catálogo inicial de servicios y repuestos ficticios.</p></div>
            <div><label className="label" htmlFor="c-oname">Dueño/a: nombre</label><input id="c-oname" className="input" data-testid="c-owner-name" value={f.owner_name} onChange={e => setF({ ...f, owner_name: e.target.value })} /></div>
            <div><label className="label" htmlFor="c-oemail">Dueño/a: correo</label><input id="c-oemail" className="input" data-testid="c-owner-email" value={f.owner_email} onChange={e => setF({ ...f, owner_email: e.target.value })} /></div>
            <div><label className="label" htmlFor="c-opass">Contraseña (opcional, mín. 8)</label><input id="c-opass" type="password" className="input" data-testid="c-owner-pass" placeholder="vacío = se genera una" value={f.owner_password} onChange={e => setF({ ...f, owner_password: e.target.value })} autoComplete="new-password" /></div>
            {err && <div className="text-sm text-rose-600" role="alert" data-testid="company-error">{err}</div>}
            <button className="btn btn-primary w-full" disabled={busy || f.name.trim().length < 3 || f.owner_name.trim().length < 2 || !f.owner_email.includes("@")} data-testid="c-submit">Crear empresa</button>
            <p className="text-[11px] text-slate-400">No es un registro público: no uses datos reales. Los canales externos siguen simulados.</p>
          </form>)}
      </div>
    </div>
  );
}
