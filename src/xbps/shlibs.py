# SPDX-License-Identifier: GPL-3.0-or-later
"""common/shlibs real desde void-packages submódulo — no copia manual.

Sincronización periódica vía git submodule o rsync.
"""
from __future__ import annotations
from pathlib import Path
import subprocess
from typing import Dict, Optional

from src.common.paths import REPO_ROOT, SHLIBS as _WS_SHLIBS, SHLIBS_SUBMODULE

DEFAULT_SHLIBS = _WS_SHLIBS
REPO_SHLIBS = SHLIBS_SUBMODULE  # submódulo git del repo
FALLBACK = Path(__file__).parent / "shlibs.fallback"  # opcional

class ShlibsDB:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else self._resolve()
        self.map: Dict[str, str] = {}  # SONAME -> "pkg>=ver"
        self._load()

    def _resolve(self) -> Path:
        for cand in [DEFAULT_SHLIBS, REPO_SHLIBS, FALLBACK]:
            if cand.exists():
                return cand
        # Si nada existe, crear vacío
        return DEFAULT_SHLIBS

    def _load(self):
        self.map.clear()
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                soname, pkgver = parts[0], parts[1]
                # guarda primero wins (ya map no sobrescribe)
                if soname not in self.map:
                    self.map[soname] = pkgver

    def lookup(self, soname: str) -> Optional[str]:
        return self.map.get(soname)

    def soname_to_dep(self, soname: str) -> Optional[str]:
        """Convierte SONAME a dependencia XBPS `pkg>=ver`.
        La segunda columna de common/shlibs es tupla 'nombre-version' (ej.
        'libgcc-4.4.0_1'); xbps exige operador >= o trata el literal como
        versión exacta y falla con MISSING cuando el instalado es más nuevo."""
        entry = self.lookup(soname)
        if not entry or "-" not in entry:
            return entry
        name, ver = entry.rsplit("-", 1)
        return f"{name}>={ver}"

    def sync_from_git(self, void_packages_dir: Path | str | None = None):
        """rsync o pull submódulo"""
        vp = Path(void_packages_dir) if void_packages_dir else DEFAULT_SHLIBS.parent.parent
        if (vp / ".git").exists():
            subprocess.run(["git", "-C", str(vp), "pull", "--ff-only"], check=False)
        # Si AxX tiene common/shlibs como submodule, actualizar puntero
        self._load()

    def deps_for_elf(self, elf_path: Path) -> list[str]:
        """Extrae NEEDED de ELF y mapea a deps XBPS."""
        try:
            out = subprocess.check_output(["readelf", "-d", str(elf_path)], text=True)
        except subprocess.CalledProcessError:
            return []
        deps = []
        for line in out.splitlines():
            if "NEEDED" in line:
                # 0x0000000000000001 (NEEDED) Shared library: [libtinfo.so.6]
                if "[" in line:
                    soname = line.split("[")[1].split("]")[0]
                    mapped = self.lookup(soname)
                    if mapped:
                        deps.append(mapped)
        return sorted(set(deps))

def ensure_submodule(axx_root: Path | None = None):
    """Sugerencia: añadir void-packages como submodule en <repo>/common/void-packages"""
    axx_root = axx_root or REPO_ROOT
    # No ejecuta automáticamente, solo documenta
    gitmodules = axx_root / ".gitmodules"
    if not gitmodules.exists():
        # crear instrucciones, no el submodule físico (requiere git repo)
        pass

if __name__ == "__main__":
    db = ShlibsDB()
    print(f"shlibs entries: {len(db.map)}")
    for k in list(db.map)[:5]:
        print(k, "->", db.map[k])
    # test NEEDED mapping con /bin/ls
    print(db.deps_for_elf(Path("/bin/ls"))[:5])
