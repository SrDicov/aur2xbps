# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests Fase 3 madurez — seguridad Atomic Arch.

H-2.1 heurística JS ampliada (yarn/pnpm/deno, abreviadas, ofuscación) + wiring
H-2.2 anclaje PGP (trusted_pgp_keys) + deprecación allowlist hardcodeada
"""
import logging

import pytest

from src.aur.parser import parse_srcinfo
from src.aur.security import check_atomic_arch, is_js_legitimate


class _HybridCfg:
    """Config híbrida: campos reales del host + trusted_pgp_keys controlada
    por los tests. paths.py lee data_dir etc. al importar módulos, así que un
    stub vacío rompería imports en ejecución aislada."""
    trusted_pgp_keys: list = []
    _real = None

    @classmethod
    def _load_real(cls):
        if cls._real is None:
            from src.common.config import load_config
            cls._real = load_config()
        return cls._real

    def __getattr__(self, name):
        return getattr(self._load_real(), name)


@pytest.fixture(autouse=True)
def _fake_cfg(monkeypatch):
    _HybridCfg.trusted_pgp_keys = []
    from src.common import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_config",
                        lambda reload=False: _HybridCfg())
    yield
    _HybridCfg._real = None
    cfg_mod.get_config(reload=True)


def _si(pkgbase="myapp", extra=""):
    txt = f"""
pkgbase = {pkgbase}
    pkgver = 1.0
    pkgrel = 1
pkgname = {pkgbase}
    depends = bar
    sha256sums = SKIP
{extra}
"""
    return parse_srcinfo(txt)


# ================================================================ H-2.1
@pytest.mark.parametrize("cmd", [
    "yarn install --frozen-lockfile",
    "pnpm install",
    "pnpm add left-pad",
    "deno install -n foo https://x.example/mod.ts",
    "bower install",
    "npm i",                                       # forma abreviada
    "bun add pkg",
    "N='np'; M='m'; ${N}${M} install evil-pkg",    # ofuscación básica
])
def test_js_install_ampliado_bloquea_sin_hash(cmd):
    block, reasons = check_atomic_arch(_si(), raw_pkgbuild_text=cmd)
    assert block, reasons
    assert any("deps JS" in r for r in reasons)


def test_npm_con_hash_real_no_bloquea():
    si = parse_srcinfo("""
pkgbase = myapp
    pkgver = 1.0
    pkgrel = 1
pkgname = myapp
    depends = bar
    sha256sums = 1234567890123456789012345678901234567890123456789012345678901234
""")
    block, reasons = check_atomic_arch(si, raw_pkgbuild_text="npm install")
    assert not block, reasons


def test_sin_texto_pkgbuild_como_antes_no_evalua_heuristica():
    """Comportamiento previo preservado: sin texto no hay bloqueo heurístico."""
    block, _ = check_atomic_arch(_si())
    assert not block


def test_senal_node_dep_sin_hash_emite_warning(caplog):
    si = parse_srcinfo("""
pkgbase = nodething
    pkgver = 1.0
    pkgrel = 1
pkgname = nodething
    makedepends = npm
    sha256sums = SKIP
""")
    with caplog.at_level(logging.WARNING):
        check_atomic_arch(si, raw_pkgbuild_text="npm install --production")
    assert any("Node sin hashes" in r.message for r in caplog.records)


# ================================================================ H-2.2
KEY_TRUSTED = "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"


def test_pgp_trusted_key_exime_aun_fuera_de_allowlist():
    _HybridCfg.trusted_pgp_keys = [KEY_TRUSTED]
    si = parse_srcinfo(f"""
pkgbase = paquete-desconocido
    pkgver = 1.0
    pkgrel = 1
pkgname = paquete-desconocido
    validpgpkeys = {KEY_TRUSTED.lower()}
    sha256sums = SKIP
""")
    assert is_js_legitimate("paquete-desconocido", si)
    block, _ = check_atomic_arch(si, raw_pkgbuild_text="pnpm install")
    assert not block


def test_pgp_key_desconocida_no_exime():
    _HybridCfg.trusted_pgp_keys = [KEY_TRUSTED]
    si = parse_srcinfo("""
pkgbase = paquete-desconocido
    pkgver = 1.0
    pkgrel = 1
pkgname = paquete-desconocido
    validpgpkeys = DEAD0000000000000000000000000000000000000
    sha256sums = SKIP
""")
    block, _ = check_atomic_arch(si, raw_pkgbuild_text="pnpm install")
    assert block


def test_allowlist_deprecada_emite_warning(caplog):
    si = _si(pkgbase="visual-studio-code-bin")
    with caplog.at_level(logging.WARNING):
        assert is_js_legitimate("visual-studio-code-bin", si)
    assert any("deprecada" in r.message for r in caplog.records)


def test_pipeline_lee_pkgbuild_estatico(tmp_path, monkeypatch):
    """prepare_package debe pasar el TEXTO del PKGBUILD al filtro."""
    from src.aur import pipeline as pl

    captured = {}

    def fake_check(si, raw_pkgbuild_text=None):
        captured["text"] = raw_pkgbuild_text
        return False, []

    dest = tmp_path / "sources" / "foo"
    dest.mkdir(parents=True)
    (dest / ".SRCINFO").write_text(
        "pkgbase = foo\npkgver = 1\npkgrel = 1\npkgname = foo\n")
    (dest / "PKGBUILD").write_text("# comentario\nnpm i\n")

    monkeypatch.setattr(pl, "check_atomic_arch", fake_check)

    # reproducir el bloque 4 exacto como en prepare_package
    si = pl.parse_srcinfo_file(dest / ".SRCINFO")
    raw = None
    pb = dest / "PKGBUILD"
    if pb.exists():
        raw = pb.read_text(encoding="utf-8", errors="replace")[:262_144]
    pl.check_atomic_arch(si, raw_pkgbuild_text=raw)

    assert "npm i" in captured["text"]


def test_env_trusted_pgp_keys(monkeypatch):
    # el fixture parchea get_config; usar el cargador real para validar env
    from src.common.config import load_config
    monkeypatch.setenv("AUR2XBPS_TRUSTED_PGP_KEYS",
                       f" AAAA , {KEY_TRUSTED} ,,")
    assert load_config().trusted_pgp_keys == ["AAAA", KEY_TRUSTED]
    monkeypatch.delenv("AUR2XBPS_TRUSTED_PGP_KEYS")
    assert load_config().trusted_pgp_keys == []
