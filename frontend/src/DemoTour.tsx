import { useEffect, useState } from "react";
import { api } from "./api";

// V0.11 (spec 006, US2) — recorrido guiado de 4 pasos para la DEMO (pública o local con datos de ejemplo). Reglas de diseño:
//  · SOLO señala y explica: nunca ejecuta una acción por el visitante (aprobar, escribir, cambiar de modo lo hace la persona).
//  · Cada paso se da por cumplido LEYENDO EL ESTADO REAL (aprobaciones, ejecuciones, planes, pantalla abierta), nunca por un clic del propio recorrido:
//    si el visitante ya aprobó antes de empezar, ese paso ya aparece cumplido y el recorrido nunca queda esperando algo que ya ocurrió.
//  · Siempre se puede cerrar; el avance se guarda solo en el navegador (localStorage con try/catch).
type Info = { demo?: boolean; public?: boolean; example?: boolean; idle_minutes?: number };
const KEY = "aicos.tour", KEY3 = "aicos.tour.flows";
const get = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };
const put = (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* sin almacenamiento */ } };
export const TOUR_OPEN_EVENT = "aicos:tour-open";

let infoPromise: Promise<Info | null> | null = null;
const loadInfo = () => (infoPromise ??= Promise.all([
  fetch("/api/demo/info").then(r => (r.ok ? r.json() : {})).catch(() => ({})),
  fetch("/api/public-demo/info").then(r => (r.ok ? r.json() : null)).catch(() => null),
]).then(([a, p]) => ({ ...a, ...(p?.public_demo ? { public: true, idle_minutes: p.idle_minutes } : {}) })));
export function useDemoInfo(): Info | null {
  const [i, setI] = useState<Info | null>(null);
  useEffect(() => { loadInfo().then(setI); }, []);
  return i;
}
const hasTour = (i: Info | null) => !!(i && (i.public || i.example));

/** Botón «Ver cómo funciona»: solo existe donde hay recorrido (demo pública o local con ejemplo). Abre el recorrido; no hace nada más. */
export function TourButton() {
  const i = useDemoInfo();
  if (!hasTour(i)) return null;
  return <button type="button" className="btn btn-ghost" data-testid="tour-howto" onClick={() => window.dispatchEvent(new Event(TOUR_OPEN_EVENT))}>Ver cómo funciona</button>;
}

