import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";

export type ViewMode = "simple" | "complete";
const norm = (v: string | null): ViewMode | null => (v === "simple" || v === "complete" ? v : null);
const read = (k: string) => { try { return norm(localStorage.getItem(k)); } catch { return null; } };
const write = (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* sin almacenamiento: vale el valor por defecto */ } };

/** Elección de modo por usuario (spec 002 FR-002/FR-003). `aicos.mode.default` es el valor por defecto del navegador; lo usan las pruebas E2E existentes para seguir ejerciendo la interfaz completa. */
export const modeKey = (userId: string) => `aicos.mode.${userId}`;
export const initialMode = (userId: string): ViewMode => read(modeKey(userId)) ?? read("aicos.mode.default") ?? "simple";

const Ctx = createContext<{ mode: ViewMode; setMode: (m: ViewMode) => void }>({ mode: "complete", setMode: () => {} });
export const useViewMode = () => useContext(Ctx);

export function ViewModeProvider({ userId, children }: { userId: string; children: ReactNode }) {
  const [mode, setModeState] = useState<ViewMode>(() => initialMode(userId));
  useEffect(() => { setModeState(initialMode(userId)); }, [userId]);
  const setMode = useCallback((m: ViewMode) => { write(modeKey(userId), m); setModeState(m); }, [userId]);
  return <Ctx.Provider value={{ mode, setMode }}>{children}</Ctx.Provider>;
}

export function ModeSwitch({ compact = false }: { compact?: boolean }) {
  const { mode, setMode } = useViewMode();
  const b = (m: ViewMode, label: string) => (
    <button type="button" data-testid={(compact ? "mode-m-" : "mode-") + m} aria-pressed={mode === m} onClick={() => setMode(m)}
      className={`flex-1 rounded-full px-3 py-1 text-xs font-semibold transition-all ${mode === m ? "bg-white shadow text-brand-700" : "text-slate-500 hover:text-slate-700"}`}>{label}</button>);
  return <div className="flex rounded-full bg-white/40 border border-white/70 p-0.5" role="group" aria-label="Modo de vista" data-testid={compact ? "mode-switch-m" : "mode-switch"}>{b("simple", "Simple")}{b("complete", "Completo")}</div>;
}
