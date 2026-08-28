# SPDX-License-Identifier: GPL-3.0-or-later
"""Autocorrector del build nix: hashes sha256/sha512 y attrs indefinidos."""
import re

from src.nix import generator


def test_regex_hash_acepta_sha512():
    err = ("specified: sha512-AAAAbbb+/==\n"
           "  got:    sha512-CCCCddd+/==")
    m = generator.SPECIFIED_GOT_RE.search(err)
    assert m and m.group(1).startswith("sha512-") and m.group(2).startswith("sha512-")
    m2 = generator.HASH_MISMATCH_RE.search("got: sha256-xyz123=")
    assert m2 and m2.group(1) == "sha256-xyz123="


def test_drop_undefined_var_elimina_token(tmp_path):
    flake = tmp_path / "flake.nix"
    flake.write_text(
        "buildInputs = with pkgs; [ boost-libs gtkmm3 python2 sqlite ];\n",
        encoding="utf-8")
    assert generator._drop_undefined_var(flake, "python2")
    txt = flake.read_text()
    assert "python2" not in txt
    assert "boost-libs" in txt and "sqlite" in txt
    # segunda pasada: otro token
    assert generator._drop_undefined_var(flake, "nodejs-yeoman") is False


def test_drop_no_toca_subcadenas(tmp_path):
    flake = tmp_path / "flake.nix"
    flake.write_text("[ glib glibmm ]\n", encoding="utf-8")
    # 'glib' no debe borrar 'glibmm' ni dejar corchetes rotos
    generator._drop_undefined_var(flake, "glib")
    txt = flake.read_text()
    assert "glibmm" in txt
    assert not re.search(r"glib(?![\w])", txt)


def test_undef_re_captura_nombre_simple():
    err = "error: undefined variable 'nodejs-yeoman' at flake.nix:22:45:"
    m = generator.UNDEF_VAR_RE.search(err)
    assert m and m.group(1) == "nodejs-yeoman"


def test_mapas_nuevos_presentes():
    from src.nix.generator import ARCH_TO_NIX
    assert ARCH_TO_NIX.get("boost-libs") == "boost"
    assert ARCH_TO_NIX.get("gconf") == "gnome2.GConf"


def test_hashfix_multi_fod_replaces_exact_specified(tmp_path, monkeypatch):
    """H-3.2: en flakes multi-FOD con placeholders distintos, el hash reportado
    por Nix debe corregir SOLO la derivación fallida, sin contagiar las demás."""
    from src.nix import generator

    p1 = "sha256-BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    p2 = "sha256-CAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    flake = tmp_path / "flake.nix"
    flake.write_text(
        'pkgA = fetchurl { sha256 = "' + p1 + '"; };\n'
        'pkgB = fetchurl { sha256 = "' + p2 + '"; };\n'
    )
    error = ("hash mismatch in fixed-output derivation ...\n"
             "  specified: " + p2 + "\n"
             "  got:      sha256-REALBREALBREALBREALBREALBREALBREALB=\n")

    class FakeRes:
        returncode = 1
        stdout = ""
        stderr = error

    def fake_run(cmd, **kw):
        return FakeRes()

    monkeypatch.setattr(generator.subprocess, "run", fake_run)
    ok, msg = generator.build_with_hash_fix(
        tmp_path, "pkgA", max_retries=1, timeout=10)
    txt = flake.read_text()
    # La derivación que falló (p2) se corrige con el hash real
    assert "sha256-REALBREALBREALBREALBREALBREALBREALB=" in txt
    assert p2 not in txt
    # La otra derivación (p1) NO se contamina con el hash ajeno
    assert p1 in txt, "p1 fue contaminado por el hash de p2"


def test_assign_unique_dummies_makes_placeholders_distinct():
    from src.nix import generator
    sample = "a = " + generator.HASH_DUMMY + "; b = " + generator.HASH_DUMMY
    out = generator._assign_unique_dummies(sample)
    parts = out.split("; ")
    assert len(set(parts)) == 2
    assert generator.HASH_DUMMY not in out
