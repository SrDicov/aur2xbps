# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests Fase 2 madurez — robustez de datos.

H-1.1 SQLite: WAL, busy_timeout, retry ante lock, GC, concurrencia real
H-1.2 Parser: control chars, límites, nombres homoglifos, arch desconocido,
     pkgver/pkgrel validados + fuzzer determinista (sin dependencias)
"""
import json
import random
import string
import subprocess
import sys
from pathlib import Path

import pytest

from src.aur.client import AURClient
from src.aur.parser import parse_srcinfo


@pytest.fixture()
def db(tmp_path):
    c = AURClient(db_path=tmp_path / "test.db")
    yield c
    c.close()


# ================================================================ H-1.1
def test_sqlite_wal_y_busy_timeout(db):
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


class _FlakyConn:
    """Proxy de conexión: falla con 'locked' las primeras N ejecuciones del
    SQL objetivo (sqlite3.Connection real no permite setattr)."""

    def __init__(self, real, fail_sql: str | None, err: str = "database is locked",
                 times: int = 1):
        self._real = real
        self._fail_sql = fail_sql
        self._err = err
        self.remaining = times

    def execute(self, sql, *a):
        if self._fail_sql is not None and self._fail_sql in sql and self.remaining > 0:
            self.remaining -= 1
            import sqlite3 as sq
            raise sq.OperationalError(self._err)
        return self._real.execute(sql, *a)

    def commit(self):
        return self._real.commit()

    def cursor(self):
        return self._real.cursor()

    def close(self):
        return self._real.close()


def test_cache_roundtrip_y_retry_ante_lock(db, monkeypatch):
    db._cache_put("u1", {"x": 1}, None, None)
    assert db._cache_get("u1")[0] == {"x": 1}

    # primera escritura falla con 'locked', la segunda pasa → sin excepción
    flaky = _FlakyConn(db._conn, "INSERT OR REPLACE INTO rpc_cache")
    monkeypatch.setattr(db, "_conn", flaky)
    db._cache_put("u2", {"y": 2}, None, None)
    assert flaky.remaining == 0                 # hubo un reintento efectivo
    assert db._conn.execute(                    # vía real tras restaurar
        "SELECT response FROM rpc_cache WHERE url='u2'").fetchone()[0] == '{"y": 2}'


def test_db_write_no_reintenta_otros_errores(db, monkeypatch):
    bad = _FlakyConn(db._conn, None, err="no such table: foo", times=10**9)
    # con fail_sql=None el proxy no interviene; usar un execute directo roto:
    import sqlite3 as sq

    class BrokenConn(_FlakyConn):
        def execute(self, sql, *a):
            raise sq.OperationalError("no such table: foo")

    monkeypatch.setattr(db, "_conn", BrokenConn(db._conn, None))
    n = {"n": 0}
    orig_execute = BrokenConn.execute

    def counting(self, sql, *a):
        n["n"] += 1
        return orig_execute(self, sql, *a)

    monkeypatch.setattr(BrokenConn, "execute", counting)
    with pytest.raises(sq.OperationalError, match="no such table"):
        db._db_write(lambda: db._conn.execute("SELECT 1"))
    assert n["n"] == 1                          # sin reintentos


def test_gc_purga_caducados(tmp_path):
    c = AURClient(db_path=tmp_path / "gc.db")
    now_old = 1000.0
    c._conn.execute(
        "INSERT INTO rpc_cache(url,method,response,fetched_at) VALUES ('old','GET','{}',?)",
        (now_old,))
    c._conn.execute("INSERT INTO meta(key,value) VALUES ('reqcount_2020-01-01','9')")
    c._conn.commit()

    c._gc()   # directa: nueva fila sobrevive, vieja fuera
    assert c._cache_get("old") is None
    assert c._conn.execute(
        "SELECT COUNT(*) FROM meta WHERE key='reqcount_2020-01-01'").fetchone()[0] == 0

    c._cache_put("fresh", {"ok": True}, None, None)
    c._gc()
    assert c._cache_get("fresh") is not None
    c.close()


def test_indice_fetched_at_existe(db):
    rows = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='rpc_cache'"
    ).fetchall()
    assert any("idx_rpc_cache_fetched" == r[0] for r in rows)


def test_concurrencia_multiproceso_sin_busy_error(tmp_path):
    """8 procesos escribiendo simultáneamente a cache.db compartida:
    WAL + busy_timeout + retry ⇒ ningún SQLITE_BUSY sin absorber."""
    script = tmp_path / "worker.py"
    script.write_text(f"""
import sys, time
sys.path.insert(0, {json.dumps(str(Path(__file__).parent.parent))})
from src.aur.client import AURClient
c = AURClient(db_path={json.dumps(str(tmp_path / "shared.db"))})
for i in range(20):
    c._bump_counter()
    c._cache_put(f"{{time.time_ns()}}-{{i}}", {{"w": i}}, None, None)
