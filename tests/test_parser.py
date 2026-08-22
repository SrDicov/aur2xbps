# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests parser — API estructurada (dicts por arch) desde Fase 1."""
from src.aur.parser import parse_srcinfo


def test_split_package():
    txt = """
pkgbase = foo
    pkgver = 1.0
    pkgrel = 2
    arch = x86_64
    source_x86_64 = https://example.com/foo-1.0.tar.gz
    sha256sums_x86_64 = abc123
pkgname = foo
    depends = bar>=1.0
pkgname = foo-docs
    depends = foo
    arch = any
"""
    si = parse_srcinfo(txt)
    assert si.pkgbase == "foo"
    assert "foo" in si.packages and "foo-docs" in si.packages
    assert si.packages["foo"].pkgver == "1.0"
    assert si.packages["foo"].depends_for() == ["bar>=1.0"]
    assert si.packages["foo-docs"].arch == ["any"]


def test_mono_package_spotify_like():
    txt = """
pkgbase = spotify
    pkgver = 1.2.96.518
    pkgrel = 1
    epoch = 1
    arch = x86_64
    depends = gtk3
    depends = nss
    source = spotify.deb::https://example.com/spotify.deb
    sha512sums = abc
pkgname = spotify
"""
    si = parse_srcinfo(txt)
    p = si.packages["spotify"]
    assert p.pkgver == "1.2.96.518"
    assert p.epoch == "1"
    assert "gtk3" in p.depends_for()
    assert p.pkgver_full == "spotify-1:1.2.96.518_1"


def test_source_x86_64_precedence():
    txt = """
pkgbase = test
    pkgver = 1.0
    pkgrel = 1
    source = https://example.com/generic.tar.gz
    source_x86_64 = https://example.com/x86_64.deb
    sha256sums = generic
    sha256sums_x86_64 = x86_64hash
pkgname = test
"""
    si = parse_srcinfo(txt)
    p = si.packages["test"]
    assert p.sources_for("x86_64") == ["https://example.com/x86_64.deb"]
    assert p.sums_for("sha256", "x86_64") == ["x86_64hash"]


def test_mono_without_pkgname_section():
    """Algunos .SRCINFO mínimos no tienen sección pkgname (monopaquete implícito)."""
    txt = """
pkgbase = solo
    pkgver = 3.2
    pkgrel = 1
    arch = x86_64
    depends = libfoo
"""
    si = parse_srcinfo(txt)
    assert "solo" in si.packages
    assert si.packages["solo"].depends_for() == ["libfoo"]