type Snap = { owner: boolean; pending: number; runs: number; blocked: boolean };
const NONE: Snap = { owner: false, pending: 0, runs: 0, blocked: false };
async function snap(): Promise<Snap> {
  try {
    const me = await api<any>("/auth/me");
    if (!me.permissions?.includes("approvals.decide")) return NONE;
    const [d, p] = await Promise.all([api<any>("/dashboard"), api<any>("/plans").catch(() => ({ plans: [] }))]);
    return { owner: true, pending: (d.pending_approvals ?? []).length, runs: (d.runs ?? []).length, blocked: (p.plans ?? []).some((x: any) => x.status === "blocked") };
  } catch { return NONE; }
}
const route = () => location.hash.replace(/^#/, "") || "/";
const isComplete = () => !!document.querySelector('[data-testid="mode-complete"][aria-pressed="true"], [data-testid="mode-m-complete"][aria-pressed="true"]');

const STEPS = [
  { id: "ver", t: "Mira la decisión que te toca", d: "Una solicitud de cliente llegó sola hasta aquí y se detuvo: el presupuesto espera TU decisión. Ningún agente puede aprobar gastos.", sel: '[data-testid="s-decision"]' },
  { id: "aprobar", t: "Decide tú", d: "Pulsa «Aprobar» (en el modo Completo, en el visualizador). Nada avanza ni se envía al cliente sin que una persona decida.", sel: '[data-testid="s-approve"], [data-testid="approve"]' },
  { id: "trazabilidad", t: "Mira qué quedó registrado", d: "En el modo Completo, el visualizador muestra cada paso con sus datos reales: qué herramienta se usó, con qué entradas y qué devolvió.", sel: '[data-testid="mode-complete"], [data-testid="mode-m-complete"]' },
  { id: "prohibido", t: "Pídele algo que no puede hacer", d: "En «Coordinador» escribe «Compra repuestos y avisa por WhatsApp a los clientes»: el plan sale BLOQUEADO, porque comprar y enviar mensajes no existen en su catálogo.", sel: '[data-testid="plan-input"]' },
];

export default function DemoTour() {
  const info = useDemoInfo();
  const [open, setOpen] = useState(get(KEY) !== "closed");
  const [s, setS] = useState<Snap>(NONE);
  const [r, setR] = useState(route());
  const [saw3, setSaw3] = useState(get(KEY3) === "1");
  const [manual, setManual] = useState<number | null>(null);
  useEffect(() => { const f = () => setR(route()); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  useEffect(() => { if (r.startsWith("/flows")) { setSaw3(true); put(KEY3, "1"); } }, [r]);
  useEffect(() => { const open = () => { setManual(null); setOpen(true); put(KEY, "open"); }; addEventListener(TOUR_OPEN_EVENT, open); return () => removeEventListener(TOUR_OPEN_EVENT, open); }, []);
  const active = hasTour(info) && open;
  useEffect(() => { if (!active) return; let live = true; const tick = () => snap().then(x => live && setS(x)); tick(); const t = setInterval(tick, 2500); return () => { live = false; clearInterval(t); }; }, [active]);

  const done2 = s.owner && s.pending === 0 && s.runs > 0;
  const done = [done2 || (s.owner && s.pending > 0 && (r === "/s/home" || r === "/dashboard")), done2, saw3, s.blocked];
  const auto = done.findIndex(x => !x);                       // el primer paso aún sin cumplir (−1 = todos cumplidos)
  useEffect(() => { setManual(null); }, [auto]);              // cuando el estado avanza, el recorrido avanza solo
  const idx = manual ?? (auto === -1 ? STEPS.length - 1 : auto);
  const st = STEPS[idx];

  useEffect(() => {                                           // resalta el elemento señalado (solo un borde: no actúa sobre él)
    if (!active || !s.owner) return;
    const mark = () => { document.querySelectorAll(".tour-hl").forEach(e => e.classList.remove("tour-hl")); if (!done[idx]) document.querySelector(st.sel)?.classList.add("tour-hl"); };
    mark(); const t = setInterval(mark, 700);
    return () => { clearInterval(t); document.querySelectorAll(".tour-hl").forEach(e => e.classList.remove("tour-hl")); };
  }, [active, s.owner, idx, done[idx]]);

  if (!hasTour(info)) return null;
  const close = () => { setOpen(false); put(KEY, "closed"); };
  const goto = () => { location.hash = idx <= 1 ? (isComplete() ? "#/dashboard" : "#/s/home") : idx === 2 ? "#/flows" : "#/coordinator"; };
  const needsComplete = idx >= 2 && !isComplete();
  const allDone = auto === -1;
  return (
    <>
      {info?.public && (
        <div className="shrink-0 bg-amber-100 border-b border-amber-300 text-amber-950 text-[12px] leading-tight px-3 py-1.5 text-center" data-testid="public-demo-banner">
          <b>DEMO PÚBLICA</b> · datos 100 % ficticios · tu espacio es solo tuyo y se borra tras {info.idle_minutes} min sin uso · WhatsApp, correo y pagos: <b>simulados (no existen)</b>
        </div>)}
      {open ? (
        <aside className="glass z-40 bottom-4 right-4 w-[min(22rem,calc(100vw-2rem))] rounded-3xl p-5" style={{ position: "fixed" }} data-testid="tour" aria-label="Recorrido guiado">   {/* inline: `.glass{position:relative}` (index.css) anula la utilidad `fixed` */}
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">{s.owner ? `Recorrido · paso ${idx + 1} de ${STEPS.length}` : "Recorrido · antes de empezar"}</span>
            <button className="text-xs text-slate-400 hover:text-slate-600" data-testid="tour-close" onClick={close} aria-label="Cerrar el recorrido">Cerrar</button>
          </div>
          {s.owner ? (<>
            <div className="flex gap-1 mt-2" aria-hidden="true">{STEPS.map((x, i) => <span key={x.id} data-testid={done[i] ? "tour-step-done" : undefined} className={`h-1.5 flex-1 rounded-full ${done[i] ? "bg-emerald-400" : i === idx ? "bg-brand-500" : "bg-slate-200"}`} />)}</div>
            <div className="font-semibold mt-2" data-testid="tour-title">{allDone && manual === null ? "¡Listo! Ya viste lo esencial" : st.t}</div>
            <p className="text-sm text-slate-600 mt-1 leading-snug">{allDone && manual === null ? "Decidiste tú, quedó el registro y lo prohibido se bloqueó. Explora el resto con libertad." : st.d}</p>
            {done[idx] && !(allDone && manual === null) && <p className="text-xs text-emerald-700 mt-1">✓ Este paso ya está cumplido.</p>}
            {needsComplete && !done[idx] && <p className="text-xs text-amber-700 mt-1">Este paso es del modo Completo: pulsa «Completo» en la barra lateral.</p>}
          </>) : (<>
            <div className="font-semibold mt-1.5" data-testid="tour-title">Entra como la dueña</div>
            <p className="text-sm text-slate-600 mt-1 leading-snug">En la pantalla de entrada pulsa «Carolina Vega» (cuentas ficticias; la contraseña ya está escrita) y luego «Entrar». El recorrido sigue desde ahí.</p>
          </>)}
          <div className="flex justify-between gap-2 mt-3">
            <button className="btn btn-ghost" data-testid="tour-prev" disabled={!s.owner || idx === 0} onClick={() => setManual(idx - 1)}>Anterior</button>
            {s.owner && !needsComplete && !done[idx] && idx !== 0 && idx !== 1 && <button className="btn btn-ghost" data-testid="tour-go" onClick={goto}>Llévame</button>}
            <button className="btn btn-primary" data-testid="tour-next" disabled={!s.owner} onClick={() => (idx === STEPS.length - 1 ? close() : setManual(idx + 1))}>{idx === STEPS.length - 1 ? "Terminar" : "Siguiente"}</button>
          </div>
        </aside>
      ) : (
        <button className="btn btn-ghost z-40 bottom-4 right-4" style={{ position: "fixed" }} data-testid="tour-open" onClick={() => { setManual(null); setOpen(true); put(KEY, "open"); }}>Ver recorrido</button>
      )}
    </>
  );
}
