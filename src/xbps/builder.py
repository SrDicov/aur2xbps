# SPDX-License-Identifier: GPL-3.0-or-later
"""XBPS builder — reproducibilidad + firma.

Correcciones: SOURCE_DATE_EPOCH=0 en xbps-create, tar --sort-name --mtime=@0,
xbps-rindex --sign --privkey desde primer repo.
"""
from __future__ import annotations
import os
import subprocess
import tarfile
import hashlib
from pathlib import Path
from typing import List, Optional

from src.common.tools import find_xbps_tool, sudo_prefix


def XBPS_CREATE() -> str:  # noqa: N802 — API histórica, resuelve en runtime
    return find_xbps_tool("xbps-create")


def XBPS_RINDEX() -> str:  # noqa: N802
    return find_xbps_tool("xbps-rindex")

# H-5.2: banderas cuyo valor siguiente es un secreto/ruta sensible — jamás
# imprimir en logs ni incrustar en excepciones (runit journal, tracebacks).
SECRET_FLAGS = {"--privkey", "--sign-key"}


def redact_cmd(cmd) -> List[str]:
    """Copia del argv con valores tras banderas de secreto sustituidos."""
    out: List[str] = []
    redact_next = False
    for part in cmd:
        s = str(part)
        if redact_next:
            out.append("<redacted>")
            redact_next = False
        else:
            out.append(s)
            redact_next = s in SECRET_FLAGS
    return out


def _run(cmd: List[str], env: dict | None = None, timeout: int = 600):
    print("$", " ".join(redact_cmd(cmd)))
    try:
        return subprocess.run(cmd, check=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        if isinstance(e.cmd, (list, tuple)):
            e.cmd = redact_cmd(e.cmd)
        raise
    except subprocess.CalledProcessError as e:
        if isinstance(e.cmd, (list, tuple)):
            e.cmd = redact_cmd(e.cmd)
        raise

def create_xbps(
    stage_dir: Path,
    out_path: Path,
    arch: str | None = None,   # None → detectada (config)
    pkgver: str = "foo-1.0_1",
    desc: str = "aur2xbps package",
    dependencies: str = "",
    provides: str = "",
    conflicts: str = "",
    replaces: str = "",
    shlib_provides: str = "",
    shlib_requires: str = "",
    compression: str = "zstd",
    license: str = "custom:unknown",
    maintainer: str = "aur2xbps <aur2xbps@local>",
    homepage: str = "",
) -> Path:
    """Stage dir debe contener árbol FHS (ej. stage/usr). Usa SOURCE_DATE_EPOCH=0."""
    if arch is None:
        from src.common.config import get_config
        arch = get_config().arch
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = "0"
    env["TZ"] = "UTC"
    env["LC_ALL"] = "C"
    # Staging 100% determinista: mtime, owner, group, sorted (ver TRH test 2026-08-21)
    # Orden mandatorio: touch -> chown -> sort; si chown falla sin sudo, reintentar con sudo
    subprocess.run(["find", str(stage_dir), "-exec", "touch", "-h", "-d", "@0", "{}", ";"], check=False)
    # chown 0:0 requiere root; intentar sin sudo y luego con sudo
    ret = subprocess.run(["find", str(stage_dir), "-exec", "chown", "-h", "0:0", "{}", ";"], capture_output=True)
    if ret.returncode != 0:
        subprocess.run([*sudo_prefix(), "find", str(stage_dir), "-exec", "chown", "-h", "0:0", "{}", ";"], check=False)
    # Verificación: todos los archivos con mtime 0 y uid 0 (debug opcional)
    # xbps-create usa libarchive con SOURCE_DATE_EPOCH; el chown asegura numeric-owner determinista

    cmd = [
        XBPS_CREATE(),
        "-A", arch,
        "-n", pkgver,
        "-s", desc,
        "-m", maintainer,
        "--compression", compression,
    ]
    if homepage:
        cmd += ["-H", homepage]
    if license:
        cmd += ["-l", license]
    if dependencies:
        cmd += ["-D", dependencies]
    if provides:
        cmd += ["-P", provides]
    if conflicts:
        cmd += ["-C", conflicts]
    if replaces:
        cmd += ["-R", replaces]
    if shlib_provides:
        cmd += ["--shlib-provides", shlib_provides]
    if shlib_requires:
        cmd += ["--shlib-requires", shlib_requires]
    # dest = stage_dir, salida via stdout redir? xbps-create dest debe ser dir, genera $pkgver.$arch.xbps en cwd
    # Uso: xbps-create ... destdir ; mover
    # En spec: xbps-create ... fake_root/ ; genera en ./
    # Implementamos con cwd temporal
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # xbps-create escribe en cwd; usamos -- (posicional destdir)
    cmd.append(str(stage_dir))
    # Capturar output? xbps-create imprime path; lo redirigimos
    # Ejecutar y luego mover el .xbps generado al out_path deseado
    # Para determinismo, fijamos cwd vacío
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        full_cmd = cmd
        print(f"$ SOURCE_DATE_EPOCH=0 {' '.join(full_cmd)}")
        subprocess.run(full_cmd, check=True, env=env, cwd=td)
        # Buscar *.xbps en td
        candidates = list(Path(td).glob("*.xbps"))
        if not candidates:
            raise RuntimeError("xbps-create no generó .xbps")
        generated = candidates[0]
        # Verificar reproducibilidad tar args si existe
        # Mover: /tmp puede ser tmpfs distinto → os.rename falla con EXDEV;
        # shutil.move copia+mueve entre dispositivos.
        import shutil
        if out_path.is_dir():
            target = out_path / generated.name
        else:
            target = out_path
            target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(generated), str(target))
    return target

