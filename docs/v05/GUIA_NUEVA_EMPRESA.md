# Guía: crear y probar una tercera empresa ficticia (V0.5)

Sin editar código ni JSON. Todo en tu Mac, datos **ficticios**, aislado del laboratorio `:8731` y de la demo `:8743`.

## 0. Arrancar el laboratorio V0.5
```bash
./run_v05_lab.sh            # BD propia backend/data/v05_lab.sqlite3, puerto libre desde 8750, creación de empresas activada (solo loopback)
./run_v05_lab.sh status     # qué corre; no toca nada
./run_v05_lab.sh reset      # borra SOLO esta BD (confirmación + copia previa)
```
La primera vez compila la UI en `frontend/dist-v05` (no toca `frontend/dist`). El script imprime la URL (normalmente `http://127.0.0.1:8750`). **No abre ningún navegador**: ábrelo tú.

## 1. Crear la empresa (UI) — 1 min
1. En la pantalla de login, pulsa **«Crear una empresa ficticia (entorno local)»**.
2. Nombre `Aire Austral`, plantilla de industria *Servicio técnico de aire acondicionado*, dueña `Elena Rojas` / `elena@aireaustral.test` y una contraseña (o déjala vacía: se genera y se muestra **una sola vez**).
3. **Crear empresa** → **Entrar como dueño/a**. Verás «Empresa y equipo» en el menú.

*Equivalente por línea de comandos* (otra empresa, misma BD del laboratorio):
```bash
cd backend && .venv/bin/python -m app.company_admin create --name "Frío Cordillera" --owner-name "Tomás Prueba" \
  --owner-email tomas@friocordillera.test --preset hvac --db data/v05_lab.sqlite3
```

## 2. Empleados y asistentes — 3 min («Empresa y equipo»)
1. **Incorporar empleado** ×4 (deja marcado «Asignar su asistente personal ahora»): `Javiera Pons` y `Rodrigo Silva` como *Administración*; `Matías Ortiz` y `Camila Vera` como *Técnico/a*. (Contraseñas: la tuya o las generadas.)
2. Incorpora un quinto, `Sin Asistente`, **desmarcando** la casilla: queda «Activo · sin asistente activo» (no puede recibir trabajo). Pulsa **asignar** → ya puede.
3. Prueba **desactivar/activar** el asistente de Rodrigo (la fila cambia de estado). Cada cambio queda en la auditoría.
4. Abre *Plantillas de agentes*: capacidades **derivadas del registro de herramientas**; un asistente personal actúa con *plantilla ∩ permisos del rol del empleado* (configurarlo no da permisos nuevos).

## 3. Un plan completo en la empresa nueva — 4 min
1. **Clientes y operaciones › Nueva solicitud**: cliente `Cliente Uno`, «Quiero una mantención preventiva del split del local».
2. **Coordinador**: `Revisa la mantención pendiente de Cliente Uno; pide a administración que verifique materiales y al técnico que confirme después de administración.`
3. Hay **2+ administradores y 2+ técnicos elegibles**: el coordinador **pregunta** (con su carga de trabajo) en vez de tomar «el primero». Elige a **Rodrigo** y luego a **Camila** → plan propuesto, **0 tareas** todavía → **Confirmar plan** (doble clic: no duplica).
4. Abre **tres sesiones independientes** (ventanas privadas o navegadores distintos): Rodrigo (`rodrigo@aireaustral.test`), Javiera y Camila. Solo Rodrigo ve la tarea; Javiera no recibe nada y no puede cerrar la de Rodrigo; la de Camila aparece «en espera» y se habilita cuando Rodrigo cierra la suya con evidencia (≥ 10 caracteres). Rodrigo puede preguntar a su asistente `¿Cuánto stock hay de GAS-R410?` (catálogo de SU empresa).
5. Camila completa con evidencia → el plan pasa a **Completada**. **Ver en el visualizador**: delegaciones, evidencias y eventos reales.
6. Prueba de control: `Compra repuestos y avisa a todos por WhatsApp.` → plan **Bloqueado**, sin pasos.

## 4. Comprobarlo contra SQLite (solo lectura)
```bash
python3 qa/dbq.py "select p.id,p.status,s.seq,s.action,u.name,s.status from plans p join plan_steps s on s.plan_id=p.id left join users u on u.id=s.assignee_user_id order by p.created_at desc,s.seq limit 8" backend/data/v05_lab.sqlite3
python3 qa/dbq.py "select t.status,u.name,t.evidence from tasks t join users u on u.id=t.assignee_user_id where t.kind='plan_step' order by t.created_at desc limit 4" backend/data/v05_lab.sqlite3
python3 qa/dbq.py "select ts,action,resource,outcome from audit_log where action like 'config.%' order by id desc limit 12" backend/data/v05_lab.sqlite3
```
El número de eventos del visualizador = `select count(*) from events where run_id='<plan>'`.

## 5. Qué NO hace (límites)
Alta de empresas **solo local/demo** (apagada por defecto; 404 si no se activa o si no viene de la propia máquina), no es un registro público. Sin usuarios reales, MFA, CSRF, TLS ni PostgreSQL (fase posterior). Los agentes se configuran **por plantilla y asignación**: no hay editor de prompts, herramientas ni permisos. Canales externos (WhatsApp, correo, teléfono, pagos): **no existen**. No se puede desactivar ni cambiar de rol a quien tiene tareas abiertas.

## 6. Verificación automática
`cd backend && .venv/bin/python -m pytest -q` (incluye `tests/test_v05_config.py`) y `cd frontend && npx playwright test` (incluye `e2e/v05_third_company.spec.ts`: este mismo recorrido con 4 sesiones de navegador).
