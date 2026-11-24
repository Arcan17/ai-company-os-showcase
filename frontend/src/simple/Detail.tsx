import { useViewMode } from "../viewMode";

/** Spec 002 FR-010: salida desde la vista simple hacia la pantalla completa equivalente. */
export function DetailLink({ to, label = "Ver detalle técnico" }: { to: string; label?: string }) {
  const { setMode } = useViewMode();
  return <a href={"#" + to} data-testid="see-detail" onClick={() => setMode("complete")} className="text-xs text-slate-400 hover:text-slate-600 underline">{label}</a>;
}

/** Estado de una visita sin ambigüedad: propuesta ≠ confirmada (FR-009). */
export function VisitState({ visit }: { visit: any | null | undefined }) {
  if (!visit) return <span className="text-sm text-slate-400">Aún sin visita propuesta</span>;
  const when = new Date(visit.slot_start).toLocaleString("es-CL", { dateStyle: "medium", timeStyle: "short" });
  return visit.customer_confirmed
    ? <span className="chip bg-emerald-50 text-emerald-700 border-emerald-200" data-testid="s-visit-state">Confirmada por el cliente · {when}</span>
    : <span className="chip bg-amber-50 text-amber-800 border-amber-200" data-testid="s-visit-state">Propuesta, aún sin confirmar por el cliente · {when}</span>;
}
