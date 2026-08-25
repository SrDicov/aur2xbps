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
UID_OFF = 108     # offset uid numérico (8 bytes octal)
GID_OFF = 116     # offset gid numérico (8 bytes octal)
TYPEFLAG_OFF = 156
CKSUM_OFF = 148
CKSUM_END = 156   # exclusivo


def _fix_header(block: bytearray) -> None:
    """Normaliza una cabecera ustar a estado canónico: uname/gname a NULs,
    uid/gid numéricos a 0 (H-4.2: no depender del chown previo, que puede
    fallar silenciosamente con check=False) y checksum recalculado."""
    block[UNAME_OFF:UNAME_OFF + 32] = b"\0" * 32
    block[GNAME_OFF:GNAME_OFF + 32] = b"\0" * 32
    block[UID_OFF:UID_OFF + 8] = b"0000000\x00"
    block[GID_OFF:GID_OFF + 8] = b"0000000\x00"
    block[CKSUM_OFF:CKSUM_END] = b" " * 8
    cksum = sum(block)
    octal = ("%06o\0 " % cksum).encode()
    block[CKSUM_OFF:CKSUM_OFF + 8] = octal


def _is_pax_entry(block: bytearray) -> bool:
    """Cabeceras pax (typeflag x/g o nombres PaxHeader/pax_global_header)
    filtran metadatos volátiles del host (xattrs, ACLs, ctime): H-4.2 — se
    eliminan para que el tar solo lleve ustar canónico."""
    tf = block[TYPEFLAG_OFF:TYPEFLAG_OFF + 1]
    if tf in (b"x", b"g"):
        return True
    name = bytes(block[0:100]).rstrip(b"\0")
    return name == b"pax_global_header" or name.endswith(b"/PaxHeader") \
        or b"/PaxHeader/" in name


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
        if _is_pax_entry(block):
            # saltar cabecera pax + su payload (metadatos volátiles del host)
            size_field = block[124:136].rstrip(b"\0 ")
            try:
                size = int(size_field, 8) if size_field else 0
            except ValueError:
                size = 0
            pos += BLOCK + (size + BLOCK - 1) // BLOCK * BLOCK
            continue
        hdr_name = block[0:100].rstrip(b"\0")
        size_field = block[124:136].rstrip(b"\0 ")
        # Cabecera válida exige: nombre presente, tamaño octal/decimal ASCII
        # (nunca high-bit → base-256 binario) y AMBAS condiciones sobre el
        # MISMO bloque. La precedencia original sin paréntesis trataba como
        # cabecera cualquier bloque cuyo size-field fuera dígitos puros.
        es_ascii_size = (not size_field) or size_field.isdigit()
        if hdr_name and not (size_field and size_field[0] & 0x80) and es_ascii_size:
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
                     zstd_bin: str | None = None) -> tuple[Path, str]:
    """Lee un .xbps, normaliza su tar interno y lo recomprime determinista.
    Sobrescribe el original. Retorna (path, sha256_nuevo).

    zstd_bin: None → resolución vía find_tool('zstd', AUR2XBPS_ZSTD) para
    permitir pinear una versión concreta (H-4.2: sub-versiones de zstd
    cambian heurísticas de compresión y rompen la identidad byte a byte)."""
    from src.common.tools import find_tool
    xbps_path = Path(xbps_path)
    zstd_bin = zstd_bin or find_tool("zstd", "AUR2XBPS_ZSTD")
    raw = subprocess.run([zstd_bin, "-dc", str(xbps_path)],
                         capture_output=True, check=True, timeout=600).stdout
    fixed = determinize_tar(raw)
    tmp = xbps_path.with_suffix(".xbps.det")
    with open(tmp, "wb") as fh:
        p = subprocess.Popen([zstd_bin, "-T1", f"-{zstd_level}", "-q"],
                             stdin=subprocess.PIPE, stdout=fh)
        try:
            # Techo duro (T-6): zstd colgado bloqueaba el pipeline para siempre
            p.communicate(fixed, timeout=300)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise RuntimeError(
                f"zstd no terminó en 300s recomprimiendo {xbps_path}")
    if p.returncode != 0:
        raise RuntimeError(f"zstd falló ({p.returncode}) con {xbps_path}")
    sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
    tmp.rename(xbps_path)
    return xbps_path, sha
