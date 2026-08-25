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


@lru_cache(maxsize=None)
def find_tool(name: str, env_var: str | None = None) -> str:
    """Resolución genérica de binarios no-xbps con override opcional por env
    (p.ej. AUR2XBPS_ZSTD para pinear un zstd concreto y blindar el TRH)."""
    if env_var:
        override = os.environ.get(env_var)
        if override and Path(override).is_file():
            return override
    which = shutil.which(name)
    if which:
        return which
    raise FileNotFoundError(
        f"{name} no encontrado en PATH"
        + (f" ni vía {env_var}" if env_var else "")
        + "; instálalo o exporta la variable de override")


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
    """COMPAT: prefijo de privilegios para elevadores de forma argv-prefijo.

    Delegado en src.common.priv (orden root › $AUR2XBPS_PRIV › [priv] command
    › sudo → doas → run0 → pkexec → su). Lanza error claro si el elevador
    resuelto NO admite forma prefijo (su/pkexec): en ese caso la llamada debe
    usar priv.priv_wrap(argv_completo), que soporta todas las shapes.
    """
    from src.common import priv

    if priv._is_root():
        return []
    toks = priv.detect()
    if priv._shape(toks) != "prefix":
        raise RuntimeError(
            f"elevador '{toks[0]}' sin forma de prefijo: usa "
            "src.common.priv.priv_wrap(<argv completo>)")
    return toks
