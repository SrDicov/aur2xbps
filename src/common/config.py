# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuración central de aur2xbps — sin rutas hardcodeadas.

Prioridad de resolución (mayor a menor):
  1. Variables de entorno ``AUR2XBPS_*``
  2. Archivo de usuario ``$XDG_CONFIG_HOME/aur2xbps/config.toml``
     (o ``AUR2XBPS_CONFIG``)
  3. Archivo de sistema ``/etc/aur2xbps/config.toml``
  4. Defaults portables (XDG)

Claves TOML aceptadas (sección ``[paths]``, ``[repo]``, ``[build]``):
  paths: data_dir, cache_dir, repo_dir, keys_dir, masterdir,
         void_packages_dir, nix_store_dir
  repo:  host, port
  build: arch, python_version, signing_key, log_level, restricted_mode

Variables de entorno:
  AUR2XBPS_CONFIG      ruta alternativa al config.toml del usuario
  AUR2XBPS_DATA_DIR    data_dir          (compat: AUR2XBPS_ROOT)
  AUR2XBPS_CACHE_DIR   cache_dir
  AUR2XBPS_REPO_DIR    repo_dir
  AUR2XBPS_KEYS_DIR    keys_dir          (compat: AUR2XBPS_KEYDIR)
  AUR2XBPS_MASTERDIR   masterdir
  AUR2XBPS_VOID_DIR    void_packages_dir
  AUR2XBPS_NIX_STORE   nix_store_dir
  AUR2XBPS_HOST        host HTTP del repo
  AUR2XBPS_PORT        puerto HTTP del repo
  AUR2XBPS_ARCH        arquitectura destino
  AUR2XBPS_OFFLINE     modo offline (1/true)
