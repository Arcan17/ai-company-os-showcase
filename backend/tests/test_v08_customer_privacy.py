"""V0.8 (spec 003, historia 1) — el técnico/a NUNCA recibe teléfono, correo ni dirección de un cliente, por ninguna ruta de lectura, en ninguna etapa.
Hasta aquí esos datos viajaban dentro de los eventos de la ejecución (entradas y salidas de herramientas) y el técnico tiene `events.read`."""
import json

from .conftest import GAS, chat, events, pending_approval

PHONE = GAS["phone"]            # +56 9 5555 0101 (Marta Rojas, ficticia)
EMAIL = "marta.rojas@example.test"


def _contact_in(obj) -> list[str]:
    s = json.dumps(obj, ensure_ascii=False)
    return [v for v in (PHONE, EMAIL) if v in s]


def _run_with_contact(anon, owner):
    r = chat(anon)
    # control: la dueña SÍ ve el contacto en los eventos (si no, la prueba sería vacía)
    assert _contact_in(events(owner, r["run_id"])), "control: la dueña debe ver el contacto"
    return r


def test_tecnico_no_ve_contacto_en_eventos_ni_en_la_ejecucion(anon, owner, tech):
    r = _run_with_contact(anon, owner)
    rid = r["run_id"]
    assert _contact_in(events(tech, rid)) == []
    assert _contact_in(tech.get(f"/api/runs/{rid}").json()) == []
    d = owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    assert d.status_code == 200
    assert _contact_in(events(tech, rid)) == []                        # tampoco después de aprobar / con visita propuesta
    assert _contact_in(tech.get("/api/tasks").json()) == []
    assert _contact_in(tech.get("/api/work-orders").json()) == []


def test_el_nombre_y_el_resto_del_evento_se_conservan(anon, owner, tech):
    rid = _run_with_contact(anon, owner)["run_id"]
    mine = events(owner, rid); theirs = events(tech, rid)
    assert [e["seq"] for e in mine] == [e["seq"] for e in theirs] and [e["type"] for e in mine] == [e["type"] for e in theirs]
    assert "Marta Rojas" in json.dumps(theirs, ensure_ascii=False)     # el técnico necesita el nombre para trabajar


def test_administracion_y_duena_siguen_viendo_el_contacto(anon, owner, admin):
    rid = _run_with_contact(anon, owner)["run_id"]
    assert _contact_in(events(admin, rid))
    assert _contact_in(owner.get(f"/api/requests/{owner.get('/api/requests').json()['requests'][0]['id']}").json())


def test_los_eventos_guardados_no_se_alteran(anon, owner, tech, app):
    from sqlalchemy import text
    rid = _run_with_contact(anon, owner)["run_id"]
    events(tech, rid)
    with app.state.eng.connect() as c:
        raw = " ".join(x[0] or "" for x in c.execute(text("SELECT data FROM events WHERE run_id=:r"), {"r": rid}))
    assert PHONE in raw                                                # el original se conserva (la auditoría no se reescribe)


def test_el_flujo_en_vivo_tampoco_lo_filtra_al_tecnico(anon, owner, tech):
    rid = _run_with_contact(anon, owner)["run_id"]
    with tech.stream("GET", f"/api/runs/{rid}/stream?after=0") as s:
        buf = ""
        for chunk in s.iter_text():
            buf += chunk
            if "run.waiting_approval" in buf:
                break
    assert PHONE not in buf and EMAIL not in buf and "run-event" in buf


def test_un_dato_de_contacto_escrito_en_un_texto_libre_tampoco_llega_al_tecnico(anon, owner, tech, app):
    """El cliente suele escribir su teléfono en el propio mensaje: esos textos viajan en la descripción de la solicitud, el resumen de un resultado y las tareas."""
    from sqlalchemy import text
    r = chat(anon)
    owner.post(f"/api/approvals/{pending_approval(owner)['id']}/decide", json={"decision": "approved"})
    rid = r["run_id"]
    with app.state.eng.begin() as c:
        c.execute(text("UPDATE requests SET description = description || ' Mi número es ' || :p"), {"p": PHONE})
        c.execute(text("UPDATE results SET summary = COALESCE(summary,'') || ' Llamar al ' || :p"), {"p": PHONE})
        c.execute(text("UPDATE tasks SET title = title || ' (cliente ' || :e || ')'"), {"e": EMAIL})
    assert PHONE in json.dumps(owner.get(f"/api/runs/{rid}").json())            # control: quien puede ver contacto lo ve
    assert _contact_in(tech.get(f"/api/runs/{rid}").json()) == []
    assert _contact_in(tech.get("/api/tasks").json()) == [] and _contact_in(tech.get("/api/work-orders").json()) == []
