# SPDX-License-Identifier: GPL-3.0-or-later
"""service-install.sh: ramas runit/dinit (usuario) y uninstall, aislados
en HOME temporal. Sin tocar /etc del host real."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "service-install.sh"


def _run(tmp_home: Path, *args, force_init: str) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(tmp_home),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "XDG_CONFIG_HOME": str(tmp_home / ".config"),
        "XDG_DATA_HOME": str(tmp_home / ".local/share"),
        "AUR2XBPS_ROOT": str(tmp_home / "ws"),
        "AUR2XBPS_FORCE_INIT": force_init,
    }
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=60, env=env)


@pytest.fixture
def fake_home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_runit_usuario(fake_home):
    r = _run(fake_home, force_init="runit")
    assert r.returncode == 0, r.stderr
    sv = fake_home / ".config/aur2xbps/runit/aur2xbps-repo/run"
    wrap = fake_home / ".local/bin/aur2xbps-serve-repo"
    assert sv.is_file() and wrap.is_file()
    body = sv.read_text()
    assert "exec" in body and str(wrap) in body
    assert "#!/bin/sh" in body


def test_dinit_usuario(fake_home):
    r = _run(fake_home, force_init="dinit")
    assert r.returncode == 0, r.stderr
    svc = fake_home / ".config/dinit.d/aur2xbps-repo"
    wrap = fake_home / ".local/bin/aur2xbps-serve-repo"
    assert svc.is_file() and wrap.is_file()
    body = svc.read_text()
    assert "type = process" in body and f"command = {wrap}" in body


def test_uninstall_limpia_ambos(fake_home):
    assert _run(fake_home, force_init="runit").returncode == 0
    r = _run(fake_home, "--uninstall", force_init="runit")
    assert r.returncode == 0, r.stderr
    assert not (fake_home / ".config/aur2xbps/runit/aur2xbps-repo").exists()


def test_arg_invalido(fake_home):
    r = _run(fake_home, "--no-existe", force_init="runit")
    assert r.returncode == 2
