# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests Fase 5 madurez — firma y distribución.

H-5.1 servidor: timeout slowloris, guard anti-traversal por symlink, TLS 1.2+
H-5.2 redacción de secretos (--privkey) en logs y excepciones
"""
import functools
import http.server
import importlib.util
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def serve_repo_mod():
    spec = importlib.util.spec_from_file_location(
        "serve_repo", "scripts/serve-repo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ================================================================ H-5.2
def test_redact_cmd_oculta_privkey():
    from src.xbps.builder import redact_cmd
    cmd = ["xbps-rindex", "--privkey", "/etc/secreta/privkey.pem",
           "--sign", "/repo"]
    out = redact_cmd(cmd)
    assert "<redacted>" in out
    assert "/etc/secreta/privkey.pem" not in out
    assert "--sign" in out and "/repo" in out      # resto intacto


def test_builder_run_no_filtra_ruta_en_stdout_ni_error(monkeypatch, capsys):
    from src.xbps import builder

    def fake_run(cmd, check, env=None, timeout=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as ei:
        builder._run(["xbps-rindex", "--privkey", "/ruta/clave.pem", "--sign", "r"])
    captured = capsys.readouterr()
    # ni el print del comando ni la excepción exponen la ruta
    assert "/ruta/clave.pem" not in captured.out + captured.err
    assert "/ruta/clave.pem" not in str(ei.value)
    assert "<redacted>" in str(ei.value.cmd)


def test_sign_repo_pasa_por_run_con_timeout(monkeypatch):
    import src.xbps.signing as sg
    seen = {}

    def fake_run(cmd, check, env=None, timeout=None, **kw):
        seen.update(cmd=cmd, timeout=timeout)
        return subprocess.CompletedProcess(cmd, 0)

    from src.xbps import builder
    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    sg.sign_repo(Path("/repo"), Path("/claves/priv.pem"))
    assert seen["timeout"] == 300
    assert "--privkey" in seen["cmd"]


# ================================================================ H-5.1
@pytest.fixture(scope="module")
def serve_repo_mod():
    return serve_repo_module()


@pytest.fixture()
def server(tmp_path):
    """Servidor real en loopback con docroot temporal."""
    serve_repo_mod = serve_repo_module()
    (tmp_path / "repo.xbps").write_bytes(b"PAQUETE")
    (tmp_path / "secreto-fuera").write_text("TOPSECRET")
    outside = Path(subprocess.run(
        ["mktemp", "-d"], capture_output=True, text=True, timeout=10).stdout.strip())
    (outside / "privkey.pem").write_text("CLAVE-PRIVADA")
    (tmp_path / "fuga").symlink_to(outside / "privkey.pem")   # escapa del root
    (tmp_path / "alias.xbps").symlink_to(tmp_path / "repo.xbps")  # interno OK

    handler = functools.partial(serve_repo_mod.RepoHandler,
                                docroot=str(tmp_path))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()


def serve_repo_module():
    spec = importlib.util.spec_from_file_location(
        "serve_repo", "scripts/serve-repo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_servidor_sirve_paquete(server):
    base, docroot = server
    status, body = _get(f"{base}/repo.xbps")
    assert status == 200 and body == b"PAQUETE"


def test_servidor_bloquea_escape_symlink(server):
    base, docroot = server
    status, body = _get(f"{base}/fuga")            # symlink → fuera del root
    assert status == 404
    assert b"CLAVE-PRIVADA" not in body


def test_servidor_permite_symlink_interno(server):
    base, _ = server
    status, body = _get(f"{base}/alias.xbps")      # dentro del root: legítimo
    assert status == 200 and body == b"PAQUETE"


def test_servidor_handler_tiene_timeout():
    assert serve_repo_module().RepoHandler.timeout == 30


def test_servidor_tls_minimo_12_en_fuente():
    src = open("scripts/serve-repo.py").read()
    assert "TLSv1_2" in src
    assert "relative_to" in src                    # guard anti-traversal
