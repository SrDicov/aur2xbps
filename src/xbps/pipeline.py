# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipeline XBPS automatizado: Nix result → stage → patchelf EEL → xbps-create
→ firma → instalación en chroot Void → smoke test EEL.

Receta determinista TRH-validada en Fase 0:
  touch @0 → chown 0:0 → SOURCE_DATE_EPOCH=0 TZ=UTC LC_ALL=C xbps-create --compression zstd
"""
from __future__ import annotations
import hashlib
import os
import re
import shlex
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from src.xbps.builder import XBPS_CREATE, XBPS_RINDEX
from src.xbps.shlibs import ShlibsDB
from src.common.config import get_config
from src.common.paths import (FAKE_ROOT, PRIVKEY,
                              REPO_X86_64 as REPO, VOID_BASE)
from src.common.tools import find_xbps_tool
from src.common.priv import priv_wrap


def MASTERDIR() -> Path:  # noqa: N802 — masterdir efectivo (config o xbps-src)
    return get_config().effective_masterdir


def XBPS_INSTALL() -> str:  # noqa: N802
    return find_xbps_tool("xbps-install")


def XBPS_REMOVE() -> str:  # noqa: N802
    return find_xbps_tool("xbps-remove")


def XBPS_QUERY() -> str:  # noqa: N802
    return find_xbps_tool("xbps-query")


def VOID_INTERP() -> str:  # noqa: N802 — intérprete ELF según arch detectada
    from src.common.config import dynamic_linker
    return dynamic_linker(get_config().arch)


def _base_dep() -> str:
    """Dep base según libc objetivo (glibc|musl), formato pkg>=ver.
    Versión real desde el masterdir si se puede consultar; piso conocido
    como fallback. NUNCA hardcodear 'glibc' en los llamadores."""
    from src.common.config import effective_libc
    cfg = get_config()
    if effective_libc(cfg) == "musl":
        pkg, floor = "musl", "1.2.5"
    else:
        pkg, floor = "glibc", "2.41"
    try:
        q = _srun([XBPS_QUERY(), "-r", str(MASTERDIR()), pkg],
                  timeout=60)
        if q.returncode == 0:
            for line in (q.stdout or "").splitlines():
                tok = line.strip()
                m = re.match(rf"^{re.escape(pkg)}-(\d[\w.+-]*)$", tok)
                if m:
                    return f"{pkg}>={m.group(1)}"
    except Exception:                                        # noqa: BLE001
        pass
    return f"{pkg}>={floor}"


@dataclass
class XbpsResult:
    pkgver: str
    xbps_path: Optional[Path] = None
    sha256: Optional[str] = None
    signed: bool = False
    installed: bool = False
    smoke_ok: bool = False
    ldd_missing: int = 0
    errors: List[str] = field(default_factory=list)


def _run(cmd: List[str], timeout: int = 600, capture: bool = True,
         env: dict | None = None) -> subprocess.CompletedProcess:
    # Sin check=True: los llamadores inspeccionan returncode (fallbacks).
    # Solo TimeoutExpired puede propagarse → scrub de argv con secretos (H-5.2).
    full_env = {**os.environ, **env} if env else None
    try:
        return subprocess.run(cmd, capture_output=capture, text=True,
                              timeout=timeout, env=full_env)
    except subprocess.TimeoutExpired as e:
        from src.xbps.builder import redact_cmd
        if isinstance(getattr(e, "cmd", None), (list, tuple)):
            e.cmd = redact_cmd(e.cmd)
        raise


def _srun(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    """_run con elevador universal (src.common.priv): sudo/doas/run0/pkexec/su
    resueltos en un único punto — NUNCA hardcodear el binario aquí."""
    return _run(priv_wrap(cmd), **kw)


def _xbps_env() -> dict:
    """XBPS_ARCH explícito para los tools estáticos: en CI son builds musl y
    sin esta env se autodetectan x86_64-musl → buscan <repo>/x86_64-musl-
    repodata (inexistente en repos glibc) → reposync 'Not Found'."""
    return {"XBPS_ARCH": get_config().arch}


def _xbps_cmd(cmd: List[str]) -> List[str]:
    """Ejecuta un tool xbps con XBPS_ARCH dentro de sh elevado: sudo/doas
    LIMPIAN el entorno, así que la env viaja dentro del comando (env(1)),
    inmune al scrub del elevador."""
    pairs = [f"{k}={v}" for k, v in _xbps_env().items()]
    return ["sh", "-c", shlex.join(["env", *pairs, *cmd])]


def _void_python_version() -> str:
    """Versión python del masterdir Void (config.python_version o autodetect)."""
    cfg = get_config()
    if cfg.python_version:
        return cfg.python_version
    r = _srun(["chroot", str(MASTERDIR()), "/usr/bin/python3",
              "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
             timeout=30)
    ver = r.stdout.strip()
    if r.returncode == 0 and ver:
        cfg.python_version = ver
        return ver
    import sys as _sys
    return f"{_sys.version_info[0]}.{_sys.version_info[1]}"


def normalize_stage(stage: Path):
    """TRH: mtime @0 + owner 0:0 en todo el árbol."""
    _run(["find", str(stage), "-exec", "touch", "-h", "-d", "@0", "{}", ";"])
    r = _run(["find", str(stage), "-exec", "chown", "-h", "0:0", "{}", ";"])
    if r.returncode != 0:
        _srun(["find", str(stage), "-exec", "chown", "-h", "0:0", "{}", ";"])


def _generate_console_scripts(stage: Path) -> int:
    """Genera wrappers /usr/bin/<script> desde entry_points.txt de wheels
    instalados en site-packages Void. Los wheels no traen los scripts."""
    import configparser
    import sysconfig
    generated = 0
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    sp_root = stage / f"usr/lib/python{pyver}/site-packages"
    bin_dir = stage / "usr/bin"
    if not sp_root.exists():
        return 0
    for eps in sp_root.glob("*.dist-info/entry_points.txt"):
        try:
            cp = configparser.ConfigParser()
            cp.read_string(eps.read_text())
        except Exception:
            continue
        if not cp.has_section("console_scripts"):
            continue
        bin_dir.mkdir(parents=True, exist_ok=True)
        for script, target in cp["console_scripts"].items():
            script = script.strip()
            mod_path, _, func = target.partition("=")
            mod = mod_path.strip().split(":")[0].strip()
            attr = func.strip() if func.strip() else "main"
            wrapper = (f"#!/usr/bin/python3\n"
                       f"import sys\n"
                       f"import importlib\n"
                       f"mod = importlib.import_module({mod!r})\n"
                       f"attr = {attr!r}\n"
                       f"f = getattr(mod, attr)\n"
                       f"sys.exit(f())\n")
            dst = bin_dir / script
            dst.write_text(wrapper)
            dst.chmod(0o755)
            generated += 1
    return generated


def _fix_shebangs(stage: Path) -> int:
    """Reescribe shebangs que apuntan a /nix/store hacia rutas FHS de Void.
    patchelf solo corrige ELF; los scripts (#!) necesitan esto aparte."""
    import re
    SHEBANG_MAP = [
        (re.compile(r"^#!/nix/store/[a-z0-9]+-bash-[^/]*/bin/bash"), "#!/bin/bash"),
        (re.compile(r"^#!/nix/store/[a-z0-9]+-bash-[^/]*/bin/sh"), "#!/bin/sh"),
        (re.compile(r"^#!/nix/store/[a-z0-9]+-(?:coreutils|gnugrep|gawk)[^/]*/bin/(\w+)"),
         r"#!/usr/bin/\1"),
        (re.compile(r"^#!/nix/store/[a-z0-9]+-python3[^/]*/bin/python3?"), "#!/usr/bin/python3"),
        (re.compile(r"^#!\s*/nix/store/[^/]+/bin/(\S+)"), r"#!/usr/bin/\1"),  # fallback genérico
    ]
    fixed = 0
    for f in subprocess.check_output(["find", str(stage), "-type", "f"],
                                     text=True).splitlines():
        try:
            head = Path(f).open("rb").read(256)
        except Exception:
            continue
        if not head.startswith(b"#!") or b"/nix/store/" not in head:
            continue
        try:
            first = Path(f).open("r", encoding="utf-8", errors="surrogateescape").readline().rstrip("\n")
        except Exception:
            continue
        new = first
        for pat, repl in SHEBANG_MAP:
            m = pat.match(first)
            if m:
                if "\\1" in repl:
                    new = pat.sub(repl, first)
                else:
                    new = repl
                break
        if new == first:
            # último recurso: basename del intérprete en /usr/bin
            interp = first[2:].split()[0]
            base = Path(interp).name
            rest = first[2:].split()[1:]
            new = "#!/usr/bin/" + base + (" " + " ".join(rest) if rest else "")
            if "/nix/store/" not in interp:
                continue
        perms = Path(f).stat().st_mode & 0o777
        content = Path(f).read_text(encoding="utf-8", errors="surrogateescape")
        content = content.replace(first, new, 1)
        try:
            Path(f).write_text(content, encoding="utf-8", errors="surrogateescape")
        except PermissionError:
            subprocess.run(priv_wrap(["chmod", "u+w", f]), check=True)
            Path(f).write_text(content, encoding="utf-8", errors="surrogateescape")
            perms = perms | 0o200  # restaurar writable como estaba el original Nix
        Path(f).chmod(perms)
        fixed += 1
    return fixed


def _require_tools() -> None:
    """Fallar temprano con mensaje accionable si faltan herramientas que el
    pipeline asume (file/readelf/patchelf): sin ellas la selección de ELFs
    se salta silenciosamente y los .xbps salen con /nix/store sin parchear."""
    import shutil
    missing = [t for t in ("file", "readelf", "patchelf") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            "herramientas ausentes: " + ", ".join(missing)
            + " (en Void: xbps-install -y file binutils patchelf)")


def verify_patched_elf(path: str) -> None:
    """Oráculo post-patchelf (H-4.1): patchelf con secciones no convencionales
    puede corromper el ELF silenciosamente. ldd debe leer el binario sin
    fallar; libs "not found" son tolerables (resuelven en el chroot Void vía
    shlibs). Lanza RuntimeError ante corrupción o cuelgue."""
    try:
        subprocess.run(["ldd", path], capture_output=True, text=True,
                       timeout=60, check=True)
    except subprocess.CalledProcessError as e:
        tail = ((e.stdout or "") + (e.stderr or ""))[-300:]
        raise RuntimeError(
            f"patchelf corrompió {path} (ldd exit {e.returncode}): {tail}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ldd colgado en {path}: ELF posiblemente corrupto") from e


def patchelf_to_void(stage: Path, include_shared: bool = True) -> int:
    """Re-parchea ELFs del Nix store al intérprete/libras de Void.
    Orden mandatorio: rpath ($ORIGIN primero) ANTES que interpreter, separados."""
    patched = 0
    find_args = ["-type", "f"]
    cmd_find = ["find", str(stage)] + find_args
    try:
        files = subprocess.check_output(cmd_find, text=True).splitlines()
    except subprocess.CalledProcessError:
        return 0
    for f in files:
        try:
            out = subprocess.check_output(["file", "-b", f], text=True)
        except Exception:
            continue
        is_elf = "ELF" in out
        # Solo ELF DINÁMICOS: patchelf sobre binarios estáticos (Go/Rust puros)
        # los corrompe (segfault), no tienen interpreter ni rpath que parchear.
        if not is_elf or "statically linked" in out:
            continue
        # Si ya apunta al intérprete de Void y no referencia /nix/store,
        # no tocar (patchelf sobre binarios Go es destructivo aunque sean dinámicos)
        try:
            d_out = subprocess.check_output(["readelf", "-d", f], text=True,
                                            stderr=subprocess.DEVNULL)
            l_out = subprocess.check_output(["readelf", "-l", f], text=True,
                                            stderr=subprocess.DEVNULL)
        except Exception:
            continue
        interp_nix = "/nix/store" in l_out
        refs_nix = "/nix/store" in d_out
        if not interp_nix and not refs_nix:
            continue
        # Guardar modo original: chmod +w es temporal, se restaura tras parchear
        # (H-4.1: sin restauración, las libs quedaban 0644+w → drift en stage)
        orig_mode = os.stat(f).st_mode & 0o777
        _run(["chmod", "+w", f])
        # 1) rpath primero
        _run(["patchelf", "--set-rpath", "$ORIGIN:/usr/lib:/usr/lib64", f])
        # 2) interpreter después, solo ejecutables (no .so puros)
        if "executable" in out:
            _run(["patchelf", "--set-interpreter", VOID_INTERP(), f])
            # Normalización de modo: ELF ejecutable siempre 0755
            # (zips/tars upstream pueden perder el bit x; root lo enmascara vía DAC_OVERRIDE)
            _run(["chmod", "755", f])
        else:
            _run(["chmod", oct(orig_mode)[2:], f])
        # 3) Oráculo post-escritura (H-4.1): detecta corrupción inmediata
        verify_patched_elf(f)
        patched += 1
    return patched


def auto_run_depends(stage: Path, db: Optional[ShlibsDB] = None,
                     extra: Optional[List[str]] = None) -> List[str]:
    """SONAMEs NEEDED de todos los ELF del stage → deps XBPS vía common/shlibs real."""
    db = db or ShlibsDB()
    deps: set[str] = set()
    for f in subprocess.check_output(["find", str(stage), "-type", "f"], text=True).splitlines():
        try:
            out = subprocess.check_output(["file", "-b", f], text=True)
        except Exception:
            continue
        if "ELF" not in out:
            continue
        try:
            readelf = subprocess.check_output(["readelf", "-d", f], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        for line in readelf.splitlines():
            if "(NEEDED)" in line and "[" in line:
                soname = line.split("[")[1].split("]")[0]
                mapped = db.soname_to_dep(soname)
                if mapped:
                    deps.add(mapped)
    for e in (extra or []):
        deps.add(e)
    return sorted(deps)


def create_signed(stage: Path, pkgver: str, desc: str,
                  depends: List[str], license_: str = "custom:unknown") -> tuple[Path, str]:
    """xbps-create determinista + firma del paquete. Retorna (path, sha256)."""
    REPO.mkdir(parents=True, exist_ok=True)
    cfg = get_config()
    out = REPO / f"{pkgver}.{cfg.arch}.xbps"
    if out.exists():
        out.unlink()
    env_base = ["env", "SOURCE_DATE_EPOCH=0", "TZ=UTC", "LC_ALL=C", XBPS_CREATE(),
                "-A", cfg.arch, "-n", pkgver, "-s", desc[:80],
                "-m", "aur2xbps <aur2xbps@local>", "-l", license_,
                "--compression", "zstd"]
    if depends:
        env_base += ["-D", " ".join(depends)]
    env_base.append(str(stage))
    r = _run(env_base, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"xbps-create falló: {r.stderr[-500:]}")
    generated = Path.cwd() / f"{pkgver}.{cfg.arch}.xbps"
    if not generated.exists():
        raise RuntimeError("xbps-create no generó el .xbps esperado en cwd")
    sha = hashlib.sha256(generated.read_bytes()).hexdigest()
    import shutil
    shutil.move(str(generated), str(out))
    # Post-proceso cross-host: uname/gname=∅ + checksum recalculado + zstd -T1
    from src.xbps.determinize import determinize_xbps
    out, sha = determinize_xbps(out)
    # firma individual: requiere privkey existente (si falta, el paquete se
    # genera SIN firma; reportarlo honestamente en res.signed)
    if PRIVKEY.is_file():
        _run([XBPS_RINDEX(), "--privkey", str(PRIVKEY),
              "--signedby", "aur2xbps <aur2xbps@local>", "--sign-pkg", str(out)])
    # reindexar repo (dedup + orden estable ante xbps-rindex)
    xbps_files = sorted({str(p) for p in REPO.glob("*.xbps")})
    if xbps_files:
        _run([XBPS_RINDEX(), "-f", "-a"] + xbps_files)
    return out, sha


def _ensure_chroot_repos(md: Path) -> None:
    """Garantiza config de repositorio oficial dentro de un root chroot.

    Los masterdirs mínimos (bootstrap manual/contenedor) pueden carecer de
    etc/xbps.d; con -r los defaults compilados NO aplican y cualquier dep
    nueva da MISSING. Idempotente. De paso siembra las claves públicas Void
    del host en el root (evita prompts de import sobre roots frescos).
    """
    conf = md / "etc" / "xbps.d" / "00-repository-main.conf"
    if not conf.exists():
        _srun(["mkdir", "-p", str(conf.parent)])
        _srun(["sh", "-c",
              f"printf 'repository=https://repo-default.voidlinux.org/current\\n' > {conf}"])
    # claves oficiales: copia defensiva desde el host (skip elegante si el
    # host no es Void o no las tiene; el `yes |` de chroot_install cubre)
    host_share = Path("/usr/share/xbps.d")
    if host_share.is_dir():
        keys = sorted(host_share.glob("*.pem")) + sorted(host_share.glob("*.list"))
        if keys:
            dst = md / "usr" / "share" / "xbps.d"
            _srun(["mkdir", "-p", str(dst)])
            for k in keys:
                _srun(["cp", "-n", str(k), str(dst / k.name)])


CHROOT_LAST_ERR = ""


def chroot_install(pkgname: str) -> bool:
    global CHROOT_LAST_ERR
    CHROOT_LAST_ERR = ""
    r = _srun(_xbps_cmd([XBPS_REMOVE(), "-r", str(MASTERDIR()), "-y", pkgname]),
              timeout=300)
    _ensure_chroot_repos(MASTERDIR())
    # --repository = subdirectorio de arch (layout FLAT: <dir>/<arch>-repodata).
    # El PADRE hace buscar <arch>/<arch>/repodata → "not found in repository pool".
    #
    # stdin SIEMPRE alimentado: la importación de la clave RSA del repo local
    # pregunta "[Y/n]" incluso con -y, y con stdin en EOF el import falla
    # ("Resource temporarily unavailable") → repo rechazado → "not found"
    # (fallo determinista de CI sin tty; AGENTS: jamás confiar en stdin).
    # stdin SIEMPRE alimentado con 'yes': sobre un masterdir vacío xbps
    # pregunta la clave RSA del REPO OFICIAL Void (el rootfs fresco no trae
    # /usr/share/xbps.d) Y LUEGO la del repo local — dos prompts, y '-y' no
    # cubre ninguna; con stdin EOF el import muere → "not found in pool".
    pairs = " ".join(f"{k}={v}" for k, v in _xbps_env().items())
    inner = shlex.join([XBPS_INSTALL(), "-r", str(MASTERDIR()),
                        f"--repository={REPO}", "-Sy", "-y", pkgname])
    r2 = _srun(["sh", "-c", f"yes | env {pairs} {inner}"],
               timeout=900)
    if os.environ.get("CHROOT_DEBUG"):
        # diagnóstico CI: transacción completa, no solo la cola
        print(f"[chroot][debug] cmd: {inner}\n[chroot][debug] rc={r2.returncode}\n"
              f"[chroot][debug] out:\n{r2.stdout}\n[chroot][debug] err:\n{r2.stderr}",
              file=sys.stderr)
    if r2.returncode == 0 and "installed successfully" in (r2.stdout + r2.stderr):
        return True
    CHROOT_LAST_ERR = ((r2.stdout or "") + (r2.stderr or ""))[-600:]
    return False


def chroot_smoke(binary_candidates: List[str]) -> tuple[bool, int]:
    """Ejecuta el primer binario encontrado con --version/--help dentro del chroot.
    EEL falla SOLO por errores del cargador (libs missing/segfault). Un exit!=0
    con salida propia de la app (ej. yay rechazando root) es ejecución válida.
    Retorna (ok, num_libs_missing)."""
    LOADER_ERRORS = ("error while loading shared libraries",
                     "cannot open shared object file")
    for b in binary_candidates:
        chk = _run(["test", "-f", f"{MASTERDIR()}{b}"])
        if chk.returncode != 0:
            continue
        ldd = _srun(["chroot", str(MASTERDIR()), "ldd", b], timeout=120)
        missing = (ldd.stdout + ldd.stderr).count("not found")
        if missing > 0:
            return False, missing
        for args in (["--version"], ["--help"], ["--no-sandbox", "--version"]):
            try:
                sm = _srun(["chroot", str(MASTERDIR()), "/usr/bin/env",
                           "LANG=C.UTF-8", b] + args, timeout=60)
            except subprocess.TimeoutExpired:
                # App GUI/demonio que bloquea sin display: cargó y quedó en
                # ejecución (no hay error de enlazado). Válido para EEL.
                return True, 0
            out = (sm.stdout + sm.stderr)
            # App GUI sin display PRIMERO: sus mensajes contienen frases que
            # colisionan con patrones de cargador (ej. wl_display ENOENT).
            if ("could not connect to display" in out or "qt.qpa" in out.lower()
                    or "cannot open display" in out):
                return True, 0
            if any(e in out for e in LOADER_ERRORS):
                return False, 0
            if "Segmentation fault" in out:
                return False, 0
            if sm.returncode == 0:
                return True, 0
            # exit 126/127: fallo de exec (intérprete ELF ausente, permisos).
            # Sin salida de la app: NUNCA es ejecución válida (EEL falso pos.)
            if sm.returncode in (126, 127):
                return False, 0
            # exit != 0 pero sin errores de cargador y con salida propia → EEL OK
            if len(out.strip()) > 0:
                return True, 0
    return False, -1


def _stage_smoke_candidates(stage: Path, pkgname: str) -> List[str]:
    """Candidatos de smoke DERIVADOS del stage real (nunca hardcodeados):
    prioriza usr/bin/<pkgname>, luego <basename sin -bin>, luego ELFs
    ejecutables, luego cualquier fichero. Máximo 4."""
    bin_dir = stage / "usr" / "bin"
    if not bin_dir.is_dir():
        return [f"/usr/bin/{pkgname}"]
    entries = sorted(p for p in bin_dir.iterdir()
                     if p.is_file() or p.is_symlink())
    if not entries:
        return [f"/usr/bin/{pkgname}"]
    base = pkgname.lower().removesuffix("-bin")

    def is_exec(p: Path) -> bool:
        try:
            target = p.resolve()
            with open(target, "rb") as fh:
                head = fh.read(2)
            return head == b"\x7fE" or head == b"#!"
        except Exception:
            return False

    def prio(p: Path):
        n = p.name.lower()
        return (n != pkgname.lower(), n != base, not is_exec(p), n)

    ordered = sorted(entries, key=prio)[:4]
    return [f"/usr/bin/{p.name}" for p in ordered]


def full_pipeline(nix_result: Path, pkgname: str, pkgver: str, desc: str,
                  smoke_binaries: Optional[List[str]] = None) -> XbpsResult:
    """Ejecuta la cadena completa para un paquete. Retorna resultado detallado."""
    _require_tools()
    res = XbpsResult(pkgver=pkgver)
    stage = FAKE_ROOT / pkgname

    # 1. Stage desde Nix result
    from src.xbps.builder import stage_from_nix_result
    stage_from_nix_result(nix_result, stage)

    # 2. Patchelf a Void + reescritura de shebangs Nix→FHS
    n = patchelf_to_void(stage)
    s = _fix_shebangs(stage)
    c = _generate_console_scripts(stage)
    print(f"[pipeline] patchelf: {n} ELFs, shebangs: {s}, console_scripts: {c}")

    # 3. Normalizar TRH
    normalize_stage(stage)

    # 4. run_depends automáticos desde shlibs real
    deps = auto_run_depends(stage)
    if not deps:
        deps = [_base_dep()]
    print(f"[pipeline] run_depends ({len(deps)}): {' '.join(deps[:6])}...")

    # 5. Crear + firmar
    try:
        out, sha = create_signed(stage, pkgver, desc, deps)
        res.xbps_path, res.sha256 = out, sha
        res.signed = PRIVKEY.is_file()
        estado_firma = "firmado" if res.signed else "SIN firmar (privkey ausente)"
        print(f"[pipeline] creado {out.name} sha256={sha[:16]}… {estado_firma}")
        # repodata local: sin índice el chroot no ve el paquete
        # ("not found in repository pool")
        try:
            from src.xbps.builder import rindex_add
            from src.common.config import get_config as _gc
            _c = _gc()
            if _c.privkey and _c.privkey.is_file():
                rindex_add(REPO, [out], sign=True, privkey=_c.privkey)
            else:
                rindex_add(REPO, [out], sign=False)
            print(f"[pipeline] rindex: {REPO.name} indexado")
        except Exception as _re:                                 # noqa: BLE001
            print(f"[pipeline] WARNING rindex falló: {_re}", file=sys.stderr)
    except Exception as e:
        res.errors.append(str(e))
        return res

    # 6. Instalar en chroot
    res.installed = chroot_install(pkgname)
    if not res.installed:
        res.errors.append("instalación en chroot falló")
        # observabilidad: superficie el motivo real capturado por chroot_install
        print(f"[chroot] detalle: {CHROOT_LAST_ERR}", file=sys.stderr)
        return res

    # 7. Smoke EEL — candidatos derivados del stage real; jamás rutas
    #    hardcodeadas (los restos chrome/code provocaban fallos fantasma)
    candidates = smoke_binaries or _stage_smoke_candidates(stage, pkgname)
    ok, missing = chroot_smoke(candidates)
    res.smoke_ok, res.ldd_missing = ok, max(missing, 0)
    if not ok:
        res.errors.append(f"smoke falló para {candidates}")
    return res


def build_python_in_void(nix_result: Path, pkgname: str, pkgver: str,
                          desc: str = "", extra_deps: str = "") -> XbpsResult:
    """Flujo Python: Nix solo obtiene fuente → copia a masterdir Void →
    pip wheel con python3 de Void (misma versión que el destino) →
    descomprime wheel a usr/lib/python3.14/site-packages → genera
    console_scripts → xbps-create determinista + firma + install + smoke.
    Resuelve la incompatibilidad python sandbox Nix (3.12) vs Void (3.14)."""
    res = XbpsResult(pkgver=pkgver)
    stage = FAKE_ROOT / pkgname

    # 1. Obtener árbol fuente desde Nix result ($out/src/)
    nix_real = Path(nix_result).resolve()
    src_dir = nix_real / "src"
    if not src_dir.exists() and (nix_real / "setup.py").exists():
        # fetchgit entrega tree directamente (no hay subdirectorio src/)
        src_dir = nix_real
    if not src_dir.exists():
        res.errors.append(f"fuente ausente en {nix_result}/src")
        return res

    # 2. Copiar fuente al masterdir para compilar con Void python
    # Copiar fuente a tmp del host, luego bind al masterdir
    host_build = Path("/tmp") / f"pybuild-{pkgname}"
    _srun(["rm", "-rf", str(host_build)])
    _srun(["cp", "-a", str(src_dir), str(host_build)])
    _srun(["chown", "-R", f"{__import__('os').getuid()}:{__import__('os').getgid()}", str(host_build)])

    build_root = MASTERDIR() / "tmp" / f"pybuild-{pkgname}"
    _srun(["rm", "-rf", str(build_root)])
    _srun(["mkdir", "-p", str(MASTERDIR() / "tmp")])
    _srun(["cp", "-a", str(host_build), str(build_root)])

    _srun(_xbps_cmd([XBPS_INSTALL(), "-r", str(MASTERDIR()), "-Sy", "-y",
                     "python3-pip", "python3-setuptools"]), timeout=600)

    # 4. Generar wheel con python de Void
    r = _srun(["chroot", str(MASTERDIR()), "/usr/bin/env",
              "HOME=/root", "SOURCE_DATE_EPOCH=0",
              "/bin/sh", "-c",
              f"cd /tmp/pybuild-{pkgname} && "
              f"python3 -m pip wheel --no-deps --no-build-isolation "
              f"--wheel-dir=/tmp/pybuild-{pkgname}/dist . 2>&1 || "
              f"python3 setup.py bdist_wheel --dist-dir=/tmp/pybuild-{pkgname}/dist"],
             timeout=600)
    wheels = list((MASTERDIR() / f"tmp/pybuild-{pkgname}/dist").glob("*.whl")) \
        if (MASTERDIR() / f"tmp/pybuild-{pkgname}/dist").exists() else []
    if not wheels:
        res.errors.append(f"wheel no generado: {r.stdout[-300:]} {r.stderr[-200:]}")
        return res
    print(f"[pyvoid] wheel: {wheels[0].name}")

    # 5. Descomprimir wheel al stage con rutas Void
    import zipfile
    py_ver = _void_python_version()
    _srun(["rm", "-rf", str(stage)])
    stage.mkdir(parents=True, exist_ok=True)
    sp = stage / "usr" / "lib" / f"python{py_ver}" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    bin_dir = stage / "usr" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(wheels[0]) as whl:
        whl.extractall(sp)

    # 6. console_scripts desde entry_points.txt
    import configparser
    for eps in sp.glob("*.dist-info/entry_points.txt"):
        try:
            cp = configparser.ConfigParser()
            cp.read_string(eps.read_text())
        except Exception:
            continue
        if not cp.has_section("console_scripts"):
            continue
        for script, target in cp["console_scripts"].items():
            script_name = script.strip()
            mod_part, _, func_part = target.partition("=")
            mod = mod_part.strip().split(":")[0].strip()
            attr = func_part.strip() or "main"
            wrapper = (f"#!/usr/bin/python3\n"
                       f"import sys, importlib\n"
                       f"m = importlib.import_module({mod!r})\n"
                       f"f = getattr(m, {attr!r})\n"
                       f"sys.exit(f())\n")
            dst = bin_dir / script_name
            dst.write_text(wrapper)
            dst.chmod(0o755)

    # 7. Determinizar stage
    normalize_stage(stage)

    # 8. run_depends: glibc mínimo + extra (asegurar formato pkg>=ver)
    import re as _re
    deps = [_base_dep()]
    if extra_deps:
        for d in extra_deps.split():
            if ">=" in d or "<" in d or "=" in d:
                deps.append(d)
            else:
                # buscar versión instalada en masterdir
                q = _srun(["chroot", str(MASTERDIR()),
                          "xbps-uhelper", "version", d.strip()], timeout=30)
                ver = q.stdout.strip()
                deps.append(f"{d}>={ver}" if ver else d)
    print(f"[pyvoid] run_depends: {' '.join(deps)}")

    # 9. xbps-create + firma
    try:
        out, sha = create_signed(stage, pkgver, desc or pkgname, deps)
        res.xbps_path, res.sha256, res.signed = out, sha, True
    except Exception as e:
        res.errors.append(str(e)[:300])
        return res

    # 10. Instalar en chroot
    res.installed = chroot_install(pkgname)
    if not res.installed:
        res.errors.append("instalación falló")
        return res

    # 11. Smoke: CLI o import del módulo
    ok, missing = chroot_smoke([f"/usr/bin/{pkgname.replace('python-','').replace('-git','')}",
                                f"/usr/bin/{attr}" if 'attr' in dir() else "/usr/bin/python3"])
    if not ok:
        # fallback: verificar import del módulo como smoke
        mod_name = pkgname.replace("python-", "").replace("-git", "").replace("-", "_")
        imp = _srun(["chroot", str(MASTERDIR()), "/usr/bin/env",
                    "HOME=/root", "/usr/bin/python3", "-c",
                    f"import {mod_name}; print('import OK')"], timeout=60)
        if imp.returncode == 0 and "import OK" in imp.stdout:
            res.smoke_ok = True
        else:
            res.errors.append(f"smoke+import fallaron: {imp.stderr[:150]}")
    else:
        res.smoke_ok = True
        res.ldd_missing = missing
    return res
