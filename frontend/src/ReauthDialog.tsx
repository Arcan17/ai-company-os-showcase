import { FormEvent, useEffect, useRef, useState } from "react";
import { api, setReauthHandler } from "./api";

// V0.7 P2 — «autenticación reciente»: cuando el servidor pide confirmar la identidad (401 reauth_required) se abre este cuadro, se confirma la contraseña de LA SESIÓN (POST /auth/reauth) y la
// operación se reintenta una vez. Varias peticiones que lo necesiten a la vez comparten UN solo cuadro. Con un proveedor de identidad la confirmación irá por el proveedor (etapa P3).
export default function ReauthDialog() {
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState(""); const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const waiters = useRef<Array<(ok: boolean) => void>>([]);
  useEffect(() => {
    setReauthHandler(() => new Promise<boolean>(res => { waiters.current.push(res); setOpen(true); }));
    return () => setReauthHandler(null);
  }, []);
  const close = (ok: boolean) => { setOpen(false); setPw(""); setErr(""); waiters.current.splice(0).forEach(w => w(ok)); };
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr("");
    try { await api("/auth/reauth", { body: { password: pw } }); close(true); }
    catch (x: any) { setErr(x.message || "No se pudo confirmar tu identidad"); }
    finally { setBusy(false); }
  };
  if (!open) return null;
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="reauth-title" data-testid="reauth-dialog" className="fixed inset-0 z-50 grid place-items-center bg-slate-900/40 p-4">
      <form onSubmit={submit} className="card p-5 w-full max-w-sm space-y-3">
        <h2 id="reauth-title" className="font-bold">Confirma tu identidad</h2>
        <p className="text-sm text-slate-600">Esta operación es delicada. Por seguridad, vuelve a escribir tu contraseña para continuar.</p>
        <div><label className="label" htmlFor="reauth-password">Contraseña</label>
          <input id="reauth-password" data-testid="reauth-password" type="password" className="input" value={pw} onChange={e => setPw(e.target.value)} autoComplete="current-password" autoFocus /></div>
        {err && <div className="text-sm text-rose-700" role="alert" data-testid="reauth-error">{err}</div>}
        <div className="flex gap-2 justify-end">
          <button type="button" className="btn btn-ghost" data-testid="reauth-cancel" onClick={() => close(false)}>Cancelar</button>
          <button className="btn btn-primary" data-testid="reauth-submit" disabled={busy || !pw}>Confirmar</button>
        </div>
      </form>
    </div>
  );
}
