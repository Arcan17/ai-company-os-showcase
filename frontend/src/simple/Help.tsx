import { Me } from "../api";
import { DetailLink } from "./Detail";

export default function Help({ me }: { me: Me }) {
  return (
    <div className="max-w-2xl mx-auto" data-testid="simple-help">
      <h1 className="h-title">Cómo funciona</h1>
      <div className="card p-6 mt-6 text-sm space-y-3 text-slate-600">
        <p><b>1.</b> Tu cliente escribe y se registra su solicitud.</p>
        <p><b>2.</b> Tu equipo prepara el presupuesto y te lo muestra en Inicio.</p>
        <p><b>3.</b> Tú apruebas o rechazas. Nada se envía al cliente sin tu decisión.</p>
        <p><b>4.</b> La visita se propone al cliente y solo queda confirmada cuando él responde.</p>
        <p><b>5.</b> Cada tarea se cierra contando qué se hizo, y todo queda registrado.</p>
        <p className="text-xs text-slate-400">Esta es una demostración con datos ficticios: los mensajes a clientes están simulados.</p>
      </div>
      <p className="mt-4"><a className="btn btn-ghost" href={`#/cliente/${me.company.slug}`} data-testid="s-try-customer">Probar como cliente</a></p>
      <div className="mt-4 text-right"><DetailLink to="/dashboard" /></div>
    </div>
  );
}
