"""V0.9 (spec 004, medición de la fase D) — carrera real del motor: `emit_in` calculaba `MAX(seq)+1` y luego insertaba, así que dos escritores de la MISMA ejecución (el hilo del motor y la petición de
aprobación de la dueña) podían elegir el mismo `seq` y uno recibía `UNIQUE constraint failed: events.run_id, events.seq`: la ejecución quedaba «failed» o la aprobación respondía 500.
Se vio con 20 visitantes simultáneos de la demo pública (3 de ~80 ejecuciones). Ahora el número se asigna en UNA sola sentencia y, si aun así hay colisión, se reintenta."""
import threading

from sqlalchemy import text

from app.db import make_engine, md
from app.events import emit


def _eng(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    md.create_all(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO companies(id,name,slug,industry,created_at) VALUES('co_1','Empresa ficticia','ficticia','pruebas','2026-01-01T00:00:00.000+00:00')"))
    return eng


def test_escritores_concurrentes_de_la_misma_ejecucion_nunca_repiten_seq(tmp_path):
    eng = _eng(tmp_path)
    errors, N, T = [], 60, 8

    def writer(k):
        try:
            for i in range(N):
                emit(eng, "co_1", "run_1", "t.x", agent=f"w{k}", title=f"{k}-{i}")
        except Exception as e:                                           # noqa: BLE001
            errors.append(repr(e))

    ths = [threading.Thread(target=writer, args=(k,)) for k in range(T)]
    [t.start() for t in ths]; [t.join() for t in ths]
    assert errors == []
    with eng.connect() as c:
        seqs = [r[0] for r in c.execute(text("SELECT seq FROM events WHERE run_id='run_1' ORDER BY seq"))]
    assert seqs == list(range(1, N * T + 1))                              # sin huecos ni repetidos


def test_la_ejecucion_devuelve_el_seq_asignado_y_las_ejecuciones_son_independientes(tmp_path):
    eng = _eng(tmp_path)
    assert emit(eng, "co_1", "run_a", "t")["seq"] == 1 and emit(eng, "co_1", "run_a", "t")["seq"] == 2
    assert emit(eng, "co_1", "run_b", "t")["seq"] == 1
