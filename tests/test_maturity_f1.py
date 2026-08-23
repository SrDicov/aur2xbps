# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests Fase 1 madurez: bugs duros del AUDIT-2026-08.

T-3   arch hardcode en build_with_hash_fix
H-4.1 oráculo post-patchelf (verify_patched_elf)
T-6   timeouts de subprocess + config.build_timeout
X-2   _StderrOnly restaura fds ante éxito y excepción
"""
import subprocess
import sys

import pytest

from src.cli import _StderrOnly


@pytest.fixture()
def clean_cfg_env(monkeypatch):
    """Aísla y restaura el cache global de configuración."""
    monkeypatch.delenv("AUR2XBPS_BUILD_TIMEOUT", raising=False)
    yield
    from src.common.config import get_config
    get_config(reload=True)


# ---------------------------------------------------------------- T-3
def test_build_attr_usa_nix_system_de_config(monkeypatch, tmp_path):
    """El attr de nix build debe derivar de nix_system(cfg.arch), jamás
    hardcodear x86_64-linux."""
    from src.nix import generator

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(generator.subprocess, "run", fake_run)

    class FakeCfg:
        arch = "aarch64"

    # generator importa get_config directamente: parchear SU referencia
    monkeypatch.setattr(generator, "get_config", lambda: FakeCfg())

    ok, _ = generator.build_with_hash_fix(tmp_path, "testpkg", max_retries=0)
    assert not ok
    assert ".#packages.aarch64-linux.testpkg" in " ".join(captured["cmd"])


def test_generator_no_hardcodea_x86_64_en_build():
    src = open("src/nix/generator.py").read()
    seg = src[src.index("def build_with_hash_fix"):src.index("HASH_MISMATCH_RE")]
    assert "x86_64-linux" not in seg


# ---------------------------------------------------------------- H-4.1
def test_oraculo_ldd_rechaza_elf_corrupto(tmp_path):
    """Un fichero no-ELF debe hacer fallar el oráculo (ldd exit != 0):
    así se detecta corrupción de patchelf antes de empaquetar."""
    from src.xbps.pipeline import verify_patched_elf

    f = tmp_path / "corrupto"
    f.write_bytes(b"\x7fELF-corrupto-no-es-un-binario-valido" * 10)
    with pytest.raises(RuntimeError, match="corrupto|ldd"):
        verify_patched_elf(str(f))


def test_oraculo_ldd_acepta_binario_sano():
    from src.xbps.pipeline import verify_patched_elf

    verify_patched_elf("/bin/sh")   # dash dinámico: ldd exit 0


# ---------------------------------------------------------------- T-6
def test_build_timeout_default_y_override(monkeypatch, clean_cfg_env):
    from src.common.config import get_config

    cfg = get_config(reload=True)
    assert isinstance(cfg.build_timeout, int) and cfg.build_timeout >= 60

    monkeypatch.setenv("AUR2XBPS_BUILD_TIMEOUT", "123")
    assert get_config(reload=True).build_timeout == 123


def test_builder_run_propaga_timeout(monkeypatch):
    from src.xbps import builder

    seen = {}

    def fake_run(cmd, check, env=None, timeout=None):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    builder._run(["true"], timeout=42)
    assert seen["timeout"] == 42


def test_shlibs_gitpull_con_techo():
    src = open("src/xbps/shlibs.py").read()
    assert 'subprocess.run(["git", "-C", str(vp), "pull", "--ff-only"],\n                           check=False, timeout=30)' in src


def test_determinize_zstd_con_techo():
    src = open("src/xbps/determinize.py").read()
    assert "communicate(fixed, timeout=300)" in src
    assert 'check=True, timeout=600' in src


def test_cli_xbps_src_con_techo_config():
    src = open("src/cli.py").read()
    assert "timeout=cfg.build_timeout" in src
    assert src.count("timeout=cfg.build_timeout") >= 2   # bootstrap + pkg


# ---------------------------------------------------------------- X-2
def test_stderrouly_restaura_en_exito():
    stdout_original = sys.stdout
    with _StderrOnly():
        assert sys.stdout is sys.stderr       # redirigido dentro
    assert sys.stdout is stdout_original      # restaurado fuera
    import os
    os.write(1, b"")                          # fd1 sigue válido


def test_stderrouly_restaura_con_excepcion():
    stdout_original = sys.stdout
    with pytest.raises(ValueError):
        with _StderrOnly():
            raise ValueError("boom")
    assert sys.stdout is stdout_original
    import os
    os.write(1, b"")
