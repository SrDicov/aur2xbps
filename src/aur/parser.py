# SPDX-License-Identifier: GPL-3.0-or-later
"""Parser .SRCINFO — sin evaluar PKGBUILD, cobertura completa de campos.

Formato: `key = value` sin comillas, claves repetidas = arrays,
cabecera pkgbase luego secciones pkgname, claves arch-qualified
(source_x86_64, depends_i686, b2sums_aarch64, ...).

Ref: AGENTS.md:Arch input, gotcha .SRCINFO stale.
"""
from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Dict, List
from src.common.types import (SrcInfo, SrcInfoPackage, SCALAR_KEYS, ARRAY_KEYS,
                              ARCHES, split_arch_key)

log = logging.getLogger(__name__)

# Sumas soportadas por makepkg -> clave interna corta
SUM_ALIASES = {
    "md5sums": "md5", "sha1sums": "sha1", "sha224sums": "sha224",
    "sha256sums": "sha256", "sha384sums": "sha384", "sha512sums": "sha512",
    "b2sums": "b2",
}

# --- Sanitización léxica (H-1.2 / AUDIT-2026-08) ---
# El .SRCINFO es input no confiable: control chars rompen plantillas Nix/XBPS,
# nombres no-ASCII permiten homoglifos que burlan mapeos y filtros.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+-]*$")     # pkgbase/pkgname
VER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+~-]*$")     # pkgver (más laxo que makepkg)
KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
MAX_FILE_BYTES = 4 * 1024 * 1024      # .SRCINFO real más grande conocido <100KB
MAX_LINE_LEN = 16 * 1024              # línea absurda = manipulación/DoS
MAX_PACKAGES = 200                    # split packages absurdos = DoS
KNOWN_ROOTS = set(SCALAR_KEYS) | set(ARRAY_KEYS)


def _check_name(kind: str, value: str) -> None:
    if not NAME_RE.match(value):
        raise ValueError(
            f"{kind} con caracteres inválidos (¿homoglifos/manipulación?): {value!r}")


