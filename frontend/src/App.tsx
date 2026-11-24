import { useCallback, useEffect, useState } from "react";
import { api, Me, ROLE_LABEL } from "./api";
import { Chip } from "./ui";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Agents from "./pages/Agents";
import Flows from "./pages/Flows";
import Assistant from "./pages/Assistant";
import Operations from "./pages/Operations";
import Coordinator from "./pages/Coordinator";
import Config from "./pages/Config";
import NewCompany from "./pages/NewCompany";
import CustomerChat from "./pages/CustomerChat";
import ReauthDialog from "./ReauthDialog";

export function useHash() {
  const [h, setH] = useState(location.hash || "#/");
  useEffect(() => { const f = () => setH(location.hash || "#/"); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h.replace(/^#/, "");
}

const NAV = [
  { to: "/dashboard", label: "Dashboard", icon: "▦" },
  { to: "/agents", label: "Equipo de agentes", icon: "◎" },
  { to: "/flows", label: "Visualizador de flujos", icon: "⌁" },
  { to: "/assistant", label: "Mi asistente", icon: "✦" },
  { to: "/coordinator", label: "Coordinador (pedir trabajo)", icon: "⇉", perm: "plans.create" },
  { to: "/config", label: "Empresa y equipo", icon: "⚙", perm: "config.manage" },
  { to: "/operations", label: "Clientes y operaciones", icon: "☰" },
];

export default function App() {
  const route = useHash();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const refresh = useCallback(() => api<Me>("/auth/me").then(setMe).catch(() => setMe(null)), []);
  useEffect(() => { refresh(); }, [refresh]);

  if (route.startsWith("/cliente/")) return <CustomerChat slug={route.split("/")[2]} loggedIn={!!me} />;
  if (route.startsWith("/nueva-empresa") && !me) return <NewCompany onDone={() => { refresh(); location.hash = "#/config"; }} />;
  if (me === undefined) return <div className="p-10 text-slate-400">Cargando…</div>;
  if (!me) return <Login onLogin={() => { refresh(); location.hash = "#/dashboard"; }} />;

  const base = "/" + (route.split("/")[1] || "dashboard");
  const logout = async () => { await api("/auth/logout", { method: "POST", body: {} }); setMe(null); location.hash = "#/"; };
  const logoutAll = async () => { if (confirm("Se cerrará tu sesión en TODOS los navegadores y dispositivos. ¿Continuar?")) { await api("/auth/logout-all", { method: "POST", body: {} }); setMe(null); location.hash = "#/"; } };
  return (
    <div className="h-full flex flex-col md:flex-row">
      <ReauthDialog />
      <aside className="md:w-64 shrink-0 bg-white border-b md:border-b-0 md:border-r border-slate-200 flex md:flex-col">
        <div className="hidden md:block px-5 pt-5 pb-4">
          <div className="flex items-center gap-2.5">
            <div className="h-8 w-8 rounded-lg bg-brand-600 grid place-items-center text-white text-sm font-bold">AI</div>
            <div><div className="font-bold leading-tight">AI Company OS</div><div className="text-[11px] text-slate-400">Prototipo local · v0.5</div></div>
          </div>
          <div className="mt-4 rounded-xl bg-slate-50 border border-slate-200 px-3 py-2">
            <div className="text-[11px] uppercase tracking-wide text-slate-400">Empresa</div>
            <div className="font-semibold text-sm" data-testid="company-name">{me.company.name}</div>
          </div>
        </div>
        <nav className="flex md:flex-col gap-1 px-2 md:px-3 py-2 overflow-x-auto flex-1">
          {NAV.filter(n => !(n as any).perm || me.permissions.includes((n as any).perm)).map(n => (
            <a key={n.to} href={"#" + n.to} data-testid={"nav-" + n.to.slice(1)}
               className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium whitespace-nowrap ${base === n.to ? "bg-brand-50 text-brand-700" : "text-slate-600 hover:bg-slate-50"}`}>
              <span className="w-4 text-center">{n.icon}</span>{n.label}
            </a>
          ))}
          <a href={`#/cliente/${me.company.slug}`} className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50 whitespace-nowrap" data-testid="nav-customer-chat">
            <span className="w-4 text-center">✉</span>Chat de cliente (prueba)
          </a>
        </nav>
        <div className="hidden md:block p-3 border-t border-slate-100">
          <div className="text-sm font-semibold truncate" data-testid="user-name">{me.name}</div>
          <div className="flex items-center gap-1.5 mt-0.5"><Chip tone="indigo">{ROLE_LABEL[me.role]}</Chip>{me.is_demo ? <Chip tone="amber" title="Cuenta ficticia de demostración">demo</Chip> : null}</div>
          <button onClick={logout} className="btn btn-ghost w-full mt-3" data-testid="logout">Cerrar sesión</button>
          <button onClick={logoutAll} className="w-full mt-1 text-[11px] text-slate-400 hover:text-slate-600 underline" data-testid="logout-all">Cerrar sesión en todos lados</button>
        </div>
      </aside>
      <main className="flex-1 min-w-0 overflow-y-auto p-4 md:p-7 scroll-thin">
        {base === "/dashboard" && <Dashboard me={me} />}
        {base === "/agents" && <Agents me={me} />}
        {base === "/flows" && <Flows me={me} runId={route.split("/")[2]} />}
        {base === "/assistant" && <Assistant me={me} />}
        {base === "/operations" && <Operations me={me} reqId={route.split("/")[2]} />}
        {base === "/coordinator" && <Coordinator me={me} planId={route.split("/")[2]} />}
        {base === "/config" && me.permissions.includes("config.manage") && <Config me={me} />}
        <div className="md:hidden mt-6"><button onClick={logout} className="btn btn-ghost w-full">Cerrar sesión ({me.name})</button></div>
      </main>
    </div>
  );
}
