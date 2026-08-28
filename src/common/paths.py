# SPDX-License-Identifier: GPL-3.0-or-later
"""Rutas del workspace aur2xbps — capa fina sobre src/common/config.py.

Todos los valores provienen de la configuración (env > TOML > defaults XDG).
NUNCA añadir rutas absolutas personales aquí.

Las rutas derivadas de config se resuelven DE FORMA PEREZOSA (PEP 562): no se
carga la config en el ``import`` del módulo. Esto evita efectos de orden (p.ej.
fijar AUR2XBPS_ROOT tras importar paths) y acelera los tests que no tocan disco.
"""
from __future__ import annotations

from pathlib import Path

# Rutas que NO dependen de la config: se calculan una vez en el import.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SHLIBS_SUBMODULE: Path = REPO_ROOT / "common" / "shlibs"

# Nombres resueltos perezosamente vía __getattr__ (abajo).
_LAZY_NAMES = {
    "ROOT", "CACHE_DIR", "RPC_CACHE_DIR", "DEFAULT_DB", "REV_PINS", "SOURCES",
    "DERIVATIONS", "FAKE_ROOT", "BATCH_RESULTS", "VOID_BASE", "MASTERDIR",
    "REPO_LOCAL", "REPO_X86_64", "VOID_PACKAGES", "SHLIBS", "PRIVKEY", "PUBKEY",
}

_LAZY_CACHE: dict | None = None


def _build_lazy(cfg) -> dict:
    return {
        "ROOT": cfg.data_dir,
        "CACHE_DIR": cfg.cache_dir,
        "RPC_CACHE_DIR": cfg.rpc_cache_db.parent,
        "DEFAULT_DB": cfg.rpc_cache_db,
        "REV_PINS": cfg.rev_pins,
        "SOURCES": cfg.sources_dir,
        "DERIVATIONS": cfg.derivations_dir,
        "FAKE_ROOT": cfg.fake_root,
        "BATCH_RESULTS": cfg.batch_results,
        "VOID_BASE": cfg.data_dir / "void",
        "MASTERDIR": cfg.masterdir,
        "REPO_LOCAL": cfg.repo_dir,
        "REPO_X86_64": cfg.repo_x86_64,
        "VOID_PACKAGES": cfg.void_packages_dir,
        "SHLIBS": cfg.shlibs_file,
        "PRIVKEY": cfg.privkey,
        "PUBKEY": cfg.pubkey,
    }


def __getattr__(name: str):
    if name in _LAZY_NAMES:
        global _LAZY_CACHE
        if _LAZY_CACHE is None:
            from src.common.config import get_config
            _LAZY_CACHE = _build_lazy(get_config())
        return _LAZY_CACHE[name]
    raise AttributeError(f"module 'src.common.paths' has no attribute {name!r}")


def reset_lazy_cache() -> None:
    """Invalida la caché perezosa (tras get_config(reload=True) o cambio de env)."""
    global _LAZY_CACHE
    _LAZY_CACHE = None


def ensure_layout() -> None:
    """Crea el layout mínimo del workspace (idempotente)."""
    from src.common.config import get_config
    get_config().ensure_layout()
