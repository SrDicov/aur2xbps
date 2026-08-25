# SPDX-License-Identifier: GPL-3.0-or-later
"""Filtro refinado Atomic Arch — AGENTS.md Gotchas.

Bloquea paquetes maliciosos exactos y la instalación de dependencias JS
(npm/bun/yarn/pnpm/deno) sin hash verificable. Exenciones SOLO con cadena
de custodia PGP ([security] trusted_pgp_keys) o allowlist deprecada.
Aborta antes de contenedor.
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
# Detección: PKGBUILD (texto crudo, NUNCA ejecutado) invoca gestores JS.
# H-2.1: cobertura ampliada — yarn/pnpm/deno/bower, formas abreviadas
# (`npm i`, `yarn ci`) y heurística básica de ofuscación (${N}${M} install).
JS_INSTALL_RE = re.compile(
    r"\b(npm|bun|yarn|pnpm|deno|bower)\s+(install|i|add|ci)\b"
    r"|\$\{[A-Za-z_][A-Za-z0-9_]*\}\s+(install|add)\b"
    r"|\b(npm|pnpm|bunx?|deno)\s+(run|exec|dlx)\s+\S+https?://",
    re.IGNORECASE)
# Alias de compatibilidad: mismo comportamiento ampliado
NPM_BUN_WITHOUT_HASH_RE = JS_INSTALL_RE
# Señal (no bloqueo): dependencia del ecosistema Node en depends/makedepends
NODE_ECOSYSTEM_DEPS = {"nodejs", "npm", "yarn", "pnpm", "deno", "bower"}

_allowlist_deprecation_warned = False

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

def is_js_legitimate(pkgbase: str, srcinfo: SrcInfo | None = None) -> bool:
    """Exención del filtro npm-sin-hash.

    Cadena de custodia preferente (H-2.2): el .SRCINFO declara validpgpkeys
    Y al menos una coincide con [security] trusted_pgp_keys de la config →
    el paquete está firmado por un mantenedor de confianza local.

    La allowlist hardcodeada está DEPRECADA: sigue funcionando para no romper
    flujos existentes pero emite warning; migrar a trusted_pgp_keys."""
    global _allowlist_deprecation_warned
    declared_keys = [k.strip().upper()
                     for p in (srcinfo.packages.values() if srcinfo else [])
                     for k in (p.validpgpkeys or [])]
    if srcinfo is not None and declared_keys:
        from src.common.config import get_config
        trusted = {k.strip().upper() for k in get_config().trusted_pgp_keys}
        if trusted and any(k in trusted for k in declared_keys):
            return True
    if pkgbase.lower() in JS_ALLOWLIST:
        if not _allowlist_deprecation_warned:
            import logging
            logging.getLogger(__name__).warning(
                "JS_ALLOWLIST hardcodeada deprecada (riesgo de secuestro de "
                "mantenedor); migra a [security] trusted_pgp_keys")
            _allowlist_deprecation_warned = True
        return True
    return False


def check_atomic_arch(srcinfo: SrcInfo, raw_pkgbuild_text: str | None = None) -> tuple[bool, List[str]]:
    """
    Retorna (bloquear, razones). False = pasa.
    Lógica refinada:
    - Bloquear si depende exactamente de atomic-lockfile/js-digest/lockfile-js
    - Bloquear si el texto del PKGBUILD instala deps JS (npm/bun/yarn/pnpm/
      deno/bower, incl. formas abreviadas/ofuscadas) sin evidencia de hash,
      salvo cadena de custodia PGP o allowlist deprecada
    - No bloquear todo npm/bun genérico
    """
    reasons: List[str] = []

    # 1. Exact malicious dep
    reasons.extend(contains_malicious_dep(srcinfo))

    # 1b. Señal ecosistema Node SIN hash en fuentes (visible, no bloqueante):
    #     el sandbox Nix (TIAR) es la contención real si llega a compilar.
    if raw_pkgbuild_text and JS_INSTALL_RE.search(raw_pkgbuild_text):
        has_hash = _has_real_hash(srcinfo)
        if not has_hash:
            node_dep = any(
                _extract_dep_names(pkg.depends_for() + pkg.makedepends_for())
                & NODE_ECOSYSTEM_DEPS
                for pkg in srcinfo.packages.values())
            if node_dep and not is_js_legitimate(srcinfo.pkgbase, srcinfo):
                import logging
                logging.getLogger(__name__).warning(
                    "%s: depende de Node sin hashes verificables — revisar "
                    "antes de publicar", srcinfo.pkgbase)

    # 2. Chequeo raw PKGBUILD si provisto (texto estático; jamás se ejecuta)
    if raw_pkgbuild_text:
        if JS_INSTALL_RE.search(raw_pkgbuild_text):
            has_hash = _has_real_hash(srcinfo)
            if not has_hash and not is_js_legitimate(srcinfo.pkgbase, srcinfo):
                reasons.append(
                    f"{srcinfo.pkgbase}: instala deps JS (npm/bun/yarn/pnpm/"
                    f"deno) sin hash SHA256 (vector Atomic Arch) y sin "
                    f"mantenedor PGP de confianza")

    block = len(reasons) > 0
    return block, reasons


def _has_real_hash(srcinfo: SrcInfo) -> bool:
    """¿Existe alguna suma real (no SKIP, longitud plausible) en el .SRCINFO?"""
    return any(
        s != "SKIP" and len(s) >= 32
        for pkg in srcinfo.packages.values()
        for algo in pkg.sums.values()
        # cualquier arch cualificada conocida + genérico
        for vals in ([algo.get("", [])] + [a for k, a in algo.items() if k])
        for s in vals
    )

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
    nonfree_markers = {"custom", "unknown", "commercial", "proprietary", "eula"}
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