"""
from __future__ import annotations

import os
import platform
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Dict, List

# ---------------------------------------------------------------- XDG helpers

def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))


USER_CONFIG = xdg_config_home() / "aur2xbps" / "config.toml"
SYSTEM_CONFIG = Path("/etc/aur2xbps/config.toml")

# ------------------------------------------------------- arquitectura soportada

#: machine (platform.machine) -> (arch XBPS, intérprete ELF dinámico, sistema Nix)
ARCH_MAP: dict[str, tuple[str, str, str]] = {
    "x86_64": ("x86_64", "/lib64/ld-linux-x86-64.so.2", "x86_64-linux"),
    "amd64": ("x86_64", "/lib64/ld-linux-x86-64.so.2", "x86_64-linux"),
    "aarch64": ("aarch64", "/lib/ld-linux-aarch64.so.1", "aarch64-linux"),
    "arm64": ("aarch64", "/lib/ld-linux-aarch64.so.1", "aarch64-linux"),
    "i686": ("i686", "/lib/ld-linux.so.2", "i686-linux"),
    "i386": ("i686", "/lib/ld-linux.so.2", "i686-linux"),
}

DEFAULT_ARCH = ARCH_MAP.get(platform.machine().lower(),
                            ARCH_MAP["x86_64"])[0]

#: intérprete ELF dinámico musl por arch XBPS (glibc vive en ARCH_MAP)
MUSL_INTERPRETER: dict[str, str] = {
    "x86_64": "/lib/ld-musl-x86_64.so.1",
    "aarch64": "/lib/ld-musl-aarch64.so.1",
    "i686": "/lib/ld-musl-i686.so.1",
}


def detect_arch() -> str:
    env = os.environ.get("AUR2XBPS_ARCH")
    if env:
        return env
    return DEFAULT_ARCH


def _probe_libc() -> str:
    """Sonda del host: 'musl' o 'glibc' vía ldd --version."""
    import shutil
    import subprocess
    ldd = shutil.which("ldd")
    if ldd:
        try:
            r = subprocess.run([ldd, "--version"], capture_output=True,
                               text=True, timeout=10)
            blob = (r.stdout + r.stderr).lower()
            if "musl" in blob:
                return "musl"
            if "glibc" in blob or "gnu libc" in blob or r.returncode == 0:
                return "glibc"
        except Exception:                                # noqa: BLE001
            pass
    return "glibc"          # default histórico del proyecto


def detect_libc() -> str:
    """libc objetivo: override AUR2XBPS_LIBC > sonda ldd del host."""
    env = os.environ.get("AUR2XBPS_LIBC", "").strip().lower()
    if env in ("glibc", "musl"):
        return env
    return _probe_libc()


def effective_libc(cfg: "Config | None" = None) -> str:
    """Resuelve el campo libc de la config ('auto' → sonda host)."""
    val = getattr(cfg, "libc", "auto") if cfg is not None \
        else os.environ.get("AUR2XBPS_LIBC", "auto")
    val = (val or "auto").strip().lower()
    return detect_libc() if val == "auto" else val


def xbps_arch(arch: str | None = None, cfg: "Config | None" = None) -> str:
    """Arch con sabor XBPS completo: x86_64 | x86_64-musl | …"""
    a = arch or (cfg.arch if cfg is not None else detect_arch())
    return f"{a}-musl" if effective_libc(cfg) == "musl" else a


def dynamic_linker(arch: str | None = None, libc: str | None = None) -> str:
    """Intérprete ELF dinámico FHS para (arch, libc)."""
    a = (arch or detect_arch()).lower()
    lib = (libc or effective_libc()).lower()
    entry = next((e for e in ARCH_MAP.values() if e[0] == a), None)
    if entry is None:
        # arch no mapeada: NO silenciar (contrato multi-arch); fallback aviso
        print(f"[config] WARNING: arch '{a}' sin mapeo; usando x86_64",
              file=sys.stderr)
        entry = ARCH_MAP["x86_64"]
        a = "x86_64"
    if lib == "musl":
        return MUSL_INTERPRETER.get(
            a, f"/lib/ld-musl-{a}.so.1")
    return entry[1]


def nix_system(arch: str | None = None) -> str:
    arch = (arch or detect_arch()).lower()
    for xb, _, system in ARCH_MAP.values():
        if xb == arch:
            return system
    print(f"[config] WARNING: arch '{arch}' sin mapeo nix; usando "
          "x86_64-linux", file=sys.stderr)
    return "x86_64-linux"


# ------------------------------------------------------------------ config

@dataclass
class Config:
    # [paths]
    data_dir: Path = field(default_factory=lambda: xdg_data_home() / "aur2xbps")
    cache_dir: Path = field(default_factory=lambda: xdg_cache_home() / "aur2xbps")
    repo_dir: Path = None            # default data_dir/repo
    keys_dir: Path = field(default_factory=lambda: xdg_config_home() / "aur2xbps" / "keys")
    masterdir: Path = None           # default data_dir/void/masterdir
    void_packages_dir: Path = None   # default data_dir/void/void-packages
    xbps_bin_dir: Path | None = None  # dir de xbps static (AUR2XBPS_XBPS_BIN_DIR / [paths] xbps_bin_dir)
    nix_store_dir: Path = Path("/nix")
    # [repo]
    host: str = "127.0.0.1"
    port: int = 8080
    # [build]
    arch: str = field(default_factory=detect_arch)
    # libc objetivo: auto|glibc|musl (auto = detectar del host con ldd);
    # en musl los precompilados AUR se descartan (ver security/binpkg)
    libc: str = "auto"
    python_version: str | None = None   # None → autodetectar del masterdir
    signing_key: Path | None = None     # None → keys_dir/privkey.pem
    log_level: str = "INFO"
    restricted_mode: bool = True        # bloquear empaquetado de no-redistribuibles
    offline: bool = False               # solo caché local, sin red
    build_timeout: int = 3600           # techo duro por compilación (segundos)
    # [nixpkgs_pins] pkgbase → ref de nixpkgs (flake input) para casos de
    # incompatibilidad ABI upstream (ej. paru+libalpm v15 → rev con pacman 6.x).
    nixpkgs_pins: Dict[str, str] = field(default_factory=dict)
    # [security] claves PGP de mantenedores AUR de confianza (H-2.2): la
    # intersección con validpgpkeys del .SRCINFO habilita exenciones del
    # filtro JS con cadena de custodia real.
    trusted_pgp_keys: List[str] = field(default_factory=list)
    # [security] paquetes maliciosos exactos (Atomic Arch). Se UNE al baseline
    # inmutable de src/aur/security.py — la config SOLO extiende, no reemplaza,
    # para no perder protecciones críticas por un TOML incompleto.
    malicious_exact: List[str] = field(default_factory=list)
    # [priv] command: elevador de privilegios forzado (misma semántica que
    # AUR2XBPS_PRIV, p.ej. "doas -u root"); vacío → autodetección priv.py
    priv_command: str = ""

    # ---- derivadas (no configurables) ----
    @property
    def effective_masterdir(self) -> Path:
        """Masterdir utilizable: el configurado si está poblado; si no, el
        que xbps-src gestiona (masterdir-<arch> dentro del árbol void)."""
        if (self.masterdir / "usr" / "bin").is_dir():
            return self.masterdir
        alt = self.void_packages_dir / f"masterdir-{self.arch}"
        if (alt / "usr" / "bin").is_dir():
            return alt
        return self.masterdir

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def derivations_dir(self) -> Path:
        return self.data_dir / "derivations"

    @property
    def srcpkgs_dir(self) -> Path:
        """Árbol de plantillas xbps-src generadas (integración vouru)."""
        return self.data_dir / "srcpkgs"

    @property
    def fake_root(self) -> Path:
        return self.data_dir / "fake-root"

    @property
    def rpc_cache_db(self) -> Path:
        return self.cache_dir / "aur-rpc" / "cache.db"

    @property
    def rev_pins(self) -> Path:
        return self.cache_dir / "rev-pins.json"

    @property
    def batch_results(self) -> Path:
        return self.data_dir / "batch-results.json"

    @property
    def shlibs_file(self) -> Path:
        return self.void_packages_dir / "common" / "shlibs"

    @property
    def privkey(self) -> Path:
        return self.signing_key if self.signing_key else self.keys_dir / "privkey.pem"

    @property
    def pubkey(self) -> Path:
        return self.keys_dir / "pubkey.pem"

    @property
    def repo_x86_64(self) -> Path:
        """Subdirectorio de arquitectura del repo local."""
        return self.repo_dir / self.arch

    def ensure_layout(self) -> None:
        for p in (self.data_dir, self.cache_dir, self.repo_dir, self.keys_dir,
                  self.masterdir, self.void_packages_dir, self.sources_dir,
                  self.derivations_dir, self.srcpkgs_dir, self.fake_root,
                  self.rpc_cache_db.parent):
            p.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = str(v) if isinstance(v, Path) else v
        return out


def _apply_toml(cfg: Config, path: Path) -> None:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as e:  # TOML inválido: avisar y seguir con defaults
        print(f"[config] WARNING: {path}: {e}", file=sys.stderr)
        return
    known = {f.name for f in fields(Config)}
    path_fields = {"data_dir", "cache_dir", "repo_dir", "keys_dir", "masterdir",
                   "void_packages_dir", "nix_store_dir", "signing_key"}
    for section, values in raw.items():
        if not isinstance(values, dict):
            continue
        if section == "nixpkgs_pins":
            cfg.nixpkgs_pins.update({str(k): str(v) for k, v in values.items()})
            continue
        if section == "priv":
            if values.get("command"):
                cfg.priv_command = str(values["command"])
            continue
        for k, v in values.items():
            if k not in known:
                continue
            cur = getattr(cfg, k)
            if k in path_fields or isinstance(cur, Path):
                setattr(cfg, k, Path(str(v)).expanduser())
            elif cur is None:
                setattr(cfg, k, v)
            elif isinstance(cur, int) and not isinstance(cur, bool):
                setattr(cfg, k, int(v))
            elif isinstance(cur, bool):
                setattr(cfg, k, bool(v))
            else:
                setattr(cfg, k, type(cur)(v))


def _apply_env(cfg: Config) -> None:
    env_map = {
        "AUR2XBPS_DATA_DIR": "data_dir",
        "AUR2XBPS_ROOT": "data_dir",         # compat documentada (AGENTS.md)
        "AUR2XBPS_CACHE_DIR": "cache_dir",
        "AUR2XBPS_REPO_DIR": "repo_dir",
        "AUR2XBPS_KEYS_DIR": "keys_dir",
        "AUR2XBPS_KEYDIR": "keys_dir",       # compat
        "AUR2XBPS_MASTERDIR": "masterdir",
        "AUR2XBPS_VOID_DIR": "void_packages_dir",
        "AUR2XBPS_NIX_STORE": "nix_store_dir",
        "AUR2XBPS_XBPS_BIN_DIR": "xbps_bin_dir",
        "AUR2XBPS_HOST": "host",
        "AUR2XBPS_PORT": "port",
        "AUR2XBPS_ARCH": "arch",
        "AUR2XBPS_LIBC": "libc",
    }
    path_fields = {"data_dir", "cache_dir", "repo_dir", "keys_dir", "masterdir",
                   "void_packages_dir", "nix_store_dir", "xbps_bin_dir"}
    for env, attr in env_map.items():
        val = os.environ.get(env)
        if not val:
            continue
        if attr in path_fields:
            setattr(cfg, attr, Path(val).expanduser())
        elif attr == "port":
            setattr(cfg, attr, int(val))
        else:
            setattr(cfg, attr, val)
    if os.environ.get("AUR2XBPS_OFFLINE", "").lower() in {"1", "true", "yes"}:
        cfg.offline = True
    if os.environ.get("AUR2XBPS_LOG_LEVEL"):
        cfg.log_level = os.environ["AUR2XBPS_LOG_LEVEL"]
    if os.environ.get("AUR2XBPS_RESTRICTED", "").lower() in {"0", "false", "no"}:
        cfg.restricted_mode = False
    if os.environ.get("AUR2XBPS_PYTHON_VERSION"):
        cfg.python_version = os.environ["AUR2XBPS_PYTHON_VERSION"]
    if os.environ.get("AUR2XBPS_BUILD_TIMEOUT"):
        try:
            cfg.build_timeout = int(os.environ["AUR2XBPS_BUILD_TIMEOUT"])
        except ValueError:
            pass
    if os.environ.get("AUR2XBPS_TRUSTED_PGP_KEYS"):
        cfg.trusted_pgp_keys = [
            k.strip() for k in os.environ["AUR2XBPS_TRUSTED_PGP_KEYS"].split(",")
            if k.strip()]


def _resolve_derived(cfg: Config) -> None:
    if cfg.repo_dir is None:
        cfg.repo_dir = cfg.data_dir / "repo"
    if cfg.masterdir is None:
        cfg.masterdir = cfg.data_dir / "void" / "masterdir"
    if cfg.void_packages_dir is None:
        cfg.void_packages_dir = cfg.data_dir / "void" / "void-packages"
    for f in fields(Config):
        v = getattr(cfg, f.name)
        if isinstance(v, Path) and not v.is_absolute() and f.name != "signing_key":
            setattr(cfg, f.name, v.expanduser())


def load_config(path: str | Path | None = None) -> Config:
    """Carga config con prioridad env > usuario > sistema > defaults.

    ``path`` explícito sustituye el DESCUBRIMIENTO de TOML (no se buscan
    ~/.config ni /etc), pero las variables AUR2XBPS_* SIEMPRE se aplican
    encima — el entorno es la capa de override final por diseño.
    Compatibilidad legado: si no existe ningún TOML pero sí
    ``~/.config/aur2xbps/root`` (archivo con una ruta), se usa como data_dir.
    """
    cfg = Config()
    if path:
        c = Path(path).expanduser()
        if c.is_file():
            _apply_toml(cfg, c)
    else:
        candidates = []
        if os.environ.get("AUR2XBPS_CONFIG"):
            candidates.append(Path(os.environ["AUR2XBPS_CONFIG"]).expanduser())
        candidates += [USER_CONFIG, SYSTEM_CONFIG]
        used = False
        for c in candidates:
            if c.is_file():
                _apply_toml(cfg, c)
                used = True
                break
        if not used:
            legacy = xdg_config_home() / "aur2xbps" / "root"
            if legacy.is_file():
                p = Path(legacy.read_text().strip()).expanduser()
                if p.is_dir():
                    cfg.data_dir = p
                    cfg.cache_dir = p / "cache"
                    cfg.repo_dir = p / "void" / "repo-local"
                    if (p / "void" / "masterdir").is_dir():
                        cfg.masterdir = p / "void" / "masterdir"
                    if (p / "void" / "void-packages").is_dir():
                        cfg.void_packages_dir = p / "void" / "void-packages"
    _apply_env(cfg)
    _resolve_derived(cfg)
    return cfg


# Singleton perezoso para imports simples en todo el código.
_cfg: Config | None = None


def get_config(reload: bool = False) -> Config:
    global _cfg
    if _cfg is None or reload:
        _cfg = load_config()
    return _cfg
