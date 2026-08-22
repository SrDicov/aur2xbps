# SPDX-License-Identifier: GPL-3.0-or-later
"""patchelf — orden mandatorio + linter + validación post-patch.

Gotcha: i686/x86_64 ~3000 LOC section-layout bug.
Orden: `patchelf --set-rpath` primero, luego `patchelf --set-interpreter` en invocaciones SEPARADAS.
Combinado en una línea → ELF corrupto, segfault, ldd falla.
"""
from __future__ import annotations
import re
import subprocess
import shlex
from pathlib import Path
from typing import List

# Linter: detecta ambos flags en misma invocación
COMBINED_RE = re.compile(r"patchelf.*--set-interpreter.*--set-rpath|patchelf.*--set-rpath.*--set-interpreter")

def lint_fixupPhase(text: str) -> List[str]:
    errs = []
    for i, line in enumerate(text.splitlines(), 1):
        if "patchelf" in line and "--set-interpreter" in line and "--set-rpath" in line:
            errs.append(f"L{i}: patchelf combinado prohibido (separar invocaciones): {line.strip()}")
        # También detectar orden inverso aunque sea en líneas separadas? Requiere análisis secuencial
    # Check orden: rpath debe aparecer antes que interpreter en el flujo
    rpath_idx = None
    interp_idx = None
    for i, line in enumerate(text.splitlines()):
        if "patchelf" in line and "--set-rpath" in line and rpath_idx is None:
            rpath_idx = i
        if "patchelf" in line and "--set-interpreter" in line and interp_idx is None:
            interp_idx = i
    if rpath_idx is not None and interp_idx is not None and interp_idx < rpath_idx:
        errs.append(f"Orden incorrecto: --set-interpreter (línea {interp_idx+1}) antes que --set-rpath ({rpath_idx+1})")
    return errs

def patchelf_fix_binary(path: Path, rpath: str, interpreter: str | None = None, dry_run: bool = False) -> List[str]:
    """Aplica patchelf en orden correcto con validación. Retorna logs."""
    logs: List[str] = []
    if dry_run:
        logs.append(f"[dry] patchelf --set-rpath {rpath} {path}")
        if interpreter:
            logs.append(f"[dry] patchelf --set-interpreter {interpreter} {path}")
        return logs
    # 1. rpath primero
    cmd1 = ["patchelf", "--set-rpath", rpath, str(path)]
    logs.append("$ " + " ".join(shlex.quote(c) for c in cmd1))
    subprocess.run(cmd1, check=True)
    # 2. interpreter después (si dado)
    if interpreter:
        cmd2 = ["patchelf", "--set-interpreter", interpreter, str(path)]
        logs.append("$ " + " ".join(shlex.quote(c) for c in cmd2))
        subprocess.run(cmd2, check=True)
    # 3. Validación post
    logs.extend(validate_elf(path))
    return logs

def validate_elf(path: Path) -> List[str]:
    errs: List[str] = []
    # readelf -d
    try:
        out = subprocess.check_output(["readelf", "-d", str(path)], text=True, stderr=subprocess.STDOUT)
        errs.append("readelf -d OK")
        # Debe contener RPATH/RUNPATH/NEEDED
        if not re.search(r"RPATH|RUNPATH", out):
            errs.append(f"WARN: {path} sin RPATH/RUNPATH tras patchelf")
        else:
            # Verificar que RPATH apunta a /nix/store
            m = re.search(r"(RPATH|RUNPATH).*\[([^\]]+)\]", out)
            if m and "/nix/store" not in m.group(2):
                errs.append(f"FAIL: RPATH no apunta a /nix/store: {m.group(2)}")
        if "NEEDED" not in out:
            errs.append(f"WARN: {path} sin NEEDED")
    except subprocess.CalledProcessError as e:
        errs.append(f"FAIL readelf: {e.output[:500]}")
        raise RuntimeError(f"readelf falló para {path}: {e}")

    # ldd
    try:
        out2 = subprocess.check_output(["ldd", str(path)], text=True, stderr=subprocess.STDOUT)
        if "not found" in out2:
            errs.append(f"FAIL ldd not found: {out2[:500]}")
            raise RuntimeError(f"ldd not found en {path}")
        errs.append("ldd OK")
    except subprocess.CalledProcessError as e:
        errs.append(f"FAIL ldd: {e.output[:500] if hasattr(e,'output') else str(e)}")
        raise
    return errs

def validate_fixup_script(path: Path) -> List[str]:
    text = Path(path).read_text()
    return lint_fixupPhase(text)

if __name__ == "__main__":
    sample_bad = "patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 --set-rpath /nix/store/foo/lib $out/bin/foo"
    assert lint_fixupPhase(sample_bad)
    sample_good = "patchelf --set-rpath /nix/store/foo/lib $out/bin/foo\npatchelf --set-interpreter $(cat $NIX_CC/nix-support/dynamic-linker) $out/bin/foo"
    assert not lint_fixupPhase(sample_good)
    print("linter OK")
