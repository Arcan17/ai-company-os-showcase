import { api, Me, ROLE_LABEL } from "../api";
import { Chip, ModeChip, PageHeader } from "./../ui";
import { usePoll } from "./Dashboard";

const GROUPS: Record<string, string> = { "requests": "Solicitudes", "customers": "Clientes", "quotes": "Presupuestos", "visits": "Visitas", "tasks": "Tareas", "approvals": "Aprobaciones",
  "workorders": "Órdenes", "inventory": "Inventario", "pricing": "Precios", "availability": "Agenda", "delegations": "Delegación", "notes": "Notas privadas", "messages": "Mensajes", "materials": "Materiales" };

function AgentCard({ a, live }: { a: any; live: boolean }) {
  const groups: Record<string, string[]> = {};
  for (const p of a.permissions as string[]) { const [g, act] = p.split("."); (groups[g] ??= []).push(act); }
  return (
    <div className="card p-4 w-full" data-testid={"agent-" + a.role}>
      <div className="flex items-start justify-between gap-2">
        <div><div className="font-semibold">{a.name}</div>
          <div className="text-xs text-slate-500 mt-0.5">{a.employee ? <>Asistente de <b>{a.employee}</b> · {ROLE_LABEL[a.employee_role]}</> : "Agente compartido de la empresa"}</div></div>
        <span data-testid="agent-status"><Chip tone={a.availability === "disabled" ? "red" : a.availability === "working" ? "indigo" : "green"} title="Disponible = habilitado y listo para recibir trabajo">{a.availability_text}</Chip></span>
      </div>
      <p className="text-sm text-slate-600 mt-2">{a.description}</p>
      <details className="mt-3"><summary className="text-xs font-semibold text-brand-700 cursor-pointer">Permisos ({a.permissions.length})</summary>
        <div className="mt-2 flex flex-wrap gap-1">{Object.entries(groups).map(([g, acts]) => <Chip key={g}>{GROUPS[g] ?? g}: {acts.join(", ")}</Chip>)}</div>
        {a.employee && <p className="text-[11px] text-slate-400 mt-2">Permisos efectivos = permisos del agente ∩ permisos del rol del empleado.</p>}
      </details>
    </div>
  );
}

export default function Agents({ me }: { me: Me }) {
  const [d] = usePoll<any>("/agents", 4000);
  const [t] = usePoll<any>("/tools", 60000);
  const [i] = usePoll<any>("/integrations", 60000);
  if (!d) return <div className="text-slate-400">Cargando…</div>;
  const by = (r: string) => d.agents.find((x: any) => x.role === r);
  const live = d.live_runs.length > 0;
  return (
    <div>
      <PageHeader title="Equipo de agentes" sub={`Organigrama de ${me.company.name}: un coordinador, un agente externo y un asistente por empleado.`} />
      <div className="card p-5">
        <div className="max-w-sm mx-auto">{by("coordinator") ? <AgentCard a={by("coordinator")} live={live} /> : null}</div>
        <div className="mx-auto h-6 w-px bg-slate-300" />
        <div className="grid md:grid-cols-4 gap-3 relative pt-3 border-t border-slate-300">
          {/* V0.5: one personal assistant PER employee (several administrators/technicians are possible) */}
          {["reception", "owner_assistant", "admin_assistant", "tech_assistant"].flatMap(r => d.agents.filter((x: any) => x.role === r)).map((a: any) => <AgentCard key={a.id} a={a} live={live} />)}
        </div>
        <p className="text-xs text-slate-400 mt-4 text-center">El agente de recepción es el «agente externo» (habla con clientes). Los asistentes personales actúan con los permisos de su empleado.</p>
      </div>
      <div className="grid lg:grid-cols-2 gap-4 mt-4">
        <div className="card p-4">
          <div className="font-semibold mb-1">Herramientas</div>
          <p className="text-xs text-slate-500 mb-3">Toda acción pasa por este registro: permiso → validación → transacción → auditoría. El esquema es compatible con MCP (aún no expuesto como servidor).</p>
          <div className="max-h-96 overflow-auto scroll-thin" data-testid="tool-list">
            {(t?.tools ?? []).map((x: any) => (
              <div key={x.name} className="flex items-center justify-between gap-2 border-t first:border-0 border-slate-100 py-1.5 text-sm">
                <span><code className="text-xs">{x.name}</code><span className="block text-[11px] text-slate-400">{x.permission}</span></span>
                <span className="flex gap-1"><Chip tone={x.access === "write" ? "red" : "slate"}>{x.access === "write" ? "escribe" : "lee"}</Chip><ModeChip m={x.mode} /></span>
              </div>))}
            <div className="border-t border-slate-100 py-1.5 text-sm flex items-center justify-between"><code className="text-xs">approvals.decide</code><Chip tone="green">solo humanos</Chip></div>
          </div>
        </div>
        <div className="card p-4">
          <div className="font-semibold mb-1">Integraciones externas</div>
          <p className="text-xs text-slate-500 mb-3">Ninguna está conectada en esta versión: todo canal externo es SIMULADO.</p>
          {(i?.integrations ?? []).map((x: any) => (
            <div key={x.kind} className="flex items-center justify-between border-t first:border-0 border-slate-100 py-2 text-sm"><span>{x.name}</span><span className="flex gap-1"><Chip tone="amber">simulado</Chip><Chip>{x.enabled ? "activa" : "no conectada"}</Chip></span></div>))}
          <div className="mt-4 rounded-lg bg-slate-50 border border-slate-200 p-3 text-xs text-slate-600">
            <b>Motor de decisiones:</b> {d.provider_mode === "llm" ? <>LLM local <code>{d.provider}</code>: solo propone clasificaciones; el motor las valida y ejecuta las acciones con permisos.</> : "motor determinista de demostración (reglas explícitas, sin credenciales ni red)."}
          </div>
        </div>
      </div>
    </div>
  );
}
