import { usePoll } from "./pages/Dashboard";

// V0.11 (spec 006, US3) — resultados de la empresa. Cada cifra sale de registros que el sistema ya guarda (GET /api/results); nada se estima ni se rellena:
// sin datos suficientes dice «aún sin datos». Siempre imprime el periodo que abarca. Solo se monta para quien tiene el permiso `company.summary`.
const fmtTime = (h: number) => (h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : `${h.toLocaleString("es-CL", { maximumFractionDigits: 1 })} h`);

export default function ResultsCard() {
  const [r] = usePoll<any>("/results", 5000);
  if (!r) return null;
  const cell = (id: string, label: string, value: string, muted = false) => (
    <div className="flex-1 min-w-[7.5rem]">
      <div className={`text-2xl font-semibold tracking-tight ${muted ? "text-slate-400" : ""}`} data-testid={id}>{value}</div>
      <div className="text-[11px] uppercase tracking-wider text-slate-400 mt-0.5">{label}</div>
    </div>);
  return (
    <section className="card p-5 mb-4" data-testid="results-card" aria-label="Resultados de la empresa">
      <div className="text-sm font-semibold">Resultados · últimos {r.period_days} días</div>
      <div className="flex flex-wrap gap-x-6 gap-y-3 mt-3">
        {cell("results-closed", "Solicitudes atendidas", String(r.requests_closed))}
        {cell("results-decided", "Decisiones tomadas", String(r.approvals_decided))}
        {r.median_hours_to_close == null ? cell("results-time", "Tiempo por solicitud", "aún sin datos", true) : cell("results-time", "Tiempo por solicitud (mediana)", fmtTime(r.median_hours_to_close))}
      </div>
      <p className="text-[11px] text-slate-400 mt-3">Calculado con los registros reales de esta empresa; nada estimado.</p>
    </section>);
}
