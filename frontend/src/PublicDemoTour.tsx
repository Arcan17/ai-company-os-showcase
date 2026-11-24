import { useEffect, useState } from "react";

const STEPS = [
  { t: "Entra como la dueña", d: "En la pantalla de entrada pulsa «Carolina Vega» (cuentas ficticias; la contraseña ya está escrita) y luego «Entrar»." },
  { t: "Haz que llegue una solicitud", d: "Abre «Ayuda» → «Probar como cliente», elige el escenario A y pulsa «Enviar mensaje»." },
  { t: "Decide tú", d: "Vuelve a «Inicio»: la solicitud espera tu aprobación. Nada avanza sin ella. Pulsa «Aprobar»." },
  { t: "Mira qué quedó", d: "En «Solicitudes» la visita aparece como PROPUESTA, no confirmada. Solo el cliente la confirma, con su código." },
  { t: "Cambia al modo Completo", d: "Con el interruptor «Completo» ves el visualizador (cada paso, con sus eventos) y, en «Clientes y operaciones», los controles de datos del cliente." },
];
const KEY = "aicos.tour";
const get = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
const put = (v: string) => { try { localStorage.setItem(KEY, v); } catch { /* sin almacenamiento */ } };

/** Solo existe en la DEMO PÚBLICA (/api/public-demo/info → public_demo:true): aviso honesto + recorrido de 5 pasos que se puede cerrar. */
export default function PublicDemoTour() {
  const [info, setInfo] = useState<any>(null);
  const [step, setStep] = useState(0); const [open, setOpen] = useState(get() !== "closed");
  useEffect(() => { fetch("/api/public-demo/info").then(r => (r.ok ? r.json() : null)).then(setInfo).catch(() => {}); }, []);
  if (!info?.public_demo) return null;
  const s = STEPS[step];
  return (
    <>
      <div className="shrink-0 bg-amber-100 border-b border-amber-300 text-amber-950 text-[12px] leading-tight px-3 py-1.5 text-center" data-testid="public-demo-banner">
        <b>DEMO PÚBLICA</b> · datos 100 % ficticios · tu espacio es solo tuyo y se borra tras {info.idle_minutes} min sin uso · WhatsApp, correo y pagos: <b>simulados (no existen)</b>
      </div>
      {open ? (
        <aside className="glass fixed z-40 bottom-4 right-4 w-[min(22rem,calc(100vw-2rem))] rounded-3xl p-5" data-testid="tour" aria-label="Recorrido guiado">
          <div className="flex items-center justify-between"><span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Recorrido · paso {step + 1} de {STEPS.length}</span>
            <button className="text-xs text-slate-400 hover:text-slate-600" data-testid="tour-close" onClick={() => { setOpen(false); put("closed"); }} aria-label="Cerrar el recorrido">Cerrar</button></div>
          <div className="font-semibold mt-1.5" data-testid="tour-title">{s.t}</div>
          <p className="text-sm text-slate-600 mt-1 leading-snug">{s.d}</p>
          <div className="flex justify-between mt-3">
            <button className="btn btn-ghost" data-testid="tour-prev" disabled={step === 0} onClick={() => setStep(step - 1)}>Anterior</button>
            <button className="btn btn-primary" data-testid="tour-next" onClick={() => (step === STEPS.length - 1 ? (setOpen(false), put("closed")) : setStep(step + 1))}>{step === STEPS.length - 1 ? "Terminar" : "Siguiente"}</button>
          </div>
        </aside>
      ) : (
        <button className="btn btn-ghost fixed z-40 bottom-4 right-4" data-testid="tour-open" onClick={() => { setStep(0); setOpen(true); put("open"); }}>Ver recorrido</button>
      )}
    </>
  );
}
