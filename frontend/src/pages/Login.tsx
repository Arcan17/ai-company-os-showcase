import { useEffect, useState } from "react";
import { api, ROLE_LABEL } from "../api";
import { Chip } from "../ui";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [err, setErr] = useState("");
  const [demo, setDemo] = useState<any>(null); const [setup, setSetup] = useState<any>(null);
  useEffect(() => { api("/auth/demo-accounts").then(setDemo).catch(() => {}); api("/setup/info").then(setSetup).catch(() => {}); }, []);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setErr("");
    try { await api("/auth/login", { body: { email, password } }); onLogin(); } catch (x: any) { setErr(x.message); }
  };
  return (
    <div className="min-h-full grid lg:grid-cols-[1.05fr_1fr] relative overflow-hidden">
      <div className="hidden lg:flex flex-col justify-between p-14">
        <div className="flex items-center gap-2.5"><div className="h-9 w-9 rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 grid place-items-center font-bold text-white shadow-lg shadow-indigo-500/30">AI</div><span className="font-semibold tracking-tight text-lg">AI Company OS</span></div>
        <div>
          <h2 className="text-5xl font-semibold leading-[1.08] tracking-tight text-slate-900">Un equipo de agentes de IA<br /><span className="text-brand-600">que trabaja a la vista.</span></h2>
          <p className="mt-5 text-slate-500 max-w-md leading-relaxed">Permisos reales, aprobación humana y trazabilidad completa de cada paso.</p>
        </div>
        <p className="text-xs text-slate-400">Prototipo local. Datos ficticios. Ninguna integración externa conectada.</p>
      </div>
      <div className="flex items-center justify-center p-6">
        <div className="glass w-full max-w-md rounded-[28px] p-8">
          <h1 className="h-title">Iniciar sesión</h1>
          <p className="text-sm text-slate-500 mt-1 mb-6">Autenticación con contraseña y sesión en el servidor.</p>
          <form onSubmit={submit} className="space-y-4">
            <div><label className="label" htmlFor="email">Correo</label><input id="email" data-testid="email" className="input" value={email} onChange={e => setEmail(e.target.value)} autoComplete="username" /></div>
            <div><label className="label" htmlFor="password">Contraseña</label><input id="password" data-testid="password" type="password" className="input" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></div>
            {err && <div className="text-sm text-rose-600" role="alert">{err}</div>}
            <button className="btn btn-primary w-full" data-testid="login-submit">Entrar</button>
          </form>
          {setup?.company_creation && <a href="#/nueva-empresa" className="block text-center text-sm font-semibold text-brand-700 mt-3" data-testid="new-company-link">Crear una empresa ficticia (entorno local) →</a>}
          {demo && (
            <div className="mt-6 rounded-2xl bg-white/45 border border-white/70 p-4" data-testid="demo-accounts">
              <div className="flex items-center gap-2 mb-1"><Chip tone="amber">Cuentas de DEMOSTRACIÓN</Chip></div>
              <p className="text-xs text-slate-500 mb-3">{demo.notice} Contraseña de todas: <code className="bg-slate-100 px-1 rounded">{demo.password}</code></p>
              <div className="space-y-1.5 max-h-64 overflow-auto scroll-thin">
                {demo.accounts.map((a: any) => (
                  <button key={a.email} type="button" onClick={() => { setEmail(a.email); setPassword(demo.password); }} data-testid={"demo-" + a.email}
                          className="w-full flex items-center justify-between text-left rounded-xl border border-white/70 bg-white/50 hover:bg-white/90 hover:border-brand-500 px-3 py-2 transition">
                    <span><span className="block text-sm font-semibold">{a.name}</span><span className="text-xs text-slate-500">{a.email}</span></span>
                    <span className="text-right"><Chip tone="indigo">{ROLE_LABEL[a.role]}</Chip><span className="block text-[11px] text-slate-400 mt-0.5">{a.company}</span></span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
