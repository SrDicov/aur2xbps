# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests Fase 4 madurez — generador Nix + TRH.

H-3.1 relativización de symlinks en DERIVATION_BIN (plantilla)
H-3.2 oráculo repology-sync solo ante inaccesibilidad HTTP total
H-4.2 determinize: pax/xattrs fuera, uid/gid=0 en cabecera, zstd pineable
"""
import io
import json
import tarfile

import pytest

from src.xbps.determinize import determinize_tar


# ---------------------------------------------------------------- H-3.1
def test_plantilla_bin_relativiza_symlinks():
    from src.nix.generator import DERIVATION_BIN
    assert "find \"$out\" -type l" in DERIVATION_BIN
    assert "realpath --relative-to" in DERIVATION_BIN
    # sin expansión ${} bash que rompa el string Nix ''
    body = DERIVATION_BIN[DERIVATION_BIN.index("unpackPhase"):DERIVATION_BIN.index("meta =")]
    assert "${_t#/" not in body and "${N}" not in body


def test_symlink_absoluto_dentro_de_jerarquia_se_relativiza(tmp_path):
    """Comportamiento real del bucle generado: simulamos el shell del
    installPhase sobre un árbol con symlink /usr/lib/foo -> /usr/share/foo."""
    import subprocess
    root = tmp_path / "out"
    (root / "usr/lib").mkdir(parents=True)
    (root / "usr/share").mkdir(parents=True)
    (root / "usr/share/target.txt").write_text("x")
    link = root / "usr/lib/foo"
    link.symlink_to("/usr/share/target.txt")       # absoluto estilo upstream

    script = r'''
set -e
out="$1"
find "$out" -type l | while IFS= read -r _l; do
  _t=$(readlink "$_l")
  case "$_t" in
    /*)
      _cand="$out$_t"
      if [ -e "$_cand" ]; then
        _rel=$(realpath --relative-to "$(dirname "$_l")" "$_cand")
        ln -sfn "$_rel" "$_l"
      else
        rm -f "$_l"
      fi
      ;;
    *)
      if [ ! -e "$_l" ]; then rm -f "$_l"; fi
      ;;
  esac
done
'''
    sh = tmp_path / "fix.sh"
    sh.write_text(script)
    subprocess.run(["sh", str(sh), str(root)], check=True, timeout=30)
    assert os_readlink(link) == "../share/target.txt"


def os_readlink(p):
    import os
    return os.readlink(p)


def test_symlink_absoluto_fuera_de_arbol_se_elimina(tmp_path):
    import subprocess
    root = tmp_path / "out"
    (root / "etc").mkdir(parents=True)
    link = root / "etc/shadow-link"
    link.symlink_to("/etc/shadow")                 # fuera de $out → roto

    script = tmp_path / "fix.sh"
    script.write_text(r'''
out="$1"
find "$out" -type l | while IFS= read -r _l; do
  _t=$(readlink "$_l")
  case "$_t" in
    /*) [ -e "$out$_t" ] || { rm -f "$_l"; echo "eliminado $_l"; } ;;
    *)  [ -e "$_l" ] || rm -f "$_l" ;;
  esac
done
''')
    r = subprocess.run(["sh", str(script), str(root)], capture_output=True,
                       text=True, timeout=30)
    assert not link.exists()
    assert "eliminado" in r.stdout


# ---------------------------------------------------------------- H-3.2
def test_repology_oracle_solo_con_inaccesibilidad_total():
    src = open("scripts/repology-sync.py").read()
    assert "http_errors >= len(candidates)" in src
    assert "no se requiere oráculo offline" in src


# ---------------------------------------------------------------- H-4.2
def _build_tar(with_pax: bool) -> bytes:
    """Construye ustar manualmente con entrada normal + PaxHeader opcional."""
    def header(name: str, size: int, typeflag: bytes = b"0",
               uid: int = 1000) -> bytearray:
        b = bytearray(512)
        nb = name.encode()[:100]
        b[0:len(nb)] = nb
        b[100:108] = b"0000644\x00"
        b[108:116] = f"{uid:07o}\0".encode()      # uid no-cero a propósito
        b[116:124] = b"0000000\x00"
        b[124:136] = (f"{size:011o}" + "\0").encode()
        b[136:148] = b"00000000000"[:11] + b"\0"  # mtime 0
        b[148:156] = b" " * 8
        b[156:157] = typeflag
        b[257:262] = b"ustar"
        b[263:265] = b"00"
        b[265:297] = b"user\0".ljust(32)          # uname string (volátil)
        ck = sum(b)
        b[148:156] = ("%06o\0 " % ck).encode()
        return b

    out = bytearray()
    if with_pax:
        payload = b"30 mtime=1699999999.999999999\n"   # metadato volátil host
        out += header("PaxHeader/global", len(payload), typeflag=b"x", uid=0)
        out += payload + b"\0" * ((512 - len(payload) % 512) % 512)
    data = b"hola-contenido-determinista\n"
    out += header("file.txt", len(data), uid=1000)
    pad = (512 - len(data) % 512) % 512
    out += data + b"\0" * pad
    out += bytes(1024)                                 # fin + padding
    return bytes(out)


def test_determinize_tar_elimina_pax_y_fuerza_uid_gid():
    raw = _build_tar(with_pax=True)
    fixed = determinize_tar(raw)
    # PaxHeader desapareció
    assert b"PaxHeader" not in fixed.split(b"hola")[0][:512 * 3]
    with tarfile.open(fileobj=io.BytesIO(fixed)) as tf:
        members = tf.getmembers()
        assert [m.name for m in members] == ["file.txt"]
        m = members[0]
        assert m.uid == 0 and m.gid == 0
        assert m.uname == "" and m.gname == ""
        assert m.pax_headers == {}


def test_determinize_tar_estable_idempotente():
    raw = _build_tar(with_pax=False)
    once = determinize_tar(raw)
    twice = determinize_tar(once)
    assert once == twice


def test_zstd_pineable_por_env(monkeypatch, tmp_path):
    from src.common.tools import find_tool
    find_tool.cache_clear()
    # override apuntando a fichero inexistente + herramienta inexistente → error
    monkeypatch.setenv("AUR2XBPS_ZSTD", "/opt/zstd-pineado/bin/zstd")
    with pytest.raises(FileNotFoundError):
        find_tool("herramienta-inexistente-xyz", "AUR2XBPS_ZSTD")
    # override válido gana sobre PATH
    fake = tmp_path / "zstd-fake-bin"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("AUR2XBPS_ZSTD", str(fake))
    assert find_tool("herramienta-inexistente-xyz", "AUR2XBPS_ZSTD") == str(fake)
    find_tool.cache_clear()


def test_determinize_xbps_resuelve_zstd_via_find_tool(tmp_path, monkeypatch):
    src = open("src/xbps/determinize.py").read()
    assert 'find_tool("zstd", "AUR2XBPS_ZSTD")' in src
