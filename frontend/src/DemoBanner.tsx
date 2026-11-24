import { useEffect, useState } from "react";

// Only visible in the commercial-demo environment (/api/demo/info → demo:true). Honest label of what the audience is looking at.
export default function DemoBanner() {
  const [d, setD] = useState<any>(null);
  useEffect(() => { fetch("/api/demo/info").then(r => r.json()).then(setD).catch(() => {}); }, []);
  if (!d?.demo) return null;
  return (
    <div className="shrink-0 bg-amber-100 border-b border-amber-300 text-amber-950 text-[12px] leading-tight px-3 py-1.5 flex flex-wrap items-center justify-center gap-x-3 gap-y-0.5" data-testid="demo-banner">
      <b>DEMO COMERCIAL</b><span>datos 100 % ficticios</span><span>motor: {d.mode_label}</span>
      <span>WhatsApp, correo, teléfono y pagos: <b>simulados (no existen)</b></span>
      <a className="underline font-semibold" href="/demo" target="_blank" rel="noopener">Guía · real vs simulado</a>
    </div>
  );
}
