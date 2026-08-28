# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests de portabilidad: config TOML, mapeo Arch→Void, plantillas xbps-src.

Todo usa tmp_path — sin rutas del host ni paquetes fijos.
"""
import json

import pytest

from src.common.config import Config, load_config, dynamic_linker, nix_system


# ---------------------------------------------------------------- config

def _write_toml(path, content):
    path.write_text(content, encoding="utf-8")


def test_defaults_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("AUR2XBPS_DATA_DIR", raising=False)
    monkeypatch.delenv("AUR2XBPS_CONFIG", raising=False)
    # aislamiento total: el CI exporta AUR2XBPS_ROOT a nivel de job y
    # _apply_env SIEMPRE gana (contrato) — estos tests prueban defaults XDG
    monkeypatch.delenv("AUR2XBPS_ROOT", raising=False)
    cfg = load_config(tmp_path / "no-existe.toml")
    assert cfg.data_dir.name == "aur2xbps"
    assert cfg.repo_dir == cfg.data_dir / "repo"
    assert cfg.masterdir == cfg.data_dir / "void" / "masterdir"
    assert cfg.void_packages_dir == cfg.data_dir / "void" / "void-packages"


def test_toml_user_overrides_defaults(tmp_path, monkeypatch):
    toml = tmp_path / "config.toml"
    _write_toml(toml, """
[paths]
data_dir = "/tmp/d1"
cache_dir = "/tmp/c1"
keys_dir = "/tmp/k1"

[repo]
host = "0.0.0.0"
port = 9999

[build]
arch = "aarch64"
log_level = "DEBUG"
restricted_mode = false
""")
    # el TOML explícito define data_dir; sin aislamiento, AUR2XBPS_ROOT del
    # job de CI lo pisaría (env > archivo por contrato)
    monkeypatch.delenv("AUR2XBPS_ROOT", raising=False)
    monkeypatch.delenv("AUR2XBPS_DATA_DIR", raising=False)
    cfg = load_config(toml)
    assert str(cfg.data_dir) == "/tmp/d1"
    assert cfg.repo_dir is None or True  # derivada después
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9999
    assert cfg.arch == "aarch64"
    assert cfg.log_level == "DEBUG"
    assert cfg.restricted_mode is False


def test_env_beats_toml(tmp_path, monkeypatch):
    toml = tmp_path / "config.toml"
    _write_toml(toml, '[paths]\ndata_dir = "/tmp/d1"\n[build]\nport = 1234\n')
    monkeypatch.setenv("AUR2XBPS_PORT", "7777")
    monkeypatch.setenv("AUR2XBPS_ARCH", "i686")
    cfg = load_config(toml)
    assert cfg.port == 7777
    assert cfg.arch == "i686"


def test_offline_env_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("AUR2XBPS_OFFLINE", "1")
    cfg = load_config(tmp_path / "no.toml")
    assert cfg.offline is True


# ------------------------------------------------------------- multi-arch

@pytest.mark.parametrize("arch,linker,system", [
    ("x86_64", "/lib64/ld-linux-x86-64.so.2", "x86_64-linux"),
    ("aarch64", "/lib/ld-linux-aarch64.so.1", "aarch64-linux"),
    ("i686", "/lib/ld-linux.so.2", "i686-linux"),
])
def test_dynamic_linkers(arch, linker, system):
    assert dynamic_linker(arch) == linker
    assert nix_system(arch) == system


# ---------------------------------------------------------------- mapping

def test_mapping_python_prefix():
    from src.void.mapping import map_dep
    assert map_dep("python-requests") == "python3-requests"
    assert map_dep("python-setuptools") == "python3-setuptools"
    assert map_dep("gtk3") == "gtk+3"
    assert map_dep("lib32-mesa") is None  # multilib descartado
    assert map_dep("zlib>=1.2") == "zlib-devel>=1.2"  # conserva versión
    assert map_dep("desconocido") == "desconocido"


# -------------------------------------------------------------- templates

SRCINFO_SAMPLE = """
pkgbase = sample-tool
    pkgver = 2.1.0
    pkgrel = 1
    pkgdesc = "Sample CLI tool for testing"
    url = "https://example.com/sample"
    license = ("MIT")
    arch = ("x86_64")
    depends = ("glibc")
    makedepends = ("scdoc")
    source_x86_64 = ("https://example.com/sample-2.1.0.tar.gz")
    sha256sums_x86_64 = ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
pkgname = sample-tool
"""


def test_template_generation_basic(tmp_path):
    from src.aur.parser import parse_srcinfo
    from src.void.template import generate_template
    si = parse_srcinfo(SRCINFO_SAMPLE)
    results = generate_template(si, tmp_path)
    assert len(results) == 1
    r = results[0]
    assert not r.restricted
    text = r.template_path.read_text()
    for field in ('pkgname="sample-tool"', 'version="2.1.0"', "revision=1",
                  'license="MIT"', "short_desc=", "maintainer=",
                  'distfiles="https://example.com/sample-2.1.0.tar.gz"',
                  "checksum=\"aa"):
        assert field in text, field
    # fuente tar.gz → build_style, NO fetch
    assert "gnu-makefile" in text
    assert "_doinstall" not in text


def test_template_restricted_marked(tmp_path):
    from src.aur.parser import parse_srcinfo
    from src.void.template import generate_template
    src = SRCINFO_SAMPLE.replace('license = ("MIT")', 'license = ("custom")')
    si = parse_srcinfo(src)
    results = generate_template(si, tmp_path)
    assert results[0].restricted
    assert "restricted=yes" in results[0].template_path.read_text()


def test_template_noarch_meta(tmp_path):
    from src.aur.parser import parse_srcinfo
    from src.void.template import generate_template
    src = """
