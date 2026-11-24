import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Chip } from "../ui";

const SCENARIOS: Record<string, { id: string; title: string; hint: string; name: string; phone: string; message: string }[]> = {
  "clima-sur": [
    { id: "A", title: "A · Reparación con repuesto en stock", hint: "Cliente existente (Marta Rojas) → flujo completo hasta propuesta preparada", name: "Marta Rojas", phone: "+56 9 5555 0101",
      message: "Hola, mi aire acondicionado split del living no enfría desde ayer, creo que le falta gas. ¿Pueden venir esta semana?" },
    { id: "B", title: "B · Falta repuesto → bloqueo y alternativas", hint: "Pedro Gálvez: la placa electrónica original no tiene stock", name: "Pedro Gálvez", phone: "+56 9 5555 0102",
      message: "Mi aire no enciende y la pantalla muestra error E6, necesito ayuda." },
    { id: "D", title: "D · Mantención: técnico + administración + dueño", hint: "Marta pide una mantención. Luego el técnico pide material desde su asistente: colaboración entre personas, 2.ª aprobación y evidencia",
      name: "Marta Rojas", phone: "+56 9 5555 0101", message: "Hola, quiero agendar una mantención preventiva de mi split del living." },
    { id: "C", title: "C · Cliente nuevo", hint: "Se crea un cliente nuevo; el dueño puede rechazar el presupuesto → alternativa", name: "Camila Torres (nueva)", phone: "+56 9 5555 0199",
      message: "Hola, el ventilador de la unidad exterior hace mucho ruido y vibra, ¿cuánto sale revisarlo?" },
  ],
  "motor-norte": [
    { id: "N", title: "Motor Norte · frenos", hint: "Segunda empresa: datos aislados de Clima Sur", name: "Luis Herrera", phone: "+56 9 5555 0201", message: "Me suenan los frenos, necesito cambiar las pastillas." },
  ],
};

// Issue #20: el id del mensaje identifica el envío y el SECRETO prueba que quien reintenta es quien lo envió. Ambos salen de un generador criptográfico (128 bits) —nunca de Math.random()—
// y viven solo en la memoria de esta pestaña: el servidor guarda únicamente una huella dentro de su clave de idempotencia.
const rand128 = () => { const a = new Uint8Array(16); crypto.getRandomValues(a); return btoa(String.fromCharCode(...a)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); };
const newSend = () => ({ mid: "web-" + rand128(), secret: rand128() });