def parse_srcinfo(text: str) -> SrcInfo:
    """Parsea texto .SRCINFO. Levanta ValueError si formato inválido o si se
    detectan caracteres de control; claves desconocidas generan warnings en
    SrcInfo.warnings sin abortar (visibilidad, no silencio)."""
    if len(text.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
        raise ValueError(f".SRCINFO excede {MAX_FILE_BYTES // (1024*1024)}MB — rechazado")
    if CONTROL_CHARS_RE.search(text):
        bad = CONTROL_CHARS_RE.search(text).group(0)
        raise ValueError(
            f".SRCINFO contiene carácter de control no imprimible "
            f"(\\x{ord(bad):02x}) — posible manipulación, rechazado")

    warnings: List[str] = []
    pkgbase: str | None = None
    current_section = "pkgbase"
    section_data: Dict[str, Dict[str, List[str]]] = {"pkgbase": {}}
    pkgs_order: List[str] = []

    for raw in text.splitlines():
        if len(raw) > MAX_LINE_LEN:
            raise ValueError(f"Línea de {len(raw)} chars excede el límite — rechazada")
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if line in ("pkgbase", "pkgname"):
                continue  # tolera headers sueltos de generadores antiguos
            raise ValueError(f"Línea sin '=' inválida: {raw!r}")
        k, v = [s.strip() for s in line.split("=", 1)]
        if not k:
            raise ValueError(f"Clave vacía en línea: {raw!r}")
        if not KEY_RE.match(k):
            raise ValueError(
                f"Clave con caracteres fuera de [A-Za-z0-9_]: {k!r}")

        root, arch = split_arch_key(k)
        if root not in KNOWN_ROOTS:
            msg = (f"clave desconocida '{k}'"
                   + (f" (sufijo arch '{arch}' inválido)" if "_" in k else "")
                   + ": ignorada")
            warnings.append(msg)
            log.warning(".SRCINFO: %s", msg)

        # Normalizar arrays parenizados de makepkg: 'key = (a b "c d")'
        if v.startswith("(") and v.endswith(")"):
            inner = v[1:-1].strip()
            parts = [p.strip().strip('"\'') for p in inner.split()]
            for p in parts:
                if not p:
                    continue
                if k == "pkgbase":
                    _check_name("pkgbase", p)
                    pkgbase = p
                    section_data["pkgbase"].setdefault("pkgbase", []).append(p)
                    current_section = "pkgbase"
                elif k == "pkgname":
                    _check_name("pkgname", p)
                    current_section = p
                    if p not in section_data:
                        section_data[p] = {}
                        pkgs_order.append(p)
                        if len(section_data) > MAX_PACKAGES:
                            raise ValueError(f"> {MAX_PACKAGES} subpaquetes — DoS, rechazado")
                    section_data[p].setdefault("pkgname", []).append(p)
                else:
                    target = section_data.setdefault(current_section, {})
                    target.setdefault(k, []).append(p)
            continue

        if k == "pkgbase":
            _check_name("pkgbase", v)
            pkgbase = v
            section_data["pkgbase"].setdefault("pkgbase", []).append(v)
            current_section = "pkgbase"
            continue
        if k == "pkgname":
            _check_name("pkgname", v)
            current_section = v
            if v not in section_data:
                section_data[v] = {}
                pkgs_order.append(v)
                if len(section_data) > MAX_PACKAGES:
                    raise ValueError(f"> {MAX_PACKAGES} subpaquetes — DoS, rechazado")
            section_data[v].setdefault("pkgname", []).append(v)
            continue

        target = section_data.setdefault(current_section, {})
        target.setdefault(k, []).append(v)

    if not pkgbase:
        if pkgs_order:
            pkgbase = pkgs_order[0]
            section_data.setdefault("pkgbase", {})["pkgbase"] = [pkgbase]
        else:
            raise ValueError("No se encontró pkgbase ni pkgname en .SRCINFO")

    base_info = section_data.get("pkgbase", {})
    result = SrcInfo(pkgbase=pkgbase, base_values=base_info, packages={},
                     warnings=warnings)

    # Monopaquete sin sección pkgname explícita: todo vive en pkgbase
    names = pkgs_order if pkgs_order else [pkgbase]

    def merged(pkg_dict: Dict[str, List[str]], key: str) -> List[str]:
        """pkg-specific primero; fallback a base para metadatos heredables."""
        if key in pkg_dict and pkg_dict[key]:
            return pkg_dict[key]
        return base_info.get(key, [])

    for pname in names:
        pdict = section_data.get(pname, {}) if pname != pkgbase or pkgs_order else base_info
        # En monopaquete (sin pkgname), pdict == base_info ya cubre todo.
        if not pkgs_order:
            pdict = base_info

        pkgver_val = (merged(pdict, "pkgver") or ["0"])[0]
        if not VER_RE.match(pkgver_val):
            raise ValueError(f"{pname}: pkgver con formato inválido: {pkgver_val!r}")
        epoch_val = (merged(pdict, "epoch") or [None])[0]
        if epoch_val is not None and not str(epoch_val).isdigit():
            raise ValueError(f"{pname}: epoch debe ser numérico: {epoch_val!r}")

        p = SrcInfoPackage(
            pkgname=pname,
            pkgbase=pkgbase,
            pkgver=pkgver_val,
            pkgrel=(merged(pdict, "pkgrel") or ["1"])[0],
            epoch=epoch_val,
            pkgdesc=(merged(pdict, "pkgdesc") or [None])[0],
            url=(merged(pdict, "url") or [None])[0],
            install=(pdict.get("install") or base_info.get("install") or [None])[0],
            changelog=(pdict.get("changelog") or base_info.get("changelog") or [None])[0],
            arch=merged(pdict, "arch"),
            license=merged(pdict, "license"),
            groups=merged(pdict, "groups"),
            options=merged(pdict, "options"),
            backup=merged(pdict, "backup"),
            noextract=merged(pdict, "noextract"),
            validpgpkeys=merged(pdict, "validpgpkeys"),
            provides=merged(pdict, "provides"),
            conflicts=merged(pdict, "conflicts"),
            replaces=merged(pdict, "replaces"),
        )

        # source[_arch]
        src_map: Dict[str, List[str]] = {}
        for key in set(list(pdict.keys()) + list(base_info.keys())):
            root, arch = split_arch_key(key)
            if root == "source":
                vals = pdict.get(key) or base_info.get(key) or []
                if vals and (src_map.get(arch or "") is None or not src_map.get(arch or "")):
                    src_map[arch or ""] = vals
        p.source = {k: v for k, v in src_map.items() if v}

        # sumas por algoritmo y arch
        sums: Dict[str, Dict[str, List[str]]] = {}
        for key in set(list(pdict.keys()) + list(base_info.keys())):
            root, arch = split_arch_key(key)
            algo = SUM_ALIASES.get(root)
            if algo:
                vals = pdict.get(key) or base_info.get(key) or []
                if vals:
                    sums.setdefault(algo, {}).setdefault(arch or "", vals)
        p.sums = sums

        # depends/makedepends/checkdepends/optdepends con variantes arch
        for dep_root, attr in (("depends", "depends"), ("makedepends", "makedepends"),
                               ("checkdepends", "checkdepends"), ("optdepends", "optdepends")):
            dm: Dict[str, List[str]] = {}
            for key in set(list(pdict.keys()) + list(base_info.keys())):
                r2, arch = split_arch_key(key)
                if r2 == dep_root:
                    vals = pdict.get(key) or base_info.get(key) or []
                    if vals:
                        dm.setdefault(arch or "", vals)
            setattr(p, attr, dm)

        result.packages[pname] = p

    return result


def parse_srcinfo_file(path: str | Path) -> SrcInfo:
    return parse_srcinfo(Path(path).read_text(encoding="utf-8"))


def validate_srcinfo_staleness(srcinfo: SrcInfo, git_tag: str | None) -> list[str]:
    """Detecta divergencia pkgver() stale. Retorna errores."""
    errs = []
    if not git_tag:
        return errs
    for pname, pkg in srcinfo.packages.items():
        norm_tag = git_tag[1:] if git_tag.startswith("v") else git_tag
        if norm_tag != pkg.pkgver:
            errs.append(f"{pname}: pkgver .SRCINFO={pkg.pkgver} != git tag {git_tag} -> stale, abortar")
    return errs


if __name__ == "__main__":
    sample = """
pkgbase = foo
    pkgver = 1.0
    pkgrel = 2
    arch = x86_64
    source_x86_64 = https://example.com/foo-1.0.tar.gz
    sha256sums_x86_64 = abc123
    validpgpkeys = ABCDEF1234567890

pkgname = foo
    depends = bar>=1.0
    depends_x86_64 = baz

pkgname = foo-docs
    depends = foo
    arch = any
"""
    si = parse_srcinfo(sample)
    assert si.pkgbase == "foo"
    assert set(si.packages) == {"foo", "foo-docs"}
    assert si.packages["foo"].depends_for() == ["baz"]          # arch-qualified gana
    assert si.packages["foo"].depends_for("i686") == ["bar>=1.0"]
    assert si.packages["foo"].validpgpkeys == ["ABCDEF1234567890"]
    print("parser completo OK")
