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
