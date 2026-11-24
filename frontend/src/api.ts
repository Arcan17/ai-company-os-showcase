export type Me = { id: string; email: string; name: string; role: "owner" | "admin" | "technician"; company_id: string; company: { slug: string; name: string; industry: string }; permissions: string[]; is_demo: number;
  auth_time?: string | null; step_up?: { enforced: boolean; fresh: boolean; max_age_seconds: number } };
export type Ev = { id: number; seq: number; ts: string; type: string; agent: string | null; mode: string | null; title: string; data: any };

export class ApiError extends Error { constructor(public status: number, msg: string) { super(msg); } }

// CSRF synchronizer token: issued by /auth/login and /auth/me for THIS session, kept in memory only (never in storage), sent on every write.
let csrfToken = "";

// V0.7 P2: una operación sensible con una autenticación vieja responde 401 `reauth_required`. La SPA pide la contraseña UNA vez (cuadro de ReauthDialog) y reintenta la MISMA petición una sola vez.
let reauthHandler: null | (() => Promise<boolean>) = null;
export function setReauthHandler(fn: null | (() => Promise<boolean>)) { reauthHandler = fn; }

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; retried?: boolean } = {}): Promise<T> {
  const method = opts.method ?? (opts.body ? "POST" : "GET");
  const headers: Record<string, string> = {};
  if (opts.body) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const r = await fetch("/api" + path, { method, headers, body: opts.body ? JSON.stringify(opts.body) : undefined, credentials: "same-origin" });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* ignore */ }
    // The session in this tab is no longer the one the browser holds (another tab signed in as someone else): never retry the write as a different person.
    if (r.status === 403 && msg.startsWith("csrf")) { csrfToken = ""; msg = "Tu sesión cambió o venció en otra pestaña. Recarga la página para continuar."; }
    if (r.status === 401 && msg === "reauth_required") {
      if (reauthHandler && !opts.retried && path !== "/auth/reauth" && (await reauthHandler())) return api<T>(path, { ...opts, retried: true });
      throw new ApiError(401, "Por seguridad, hace falta confirmar tu identidad otra vez para esta operación.");
    }
    throw new ApiError(r.status, msg);
  }
  const j = await r.json();
  if (path === "/auth/logout" || path === "/auth/logout-all") csrfToken = "";
  else if (j && typeof j === "object" && typeof j.csrf_token === "string") csrfToken = j.csrf_token;
  return j;
}

export const money = (n: number) => "$" + Math.round(n).toLocaleString("es-CL");
export const ROLE_LABEL: Record<string, string> = { owner: "Dueño/a", admin: "Administración", technician: "Técnico/a" };
export const STATUS_LABEL: Record<string, string> = {
  nueva: "Nueva", clasificada: "Clasificada", presupuesto_en_revision: "Presupuesto en revisión", bloqueada_repuesto: "Bloqueada: repuesto",
  propuesta_preparada: "Propuesta preparada", visita_confirmada: "Visita confirmada", presupuesto_rechazado: "Presupuesto rechazado",
  alternativa_preparada: "Alternativa preparada", cerrada: "Cerrada", ejecucion_fallida: "Ejecución fallida", running: "En ejecución", waiting_approval: "Esperando aprobación",
  blocked: "Bloqueada", draft: "Borrador", needs_clarification: "Necesita aclaración", awaiting_user_confirmation: "Esperando tu confirmación", waiting_human: "Esperando a una persona", cancelled: "Cancelado", waiting_technician: "Esperando al técnico", waiting_task: "Esperando tarea con evidencia", completed: "Completada", failed: "Fallida · requiere revisión",
};
