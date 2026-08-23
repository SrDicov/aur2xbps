# SPDX-License-Identifier: GPL-3.0-or-later
"""Tipos comunes G_Arch -> G_Nix -> G_XBPS — cobertura completa de campos .SRCINFO"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Claves escalares (una sola por sección; la última gana en .SRCINFO real)
SCALAR_KEYS = {"pkgbase", "pkgname", "pkgver", "pkgrel", "epoch", "pkgdesc", "url",
               "install", "changelog"}
# Claves array (repetibles). Las arch-qualified se detectan por sufijo _<arch>.
ARRAY_KEYS = {
    "arch", "license", "groups", "options", "backup",
    "source", "noextract", "validpgpkeys",
    "md5sums", "sha1sums", "sha224sums", "sha384sums", "sha256sums", "sha512sums", "b2sums",
    "depends", "makedepends", "checkdepends", "optdepends",
    "provides", "conflicts", "replaces",
}
ARCHES = ["x86_64", "i686", "aarch64", "armv7h", "armv6h", "pentium4", "any"]

def split_arch_key(key: str) -> tuple[str, str | None]:
    """'source_x86_64' -> ('source','x86_64'); 'depends' -> ('depends',None)"""
    for a in ARCHES:
        if key.endswith("_" + a):
            return key[: -(len(a) + 1)], a
    return key, None


@dataclass
class SrcInfoPackage:
    """Un subpaquete pkgname dentro de .SRCINFO con TODOS los campos válidos."""
    pkgname: str
    pkgbase: str
    pkgver: str = "0"
    pkgrel: str = "1"
    epoch: Optional[str] = None
    pkgdesc: Optional[str] = None
    url: Optional[str] = None
    install: Optional[str] = None
    changelog: Optional[str] = None
    arch: List[str] = field(default_factory=list)
    license: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    options: List[str] = field(default_factory=list)
    backup: List[str] = field(default_factory=list)
    # arrays genéricos y arch-qualified
    source: Dict[str, List[str]] = field(default_factory=dict)          # "" o arch
    noextract: List[str] = field(default_factory=list)
    validpgpkeys: List[str] = field(default_factory=list)
    sums: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)  # algo -> {arch: [vals]}
    depends: Dict[str, List[str]] = field(default_factory=dict)
    makedepends: Dict[str, List[str]] = field(default_factory=dict)
    checkdepends: Dict[str, List[str]] = field(default_factory=dict)
    optdepends: Dict[str, List[str]] = field(default_factory=dict)
    provides: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    replaces: List[str] = field(default_factory=list)

    # ---- helpers de acceso ----
    def _for_arch(self, d: Dict[str, List[str]], arch: str = "x86_64") -> List[str]:
        """Prioriza arch-qualified, fallback al genérico."""
        return d.get(arch) or d.get("", []) or []

    @property
    def all_sources(self, arch: str = "x86_64") -> List[str]:
        return self._for_arch(self.source, arch)

    def sources_for(self, arch: str = "x86_64") -> List[str]:
        return self._for_arch(self.source, arch)

    def sums_for(self, algo: str = "sha256", arch: str = "x86_64") -> List[str]:
        return self._for_arch(self.sums.get(algo, {}), arch)

    def depends_for(self, arch: str = "x86_64") -> List[str]:
        return self._for_arch(self.depends, arch)

    def makedepends_for(self, arch: str = "x86_64") -> List[str]:
        return self._for_arch(self.makedepends, arch)

    def checkdepends_for(self, arch: str = "x86_64") -> List[str]:
        return self._for_arch(self.checkdepends, arch)

    def optdepends_for(self, arch: str = "x86_64") -> List[str]:
        return self._for_arch(self.optdepends, arch)

    # Compatibilidad con API previa (atributos planos x86_64)
    @property
    def source_x86_64(self) -> List[str]:
        return self.sources_for("x86_64")

    @property
    def sha256sums_x86_64(self) -> List[str]:
        return self.sums_for("sha256", "x86_64")

    @property
    def sha512sums_x86_64(self) -> List[str]:
        return self.sums_for("sha512", "x86_64")

    @property
    def b2sums_x86_64(self) -> List[str]:
        return self.sums_for("b2", "x86_64")

    @property
    def flat_depends(self) -> List[str]:
        return self.depends_for("x86_64")

    @property
    def flat_makedepends(self) -> List[str]:
        return self.makedepends_for("x86_64")

    @property
    def pkgver_full(self) -> str:
        """Tupla XBPS: name[-epoch:]ver_rev"""
        ver = f"{self.pkgver}_{self.pkgrel}"
        if self.epoch and self.epoch != "0":
            ver = f"{self.epoch}:{ver}"
        return f"{self.pkgname}-{ver}"


@dataclass
class SrcInfo:
    pkgbase: str
    base_values: Dict[str, List[str]] = field(default_factory=dict)
    packages: Dict[str, SrcInfoPackage] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)   # H-1.2: claves/valores sospechosos tolerados


@dataclass
class ValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
