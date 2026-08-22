# SPDX-License-Identifier: GPL-3.0-or-later
from src.nix.patchelf import lint_fixupPhase

def test_combined_reject():
    bad = "patchelf --set-interpreter /lib/ld.so --set-rpath /nix/store/foo/lib $out/bin/foo"
    errs = lint_fixupPhase(bad)
    assert errs

def test_order_correct():
    good = "patchelf --set-rpath /nix/store/foo/lib $out/bin/foo\npatchelf --set-interpreter /lib/ld-linux.so $out/bin/foo"
    assert not lint_fixupPhase(good)

def test_order_incorrect():
    bad = "patchelf --set-interpreter /lib/ld.so $out/bin/foo\npatchelf --set-rpath /nix/store/foo $out/bin/foo"
    errs = lint_fixupPhase(bad)
    assert any("Orden" in e for e in errs)

def test_nix_flake_lint():
    from pathlib import Path
    flake = Path(__file__).parent / "fixtures" / "flakes" / "sample-bin-flake.nix"
    assert not lint_fixupPhase(flake.read_text())

