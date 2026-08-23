# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipeline seguro AUR → validación → clonado local.

Integra security.check_atomic_arch como filtro PREVIO a cualquier descarga
de fuentes. Aborta antes de instanciar contenedor (AGENTS.md Gotchas).
"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from src.aur.client import AURClient
from src.aur.parser import parse_srcinfo_file, SrcInfo
from src.aur.security import check_atomic_arch, validate_license
from src.common.paths import SOURCES as DEFAULT_SOURCES

AUR_GIT = "https://aur.archlinux.org/{pkg}.git"


@dataclass
class PrepareResult:
    pkgbase: str
    srcinfo: Optional[SrcInfo] = None
    cloned: bool = False
    blocked: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def prepare_package(pkgname: str, sources_dir: Path = DEFAULT_SOURCES,
                    client: Optional[AURClient] = None,
                    allow_nonfree_warning: bool = True,
                    offline: bool = False) -> PrepareResult:
    """Flujo seguro: RPC → existe en índice → clone → parse .SRCINFO → filtro Atomic → licencias."""
    result = PrepareResult(pkgbase=pkgname)
    client = client or AURClient(offline=offline)

    # 1. Verificar existencia vía RPC multiinfo (barato, cacheado)
    try:
        info = client.info_one(pkgname)
    except Exception as e:
        result.errors.append(f"RPC info falló: {e}")
        return result
    if not info:
        result.errors.append(
            f"{pkgname}: no existe en AUR (eliminado upstream o nombre erróneo)")
        return result

    # 2. Clonar repo git del PKGBUILD (solo metadatos; NO se ejecuta)
    dest = Path(sources_dir) / pkgname
    try:
        if (dest / ".SRCINFO").exists():
            subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"],
                           check=False, capture_output=True, timeout=60)
        else:
            if dest.exists():
                subprocess.run(["sudo", "rm", "-rf", str(dest)], check=True)
            subprocess.run(["git", "clone", "--depth", "1",
                            AUR_GIT.format(pkg=pkgname), str(dest)],
                           check=True, capture_output=True, timeout=120)
            result.cloned = True
    except subprocess.CalledProcessError as e:
        result.errors.append(f"git clone/pull falló: {e.stderr[:200] if e.stderr else e}")
        return result

    # 3. Parsear .SRCINFO (nunca evaluar PKGBUILD)
    srcinfo_path = dest / ".SRCINFO"
    if not srcinfo_path.exists():
        result.errors.append(f"{pkgname}: .SRCINFO ausente en repo")
        return result
    try:
        si = parse_srcinfo_file(srcinfo_path)
    except ValueError as e:
        result.errors.append(f"parse .SRCINFO inválido: {e}")
        return result
    result.srcinfo = si

    # 4. FILTRO DE SEGURIDAD previo a descarga de fuentes
    # H-2.1: pasar el TEXTO del PKGBUILD (lectura estática, jamás ejecutado)
    # para que la heurística JS opere sobre evidencia real, no solo metadatos.
    raw_pkgbuild: str | None = None
    pkgbuild_path = dest / "PKGBUILD"
    if pkgbuild_path.exists():
        try:
            raw_pkgbuild = pkgbuild_path.read_text(
                encoding="utf-8", errors="replace")[:262_144]   # cap anti-DoS
        except OSError:
            raw_pkgbuild = None
    block, reasons = check_atomic_arch(si, raw_pkgbuild_text=raw_pkgbuild)
    if block:
        result.blocked = True
        result.errors.extend(reasons)
        return result

    # 5. Licencias (warning, no bloquea build local)
    for w in validate_license(si):
        result.warnings.append(w)

    # 6. Staleness básico: comparar versión RPC vs .SRCINFO
    rpc_ver = info.get("Version", "")
    for pname, p in si.packages.items():
        rpc_norm = rpc_ver.split(":")[-1]
        local_full = f"{p.pkgver}-{p.pkgrel}"
        if rpc_norm and rpc_norm != local_full:
            result.warnings.append(
                f"{pname}: .SRCINFO {local_full} != RPC {rpc_norm} (posible stale)")

    return result


def prepare_many(pkgnames: List[str], sources_dir: Path = DEFAULT_SOURCES,
                 offline: bool = False) -> List[PrepareResult]:
    """Para lotes masivos: offline=True evita consumir rate-limit RPC."""
    client = AURClient(offline=offline)
    return [prepare_package(p, sources_dir, client) for p in pkgnames]
