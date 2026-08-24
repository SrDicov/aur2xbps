# SPDX-License-Identifier: GPL-3.0-or-later
"""Mapeo heurístico de nombres de dependencia Arch → Void Linux.

Void y Arch comparten la mayoría de nombres, pero difieren en:
  - prefijo python:  Arch ``python-foo`` → Void ``python3-foo``
  - toolchain python: Arch ``setuptools`` → Void ``python3-setuptools``
  - GTK:             ``gtk3`` → ``gtk+3``, ``gtk4`` → ``gtk+4``
  - multilib:        ``lib32-*`` no existe en Void (se descarta)
  - paquetes meta:   ``base-devel`` → grupo ``base-devel`` (Void lo tiene)

Estrategia en capas: tabla manual → reglas de prefijos/sufijos → nombre directo.
Best-effort documentado: las plantillas son revisables antes de compilar
(vouru muestra el template al usuario).
"""
from __future__ import annotations

# nombre Arch → nombre Void (None = descartar)
MANUAL: dict[str, str | None] = {
    # toolchain / build básico
    "git": "git", "base-devel": "base-devel",
    "meson": "meson", "cmake": "cmake", "ninja": "ninja",
    "pkgconf": "pkg-config", "pkg-config": "pkg-config",
    "autoconf": "autoconf", "automake": "automake", "libtool": "libtool",
    "make": "make", "gcc": "gcc", "clang": "clang", "patchelf": "patchelf",
    "which": "which", "file": "file",
    # python
    "python": "python3", "python-setuptools": "python3-setuptools",
    "python-pip": "python3-pip", "python-wheel": "python3-wheel",
    "python-build": "python3-build", "python-installer": "python3-installer",
    "python-setuptools-scm": "python3-setuptools_scm",
    # gtk / gui
    "gtk3": "gtk+3", "gtk4": "gtk+4", "gtk2": "gtk+2",
    "glib2": "glib-devel", "gobject-introspection": "gobject-introspection",
    "libnotify": "libnotify", "libx11": "libX11-devel",
    "libxext": "libXext-devel", "libxrandr": "libXrandr-devel",
    "libxinerama": "libXinerama-devel", "libxcursor": "libXcursor-devel",
    "libxi": "libXi-devel", "libxrender": "libXrender-devel",
    "libxft": "libXft-devel", "libxtst": "libXtst-devel",
    "libxcb": "libxcb-devel", "cairo": "cairo-devel",
    "pango": "pango-devel", "gdk-pixbuf2": "gdk-pixbuf-devel",
    "harfbuzz": "harfbuzz-devel", "freetype2": "freetype-devel",
    "fontconfig": "fontconfig-devel", "wayland": "wayland-devel",
    "wayland-protocols": "wayland-protocols",
    "libsdl2": "SDL2-devel", "libsdl3": "SDL3-devel",
    "openssl": "openssl-devel",
    "curl": "curl-devel", "zlib": "zlib-devel",
    "xz": "liblzma-devel", "bzip2": "bzip2-devel",
    "sqlite": "sqlite-devel", "icu": "icu-devel",
    "ncurses": "ncurses-devel", "readline": "readline-devel",
    "dbus": "dbus-devel", "alsa-lib": "alsa-lib-devel",
    "pulseaudio": "pulseaudio-devel", "pipewire": "pipewire-devel",
    "ffmpeg": "ffmpeg-devel", "libpng": "libpng-devel",
    "libjpeg-turbo": "libjpeg-turbo-devel", "giflib": "giflib-devel",
    "expat": "expat-devel", "libuv": "libuv-devel",
    "libxss": "libXScrnSaver-devel", "libcups": "cups-devel",
    "nss": "nss",
    "ttf-liberation": "liberation-fonts-ttf",
    "ttf-font": "fontconfig",
    "ttf-nerd-fonts-symbols": "nerd-fonts-symbols-ttf",
    "gcc-libs": "libstdc++",
    "libcurl-gnutls": "libcurl",  # Void solo trae curl OpenSSL; el generador crea el shim .so junto a la app
    "libsm": "libSM",
    # deps comunes que faltaban en campaña 100-pkg
    "r": "R",
    "npm": "nodejs",
    "sh": "dash",
    "sh4": "dash",
    "systemd": None,
    "systemd-libs": None,
    # sin equivalente en Void: metadatos LSB, ninguna app los necesita en runtime
    "lsb-release": None,
    # systemd no existe en Void (runit+eudev); los Electron la cargan por
    # dlopen de forma opcional → descartar (si una app la exige, fallará
    # en el smoke con "libsystemd" y se añadirá shim específico)
    "systemd-libs": None,
    "xdg-utils": "xdg-utils", "libxt": "libXt-devel",
    "libxcomposite": "libXcomposite-devel", "libxdamage": "libXdamage-devel",
    "libxfixes": "libXfixes-devel", "libxxf86vm": "libXxf86vm-devel",
    "mesa": "MesaLib-devel", "libglvnd": "libglvnd-devel",
    "vulkan-icd-loader": "vulkan-loader", "vulkan-headers": "vulkan-headers",
}

# prefijos Arch → prefijos Void
PREFIX_RULES: list[tuple[str, str]] = [
    ("python-", "python3-"),
]


def map_dep(name: str) -> str | None:
    """Mapea un nombre de dependencia Arch a Void. None = descartar.

    Acepta versiones (``foo>=1.2``) y conserva el operador.
    """
    ver = ""
    base = name
    for sep in ("<", ">=", "=", ">"):
        if sep in base:
            base, _, ver = base.partition(sep)
            ver = sep + ver
            break
    base = base.strip()

    if base.startswith("lib32-"):
        return None

    if base in MANUAL:
        m = MANUAL[base]
        return f"{m}{ver}" if m else None

    for prefix, replacement in PREFIX_RULES:
        if base.startswith(prefix):
            return f"{replacement}{base[len(prefix):]}{ver}"

    return f"{base}{ver}"


def map_deps(deps: list[str]) -> list[str]:
    """Mapea una lista; descarta None y deduplica preservando orden."""
    out: list[str] = []
    for d in deps:
        m = map_dep(d)
        if m and m not in out:
            out.append(m)
    return out
