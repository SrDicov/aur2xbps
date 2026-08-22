# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolución de binarios externos sin rutas fijas.

Orden para herramientas xbps:
  1. ``AUR2XBPS_XBPS_BIN_DIR`` / config ``paths.xbps_bin_dir`` (opcional)
  2. ``shutil.which`` (PATH del sistema — caso Void nativo)
  3. Fallback histórico ``/usr/local/xbps/usr/bin`` si existe
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

_LEGACY_STATIC = Path("/usr/local/xbps/usr/bin")


@lru_cache(maxsize=None)
def find_xbps_tool(name: str) -> str:
    env_dir = os.environ.get("AUR2XBPS_XBPS_BIN_DIR")
    if env_dir:
        p = Path(env_dir) / name
        if p.is_file():
            return str(p)
    which = shutil.which(name)
    if which:
        return which
    legacy = _LEGACY_STATIC / name
    if legacy.is_file():
        return str(legacy)
    raise FileNotFoundError(
        f"{name} no encontrado en PATH ni en {env_dir or _LEGACY_STATIC}; "
        f"instala xbps static utilities o exporta AUR2XBPS_XBPS_BIN_DIR")


def has_nix() -> bool:
    """True si nix está disponible en PATH."""
    return shutil.which("nix") is not None


def nix_version() -> str | None:
    if not has_nix():
        return None
    import subprocess
    try:
        out = subprocess.run(["nix", "--version"], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        return out.split()[-1] if out else None
    except Exception:
        return None


def sudo_prefix() -> list[str]:
    """Prefijo de privilegios: [] si somos root; ['sudo'] en otro caso."""
    import os
    if os.getuid() == 0:
        return []
    return ["sudo"]
