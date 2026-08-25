# SPDX-License-Identifier: GPL-3.0-or-later
"""Dimensión libc objetivo (glibc|musl): detección, arch XBPS, intérprete,
deps base y masterdir por sabor."""
import subprocess
from pathlib import Path

import pytest

from src.common import config


# ------------------------------------------------------------ detección

def test_detect_libc_env_gana(monkeypatch):
    monkeypatch.setenv("AUR2XBPS_LIBC", "musl")
    assert config.detect_libc() == "musl"
    monkeypatch.setenv("AUR2XBPS_LIBC", "GLIBC")
    assert config.detect_libc() == "glibc"
    monkeypatch.setenv("AUR2XBPS_LIBC", "auto")
    monkeypatch.setattr(config, "_probe_libc", lambda: "glibc")
    assert config.detect_libc() == "glibc"


def test_probe_ldd_musl_vs_glibc(monkeypatch):
    monkeypatch.delenv("AUR2XBPS_LIBC", raising=False)

    def fake_run_musl(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="musl libc (x86_64)\nVersion 1.2.5", stderr="")

    def fake_run_gnu(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="ldd (GNU libc) 2.41\n", stderr="")

    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda n: "/usr/bin/ldd" if n == "ldd" else None)
    monkeypatch.setattr(subprocess, "run", fake_run_musl)
    assert config._probe_libc() == "musl"
    monkeypatch.setattr(subprocess, "run", fake_run_gnu)
    assert config._probe_libc() == "glibc"


def test_probe_sin_ldd_default_glibc(monkeypatch):
    import shutil as _sh
    monkeypatch.delenv("AUR2XBPS_LIBC", raising=False)
    monkeypatch.setattr(_sh, "which", lambda n: None)
    assert config._probe_libc() == "glibc"


def test_effective_libc_resuelve_auto():
    cfg = type("C", (), {"libc": "auto"})()
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(config, "detect_libc", lambda: "musl")
        assert config.effective_libc(cfg) == "musl"
        cfg.libc = "glibc"
        assert config.effective_libc(cfg) == "glibc"
    finally:
        monkey.undo()


# ------------------------------------------------------ arch + intérprete

@pytest.mark.parametrize("arch,musl_interp", [
    ("x86_64", "/lib/ld-musl-x86_64.so.1"),
    ("aarch64", "/lib/ld-musl-aarch64.so.1"),
])
def test_dynamic_linker_matriz(arch, musl_interp, monkeypatch):
    monkeypatch.setenv("AUR2XBPS_LIBC", "glibc")
    glibc = config.dynamic_linker(arch, "glibc")
    assert "musl" not in glibc
    assert config.dynamic_linker(arch, "musl") == musl_interp


def test_xbps_arch_sufijo(monkeypatch):
    monkeypatch.setenv("AUR2XBPS_ARCH", "x86_64")
    monkeypatch.setenv("AUR2XBPS_LIBC", "glibc")
    assert config.xbps_arch() == "x86_64"
    monkeypatch.setenv("AUR2XBPS_LIBC", "musl")
    assert config.xbps_arch() == "x86_64-musl"
    assert config.xbps_arch("aarch64") == "aarch64-musl"


def test_void_interp_sigue_config(tmp_path, monkeypatch):
    from src.xbps import pipeline
    monkeypatch.setenv("AUR2XBPS_LIBC", "musl")
    assert pipeline.VOID_INTERP().startswith("/lib/ld-musl-")
    monkeypatch.setenv("AUR2XBPS_LIBC", "glibc")
    assert "musl" not in pipeline.VOID_INTERP()


# ---------------------------------------------------------- deps base

def test_base_dep_parse_masterdir(monkeypatch):
    from src.xbps import pipeline
    monkeypatch.setenv("AUR2XBPS_LIBC", "glibc")
    monkeypatch.setattr(pipeline, "_srun",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0, stdout="glibc-2.42_1\n", stderr=""))
    assert pipeline._base_dep() == "glibc>=2.42_1"


def test_base_dep_floor_sin_query(monkeypatch):
    from src.xbps import pipeline
    monkeypatch.setenv("AUR2XBPS_LIBC", "musl")
    monkeypatch.setattr(pipeline, "_srun",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="x"))
    assert pipeline._base_dep() == "musl>=1.2.5"


# ------------------------------------------------- generator y masterdir

def test_generator_base_nix_input(monkeypatch):
    from src.nix import generator
    monkeypatch.setenv("AUR2XBPS_LIBC", "musl")
    assert generator._base_nix_input() == "musl"
    monkeypatch.setenv("AUR2XBPS_LIBC", "glibc")
    assert generator._base_nix_input() == "glibc"


def test_masterdir_rebootstrap_por_sabor(tmp_path):
    from src.cli import _masterdir_needs_rebootstrap
    md = tmp_path / "masterdir"
    # sin masterdir → nunca wipe (bootstrap normal lo crea)
    assert _masterdir_needs_rebootstrap(md, "musl") is False
    # legado sin marca → se asume glibc
    (md / "etc").mkdir(parents=True)
    assert _masterdir_needs_rebootstrap(md, "glibc") is False
    assert _masterdir_needs_rebootstrap(md, "musl") is True
    # marcador presente decide
    (md / ".aur2xbps-libc").write_text("musl\n")
    assert _masterdir_needs_rebootstrap(md, "glibc") is True
    assert _masterdir_needs_rebootstrap(md, "musl") is False