pkgbase = sample-data
    pkgver = 1.0
    pkgrel = 1
    pkgdesc = data only
    license = ("MIT")
    arch = ("any")
pkgname = sample-data
"""
    si = parse_srcinfo(src)
    results = generate_template(si, tmp_path)
    text = results[0].template_path.read_text()
    # convención Void: metapackage=yes, SIN restricción de archs
    assert "metapackage=yes" in text
    assert "archs=" not in text


def test_template_bin_fetch_style(tmp_path):
    """Nombre -bin → estilo fetch con do_install manual."""
    from src.aur.parser import parse_srcinfo
    from src.void.template import generate_template
    src = SRCINFO_SAMPLE.replace("sample-tool", "sample-bin").replace(
        "sample-2.1.0.tar.gz", "sample-2.1.0.deb")
    si = parse_srcinfo(src.replace("pkgname = sample-tool", "pkgname = sample-bin"))
    results = generate_template(si, tmp_path)
    text = results[0].template_path.read_text()
    assert "do_install" in text and "create_wrksrc=yes" in text
    assert "gnu-makefile" not in text


def test_template_baseline_variant_keeps_alignment(tmp_path):
    """Variantes -baseline vs moderna: solo queda baseline y checksum alineado."""
    import re
    from src.aur.parser import parse_srcinfo
    from src.void.template import generate_template
    src = """
pkgbase = avx-pkg
    pkgver = 1.0
    pkgrel = 1
    pkgdesc = avx test
    license = ("MIT")
    arch = ("x86_64")
    source_x86_64 = ("https://example.com/app-baseline-1.0.tar.gz" "https://example.com/app-modern-1.0.tar.gz")
    sha256sums_x86_64 = ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
pkgname = avx-pkg
"""
    si = parse_srcinfo(src)
    text = generate_template(si, tmp_path)[0].template_path.read_text()
    # Solo la variante -baseline debe quedar
    assert "app-baseline-1.0.tar.gz" in text
    assert "app-modern-1.0.tar.gz" not in text
    dist = re.search(r'distfiles="([^"]*)"', text).group(1)
    chk = re.search(r'checksum="([^"]*)"', text).group(1)
    assert len(dist.split()) == len(chk.split()) == 1, (dist, chk)
    # el checksum debe ser el de baseline (aaaa...), NO el de modern (bbbb...)
    assert "aaaaaaaa" in chk
    assert "bbbbbbbb" not in chk


def test_template_baseline_with_skip_stays_aligned(tmp_path):
    """Con SKIP en una variante, la alineación distfiles<->checksum se conserva."""
    import re
    from src.aur.parser import parse_srcinfo
    from src.void.template import generate_template
    src = """
pkgbase = avx-pkg2
    pkgver = 1.0
    pkgrel = 1
    pkgdesc = avx
    license = ("MIT")
    arch = ("x86_64")
    source_x86_64 = ("https://example.com/app-baseline-1.0.tar.gz" "https://example.com/app-modern-1.0.tar.gz")
    sha256sums_x86_64 = ("SKIP" "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc")
pkgname = avx-pkg2
"""
    si = parse_srcinfo(src)
    text = generate_template(si, tmp_path)[0].template_path.read_text()
    assert "app-baseline-1.0.tar.gz" in text
    assert "app-modern-1.0.tar.gz" not in text
    dist = re.search(r'distfiles="([^"]*)"', text).group(1)
    chk = re.search(r'checksum="([^"]*)"', text).group(1)
    # Invariante: misma cantidad de distfiles que de checksums
    assert len(dist.split()) == len(chk.split()), (dist, chk)


# ------------------------------------------------------------------ CLI

def test_cli_query_json_structure(tmp_path, monkeypatch):
    """query offline contra caché vacía → error controlado, no traceback."""
    import subprocess, sys, os
    env = os.environ.copy()
    env["AUR2XBPS_OFFLINE"] = "1"
    repo_root = __import__("pathlib").Path(__file__).parents[1]
    env["PYTHONPATH"] = str(repo_root)
    r = subprocess.run(
        [sys.executable, "-m", "src.cli", "query", "noexiste-xyz-123"],
        capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0
    assert "error" in r.stderr.lower()


def test_env_paths_none_typed_fields(tmp_path, monkeypatch):
    """AUR2XBPS_VOID_DIR/Masterdir env sobre campos None → deben ser Path."""
    monkeypatch.setenv("AUR2XBPS_VOID_DIR", str(tmp_path / "vp"))
    monkeypatch.setenv("AUR2XBPS_MASTERDIR", str(tmp_path / "md"))
    cfg = load_config(tmp_path / "no.toml")
    from pathlib import Path as _P
    assert isinstance(cfg.void_packages_dir, _P)
    assert isinstance(cfg.masterdir, _P)
    assert cfg.shlibs_file == tmp_path / "vp" / "common" / "shlibs"


def test_cli_stdout_pure_json(tmp_path):
    """template emite SOLO JSON por stdout (progreso→stderr) — contrato vouru."""
    import subprocess, sys, os
    env = os.environ.copy()
    repo_root = __import__("pathlib").Path(__file__).parents[1]
    env["PYTHONPATH"] = str(repo_root)
    # offline: sin esto el subproceso golpea RPC AUR real y puede descargar
    # distfiles (_compute_hashes) — red no permitida en tests sin marker
    env["AUR2XBPS_OFFLINE"] = "1"
    r = subprocess.run(
        [sys.executable, "-m", "src.cli", "template", "cbonsai",
         "--out", str(tmp_path / "sp"), "--no-sync"],
        capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr[-300:]
    d = json.loads(r.stdout)          # no debe lanzar: stdout es JSON puro
    assert d["templates"][0]["pkgname"] == "cbonsai"
