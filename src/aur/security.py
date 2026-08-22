# SPDX-License-Identifier: GPL-3.0-or-later
"""Filtro refinado Atomic Arch — AGENTS.md Gotchas.

Bloquea solo paquetes maliciosos exactos y npm/bun install sin hash,
mantiene allowlist para proyectos JS legítimos. Aborta antes de contenedor.
"""
from __future__ import annotations
import re
from typing import List, Set
from src.common.types import SrcInfo

# Paquetes maliciosos exactos reportados (jun 2026+ Atomic Arch)
MALICIOUS_EXACT: Set[str] = {
    "atomic-lockfile",
    "js-digest",
    "lockfile-js",
    # variantes vistas en iteraciones: normalizar sin versión
}

# Allowlist JS legítimo — proyectos que legit usan npm/bun pero son -bin o requieren build
# Por spec: visual-studio-code-bin es -bin pero no compila JS → debe pasar
JS_ALLOWLIST: Set[str] = {
    "visual-studio-code-bin",
    "code",  # variante
    "spotify",  # no JS pero por completitud
    "google-chrome",  # no JS
    "discord", "slack-desktop", "postman-bin", "insomnia-bin",
    # Añadir bajo demanda con justificación
}

# Patrones que indican instalación sin hash (vector)
# Detección: makedepends/depends contiene `npm`/`bun` + PKGBUILD implicaría `npm install` sin fetcher hash
# En .SRCINFO solo vemos depends; correlacionamos con nombres sospechosos
NPM_BUN_WITHOUT_HASH_RE = re.compile(r"\b(npm|bun)\s+install\b", re.IGNORECASE)
# También dependencias bare `npm`/`nodejs` sin versión pinned pueden ser señal pero no bloqueamos por sí solas

def _extract_dep_names(deps: List[str]) -> Set[str]:
    names = set()
    for d in deps:
        # "pkg>=1.0" -> "pkg", "pkg" -> "pkg"
        m = re.match(r"([a-zA-Z0-9_\-+]+)", d.strip())
        if m:
            names.add(m.group(1).lower())
    return names

def is_malicious_package(pkgname: str) -> bool:
    return pkgname.lower() in MALICIOUS_EXACT

def contains_malicious_dep(srcinfo: SrcInfo) -> List[str]:
    hits = []
    # 1) El paquete EN SÍ es uno de los maliciosos exactos
    if srcinfo.pkgbase.lower() in MALICIOUS_EXACT:
        hits.append(f"{srcinfo.pkgbase}: paquete malicioso exacto (Atomic Arch)")
    for pname, pkg in srcinfo.packages.items():
        if pname.lower() in MALICIOUS_EXACT:
            hits.append(f"{pname}: paquete malicioso exacto (Atomic Arch)")
            continue
        # 2) Depende de un malicioso exacto
        deps = set(_extract_dep_names(pkg.depends_for() + pkg.makedepends_for()))
        for mal in MALICIOUS_EXACT:
            if mal in deps:
                hits.append(f"{pname}: depende de paquete malicioso exacto '{mal}'")
    return hits

def is_js_legitimate(pkgbase: str) -> bool:
    return pkgbase.lower() in JS_ALLOWLIST

def check_atomic_arch(srcinfo: SrcInfo, raw_pkgbuild_text: str | None = None) -> tuple[bool, List[str]]:
    """
    Retorna (bloquear, razones). False = pasa.
    Lógica refinada:
    - Bloquear si depende exactamente de atomic-lockfile/js-digest/lockfile-js
    - Bloquear si contiene npm install / bun install sin evidencia de hash/pinning (solo si no está en allowlist)
    - No bloquear todo npm/bun genérico
    """
    reasons: List[str] = []

    # 1. Exact malicious dep
    reasons.extend(contains_malicious_dep(srcinfo))

    # 2. Chequeo raw PKGBUILD si provisto (solo para auditoría, no para evaluar)
    if raw_pkgbuild_text:
        if NPM_BUN_WITHOUT_HASH_RE.search(raw_pkgbuild_text):
            # Verificar si hay hash asociado: buscar sha256sums no SKIP en .SRCINFO
            has_hash = any(
                s != "SKIP" and len(s) >= 32
                for pkg in srcinfo.packages.values()
                for algo in pkg.sums.values()
                # cualquier arch cualificada conocida + genérico
                for vals in ([algo.get("", [])] + [a for k, a in algo.items() if k])
                for s in vals
            )
            if not has_hash and not is_js_legitimate(srcinfo.pkgbase):
                reasons.append(
                    f"{srcinfo.pkgbase}: contiene `npm/bun install` sin hash SHA256 (vector Atomic Arch) y no está en allowlist"
                )
            elif not has_hash and is_js_legitimate(srcinfo.pkgbase):
                # allowlist pasa pero log warning
                pass

    # 3. Heurística: dependencia npm/bun suelta NO es bloqueo si está en allowlist o si hay -bin legítimo
    # (no añadimos bloqueo genérico aquí)

    block = len(reasons) > 0
    return block, reasons

def is_restricted(srcinfo) -> tuple[bool, str]:
    """Retorna (restringido, razón). Un paquete restringido tiene licencia
    no redistribuible (custom/commercial/EULA o lista explícita): su plantilla
    se marca `restricted=yes` y el .xbps generado NUNCA debe publicarse."""
    explicit = {"spotify", "google-chrome", "visual-studio-code-bin",
                "postman-bin", "anydesk-bin", "brave-bin"}
    nonfree_markers = {"custom", "unknown", "commercial", "proprietary", "eula"}
    for pname, pkg in srcinfo.packages.items():
        for lic in [l.lower() for l in pkg.license]:
            if any(m in lic for m in nonfree_markers):
                return True, f"{pname}: licencia '{lic}'"
    if srcinfo.pkgbase.lower() in explicit:
        return True, f"{srcinfo.pkgbase}: binario propietario upstream"
    return False, ""


def validate_license(srcinfo: SrcInfo, allow_nonfree: bool = False) -> List[str]:
    """Filtra licencias no redistribuibles si repo será público."""
    warnings = []
    nonfree_markers = {"custom", "unknown", "commercial", "proprietary", "EULA"}
    for pname, pkg in srcinfo.packages.items():
        licenses = [l.lower() for l in pkg.license]
        for lic in licenses:
            if any(m in lic for m in nonfree_markers):
                if not allow_nonfree:
                    warnings.append(
                        f"{pname}: licencia '{lic}' posiblemente no redistribuible (spotify/chrome). No publicar .xbps sin permiso."
                    )
    # Caso explícito spotify/chrome
    if srcinfo.pkgbase.lower() in {"spotify", "google-chrome"} and not allow_nonfree:
        warnings.append(f"{srcinfo.pkgbase}: binario propietario, filtrar si repo público")
    return warnings

if __name__ == "__main__":
    from src.aur.parser import parse_srcinfo
    txt = """
pkgbase = evil
    pkgver = 1
    pkgrel = 1
pkgname = evil
    depends = atomic-lockfile
    makedepends = npm
"""
    si = parse_srcinfo(txt)
    block, rs = check_atomic_arch(si)
    assert block, rs
    print("security block OK", rs)
    txt2 = """
pkgbase = visual-studio-code-bin
    pkgver = 1.9
    pkgrel = 1
pkgname = visual-studio-code-bin
    depends = libx11
"""
    si2 = parse_srcinfo(txt2)
    block2, _ = check_atomic_arch(si2, raw_pkgbuild_text="npm install")
    assert not block2
    print("allowlist OK")
