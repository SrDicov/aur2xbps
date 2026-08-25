# SPDX-License-Identifier: GPL-3.0-or-later
"""Filtro Void-musl: descarte de precompilados AUR (glibc upstream)."""
import pytest

from src.common.binpkg import is_precompiled
from src.aur.pipeline import _musl_precompiled_block


# ------------------------------------------------- predicado compartido

def test_nombre_bin_es_precompilado():
    assert is_precompiled("brave-bin")
    assert is_precompiled("foo", "bar-bin")


@pytest.mark.parametrize("url", [
    "https://dl.example.com/app.deb",
    "https://x.example.com/r.rpm?sig=abc",
    "https://github.com/o/r/releases/download/v1/tool.AppImage",
    "https://github.com/o/r/releases/download/v1/tool-linux-amd64",  # suelto
])
def test_formatos_inequivocos(url):
    assert is_precompiled("tool", "tool", [url])


@pytest.mark.parametrize("url", [
    "https://github.com/o/r/archive/refs/tags/v1.tar.gz",
    "https://github.com/o/r/releases/download/v1/src.tar.xz",
    "https://example.com/dist/tool.zip",
    "https://example.com/dist/tool.tar.zst",
])
def test_tarballs_son_fuente(url):
    assert not is_precompiled("tool", "tool", [url])


def test_sin_urls_solo_decide_nombre():
    assert is_precompiled("yay-bin")           # nombre basta
    assert not is_precompiled("cbonsai")       # fuente sin URLs


# --------------------------------------------------- gating en pipeline

class _FakePkg:
    def __init__(self, urls_by_arch):
        self._u = urls_by_arch

    def sources_for(self, arch):
        return self._u.get(arch, [])


class _FakeSI:
    def __init__(self, pkgs):
        self.packages = pkgs


@pytest.fixture
def libc_toggle(monkeypatch):
    def set_(v):
        monkeypatch.setenv("AUR2XBPS_LIBC", v)
    return set_


def test_glibc_no_bloquea(libc_toggle, monkeypatch):
    libc_toggle("glibc")
    monkeypatch.delenv("AUR2XBPS_MUSL_ALLOW_BIN", raising=False)
    si = _FakeSI({"main": _FakePkg({"": ["https://x/y.deb"]})})
    assert _musl_precompiled_block(si, "whatever") == []


def test_musl_bloquea_por_nombre(libc_toggle, monkeypatch):
    libc_toggle("musl")
    monkeypatch.delenv("AUR2XBPS_MUSL_ALLOW_BIN", raising=False)
    errs = _musl_precompiled_block(None, "brave-bin")
    assert errs and "Void-musl" in errs[0]


def test_musl_bloquea_por_urls_deep(libc_toggle, monkeypatch):
    libc_toggle("musl")
    monkeypatch.delenv("AUR2XBPS_MUSL_ALLOW_BIN", raising=False)
    si = _FakeSI({"main": _FakePkg({"": ["https://x/y.deb"]})})
    errs = _musl_precompiled_block(si, "app")
    assert errs and ".deb" in str(errs[0]) or "precompilado" in errs[0]


def test_musl_permite_fuente(libc_toggle, monkeypatch):
    libc_toggle("musl")
    monkeypatch.delenv("AUR2XBPS_MUSL_ALLOW_BIN", raising=False)
    si = _FakeSI({"main": _FakePkg({"": ["https://x/y.tar.gz"]})})
    assert _musl_precompiled_block(si, "app") == []


def test_valvula_escape(libc_toggle, monkeypatch):
    libc_toggle("musl")
    monkeypatch.setenv("AUR2XBPS_MUSL_ALLOW_BIN", "1")
    assert _musl_precompiled_block(None, "brave-bin") == []
