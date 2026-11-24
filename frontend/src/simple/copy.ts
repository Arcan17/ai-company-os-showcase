// Textos de negocio del modo simple (spec 002 FR-005/FR-009) y lista de términos que NUNCA deben verse en él (SC-002).
export const FORBIDDEN_TERMS = ["agente", "herramienta", "permiso", "evento", "motor determinista", "req_", "run_", "pln_", "tool.", "llm", "asistente personal", "bd escritura", "lógica real", "sse"];

export const REQUEST_STATUS: Record<string, string> = {
  nueva: "Recibida", clasificada: "En revisión", presupuesto_en_revision: "Presupuesto listo para aprobar", bloqueada_repuesto: "Esperando un repuesto",
  propuesta_preparada: "Visita propuesta al cliente", visita_confirmada: "Visita confirmada", presupuesto_rechazado: "Presupuesto rechazado",
  alternativa_preparada: "Alternativa propuesta", cerrada: "Terminada", ejecucion_fallida: "Necesita revisión",
};
export const requestStatus = (s: string) => REQUEST_STATUS[s] ?? "En curso";

// Línea de avance de una solicitud (de qué etapa viene y cuál sigue).
export const STAGES = ["Llegó la solicitud", "Presupuesto listo", "Tu aprobación", "Visita", "Cierre"];
export const stageIndex = (status: string): number => ({ nueva: 0, clasificada: 0, presupuesto_en_revision: 2, bloqueada_repuesto: 1, propuesta_preparada: 3, alternativa_preparada: 3, visita_confirmada: 3, presupuesto_rechazado: 2, cerrada: 4 } as Record<string, number>)[status] ?? 1;

export const ROLE_HOME: Record<string, string> = { owner: "/s/home", admin: "/s/tasks", technician: "/s/tasks" };
export const SIMPLE_MENU: Record<string, { to: string; label: string; perm?: string }[]> = {
  owner: [{ to: "/s/home", label: "Inicio" }, { to: "/s/requests", label: "Solicitudes", perm: "requests.read" }, { to: "/s/help", label: "Ayuda" }],
  admin: [{ to: "/s/tasks", label: "Mis tareas" }, { to: "/s/requests", label: "Solicitudes", perm: "requests.read" }],
  technician: [{ to: "/s/tasks", label: "Mis tareas" }, { to: "/s/requests", label: "Solicitudes", perm: "requests.read" }],
};