c.close()
""")
    procs = [subprocess.Popen([sys.executable, str(script)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
             for _ in range(8)]
    errs = []
    for p in procs:
        _, e = p.communicate(timeout=120)
        if p.returncode != 0:
            errs.append(e.decode())
    assert not errs, f"workers fallaron:\n" + "\n".join(errs)


# ================================================================ H-1.2
VALID = """
pkgbase = foo
pkgver = 1.0
pkgrel = 1
arch = x86_64 any
source = https://example.com/foo.tar.gz
sha256sums = SKIP

pkgname = foo
depends = bar>=1.0
"""


def test_sanitizacion_control_char_rechazado():
    with pytest.raises(ValueError, match="control|manipulación"):
        parse_srcinfo(VALID.replace("foo.tar.gz", "fo\x00o.tar.gz"))


def test_sanitizacion_del_escape_x7f():
    with pytest.raises(ValueError, match="control"):
        parse_srcinfo("pkgbase = fo\x7fbar\npkgname = foobar\n")


def test_nombre_homoglifo_o_raro_rechazado():
    # cirílico 'о' en vez de 'o' latin
    bad = VALID.replace("pkgbase = foo", "pkgbase = fоо")  # noqa: RUF001
    with pytest.raises(ValueError, match="inválidos"):
        parse_srcinfo(bad)


def test_clave_desconocida_genera_warning_no_aborta():
    si = parse_srcinfo(VALID + "source_sparc = https://evil.example/x\n"
                                "weirdkey = val\n")
    assert any("source_sparc" in w for w in si.warnings)
    assert any("weirdkey" in w for w in si.warnings)
    # el resto se parseó bien
    assert "foo" in si.packages


def test_pkgver_invalido_rechazado():
    with pytest.raises(ValueError, match="pkgver"):
        parse_srcinfo("pkgbase = x\npkgver = 1.0; rm -rf /\npkgname = x\n")


def test_epoch_no_numerico_rechazado():
    with pytest.raises(ValueError, match="epoch"):
        parse_srcinfo("pkgbase = x\nepoch = dos\npkgname = x\n")


def test_limites_tamano(tmp_path):
    with pytest.raises(ValueError, match="límite|excede"):
        parse_srcinfo("pkgbase = x\n# " + "A" * (17 * 1024) + "\npkgname = x\n")


def test_demasiados_subpaquetes():
    text = "pkgbase = big\n"
    text += "".join(f"pkgname = p{i}\n" for i in range(250))
    with pytest.raises(ValueError, match="DoS"):
        parse_srcinfo(text)


# ------------------------------------------------------ fuzzer determinista
FUZZ_SEED = 20260823
FUZZ_ITERATIONS = 300


def _mutate(rng: random.Random, base: str) -> str:
    out = list(base)
    for _ in range(rng.randint(1, 6)):
        op = rng.randint(0, 5)
        pos = rng.randrange(len(out)) if out else 0
        if op == 0 and out:                      # borrar char
            del out[pos % len(out)]
        elif op == 1:                            # insertar char aleatorio
            pool = string.printable + "\x00\x01\x1b\x7fü漢"
            out.insert(pos % max(len(out), 1), rng.choice(pool))
        elif op == 2 and out:                    # sustituir
            out[pos % len(out)] = rng.choice(string.printable)
        elif op == 3:                            # truncar
            out = out[:pos]
        elif op == 4:                            # duplicar línea
            lines = base.splitlines()
            if lines:
                i = rng.randrange(len(lines))
                out = list(base + "\n" + lines[i])
    return "".join(out)


def test_fuzzer_parser_nunca_crashea_fuera_de_valueerror():
    """Propiedad: ante inputs arbitrarios mutados, el parser solo acepta o
    lanza ValueError limpio — jamás IndexError/KeyError/AttributeError/etc."""
    rng = random.Random(FUZZ_SEED)
    unexpected = []
    for i in range(FUZZ_ITERATIONS):
        blob = _mutate(rng, VALID)
        try:
            si = parse_srcinfo(blob)
        except ValueError:
            continue
        except Exception as e:                   # noqa: BLE001
            unexpected.append((i, type(e).__name__, str(e)[:80]))
            continue
        # aceptado: invariantes mínimas deben sostenerse
        assert si.pkgbase
        for pname, p in si.packages.items():
            assert p.pkgname == pname
    assert not unexpected, f"crashes inesperados: {unexpected}"


def test_fuzzer_parser_inputs_totalmente_aleatorios():
    rng = random.Random(FUZZ_SEED + 1)
    unexpected = []
    for i in range(150):
        blob = "".join(rng.choice(string.printable + "\x00\x7f")
                       for _ in range(rng.randint(0, 400)))
        try:
            parse_srcinfo(blob)
        except ValueError:
            continue
        except Exception as e:                   # noqa: BLE001
            unexpected.append((i, type(e).__name__, str(e)[:80]))
    assert not unexpected, f"crashes inesperados: {unexpected}"
