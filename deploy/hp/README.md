# Despliegue de la demo pública en el HP (Quadlet) — NADA de esto se ha ejecutado

Esta carpeta solo contiene la unidad y los pasos para un servidor con Podman rootless (4.9 o superior) y systemd de usuario. **Cada paso se ejecuta únicamente con la aprobación explícita de quien opera el servidor.**

## P1 — Llevar el código e imagen al HP (escribe en el HP)
```bash
# desde tu computador (solo los archivos que Git rastrea; sin historial ni secretos)
git archive HEAD | ssh hp-homelab 'mkdir -p ~/aicos-demo-src && tar -x -C ~/aicos-demo-src'
# en el HP, como el usuario normal (sin sudo)
cd ~/aicos-demo-src && podman build -f Containerfile -t localhost/aicos-demo:v11 .
```
## P2 — Instalar la unidad y arrancar (escribe en el HP)
```bash
mkdir -p ~/.config/containers/systemd
cp deploy/hp/aicos-demo.container ~/.config/containers/systemd/
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun     # solo valida: no instala nada
systemctl --user daemon-reload && systemctl --user start aicos-demo.service
systemctl --user status aicos-demo.service --no-pager
```
## P3 — Probar SIN exponer nada (desde el Mac, por túnel SSH)
```bash
ssh -N -L 18731:127.0.0.1:8731 hp-homelab &        # solo tu Mac ve el puerto
python3 qa/smoke_http.py http://127.0.0.1:18731
python3 qa/smoke_public.py http://127.0.0.1:18731
```
La medición de memoria con 20 visitantes (`qa/measure_public_demo.py`) se repite DENTRO del HP antes de subir `AICOS_PUBLIC_MAX_VISITORS` por encima de 10.

## P4 — Exponer (opción A: solo tailnet) — cambia `tailscale serve`
Si `tailscale serve` ya publica algo en la raíz `/`, la demo va en **otro puerto HTTPS** (p. ej. 8443); no se toca la configuración existente. Detrás de `serve` hay que declarar `AICOS_TRUSTED_PROXIES` o el límite por origen verá un solo origen. Funnel (público) NO se activa sin aprobación aparte.

## Apagado programado (decidido: 30 días → 2026-12-25 12:00 UTC)
```bash
cp deploy/hp/aicos-demo-apagado.timer deploy/hp/aicos-demo-apagado.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now aicos-demo-apagado.timer
systemctl --user list-timers aicos-demo-apagado.timer --no-pager      # debe mostrar la fecha
```
Al llegar la fecha detiene la demo y retira su unidad Quadlet (no reaparece al reiniciar); conserva el volumen y la imagen. Para **prolongarla**: cambia `OnCalendar` y `daemon-reload`. Para **cancelar el apagado**: `systemctl --user disable --now aicos-demo-apagado.timer`.

## Apagar / volver atrás
```bash
systemctl --user stop aicos-demo.service
rm ~/.config/containers/systemd/aicos-demo.container && systemctl --user daemon-reload
podman volume rm aicos-demo-data        # borra los datos (ficticios) de la demo
```
La imagen anterior se conserva con su etiqueta; para volver a una versión previa basta reetiquetarla como `v11` (o la etiqueta vigente) y reiniciar el servicio.


## Demo PRIVADA con IA local (`aicos-demo-ia`) — instalada el 2026-11-26 con aprobación del autor
Otra instancia, distinta de la pública: imagen `v12`, puerto `127.0.0.1:8732`, volumen `aicos-demo-ia-data`, base persistente y el planificador del coordinador usando el **Ollama del propio HP** (`qwen3:4b-instruct-hermes`, el modelo que ya estaba cargado: no se carga ninguno nuevo). **No es pública y no se expone**: se alcanza desde el HP o por túnel SSH.
```bash
# construir (en el HP, con el código de main llevado a ~/aicos-demo-src-v12 como en P1)
podman build -f Containerfile -t localhost/aicos-demo:v12 .
# instalar y arrancar (sin sudo)
cp deploy/hp/aicos-demo-ia.container ~/.config/containers/systemd/
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun     # solo valida
systemctl --user daemon-reload && systemctl --user start aicos-demo-ia.service
# usarla desde tu Mac
ssh -N -L 18732:127.0.0.1:8732 hp-homelab &        # y abre http://127.0.0.1:18732
```
- **Red:** el Ollama del HP solo escucha en el loopback del anfitrión, así que el contenedor usa `Network=slirp4netns:allow_host_loopback=true` (el loopback del HP aparece como `10.0.2.2`). Es lo mínimo para llegar a él; **no** se usa la red del anfitrión ni se cambia cómo escucha Ollama. Contrapartida: desde ese contenedor es alcanzable cualquier servicio del loopback del HP; la app solo llama a la URL fija de la unidad, no maneja datos reales y el contenedor es de solo lectura y sin capacidades.
- **Lentitud esperada (solo CPU):** un plan tarda ~16 s (medido: mediana 16 s, máx. 18,5 s); mientras tanto la pantalla dice «Pensando…». Con el modelo activo, la clasificación de solicitudes y los borradores del flujo «Probar como cliente» también pasan por el modelo, así que ese flujo tarda bastante más que con reglas. Si Hermes u Oficina V2 usan el modelo a la vez, irá más lento; pasado el plazo (90 s) se usan las reglas, rotulado.
- **Reiniciar a la decisión de ejemplo inicial:** `systemctl --user stop aicos-demo-ia.service && podman volume rm aicos-demo-ia-data && systemctl --user start aicos-demo-ia.service`.
- **Apagar y quitar:** `systemctl --user stop aicos-demo-ia.service && rm ~/.config/containers/systemd/aicos-demo-ia.container && systemctl --user daemon-reload` (la imagen y el volumen se conservan hasta que los borres). Tiene su propio apagado programado (2026-12-25 12:00 UTC, mismo día que la demo pública): `aicos-demo-ia-apagado.timer/.service`, que se instalan igual que los de arriba (`cp deploy/hp/aicos-demo-ia-apagado.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now aicos-demo-ia-apagado.timer`); para cancelarlo: `systemctl --user disable --now aicos-demo-ia-apagado.timer`.
