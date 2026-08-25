# SPDX-License-Identifier: GPL-3.0-or-later
"""Detección ÚNICA de paquetes precompilados (fuente de verdad compartida
por el motor Nix, las plantillas xbps-src y el filtro musl).

Doctrina: solo nombre ``-bin`` o formatos inequívocos (.deb/.rpm/.AppImage)
son precompilados; tarballs/zip son FUENTE. Un asset suelto bajo
``/releases/download/`` que no sea tarball conocido también lo es.

En Void-musl estos paquetes se DESCARTAN por defecto (upstream glibc),
válvula: AUR2XBPS_MUSL_ALLOW_BIN=1.
"""
from __future__ import annotations

BIN_NAME_SUFFIX = "-bin"
#: formatos inequívocamente binarios (independientes del nombre)
UNAMBIGUOUS_EXTS = (".deb", ".rpm", ".appimage")
#: tarballs conocidos = FUENTE aunque vivan en releases/
TARBALL_EXTS = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2",
                ".tar.zst", ".tzst", ".zip", ".tar")


def _clean_url(u: str) -> str:
    return str(u).lower().split("?")[0].split("#")[0]


def is_precompiled(pkgname: str, pkgbase: str = "", urls=()) -> bool:
    """True si el paquete distribuye un binario precompilado upstream."""
    name = pkgname.lower()
    base = (pkgbase or pkgname).lower()
    if name.endswith(BIN_NAME_SUFFIX) or base.endswith(BIN_NAME_SUFFIX):
        return True
    for u in urls or ():
        low = _clean_url(u)
        if low.endswith(UNAMBIGUOUS_EXTS):
            return True
        if "releases/download/" in low and not low.endswith(TARBALL_EXTS):
            return True
    return False
