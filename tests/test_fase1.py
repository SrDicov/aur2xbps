# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests Fase 1 — parser/client/security/pipeline contra 56 .SRCINFO reales de AUR.

Fixtures: tests/fixtures/srcinfo/*.SRCINFO (49 reales + 2 split + 3 sintéticos maliciosos
atomic-lockfile/js-digest/lockfile-js purgados del AUR, recreados para test de bloqueo).
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "srcinfo"
ALL_FIXTURES = sorted(FIXTURES.glob("*.SRCINFO"))

MALICIOUS = {"atomic-lockfile", "js-digest", "lockfile-js"}

def _pkgname_of(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.strip().startswith("pkgbase ="):
            return line.split("=", 1)[1].strip()
    return path.stem


# ---------- Parser: cobertura total ----------

@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: p.stem)
def test_parse_real_srcinfo(fixture):
    """Todo fixture real debe parsear sin excepción y producir >=1 paquete válido."""
    from src.aur.parser import parse_srcinfo_file
    si = parse_srcinfo_file(fixture)
    assert si.pkgbase
    assert len(si.packages) >= 1
    for name, pkg in si.packages.items():
        assert pkg.pkgname == name
        assert pkg.pkgver and pkg.pkgver != "0" or pkg.pkgver == "0"
        assert isinstance(pkg.depends_for(), list)
        assert isinstance(pkg.makedepends_for(), list)


def test_fixture_count_over_50():
    assert len(ALL_FIXTURES) >= 50, f"solo {len(ALL_FIXTURES)} fixtures"


def test_split_packages_detected():
    from src.aur.parser import parse_srcinfo_file
    splits = [f for f in ALL_FIXTURES
              if sum(1 for l in f.read_text().splitlines() if l.strip().startswith("pkgname =")) > 1]
    assert splits, "se requieren split packages en fixtures"
    for f in splits:
        si = parse_srcinfo_file(f)
        assert len(si.packages) > 1
        # cada subpaquete conserva su nombre y hereda pkgbase
        for name, pkg in si.packages.items():
            assert pkg.pkgbase == si.pkgbase


def test_arch_qualified_deps_priority():
    from src.aur.parser import parse_srcinfo
    txt = """
pkgbase = t
    pkgver = 1
    pkgrel = 1
pkgname = t
    depends = generic
    depends_x86_64 = specific64
"""
    si = parse_srcinfo(txt)
    assert si.packages["t"].depends_for("x86_64") == ["specific64"]
    assert si.packages["t"].depends_for("i686") == ["generic"]


def test_all_hash_algos_and_b2():
    from src.aur.parser import parse_srcinfo
    txt = """
pkgbase = h
    pkgver = 1
    pkgrel = 1
    md5sums = aaa
    sha1sums = bbb
    sha256sums = ccc
    sha512sums = ddd
    b2sums_x86_64 = eee
pkgname = h
"""
    si = parse_srcinfo(txt)
    p = si.packages["h"]
    assert p.sums_for("md5") == ["aaa"]
    assert p.sums_for("sha1") == ["bbb"]
    assert p.sums_for("sha256") == ["ccc"]
    assert p.sums_for("sha512") == ["ddd"]
    assert p.sums_for("b2", "x86_64") == ["eee"]
    assert p.sums_for("b2", "i686") == []


def test_validpgpkeys_and_options():
    from src.aur.parser import parse_srcinfo_file
    si = parse_srcinfo_file(FIXTURES / "firefox-nightly.SRCINFO")
    p = si.packages["firefox-nightly"]
    assert p.validpgpkeys
    assert any("!lto" in o or o == "!lto" for o in p.options)


def test_epoch_handling():
    from src.aur.parser import parse_srcinfo_file
    si = parse_srcinfo_file(FIXTURES / "spotify.SRCINFO")
    p = si.packages["spotify"]
    assert p.epoch == "1"
    assert ":1:" in p.pkgver_full or p.pkgver_full.startswith("spotify-1:")


def test_invalid_line_raises():
    from src.aur.parser import parse_srcinfo
    with pytest.raises(ValueError):
        parse_srcinfo("línea sin igual\n")


def test_no_pkgbase_nor_pkgname_raises():
    from src.aur.parser import parse_srcinfo
    with pytest.raises(ValueError):
        parse_srcinfo("foo = bar\n")


# ---------- Security: filtro Atomic Arch ----------

@pytest.mark.parametrize("mal", sorted(MALICIOUS))
def test_malicious_exact_blocked(mal):
    from src.aur.parser import parse_srcinfo_file
    from src.aur.security import check_atomic_arch
    si = parse_srcinfo_file(FIXTURES / f"{mal}.SRCINFO")
    block, reasons = check_atomic_arch(si)
    assert block, reasons


def test_allowlist_js_legitimate_passes():
    from src.aur.parser import parse_srcinfo_file
    from src.aur.security import check_atomic_arch
    si = parse_srcinfo_file(FIXTURES / "visual-studio-code-bin.SRCINFO")
    block, _ = check_atomic_arch(si, raw_pkgbuild_text="npm install --no-save")
    assert not block


def test_generic_npm_not_blocked_when_hashed():
    from src.aur.parser import parse_srcinfo
    from src.aur.security import check_atomic_arch
    txt = """
pkgbase = legit-js
    pkgver = 1
    pkgrel = 1
pkgname = legit-js
    makedepends = npm
    source = https://example.com/x.tar.gz
    sha256sums = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
"""
    si = parse_srcinfo(txt)
    block, _ = check_atomic_arch(si, raw_pkgbuild_text="npm install")
    assert not block


# ---------- Todos los fixtures pasan el filtro (salvo los maliciosos) ----------

@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda p: p.stem)
def test_security_filter_on_fixtures(fixture):
    from src.aur.parser import parse_srcinfo_file
    from src.aur.security import check_atomic_arch
    si = parse_srcinfo_file(fixture)
    block, reasons = check_atomic_arch(si)
    should_block = _pkgname_of(fixture) in MALICIOUS
    assert block == should_block, reasons


# ---------- Client: SQLite cache + batch (sin red salvo marcado) ----------

def test_client_batch_respects_uri_limit():
    from src.aur.client import AURClient, MAX_URI_BYTES, MAX_BATCH
    from urllib.parse import urlencode
    from src.aur.client import AUR_RPC
    names = [f"pkg{i:03d}" for i in range(500)]
    client = AURClient(db_path=":memory:")
    # monkeypatch _info_batch para capturar lotes sin red
    batches = []
    client._info_batch = lambda b: batches.append(list(b)) or []
    client.info(names)
    assert all(len(b) <= MAX_BATCH for b in batches)
    for b in batches:
        q = urlencode([("arg[]", p) for p in b], doseq=True)
        assert len(f"{AUR_RPC}/info?{q}".encode()) <= MAX_URI_BYTES
    assert sum(len(b) for b in batches) == len(names)


def test_client_rate_limit_enforced():
    from src.aur.client import AURClient, RateLimitError
    client = AURClient(db_path=":memory:", daily_limit=3)
    client._bump_counter(); client._bump_counter(); client._bump_counter()
    with pytest.raises(RateLimitError):
        client._bump_counter()


def test_client_cache_roundtrip():
    from src.aur.client import AURClient
    client = AURClient(db_path=":memory:")
    client._cache_put("https://x/test", {"type": "multiinfo", "results": []}, "etag1", None)
    got = client._cache_get("https://x/test")
    assert got and got[0]["type"] == "multiinfo" and got[1] == "etag1"


def test_search_rejects_short_keyword():
    from src.aur.client import AURClient
    client = AURClient(db_path=":memory:")
    with pytest.raises(ValueError):
        client.search("a")


# ---------- Pipeline: integración end-to-end (red real, marcada) ----------

@pytest.mark.network
def test_pipeline_blocks_malicious_before_clone(tmp_path):
    """atomic-lockfile fue purgado del AUR: resultado seguro = no existe O bloqueado.
    En ambos casos NUNCA debe llegar a parsear/clonar fuentes."""
    from src.aur.pipeline import prepare_package
    r = prepare_package("atomic-lockfile", sources_dir=tmp_path)
    # seguro si: bloqueado por filtro, o inexistente en AUR (purgado)
    assert r.blocked or r.srcinfo is None
    assert not (tmp_path / "atomic-lockfile" / ".SRCINFO").exists()


@pytest.mark.network
def test_pipeline_real_package(tmp_path):
    from src.aur.pipeline import prepare_package
    r = prepare_package("yay-bin", sources_dir=tmp_path)
    assert not r.blocked
    assert r.srcinfo is not None
    assert (tmp_path / "yay-bin" / ".SRCINFO").exists()


# ---------- Staleness ----------

def test_staleness_detects_divergence():
    from src.aur.parser import parse_srcinfo, validate_srcinfo_staleness
    txt = """
pkgbase = s
    pkgver = 1.0
    pkgrel = 1
pkgname = s
"""
    errs = validate_srcinfo_staleness(parse_srcinfo(txt), "v2.0")
    assert errs and "stale" in errs[0]


def test_staleness_ok_when_match():
    from src.aur.parser import parse_srcinfo, validate_srcinfo_staleness
    txt = """
pkgbase = s
    pkgver = 2.0
    pkgrel = 1
pkgname = s
"""
    assert validate_srcinfo_staleness(parse_srcinfo(txt), "v2.0") == []
