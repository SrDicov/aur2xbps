# SPDX-License-Identifier: GPL-3.0-or-later
"""Elevador universal de privilegios (src.common.priv)."""
import shlex
import subprocess

import pytest

from src.common import priv


@pytest.fixture(autouse=True)
def _no_root_no_env(monkeypatch):
    monkeypatch.setattr(priv.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.delenv("AUR2XBPS_PRIV", raising=False)
    priv.reset_state()
    yield
    priv.reset_state()


def test_root_no_envuelve(monkeypatch):
    monkeypatch.setattr(priv.os, "getuid", lambda: 0, raising=False)
    assert priv.priv_wrap(["chroot", "/m", "id"]) == ["chroot", "/m", "id"]


def test_env_override_multi_token(monkeypatch):
    monkeypatch.setenv("AUR2XBPS_PRIV", "doas -u root")
    assert priv.priv_wrap(["id"]) == ["doas", "-u", "root", "id"]


def test_orden_autodeteccion(monkeypatch):
    which = {"sudo": None, "doas": "/usr/bin/doas", "run0": None,
             "pkexec": None, "su": "/usr/bin/su"}
    monkeypatch.setattr(priv.shutil, "which",
                        lambda n: which.get(n), raising=False)
    assert priv.detect() == ["doas"]


def test_pkexec_ruta_absoluta(monkeypatch):
    monkeypatch.setattr(priv.shutil, "which",
                        lambda n: "/usr/bin/pkexec" if n == "pkexec"
                        else ("/usr/bin/env" if n == "env" else None),
                        raising=False)
    out = priv.priv_wrap(["env", "FOO=1", "id"])
    assert out == ["pkexec", "/usr/bin/env", "FOO=1", "id"]


def test_su_ultimo_recurso_con_warning(monkeypatch, capsys):
    which = {t: None for t in priv.DETECT_ORDER}
    which["su"] = "/usr/bin/su"
    monkeypatch.setattr(priv.shutil, "which",
                        lambda n: which.get(n), raising=False)
    argv = priv.priv_wrap(["sh", "-c", 'echo "hola mundo"'])
    err = capsys.readouterr().err
    assert "WARNING" in err and "su" in err
    assert argv[0] == "su" and argv[1] == "root" and argv[2] == "-c"
    # el string-shell preserva el comando original al re-parsear
    assert shlex.split(argv[3]) == ["sh", "-c", 'echo "hola mundo"']


def test_warning_su_una_sola_vez(monkeypatch):
    which = {t: None for t in priv.DETECT_ORDER}
    which["su"] = "/usr/bin/su"
    monkeypatch.setattr(priv.shutil, "which",
                        lambda n: which.get(n), raising=False)
    priv.priv_wrap(["true"])
    priv.reset_state()
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        priv.priv_wrap(["true"])
        priv.priv_wrap(["true"])
    assert buf.getvalue().count("WARNING") == 1


def test_error_sin_elevador(monkeypatch):
    monkeypatch.setattr(priv.shutil, "which", lambda n: None, raising=False)
    with pytest.raises(FileNotFoundError):
        priv.priv_wrap(["true"])


def test_env_vacio_tras_shlex_falla(monkeypatch):
    monkeypatch.setenv("AUR2XBPS_PRIV", '""')
    with pytest.raises(ValueError):
        priv.priv_wrap(["true"])


def test_sudo_prefix_rechaza_shell_shape(monkeypatch):
    from src.common import tools
    which = {t: None for t in priv.DETECT_ORDER}
    which["su"] = "/usr/bin/su"
    monkeypatch.setattr(priv.shutil, "which",
                        lambda n: which.get(n), raising=False)
    with pytest.raises(RuntimeError):
        tools.sudo_prefix()


def test_sudo_prefix_compat_prefijo(monkeypatch):
    from src.common import tools
    monkeypatch.setenv("AUR2XBPS_PRIV", "sudo")
    assert tools.sudo_prefix() == ["sudo"]