export default function CustomerChat({ slug, loggedIn }: { slug: string; loggedIn: boolean }) {
  const scenarios = SCENARIOS[slug] ?? SCENARIOS["clima-sur"];
  const [name, setName] = useState(scenarios[0].name); const [phone, setPhone] = useState(scenarios[0].phone); const [message, setMessage] = useState(scenarios[0].message);
  const [sent, setSent] = useState<{ run_id: string; token: string; request_id?: string | null; mid: string } | null>(null);
  const [box, setBox] = useState<any>(null); const [err, setErr] = useState(""); const [confirmed, setConfirmed] = useState(false);
  const ids = useRef(newSend());

  const pick = (s: typeof scenarios[0]) => { setName(s.name); setPhone(s.phone); setMessage(s.message); ids.current = newSend(); setSent(null); setBox(null); setConfirmed(false); };
  const send = async () => {
    setErr("");
    try {
      const r = await api(`/public/${slug}/chat`, { body: { message, name, phone, client_msg_id: ids.current.mid, retry_secret: ids.current.secret } });
      setSent({ run_id: r.run_id, token: r.conversation_token, request_id: r.request_id, mid: ids.current.mid });
    } catch (e: any) { setErr(e.message); }
  };
  useEffect(() => {
    if (!sent) return;
    let stop = false;
    const tick = async () => { try { const b = await api(`/public/${slug}/outbox?token=${sent.token}`); if (!stop) setBox(b); } catch { /* ignore */ } };
    tick(); const t = setInterval(tick, 1200); return () => { stop = true; clearInterval(t); };
  }, [sent, slug]);

  const proposal = box?.messages?.find((m: any) => m.status === "preparado_no_enviado" && m.body.includes("Código de confirmación"));
  const code = proposal ? proposal.body.split("confirmación: ")[1]?.replace(/\.$/, "") : null;
  const confirm = async () => {
    try { await api(`/public/${slug}/visits/confirm`, { body: { request_id: box.request.id, code } }); setConfirmed(true); } catch (e: any) { setErr(e.message); }
  };

  return (
    <div className="min-h-full bg-slate-50 py-8 px-4">
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center justify-between mb-5">
          <div><h1 className="h-title">Chat de atención al cliente <span className="text-slate-400">· {slug === "motor-norte" ? "Motor Norte" : "Clima Sur"}</span></h1>
            <p className="text-sm text-slate-500">Canal de PRUEBA: simula a un cliente escribiendo a la empresa. No se conecta a WhatsApp ni a correo.</p></div>
          <a href="#/dashboard" className="btn btn-ghost">← Volver al panel</a>
        </div>
        <div className="card p-4 mb-4">
          <div className="text-xs font-semibold text-slate-500 mb-2">Escenarios de demostración</div>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-2">
            {scenarios.map(s => (
              <button key={s.id} onClick={() => pick(s)} data-testid={"scenario-" + s.id} className="text-left rounded-lg border border-slate-200 hover:border-brand-500 hover:bg-brand-50 p-2.5">
                <div className="text-sm font-semibold">{s.title}</div><div className="text-xs text-slate-500 mt-0.5">{s.hint}</div>
              </button>
            ))}
          </div>
        </div>
        <div className="card p-4 space-y-3">
          <div className="grid sm:grid-cols-2 gap-3">
            <div><label className="label">Nombre</label><input className="input" data-testid="cust-name" value={name} onChange={e => setName(e.target.value)} /></div>
            <div><label className="label">Teléfono (ficticio)</label><input className="input" data-testid="cust-phone" value={phone} onChange={e => setPhone(e.target.value)} /></div>
          </div>
          <div><label className="label">Mensaje</label><textarea className="input" rows={3} data-testid="cust-message" value={message} onChange={e => setMessage(e.target.value)} /></div>
          {err && <div className="text-sm text-rose-600">{err}</div>}
          <div className="flex gap-2 flex-wrap items-center">
            <button className="btn btn-primary" onClick={send} data-testid="send-chat">Enviar mensaje</button>
            {sent && <button className="btn btn-ghost" onClick={send} data-testid="resend-chat" title="Reintento con el MISMO id de mensaje: no debe duplicar nada">Reintentar envío (mismo id)</button>}
            {sent && (loggedIn
              ? <a className="btn btn-ghost" href={`#/flows/${sent.run_id}`} data-testid="open-run">Ver ejecución en el visualizador →</a>
              : <span className="text-xs text-slate-400">Inicia sesión como personal de la empresa para ver el visualizador.</span>)}
          </div>
        </div>
        {sent && (
          <div className="card p-4 mt-4" data-testid="conversation">
            <div className="flex items-center gap-2 mb-3"><div className="font-semibold text-sm">Conversación</div><Chip tone="amber">Canal simulado</Chip></div>
            <div className="space-y-2">
              <div className="flex justify-end"><div className="bg-brand-600 text-white rounded-2xl rounded-br-sm px-3.5 py-2 max-w-[85%] text-sm">{message}</div></div>
              {(box?.messages ?? []).filter((m: any) => m.sender !== "customer").map((m: any, i: number) => (
                <div key={i} className="flex justify-start"><div className="bg-slate-100 rounded-2xl rounded-bl-sm px-3.5 py-2 max-w-[85%] text-sm">
                  {m.status === "preparado_no_enviado" && <div className="mb-1"><Chip tone="amber">Mensaje PREPARADO · no enviado (simulado)</Chip></div>}
                  {m.body}</div></div>
              ))}
              {!box && <div className="text-xs text-slate-400">Esperando respuesta…</div>}
            </div>
            {proposal && (
              <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3">
                <div className="text-sm font-semibold text-amber-900">La visita está PROPUESTA, no confirmada</div>
                <p className="text-xs text-amber-800 mt-0.5">Solo cuando el cliente confirma de verdad la visita queda confirmada. Este botón simula que el cliente responde con el código.</p>
                <button className="btn btn-primary mt-2" onClick={confirm} disabled={confirmed} data-testid="customer-confirm">{confirmed ? "Visita confirmada por el cliente ✓" : "Simular: el cliente responde CONFIRMO"}</button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