def rindex_add(repo_dir: Path, xbps_files: List[Path], sign: bool = True,
               privkey: Optional[Path] = None,
               signedby: str = "aur2xbps <aur2xbps@local>") -> None:
    """Indexa paquetes en el repositorio y firma (pkg .sig2 + repodata).

    Semántica xbps-rindex:
      -a            añade al índice
      --sign-pkg    genera firma RSA por-paquete (<pkg>.sig2)
      --sign        firma el repodata del repositorio
    """
    repo_dir = Path(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    for f in xbps_files:
        f = Path(f)
        if f.parent != repo_dir:
            shutil.copy2(f, repo_dir / f.name)

    files = sorted(str(p) for p in repo_dir.glob("*.xbps"))
    if not files:
        return

    # 1. indexar (-f obligatorio: sin él xbps-rindex conserva silenciosamente
    #    la entrada previa si el pkgver ya estaba indexado, con metadatos stale)
    _run([XBPS_RINDEX(), "-f", "-a"] + files)

    if sign and privkey and Path(privkey).exists():
        # 2. firmar cada paquete (.sig2). xbps-rindex NO regenera firmas
        #    existentes: si el .xbps cambió, la firma vieja invalida el
        #    paquete ("verifying RSA signature… removed"). Limpiar antes.
        for f in repo_dir.glob("*.sig2"):
            f.unlink()
        _run([XBPS_RINDEX(), "--sign-pkg", "--privkey", str(privkey),
              "--signedby", signedby] + files)
        # 3. firmar repodata
        _run([XBPS_RINDEX(), "--sign", "--privkey", str(privkey),
              "--signedby", signedby, str(repo_dir)])
    elif sign:
        print(f"WARN: privkey {privkey} no existe, repo sin firma")

def stage_from_nix_result(nix_result: Path, stage: Path):
    """Copia $out (usr/opt) → stage preservando perms y normalizando para TRH."""
    nix_result = Path(nix_result)
    stage = Path(stage)
    import shutil as _shutil
    if stage.exists():
        try:
            _shutil.rmtree(stage)
        except PermissionError:
            subprocess.run([*sudo_prefix(), "rm", "-rf", str(stage)], check=False)
            # reintentar si aún existe
            if stage.exists():
                _shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    real = nix_result.resolve()
    # Copiar usr y opt si existen (chrome necesita opt)
    for sub in ["usr", "opt", "etc", "var"]:
        src = real / sub
        if src.exists():
            dest = stage / sub
            _shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    # fallback: si no hay usr/opt (ej. hello con bin/share)
    if not any((stage / d).exists() for d in ["usr","opt"]):
        for item in real.iterdir():
            if item.name in ("usr","opt","etc","var"):
                continue
            dest = stage / item.name
            if item.is_dir():
                _shutil.copytree(item, dest, symlinks=True, dirs_exist_ok=True)
            else:
                _shutil.copy2(item, dest)
        # hello tiene bin/share en real root, no en usr? Ya copiado arriba si exists, sino fallback
        # Para hello, real tiene bin/share directamente, no usr
        if not (stage / "usr").exists() and (real / "bin").exists():
            # hello case: copiar bin/share a stage/usr? No, stage debe reflejar FHS
            # Para hello, queremos stage/usr/bin etc, pero real tiene bin en root, no usr
            # Copiamos bin->usr/bin y share->usr/share
            for sub in ["bin","share","lib"]:
                src = real / sub
                if src.exists():
                    dest = stage / "usr" / sub
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
    # Normalizar mtime + owner para TRH (ver test TRH 2026-08-21: requiere chown 0:0 + touch)
    subprocess.run(["find", str(stage), "-exec", "touch", "-h", "-d", "@0", "{}", ";"], check=False)
    ret = subprocess.run(["find", str(stage), "-exec", "chown", "-h", "0:0", "{}", ";"], capture_output=True)
    if ret.returncode != 0:
        subprocess.run([*sudo_prefix(), "find", str(stage), "-exec", "chown", "-h", "0:0", "{}", ";"], check=False)
    # Patchelf EEL se aplica antes de stage si viene de nix_result ya parcheado a Void; aquí solo normaliza

if __name__ == "__main__":
    print("builder ready")
