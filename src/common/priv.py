# SPDX-License-Identifier: GPL-3.0-or-later
"""Elevador de privilegios universal — CERO 'sudo' hardcodeado en el repo.

Orden de resolución:
  1. root efectivo → argv sin envolver
  2. ``$AUR2XBPS_PRIV`` (shlex.split: admite "doas -u root", "sudo -n", …)
  3. TOML ``[priv] command`` (misma semántica que la env)
  4. Autodetección en PATH: sudo → doas → run0 → pkexec → su

Shapes soportadas:
  - prefijo argv   : sudo / doas / run0 / override multi-token
  - pkexec         : exige ruta ABSOLUTA del ejecutable (polkit)
  - su             : ÚLTIMO recurso, shape string-shell + warning a stderr
                     (pide contraseña en tty → inviable sin terminal)

API canónica: ``priv_wrap(argv)``. ``tools.sudo_prefix()`` queda como
compatibilidad SOLO para elevadores de forma prefijo.
"""
from __future__ import annotations

import os
import shlex
import shutil
import sys

#: elevadores con shape "prefijo argv": [<elevador> ...] + argv
PREFIX_ELEVATORS = ("sudo", "doas", "run0")
#: orden completo de autodetección (pkexec tiene shape propia)
DETECT_ORDER = ("sudo", "doas", "run0", "pkexec", "su")

_warned_su = False


def _is_root() -> bool:
    return hasattr(os, "getuid") and os.getuid() == 0


def _config_command() -> str:
    """Valor de ``[priv] command`` del TOML (la env se lee live aparte)."""
    try:
        from src.common.config import get_config
        return getattr(get_config(), "priv_command", "") or ""
    except Exception:                                        # noqa: BLE001
        return ""


def detect() -> list[str]:
    """Tokens base del elevador. Sin cache intencional: barato (which ×5)
    y así los monkeypatch de tests y cambios de env aplican al vuelo."""
    forced = os.environ.get("AUR2XBPS_PRIV") or _config_command()
    if forced:
        toks = shlex.split(forced)
        if not toks or not all(toks):
            raise ValueError(
                "AUR2XBPS_PRIV/[priv] command sin tokens válidos tras "
                f"shlex.split: {forced!r}")
        return toks
    for tool in DETECT_ORDER:
        if shutil.which(tool):
            return [tool]
    raise FileNotFoundError(
        "sin root ni elevador disponible (sudo/doas/run0/pkexec/su): "
        "instala doas o exporta AUR2XBPS_PRIV='<comando> [args]'")


def _shape(tokens: list[str]) -> str:
    base = os.path.basename(tokens[0])
    if base == "pkexec":
        return "pkexec"
    if base == "su":
        return "shell"
    return "prefix"


def priv_wrap(argv: list[str]) -> list[str]:
    """Envuelve ``argv`` para ejecutarlo con privilegios según el elevador."""
    global _warned_su
    if _is_root():
        return list(argv)
    toks = detect()
    shape = _shape(toks)
    if shape == "prefix":
        return [*toks, *argv]
    if shape == "pkexec":
        exe = shutil.which(argv[0]) or argv[0]
        return [*toks, exe, *argv[1:]]
    # su: último recurso — aviso único por proceso
    if not _warned_su:
        print("[priv] WARNING: elevador 'su' detectado: pide contraseña en "
              "tty y NO sirve para pipelines automáticos "
              "(exporta AUR2XBPS_PRIV=sudo|doas)", file=sys.stderr)
        _warned_su = True
    return [*toks, "root", "-c", shlex.join(argv)]


def reset_state() -> None:
    """Limpia aviso único de su (uso en tests)."""
    global _warned_su
    _warned_su = False
