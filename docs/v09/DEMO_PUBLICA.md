# V0.9 — Demo pública con un espacio aislado por visitante (spec 004, fase B)

Datos 100 % ficticios. Sin integraciones externas. **Nada de esto está desplegado ni expuesto a internet**: es código y pruebas; el despliegue (fase D) se decide aparte.

## Cómo funciona
`AICOS_PUBLIC_DEMO=1 ./start.sh` (o `AICOS_PUBLIC_DEMO=1 uvicorn app.main:app`) arranca un **enrutador de visitantes** delante de la aplicación:

| Pieza | Comportamiento |
|---|---|
| Espacio por visitante | Cada visitante recibe **su propia aplicación con su propia base SQLite**, copiada de una plantilla sembrada una sola vez. No comparten tablas, sesiones ni ejecuciones (la sesión de uno no existe en la base de otro). |
| Identidad del espacio | Cookie `aicos_visitor` (id de 128 bits + firma HMAC, `HttpOnly`, `SameSite=Lax`). Una cookie falsa, alterada o de un proceso anterior crea un espacio **nuevo**; nunca abre el de otro. |
| Caducidad | Un espacio inactivo se descarta (30 min por defecto, `AICOS_PUBLIC_IDLE_SECONDS`) y se borra su base; uno con una petición en curso no se toca. |
| Tope | Máximo de espacios simultáneos (20 por defecto, `AICOS_PUBLIC_MAX_VISITORS`): con la demo llena responde 503 con un mensaje claro y `Retry-After`. |
| Límite por origen | Máximo de espacios **nuevos** por origen y ventana (6 / 10 min, `AICOS_PUBLIC_NEW_PER_SOURCE`): 429. |
| Lo que no consume cupo | La interfaz (`/`, `/assets`) y `/api/health`: rastreadores y comprobaciones de salud no agotan la demo. |
| Configuraciones que rechaza | Se niega a arrancar con `AICOS_ENV=pilot` o con `AICOS_ALLOW_COMPANY_CREATION=1`. |
| Recorrido guiado | Aviso «DEMO PÚBLICA» + recorrido de 5 pasos (cerrable), solo cuando `/api/public-demo/info` lo indica. |

## Evidencia
- `backend/tests/test_v09_public_demo.py` (11): aislamiento, sesión ajena, cookie falsa, flags de cookie, estáticos/salud sin cupo, tope, límite por origen, caducidad y borrado, petición en curso, configuración insegura, reinicio.
- `qa/mutations_v09.py`: 12 mutaciones, todas detectadas.
- `frontend/e2e_public/public_demo.spec.ts`: dos visitantes simultáneos en un navegador real y el recorrido guiado.
- `qa/smoke_container.sh` con `AICOS_PUBLIC_DEMO=1` + `qa/smoke_public.py`: lo mismo dentro del contenedor, y corre en el CI.

## Límites conocidos
- El estado vive en memoria y en archivos locales: **una sola instancia** (no escala horizontalmente) y un reinicio descarta todos los espacios, por diseño.
- El límite por origen usa la dirección del cliente que ve la aplicación; detrás de un túnel o proxy hay que declarar `AICOS_TRUSTED_PROXIES`, o todos los visitantes parecerán el mismo origen. Se decide en la fase D.
- No se midió aún el consumo de memoria con 20 visitantes reales (tarea T016).
- La entrada «por rol» usa las cuentas ficticias ya existentes (botón y «Entrar»), no un enlace directo.
