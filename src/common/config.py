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


def detect_arch() -> str:
    env = os.environ.get("AUR2XBPS_ARCH")
    if env:
        return env
    return DEFAULT_ARCH


def dynamic_linker(arch: str | None = None) -> str:
    """Intérprete ELF dinámico estándar FHS para la arquitectura dada."""
    arch = (arch or detect_arch()).lower()
    for xb, interp, _ in ARCH_MAP.values():
        if xb == arch:
            return interp
    return ARCH_MAP["x86_64"][1]


def nix_system(arch: str | None = None) -> str:
    arch = (arch or detect_arch()).lower()
    for xb, _, system in ARCH_MAP.values():
        if xb == arch:
            return system
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
    nix_store_dir: Path = Path("/nix")
    # [repo]
    host: str = "127.0.0.1"
    port: int = 8080
    # [build]
    arch: str = field(default_factory=detect_arch)
    python_version: str | None = None   # None → autodetectar del masterdir
    signing_key: Path | None = None     # None → keys_dir/privkey.pem
    log_level: str = "INFO"
    restricted_mode: bool = True        # bloquear empaquetado de no-redistribuibles
    offline: bool = False               # solo caché local, sin red

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
        "AUR2XBPS_CACHE_DIR": "cache_dir",
        "AUR2XBPS_REPO_DIR": "repo_dir",
        "AUR2XBPS_KEYS_DIR": "keys_dir",
        "AUR2XBPS_KEYDIR": "keys_dir",       # compat
        "AUR2XBPS_MASTERDIR": "masterdir",
        "AUR2XBPS_VOID_DIR": "void_packages_dir",
        "AUR2XBPS_NIX_STORE": "nix_store_dir",
        "AUR2XBPS_HOST": "host",
        "AUR2XBPS_PORT": "port",
        "AUR2XBPS_ARCH": "arch",
    }
    path_fields = {"data_dir", "cache_dir", "repo_dir", "keys_dir", "masterdir",
                   "void_packages_dir", "nix_store_dir"}
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

    Si se pasa ``path`` explícito, SOLO se usa ese archivo (sin fallbacks)
    — semántica determinista para tests y despliegues.
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
