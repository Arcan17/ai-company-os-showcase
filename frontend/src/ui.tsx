import { ReactNode } from "react";
import { STATUS_LABEL } from "./api";

const TONES: Record<string, string> = {
  slate: "bg-slate-100 text-slate-700 border-slate-200", indigo: "bg-indigo-50 text-indigo-700 border-indigo-200",
  green: "bg-emerald-50 text-emerald-700 border-emerald-200", amber: "bg-amber-50 text-amber-800 border-amber-200",
  red: "bg-rose-50 text-rose-700 border-rose-200", violet: "bg-violet-50 text-violet-700 border-violet-200", sky: "bg-sky-50 text-sky-700 border-sky-200",
};
export const Chip = ({ tone = "slate", children, title }: { tone?: keyof typeof TONES; children: ReactNode; title?: string }) =>
  <span className={`chip ${TONES[tone]}`} title={title}>{children}</span>;

const STATUS_TONE: Record<string, keyof typeof TONES> = {
  nueva: "slate", clasificada: "sky", presupuesto_en_revision: "amber", bloqueada_repuesto: "red", propuesta_preparada: "indigo", visita_confirmada: "green",
  presupuesto_rechazado: "red", alternativa_preparada: "violet", cerrada: "slate", ejecucion_fallida: "red", running: "indigo", waiting_approval: "amber", waiting_technician: "amber", waiting_task: "amber", blocked: "red", completed: "green", failed: "red", draft: "slate", needs_clarification: "amber", awaiting_user_confirmation: "amber", waiting_human: "amber", cancelled: "slate",
};
export const StatusChip = ({ s }: { s: string }) => <Chip tone={STATUS_TONE[s] ?? "slate"}>{STATUS_LABEL[s] ?? s}</Chip>;

// Honest labelling of what kind of thing produced a step (requirement: real logic vs rules vs LLM vs simulated)
export const MODE: Record<string, { label: string; tone: keyof typeof TONES; hint: string }> = {
  real: { label: "Lógica real", tone: "green", hint: "Lógica de negocio real sobre la base de datos" },
  deterministic: { label: "Determinista", tone: "sky", hint: "Regla determinista del motor de demostración (sin LLM)" },
  llm: { label: "LLM", tone: "violet", hint: "Decisión propuesta por un modelo de lenguaje (validada antes de usarse)" },
  simulated: { label: "Simulado", tone: "amber", hint: "Acción externa SIMULADA: no se envía nada a terceros" },
};
export const ModeChip = ({ m }: { m?: string | null }) => m && MODE[m] ? <Chip tone={MODE[m].tone} title={MODE[m].hint}>{MODE[m].label}</Chip> : null;

export const PageHeader = ({ title, sub, right }: { title: string; sub?: string; right?: ReactNode }) => (
  <div className="flex flex-wrap items-end justify-between gap-3 mb-6">
    <div><h1 className="h-title">{title}</h1>{sub && <p className="text-sm text-slate-500 mt-0.5">{sub}</p>}</div>{right}
  </div>
);

export const Empty = ({ children }: { children: ReactNode }) => <div className="text-sm text-slate-400 py-6 text-center">{children}</div>;
export const fmtTime = (ts: string) => new Date(ts).toLocaleTimeString("es-CL", { hour12: false });
export const fmtDate = (ts: string) => new Date(ts).toLocaleString("es-CL", { dateStyle: "short", timeStyle: "short" });
