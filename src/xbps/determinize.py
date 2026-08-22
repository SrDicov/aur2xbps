# SPDX-License-Identifier: GPL-3.0-or-later
"""Post-procesador de reproducibilidad cross-host para .xbps.

Problema descubierto (T1): libarchive escribe uname/gname como STRING en las
cabeceras ustar cuando getpwuid(uid) resuelve (p.ej. root en contenedor),
dejándolas vacías en otros entornos. El checksum de cabecera cambia y con él
el hash del .xbps completo, aunque contenido y metadatos numéricos sean idénticos.

Solución: normalizar el tar interno — uname/gname a NULs (numeric-owner puro),
checksum recalculado — y recomprimir zstd determinista (-T1, nivel fijo).
"""
from __future__ import annotations
import hashlib
import subprocess
import tempfile
from pathlib import Path

BLOCK = 512
UNAME_OFF = 265   # offset uname en cabecera ustar
GNAME_OFF = 297   # offset gname
CKSUM_OFF = 148
CKSUM_END = 156   # exclusivo


def _fix_header(block: bytearray) -> None:
    """Cero uname/gname y recalcula checksum de una cabecera ustar."""
    block[UNAME_OFF:UNAME_OFF + 32] = b"\0" * 32
    block[GNAME_OFF:GNAME_OFF + 32] = b"\0" * 32
    block[CKSUM_OFF:CKSUM_END] = b" " * 8
    cksum = sum(block)
    octal = ("%06o\0 " % cksum).encode()
    block[CKSUM_OFF:CKSUM_OFF + 8] = octal


def determinize_tar(raw_tar: bytes) -> bytes:
    """Normaliza todas las cabeceras ustar del tar (uname/gname=∅, cksum OK)."""
    out = bytearray()
    pos = 0
    n = len(raw_tar)
    while pos < n:
        block = bytearray(raw_tar[pos:pos + BLOCK])
        if len(block) < BLOCK:
            out += block
            break
        if block == bytes(BLOCK):          # fin de archivo (bloques cero)
            out += raw_tar[pos:]           # preservar padding final tal cual
            break
        hdr_name = block[0:100].rstrip(b"\0")
        size_field = block[124:136].rstrip(b"\0 ")
        if hdr_name and size_field.startswith(b"0") or size_field.isdigit():
            try:
                size = int(size_field, 8) if size_field else 0
            except ValueError:
                size = 0
            _fix_header(block)
            out += block
            pos += BLOCK
            # copiar datos + padding de esta entrada sin tocar
            data_len = (size + BLOCK - 1) // BLOCK * BLOCK if size else 0
            out += raw_tar[pos:pos + data_len]
            pos += data_len
        else:
            # bloque no-cabecera (raro): copiar tal cual
            out += block
            pos += BLOCK
    return bytes(out)


def determinize_xbps(xbps_path: Path, zstd_level: int = 3,
                     zstd_bin: str = "zstd") -> tuple[Path, str]:
    """Lee un .xbps, normaliza su tar interno y lo recomprime determinista.
    Sobrescribe el original. Retorna (path, sha256_nuevo)."""
    xbps_path = Path(xbps_path)
    raw = subprocess.run([zstd_bin, "-dc", str(xbps_path)],
                         capture_output=True, check=True).stdout
    fixed = determinize_tar(raw)
    tmp = xbps_path.with_suffix(".xbps.det")
    with open(tmp, "wb") as fh:
        p = subprocess.Popen([zstd_bin, "-T1", f"-{zstd_level}", "-q"], stdin=subprocess.PIPE,
                             stdout=fh)
        p.communicate(fixed)
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    tmp.rename(xbps_path)
    return xbps_path, sha
