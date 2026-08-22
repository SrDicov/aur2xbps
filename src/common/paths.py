# SPDX-License-Identifier: GPL-3.0-or-later
"""Rutas del workspace aur2xbps — capa fina sobre src/common/config.py.

Todos los valores provienen de la configuración (env > TOML > defaults XDG).
NUNCA añadir rutas absolutas personales aquí.
"""
from __future__ import annotations

from pathlib import Path

from src.common.config import get_config

_cfg = get_config()

# Compat con código existente
ROOT: Path = _cfg.data_dir
CACHE_DIR: Path = _cfg.cache_dir
RPC_CACHE_DIR: Path = _cfg.rpc_cache_db.parent
DEFAULT_DB: Path = _cfg.rpc_cache_db
REV_PINS: Path = _cfg.rev_pins
SOURCES: Path = _cfg.sources_dir
DERIVATIONS: Path = _cfg.derivations_dir
FAKE_ROOT: Path = _cfg.fake_root
BATCH_RESULTS: Path = _cfg.batch_results
VOID_BASE: Path = ROOT / "void"
MASTERDIR: Path = _cfg.masterdir
REPO_LOCAL: Path = _cfg.repo_dir
REPO_X86_64: Path = _cfg.repo_x86_64  # nombre histórico; usa cfg.repo_x86_64
VOID_PACKAGES: Path = _cfg.void_packages_dir
SHLIBS: Path = _cfg.shlibs_file
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
SHLIBS_SUBMODULE: Path = REPO_ROOT / "common" / "shlibs"
PRIVKEY: Path = _cfg.privkey
PUBKEY: Path = _cfg.pubkey


def ensure_layout() -> None:
    """Crea el layout mínimo del workspace (idempotente)."""
    get_config().ensure_layout()
