# SPDX-License-Identifier: GPL-3.0-or-later
from src.aur.parser import parse_srcinfo
from src.aur.security import check_atomic_arch, _MALICIOUS_BASE

def test_malicious_exact_block():
    txt = """
pkgbase = evil
    pkgver = 1
    pkgrel = 1
pkgname = evil
    depends = atomic-lockfile
"""
    si = parse_srcinfo(txt)
    block, reasons = check_atomic_arch(si)
    assert block
    assert any("atomic-lockfile" in r for r in reasons)

def test_js_allowlist_pass():
    txt = """
pkgbase = visual-studio-code-bin
    pkgver = 1.9
    pkgrel = 1
pkgname = visual-studio-code-bin
    depends = libx11
"""
    si = parse_srcinfo(txt)
    block, _ = check_atomic_arch(si, raw_pkgbuild_text="npm install")
    assert not block

def test_npm_install_without_hash_block():
    txt = """
pkgbase = myapp
    pkgver = 1.0
    pkgrel = 1
pkgname = myapp
    depends = bar
    sha256sums = SKIP
"""
    si = parse_srcinfo(txt)
    block, reasons = check_atomic_arch(si, raw_pkgbuild_text="npm install")
    assert block

def test_license_spotify():
    from src.aur.security import validate_license
    txt = """
pkgbase = spotify
    pkgver = 1.0
    pkgrel = 1
    license = custom
pkgname = spotify
"""
    si = parse_srcinfo(txt)
    warns = validate_license(si, allow_nonfree=False)
    assert any("spotify" in w for w in warns)
