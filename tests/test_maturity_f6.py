# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests Fase 6 madurez — harness mass-validate (funciones puras)."""
import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def mv():
    """Carga scripts/mass-validate.py como módulo (no es paquete)."""
    import sys
    spec = importlib.util.spec_from_file_location(
        "mass_validate", "scripts/mass-validate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mass_validate"] = mod   # requerido por @dataclass en el script
    spec.loader.exec_module(mod)
    return mod


def test_parse_json_multilinea(mv):
    out = '{\n  "engine": "xbps-src",\n  "ok": true,\n  "binpkgs": ["/a.xbps"]\n}'
    p = mv._parse_json_out(out)
    assert p == {"engine": "xbps-src", "ok": True,
                 "binpkgs": ["/a.xbps"]}


def test_parse_json_con_ruido_previo(mv):
    out = 'progreso...\nmás líneas\n{"ok": false}\n'
    assert mv._parse_json_out(out) == {"ok": False}


def test_parse_json_vacio_o_malo(mv):
    assert mv._parse_json_out("") is None
    assert mv._parse_json_out("sin llaves") is None


def _make_xbps(tmp_path: Path, entries: dict) -> Path:
    """Crea un mini .xbps real (tar+pipe zstd -T1 como el empaquetador)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in entries.items():
            data = content.encode()
            ti = tarfile.TarInfo("./" + name)
            ti.size = len(data)
            ti.mode = 0o755
            tf.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    xbps = tmp_path / "fake-1.0_1.x86_64.xbps"
    subprocess.run(["zstd", "-T1", "-3", "-q", "-o", str(xbps)],
                   input=raw, check=True, timeout=30)
    return xbps


def test_smoke_extrae_y_ejecuta_binario(mv, tmp_path):
    xbps = _make_xbps(tmp_path, {
        "usr/bin/hello": "#!/bin/sh\necho hello-1.0\n",
        "usr/lib/libfoo.so": "\x7fELF-fake",
    })
    r = mv.smoke_functional(xbps, timeout=15)
    assert r["smoke"] is True, r
    assert r["bin"] == "hello"


def test_smoke_sigue_symlink_usr_bin(mv, tmp_path):
    # paquete estilo bundle: binario real en lib, enlace en usr/bin
    xbps = _make_xbps(tmp_path, {
        "usr/bin/yazi": "../lib/yazi/x",   # symlink roto en tar plano: sin efecto
        "usr/bin/tool": "#!/bin/sh\necho ok\n",
    })
    r = mv.smoke_functional(xbps, timeout=15)
    # el symlink ../lib no existe tras extraer → se ignora; tool corre
    assert r["smoke"] is True and r["bin"] == "tool"


def test_smoke_sin_binarios(mv, tmp_path):
    xbps = _make_xbps(tmp_path, {"usr/share/doc/readme": "hola"})
    r = mv.smoke_functional(xbps, timeout=15)
    assert r["smoke"] is False and r["reason"] == "sin_binarios_usr_bin"


# ------------------------------------------------------ elevador privilegios
def test_sudo_prefix_detecta_doas(monkeypatch):
    from src.common import tools
    monkeypatch.delenv("AUR2XBPS_PRIV", raising=False)
    # sin sudo en PATH → cae a doas (Void)
    def fake_which(name):
        return "/usr/bin/doas" if name == "doas" else None

    monkeypatch.setattr("shutil.which", fake_which)
    assert tools.sudo_prefix() == ["doas"]

    # override explícito por env gana; pkexec NO es prefijo (shape propia,
    # ver tests/test_priv.py) → override con forma prefijo válida
    monkeypatch.setenv("AUR2XBPS_PRIV", "sudo -u root")
    assert tools.sudo_prefix() == ["sudo", "-u", "root"]


def test_sudo_prefix_error_claro_sin_elevador(monkeypatch):
    from src.common import tools
    monkeypatch.delenv("AUR2XBPS_PRIV", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        tools.sudo_prefix()
