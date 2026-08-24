# SPDX-License-Identifier: GPL-3.0-or-later
"""Generador automático de flakes Nix desde SrcInfo — Fase 2.

- Una derivación por subpaquete (split packages).
- buildFHSEnv solo para -bin; mkDerivation para fuente.
- Tabla Arch→Nix ampliada + heurística fallback.
- Reproducibilidad: SOURCE_DATE_EPOCH=0, sandbox, patchelf ordenado con chmod ±w.
"""
from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from src.common.paths import REPO_ROOT
from src.common.config import get_config, nix_system
from src.common.types import SrcInfo, SrcInfoPackage

# rama con toolchains actuales (go 1.26, rust nuevo): yay/paru exigen go>=1.26.
# El flake.lock fija el rev → determinismo por paquete intacto.
NIXOS_REF = "github:NixOS/nixpkgs/nixos-unstable"

FLAKE_TEMPLATE = """# Generado por aur2xbps — NO editar a mano
{restricted_header}{{
  description = "aur2xbps — {pkgbase} transpiled from AUR";

  inputs.nixpkgs.url = "{nixos_ref}";

  outputs = {{ self, nixpkgs }}:
    let
      system = "{nix_system}";
      pkgs = import nixpkgs {{ inherit system; config.allowUnfree = true; }};
    in {{
      packages.${{system}} = {{
{derivations}
      }};
    }};
}}
"""

DERIVATION_BIN = '''
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          # unzip/dpkg incondicionales: el unpackPhase de BIN los invoca por
          # tipo de fuente y su ausencia reventaba el build (exit 127)
          nativeBuildInputs = with pkgs; [ file unzip dpkg {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
          dontStrip = true;   # strip de stdenv corrompe binarios Go/precompilados
          dontWrapQtApps = true;  # el wrapper de Qt rompe apps precompiladas
          unpackPhase = ''
            runHook preUnpack
            case "$src" in
              *.deb) dpkg-deb --fsys-tarfile $src | tar -x --no-same-owner --no-same-permissions ;;
              *.rpm) rpm2cpio $src | cpio -idm --quiet ;;
              *.zip) unzip -q $src ;;
              *)     tar -xf $src --no-same-owner --no-same-permissions 2>/dev/null || dpkg-deb -x $src . ;;
            esac
            runHook postUnpack
          '';
          installPhase = ''
            mkdir -p $out
            cp -a usr $out/ 2>/dev/null || true
            cp -a opt $out/ 2>/dev/null || true
            # zips/tars sin estructura FHS: solo ELF ejecutables y shared objects
            if [ ! -e $out/usr ] && [ ! -e $out/opt ]; then
              mkdir -p $out/bin $out/lib
              find . -maxdepth 3 -type f -exec sh -c \\
                'file "$1" | grep -q "ELF.*executable" && cp "$1" $out/bin/' _ {{}} \\; 2>/dev/null || true
              find . -maxdepth 3 -type f -name "*.so*" -exec sh -c \\
                'file "$1" | grep -q "ELF" && cp "$1" $out/lib/' _ {{}} \\; 2>/dev/null || true
            fi
            # H-3.1: symlinks absolutos preservados del upstream apuntan al
            # filesystem del host (ej. /etc/shadow) o quedan rotos bajo $out.
            # Relativizarlos a la jerarquía contenida; irrecuperables → fuera.
            find "$out" -type l | while IFS= read -r _l; do
              _t=$(readlink "$_l")
              case "$_t" in
                /*)
                  _cand="$out$_t"
                  if [ -e "$_cand" ]; then
                    _rel=$(realpath --relative-to "$(dirname "$_l")" "$_cand")
                    ln -sfn "$_rel" "$_l"
                  else
                    echo "aur2xbps: symlink absoluto roto eliminado: $_l -> $_t"
                    rm -f "$_l"
                  fi
                  ;;
                *)
                  if [ ! -e "$_l" ]; then
                    echo "aur2xbps: symlink relativo roto eliminado: $_l -> $_t"
                    rm -f "$_l"
                  fi
                  ;;
              esac
            done
            find $out -exec touch -h -d @0 {{}} +
          '';
          # Sin patchelf en sandbox: los binarios quedan PRISTINOS desde upstream.
          # Toda la adaptación de enlace (rpath/intérprete a Void) la hace
        # src/xbps/pipeline.py sobre el stage, con validación readelf/ldd.
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            license = licenses.unfree;
            platforms = [ "{nix_system}" ];
          }};
        }};
'''

DERIVATION_SOURCE = '''
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
          makeFlags = [ "PREFIX=$(out)" ];
{install_guard}          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }}
'''.rstrip(" ") + ";"

# Tabla Arch → Nix ampliada (~90 entradas)
ARCH_TO_NIX = {
    # base / toolchain
    "glibc": "glibc", "gcc-libs": "gcc-unwrapped.lib", "libgcc": "gcc-unwrapped.lib",
    "libstdc++": "gcc-unwrapped.lib", "filesystem": "filesystem",
    "go": "go", "gcc-go": "gccgo",
    # make vive dentro de stdenv; el attr explícito es gnumake
    "make": "gnumake",
    # renombres históricos nixpkgs: gconf vive en gnome2.GConf
    "gconf": "gnome2.GConf",
    "boost-libs": "boost",
    # libalpm vive en pacman (nixpkgs); Arch la separa
    "libalpm": "pacman", "pacman": "pacman",
    # GUI core
    "gtk3": "gtk3", "gtk+3": "gtk3", "gtk4": "gtk4", "qt5-base": "qt5.qtbase",
    "qt6-base": "qt6.qtbase", "pango": "pango", "cairo": "cairo",
    "glib2": "glib", "glib": "glib", "gdk-pixbuf2": "gdk-pixbuf",
    "at-spi2-core": "at-spi2-core", "at-spi2-atk": "at-spi2-atk",
    "libayatana-appindicator": "libayatana-appindicator",
    "libappindicator-gtk3": "libappindicator-gtk3",
    # X11 / Wayland
    "libx11": "xorg.libX11", "libxft": "xorg.libXft",
    "freetype2": "freetype", "libxcomposite": "xorg.libXcomposite",
    "libxdamage": "xorg.libXdamage", "libxrandr": "xorg.libXrandr",
    "libxss": "xorg.libXScrnSaver", "libxtst": "xorg.libXtst",
    "libxext": "xorg.libXext", "libxfixes": "xorg.libXfixes",
    "libxcursor": "xorg.libXcursor", "libxi": "xorg.libXi",
    "libxinerama": "xorg.libXinerama", "libxrender": "xorg.libXrender",
    "libxcb": "xorg.libxcb", "libsm": "xorg.libSM", "libice": "xorg.libICE",
    "libxkbfile": "xorg.libxkbfile", "libxkbcommon": "libxkbcommon",
    "wayland": "wayland", "mesa": "mesa", "libglvnd": "libglvnd",
    "libdrm": "libdrm", "gbm": "mesa",
    # crypto / red
    "openssl": "openssl", "nss": "nss", "nspr": "nspr", "gnutls": "gnutls",
    "curl": "curl", "libcurl": "curl", "libcurl-gnutls": "curl",
    "ca-certificates": "ca-certificates", "expat": "expat",
    # multimedia / hw
    "alsa-lib": "alsa-lib", "pulseaudio": "pulseaudio", "pipewire": "pipewire",
    "libcups": "cups", "cups": "cups", "dbus": "dbus", "dbus-libs": "dbus",
    "libudev": "eudev", "systemd-libs": "systemdLibs",
    "zlib": "zlib", "ncurses": "ncurses", "bzip2": "bzip2", "xz": "xz", "zstd": "zstd",
    "lz4": "lz4", "ffmpeg": "ffmpeg", "ffmpeg4.4": "ffmpeg_4",
    "libva": "libva", "libvdpau": "libvdpau",
    # fuentes / iconos / mime
    "ttf-liberation": "liberation_ttf", "fontconfig": "fontconfig",
    "shared-mime-info": "shared-mime-info", "hicolor-icon-theme": "hicolor-icon-theme",
    "desktop-file-utils": "desktop-file-utils", "xdg-utils": "xdg-utils",
    # utilidades CLI comunes en depends
    "git": "git", "gnupg": "gnupg", "lsof": "lsof", "which": "which",
    "libnotify": "libnotify", "libsecret": "libsecret",
    "libxslt": "libxslt", "libxml2": "libxml2",
    "gnome-keyring": "gnome-keyring", "kwallet": "kwallet",
    "kdialog": "kdialog", "zenity": "zenity",
    "libdbusmenu-glib": "libdbusmenu-glib",
    "libdbusmenu-gtk3": "libdbusmenu-gtk3",
    "python": "python3", "nodejs": "nodejs", "npm": "nodejs",
    "rust": "rustc", "cargo": "cargo", "go": "go",
}

# Prefijos que indican binario precompilado
BIN_URL_EXTS = (".deb", ".rpm", ".AppImage", ".tar.zst", ".tar.gz", ".tgz")


REPOLOGY_TABLE: dict[str, str] = {}


def _load_repology_table() -> None:
    """Carga una vez la tabla Repología generada por scripts/repology-sync.py."""
    if REPOLOGY_TABLE:
        return
    for cand in (Path(__file__).parent.parent / "common" / "arch_to_nix_repology.json",
                 REPO_ROOT / "src" / "common" / "arch_to_nix_repology.json"):
        if cand.exists():
            try:
                REPOLOGY_TABLE.update(json.loads(cand.read_text()))
            except Exception:
                pass
            break


def _map_one(name: str, strict: bool = False) -> Optional[str]:
    """Mapea un nombre Arch a attr de nixpkgs. None = descartar.
    Capas: ARCH_TO_NIX manual → repology JSON → prefijos → None/raw.
    strict=True: solo entradas de la tabla (para -bin, donde buildInputs es
    prescindible y un attr desconocido rompe la evaluación Nix)."""
    _load_repology_table()
    if name in ARCH_TO_NIX:
        return ARCH_TO_NIX[name]
    if name in REPOLOGY_TABLE:
        return REPOLOGY_TABLE[name]
    if strict:
        return None
    # prefijos de ecosistema: python-X → python3Packages.X, etc.
    for prefix, scope in (("python-", "python3Packages."), ("perl-", "perlPackages."),
                          ("ruby-", "rubyPackages.")):
        if name.startswith(prefix):
            return scope + name[len(prefix):]
    if name.startswith("libx") and len(name) > 4:
        # libx* → xorg.libX*: casos conocidos + regla capitalizada segura
        rest = name[4:]
        known = {"xft": "xorg.libXft", "xkbfile": "xorg.libxkbfile",
                 "xscrnsaver": "xorg.libXScrnSaver", "xss": "xorg.libXScrnSaver",
                 "xtst": "xorg.libXtst", "xrandr": "xorg.libXrandr",
                 "xinerama": "xorg.libXinerama", "xcursor": "xorg.libXcursor",
                 "xcomposite": "xorg.libXcomposite", "xdamage": "xorg.libXdamage",
                 "xext": "xorg.libXext", "xfixes": "xorg.libXfixes",
                 "xi": "xorg.libXi", "xmu": "xorg.libXmu", "xp": "xorg.libXp",
                 "xpm": "xorg.libXpm", "xrender": "xorg.libXrender",
                 "xres": "xorg.libXres", "xt": "xorg.libXt", "xv": "xorg.libXv"}
        return known.get(rest, "xorg.libX" + rest.capitalize())
    if name.startswith("lib32-"):
        return None                      # multilib: fuera de alcance x86_64
    if name.startswith("ttf-") or name.startswith("otf-"):
        return "dejavu_fonts"
    if name in ("base", "base-devel"):
        return None                      # implícitos en sandbox Nix
    return name                           # fallback (validar en build)


def map_deps_to_nix(deps: List[str], strict: bool = False) -> str:
    names = []
    for d in deps:
        raw = re.split(r"[<>=]", d)[0].strip()
        # Soname deps estilo Arch (libalpm.so>13, libcurl.so.4): el sufijo
        # .so* jamás es un attr de nixpkgs → normalizar al nombre base ANTES
        # de mapear (paru: buildInputs=[libalpm.so] reventaba la evaluación).
        raw = re.sub(r"\.so.*$", "", raw)
        if not raw or "." in raw:
            continue
        mapped = _map_one(raw, strict=strict)
        if mapped:
            names.append(mapped)
    return " ".join(sorted(set(n for n in names if n)))




def _extract_url(raw: str) -> str:
    return raw.split("::", 1)[1].strip() if "::" in raw else raw.strip()


def _pick_hash(pkg: SrcInfoPackage):
    """Prefiere sha256 x86_64; fallback sha512/b2. Retorna (attr_nix, valor).
    fetchurl solo soporta sha256/sha512 (no attr 'b2'): b2 se pasa como sha512
    hex (nix acepta hex de cualquier algoritmo vía el attr correcto; b2 real
    requeriría conversión — documentado)."""
    order = [("sha256", "sha256"), ("sha512", "sha512"), ("b2", "sha512")]
    arch = get_config().arch
    for algo, attr in order:
        vals = pkg.sums_for(algo, arch)
        if vals and vals[0] != "SKIP":
            return attr, vals[0]
    # fallback genérico (sin arch)
    for algo, attr in order:
        vals = pkg.sums_for(algo, "")
        if vals and vals[0] != "SKIP":
            return attr, vals[0]
    # último recurso: cualquier otra arch cualificada con hash real
    for algo, attr in order:
        for a, vals in pkg.sums.get(algo, {}).items():
            if a and vals and vals[0] != "SKIP":
                return attr, vals[0]
    return "sha256", "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _is_bin(pname: str, pkgbase: str, url: str) -> bool:
    """Precompilado si el nombre lo indica, o formatos inequívocamente
    binarios (deb/rpm/AppImage). Los tarballs (.tar.gz/.zip/.tgz) son
    ambiguos → se tratan como FUENTE; los -bin reales ya traen -bin en
    el nombre y sus assets también suelen llamarse igual."""
    if pname.endswith("-bin") or pkgbase.endswith("-bin"):
        return True
    low = url.lower()
    return any(low.endswith(e) or e + "?" in low
               for e in (".deb", ".rpm", ".appimage"))


class VCSPackageError(RuntimeError):
    """Paquete VCS (-git/-svn/-hg): requiere fetchgit con rev fijado (fuera de alcance simple)."""


def _is_vcs_url(url: str) -> bool:
    low = url.lower()
    return any(low.startswith(p) for p in ("git+", "hg+", "bzr+", "svn+")) or \
        re.search(r"\.git(#.*)?$", low) is not None


def _vcs_repo_url(url: str) -> str:
    """'neofetch.git::git+https://github.com/x/y.git#tag=v1.5' → 'https://github.com/x/y.git'"""
    u = _extract_url(url)
    for p in ("git+", "hg+", "bzr+", "svn+"):
        if u.lower().startswith(p):
            u = u[len(p):]
    # fragmentos de AUR: #tag=v1.56, #commit=abc, #branch=x
    return re.sub(r"#.*$", "", u)


def pin_git_rev(repo_url: str, timeout: int = 60) -> str:
    """Obtiene el commit HEAD actual vía git ls-remote (sin clonar).
    Ese rev se fija en la derivación: reproducible hasta que se re-pinnee."""
    r = subprocess.run(["git", "ls-remote", repo_url, "HEAD"],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise VCSPackageError(f"git ls-remote falló para {repo_url}: {r.stderr[:200]}")
    return r.stdout.split()[0]



# ---------- Plantillas por ecosistema (paquetes fuente) ----------
# Detección por makedepends del .SRCINFO (única fuente de verdad sin evaluar PKGBUILD)

def detect_ecosystem(pkg: SrcInfoPackage) -> str:
    md = " ".join(pkg.makedepends_for()).lower()
    # Muchos paquetes Python listan python-setuptools etc. en DEPENDS (no makedepends)
    all_dep_str = " ".join(pkg.makedepends_for() + pkg.depends_for()).lower()
    name = pkg.pkgname.lower()
    base = pkg.pkgbase.lower()
    if any(name.startswith(s) or base == s for s in ("dwm", "st", "slstatus", "sent", "slock")):
        return "suckless"
    # ecosistemas con fetch de dependencias propio: requieren builders
    # dedicados con hash de vendor (paru=Rust, yay=Go)
    md_names = [re.split(r"[<>=]", d)[0].strip() for d in pkg.makedepends_for()]
    if any(x in ("cargo", "rust", "rustup") for x in md_names) or "cargo" in name:
        return "cargo"
    if any(x in ("go", "gcc-go", "go-pie") for x in md_names):
        return "go"
    # Node.js: makedepends node/npm o tarballs de registry.npmjs.org
    try:
        src_blob = " ".join(pkg.sources_for()).lower()
    except Exception:                                   # noqa: BLE001
        src_blob = ""
    if ("nodejs" in md_names or "npm" in md_names
            or "registry.npmjs.org" in src_blob or name.startswith("nodejs-")):
        return "nodejs"
    if "meson" in md:
        return "meson"
    if "cmake" in md:
        return "cmake"
    if any(k in all_dep_str for k in ("python-build", "python-installer", "poetry-core")):
        return "python-pep517"
    if any(k in all_dep_str for k in ("python-setuptools", "python-wheel", "python-pip",
                                      "setuptools", "python-distribute")):
        return "python-legacy"
    return "autotools"


DERIV_PYTHON_PEP517 = """
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          format = "other";
          nativeBuildInputs = with pkgs; [
            {native_inputs}
            python3Packages.build python3Packages.pip python3Packages.wheel file
          ];
          buildInputs = with pkgs; [ {build_inputs} ];
          dontStrip = true;
          buildPhase = ''
            runHook preBuild
            export SOURCE_DATE_EPOCH=0
            python -m build --wheel --no-isolation --outdir dist/
            runHook postBuild
          '';
          installPhase = ''
            runHook preInstall
            WHEEL=$(ls dist/*.whl | head -n1)
            PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            SP="$out/usr/lib/python$PYVER/site-packages"
            mkdir -p "$SP" "$out/usr/bin"
            # Wheel = zip con rutas relativas → extraer directo a site-packages Void
            unzip -q -o "$WHEEL" -d "$SP"
            find "$out" -exec touch -h -d @0 {{}} +
            runHook postInstall
          '';
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }};
"""

DERIV_CMAKE = """
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ cmake ninja file wrapQtAppsHook {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }};
"""

DERIV_MESON = """
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          unpackPhase = ''
            # Normaliza fuente con múltiples entradas raíz
            entries=($(ls -A))
            if [ ''${{#entries[@]}} = 1 ] && [ -d "''${{entries[0]}}" ]; then
              cd "''${{entries[0]}}"
            fi
            runHook preUnpack
            mkdir -p source && cd source
            case "$src" in
              *.zip) unzip -q $src ;;
              *)     tar -xf $src --no-same-owner ;;
            esac
          '';
          nativeBuildInputs = with pkgs; [ meson ninja pkg-config file wrapQtAppsHook {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }};
"""

DERIV_AUTOTOOLS = """
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ file pkg-config python3 gnome-common glib libtool automake {native_inputs} ]
            ++ pkgs.lib.optionals ({needs_autoreconf}) [ autoreconfHook scdoc ];
          buildInputs = with pkgs; [ {build_inputs} ];
          strictDeps = true;
          makeFlags = [ "PREFIX=$(out)" ];  # algunos Makefiles ignoran prefix por defecto
          dontStrip = true;
{install_guard}
          postPatch = \'\'
            # Normaliza PREFIX hardcodeado /usr/local → $out
            if grep -q "/usr/local" Makefile 2>/dev/null; then
              substituteInPlace Makefile --replace-fail "/usr/local" "$(out)"
            fi
          \'\';
          preBuild = \'\'
            # Muchos ./configure invocan "python" (no python3)
            command -v python >/dev/null 2>&1 || {{
              mkdir -p "$TMPDIR"
              ln -sf "$(command -v python3)" "$TMPDIR/python"
              export PATH="$TMPDIR:$PATH"
            }}
          \'\';
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }};
"""

# python-legacy (setup.py con cmdclass custom) es EXPERIMENTAL: se mapea a
# autotools-genérico no, se deja fuera hasta plantilla dedicada estable.
DERIV_SUCKLESS = """
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ file pkg-config {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
          dontStrip = true;
          unpackPhase = ''
            runHook preUnpack
            mkdir -p source && cd source
            tar -xf $src --no-same-owner
            entries=($(ls -A))
            if [ ''${{#entries[@]}} = 1 ] && [ -d "''${{entries[0]}}" ]; then
              cd "''${{entries[0]}}"
            fi
            runHook postUnpack
          '';
          postPatch = \'\'
            [ -f config.def.h ] && [ ! -f config.h ] && cp config.def.h config.h || true
            # Solo PREFIX hardcodeado; NO tocar /usr/include del config.mk
            grep -q "^PREFIX" Makefile || \
              sed -i "s|^PREFIX.*|PREFIX = $(out)|" Makefile || true
          \'\';
          preBuild = \'\'
            [ -f config.def.h ] && [ ! -f config.h ] && cp config.def.h config.h || true
          \'\';
          installPhase = \'\'
            runHook preInstall
            mkdir -p "$out/bin" "$out/share/man/man1"
            found=""
            for b in slstatus dwm st slock sent {pkgname}; do
              if [ -f "$b" ]; then install -Dm755 "$b" "$out/bin/$b"; found=1; fi
            done
            for m in *.1; do
              [ -f "$m" ] && install -Dm644 "$m" "$out/share/man/man1/$m"
            done
            [ -n "$found" ] || {{ echo "SUCKLESS_FAIL: binario ausente"; exit 1; }}
            runHook postInstall
          \'\';
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }};
"""

DERIV_PYTHON_SOURCE_ONLY = """
        "{pkgname}-src" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}-src";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          dontStrip = true;
          dontFixup = true;
          unpackPhase = \'\'
            runHook preUnpack
            case "$src" in
              *.zip) unzip -q $src ;;
              *)     tar -xf $src --no-same-owner ;;
            esac
            entries=($(ls -A))
            if [ ''${{#entries[@]}} = 1 ] && [ -d "''${{entries[0]}}" ]; then
              mkdir -p "$out/src"
              cp -a "''${{entries[0]}}"/* "$out/src/"
            else
              mkdir -p "$out/src"
              cp -a . "$out/src/"
            fi
            runHook postUnpack
          \'\';
          installPhase = "touch $out";
        }};
"""

DERIV_CARGO = """
        "{pkgname}" = pkgs.rustPlatform.buildRustPackage rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ pkg-config file {native_inputs} ];
          # openssl: openssl-sys es la dependencia nativa más común del
          # ecosistema Rust; sin ella el build revienta en el runner
          buildInputs = with pkgs; [ openssl {build_inputs} ];
          dontStrip = true;
          # vendor placeholder → build_with_hash_fix lo sustituye por el
          # sha256 real que Nix reporta en el error del FOD. Attr correcto
          # según nixpkgs 24.11: cargoHash (vendorHash es de buildGoModule).
          cargoHash = "{hash_vendor}";
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }}
""".rstrip(" ") + ";"


DERIV_GO = """
        "{pkgname}" = pkgs.buildGoModule rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ file {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
          doCheck = false;
          ldflags = [ "-s -w" ];
          # solo el paquete raíz: ./... arrastra dirs sin .go (scripts/gendocs)
          subPackages = [ "." ];
          # vendor placeholder → auto-corregido en build (ver HASH_DUMMY)
          vendorHash = "{hash_vendor}";
          preBuild = \'\'
            # Fix Go build cache permission denied in sandbox
            export GOBUILDCACHE="$TMPDIR/go-build"
            export GOCACHE="$TMPDIR/go-cache"
            mkdir -p "$GOBUILDCACHE" "$GOCACHE"
          \'\';
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }}
""".rstrip(" ") + ";"


DERIV_NODEJS = """
        "{pkgname}" = pkgs.buildNpmPackage rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchurl {{
            url = "{url}";
            {hash_attr} = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ file nodejs python3 {native_inputs} ];
          buildInputs = with pkgs; [ nodejs {build_inputs} ];
          dontStrip = true;
          dontNpmBuild = true;
          # deps placeholder → auto-corregido en build (ver HASH_DUMMY)
          npmDepsHash = "{hash_vendor}";
          preBuild = \'\'
            # Algunos configure scripts invocan "python" (no python3)
            command -v python >/dev/null 2>&1 || {{
              mkdir -p "$TMPDIR"
              ln -sf "$(command -v python3)" "$TMPDIR/python"
              export PATH="$TMPDIR:$PATH"
            }}
          \'\';
          meta = with pkgs.lib; {{
            description = "{pkgdesc}";
            platforms = [ "{nix_system}" ];
          }};
        }}
""".rstrip(" ") + ";"


ECOSYSTEM_TEMPLATES = {
    "python-pep517": DERIV_PYTHON_SOURCE_ONLY,
    "python-legacy": DERIV_PYTHON_SOURCE_ONLY,
    "cmake":         DERIV_CMAKE,
    "meson":         DERIV_MESON,
    "autotools":     DERIV_AUTOTOOLS,
    "suckless":      DERIV_SUCKLESS,
    "cargo":         DERIV_CARGO,
    "go":            DERIV_GO,
    "nodejs":        DERIV_NODEJS,
}
SUPPORTED_ECOSYSTEMS = set(ECOSYSTEM_TEMPLATES) | {"python-pep517"}

# Placeholder canónico de hash pendiente: build_with_hash_fix lo sustituye
# globalmente con el hash real que Nix reporta en el error (fetchurl, fetchgit
# y vendorHash/cargoVendorHash de FODs).
HASH_DUMMY = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

# Guard universal anti-paquete-fantasma: si el Makefile del upstream instala
# en silencio a /usr (que en sandbox falla o no-op), el $out queda VACÍO y el
# pipeline empaqueta un .xbps sin ficheros. Esta fase garantiza binarios en
# $out/bin o aborta con error claro.
INSTALL_GUARD_PHASE = """
          installPhase = ''
            runHook preInstall
            if [ -f Makefile ]; then
              make PREFIX=$out DESTDIR= install || true
            fi
            mkdir -p $out/bin
            find . -maxdepth 2 -type f ! -name Makefile | while read -r _f; do
              case "$(file -b "$_f")" in
                *ELF*|*script*|*text*executable*)
                  [ -x "$_f" ] && install -Dm755 "$_f" "$out/bin/$(basename "$_f")"
                  ;;
              esac
            done
            if [ -z "$(ls -A $out/bin 2>/dev/null)" ] \\
               && [ -z "$(ls -A $out 2>/dev/null | grep -v '^bin$')" ]; then
              echo "aur2xbps: ERROR instalacion vacia (paquete fantasma)"
              exit 1
            fi
            runHook postInstall
          '';
"""


DERIV_VCS_ECO = """
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchgit {{
            url = "{url}";
            rev = "{rev}";
            hash = "{hash_val}";
          }};
{eco_body}
          meta = with pkgs.lib; {{
            description = "{pkgdesc} (VCS pinneado a {rev_short})";
            platforms = [ "{nix_system}" ];
          }};
        }};
"""

# Cuerpo por ecosistema para VCS (inputs+fases sin src ni meta)
ECO_BODIES = {
    "python-pep517": """
          nativeBuildInputs = with pkgs; [
            {native_inputs}
            python3Packages.build python3Packages.pip python3Packages.wheel file
          ];
          buildInputs = with pkgs; [ {build_inputs} ];
          dontStrip = true;
          buildPhase = \'\'
            runHook preBuild
            export SOURCE_DATE_EPOCH=0
            python -m build --wheel --no-isolation --outdir dist/
            runHook postBuild
          \'\';
          installPhase = \'\'
            runHook preInstall
            python -m pip install --no-deps --no-build-isolation --no-index \
              --prefix="$out" dist/*.whl
            runHook postInstall
          \'\';
""",
    "cmake": """
          nativeBuildInputs = with pkgs; [ cmake ninja file {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
""",
    "meson": """
          unpackPhase = ''
            runHook preUnpack
            # fetchgit entrega un directorio; los tarballs se normalizan
            if [ -d "$src" ]; then
              cp -a "$src" ./source
              cd source
            else
              mkdir -p source && cd source
              case "$src" in
                *.zip) unzip -q $src ;;
                *)     tar -xf $src --no-same-owner ;;
              esac
              entries=($(ls -A))
              if [ ''${{#entries[@]}} = 1 ] && [ -d "''${{entries[0]}}" ]; then
                cd "''${{entries[0]}}"
              fi
            fi
            runHook postUnpack
          '';
          nativeBuildInputs = with pkgs; [ meson ninja pkg-config file {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
""",
    "python-legacy": """
          nativeBuildInputs = with pkgs; [
            {native_inputs} python3Packages.setuptools python3Packages.wheel file
          ];
          buildInputs = with pkgs; [ {build_inputs} ];
          dontStrip = true;
          postPatch = \'\'
            # experimental: neutraliza targets check/lint (yapf/flake8 no hay
            # en sandbox) que setup.py cmdclass invoca en install
            [ -f Makefile ] && sed -i "/^check:/,/^$/d" Makefile || true
            sed -i "/yapf/d; /flake8/d; /pylint/d" Makefile 2>/dev/null || true
          \'\';
          installPhase = \'\'
            runHook preInstall
            export SOURCE_DATE_EPOCH=0
            python -m pip install . --no-deps --no-build-isolation \\
              --prefix="$out" || python setup.py install --prefix="$out"
            runHook postInstall
          \'\';
""",
    "autotools": """
          nativeBuildInputs = with pkgs; [ file {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
""",
}

DERIVATION_VCS = '''
        "{pkgname}" = pkgs.stdenv.mkDerivation rec {{
          pname = "{pkgname}";
          version = "{pkgver}";
          src = pkgs.fetchgit {{
            url = "{url}";
            rev = "{rev}";
            hash = "{hash_val}";
          }};
          nativeBuildInputs = with pkgs; [ {native_inputs} ];
          buildInputs = with pkgs; [ {build_inputs} ];
          makeFlags = [ "PREFIX=$(out)" ];
{install_guard}          meta = with pkgs.lib; {{
            description = "{pkgdesc} (VCS pinneado a {rev_short})";
            platforms = [ "{nix_system}" ];
          }};
        }};
'''


ARCHIVE_EXTS = (".deb", ".rpm", ".appimage", ".zip", ".tar.gz", ".tgz",
                ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar.zst", ".tzst")
ICON_EXTS = (".png", ".svg", ".jpg", ".jpeg", ".gif", ".ico", ".desktop",
             ".xml", ".html", ".sig", ".patch")


def _pick_main_source(urls: List[str]) -> str:
    """Elige la fuente principal: prioriza archivos comprimidos/binarios;
    descarta iconos, .desktop y metadatos."""
    if not urls:
        return "https://example.com/dummy.tar.gz"

    def ext_of(u: str) -> str:
        path = _extract_url(u).split("?")[0].lower()
        for e in ARCHIVE_EXTS:
            if path.endswith(e):
                return e
        return ""

    archives = [u for u in urls if ext_of(u)]
    if archives:
        # el más "grande" por nombre de extensión compuesta primero (.tar.xz > .tgz)
        archives.sort(key=lambda u: -len(ext_of(u)))
        return archives[0]
    non_icon = [u for u in urls
                if not _extract_url(u).lower().endswith(ICON_EXTS)]
    return (non_icon or urls)[0]



def _native_inputs_for_url(url: str, base: str) -> str:
    """nativeBuildInputs según tipo de fuente: file siempre; unzip/dpkg/rpm
    según extensión."""
    extra = ["file"]  # necesario para detección ELF en installPhase/fixup
    low = url.lower()
    if low.endswith(".zip"):
        extra.append("unzip")
    if low.endswith(".deb") or "dpkg-deb" in base:
        extra.append("dpkg")
    if low.endswith(".rpm") or "rpm2cpio" in base:
        extra.extend(["rpm", "cpio"])
    return f"{base} {' '.join(extra)}".strip()


def _rpath_extra(arch: str) -> str:
    """Rutas RUNPATH extra según arch: /usr/lib64 solo existe en x86_64."""
    return "/usr/lib:/usr/lib64" if arch == "x86_64" else "/usr/lib"


def generate_flake(srcinfo: SrcInfo, out_dir: Path,
                   nixos_ref: str = NIXOS_REF,
                   eco_override: str | None = None,
                   force_autoreconf: bool = False) -> Path:
    """Genera flake.nix con una derivación por subpaquete."""
    from src.common.config import get_config, nix_system as _nix_system
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    derivations = []
    cfg_arch = get_config().arch

    for pname, pkg in srcinfo.packages.items():
        urls = pkg.sources_for(cfg_arch) or pkg.sources_for("")
        http_urls = [u for u in urls if "http" in u]
        raw = _pick_main_source(http_urls or urls)
        url = _extract_url(raw)
        hash_attr, hash_val = _pick_hash(pkg)
        # -bin: estricto (solo attrs conocidos); fuente: heurística completa
        is_bin = _is_bin(pname, srcinfo.pkgbase, url) if not _is_vcs_url(url) else False
        build_inputs = map_deps_to_nix(pkg.depends_for(), strict=is_bin)
        native_inputs = _native_inputs_for_url(
            url, map_deps_to_nix(pkg.makedepends_for()) or "patchelf")
        pkgdesc = (pkg.pkgdesc or pname).replace('"', "'").replace("\\", "")

        if _is_vcs_url(url):
            # VCS: fijar rev HEAD vía git ls-remote; hash se auto-corrige en build
            repo = _vcs_repo_url(raw)
            rev = pin_git_rev(repo)
            eco_vcs = detect_ecosystem(pkg)
            # Python fuente/VCS → source-only (build con python de Void en pipeline)
            if eco_vcs in ("python-pep517", "python-legacy"):
                # VCS python → source-only con fetchgit; build real en Void pipeline.
                derivations.append(f'''        "{pname}-src" = pkgs.stdenv.mkDerivation {{
                  pname = "{pname}-src";
                  version = "{pkg.pkgver}";
                  src = pkgs.fetchgit {{
                    url = "{repo}";
                    rev = "{rev}";
                    hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
                    fetchSubmodules = true;
                  }};
                  dontStrip = true;
                  dontFixup = true;
                  installPhase = "cp -a . $out";
                  meta.description = "{pkgdesc}";
                }};
''')
                continue
            eco_body_fmt = ECO_BODIES.get(eco_vcs, ECO_BODIES["autotools"])
            if eco_vcs == "autotools":
                md_names = [re.split(r"[<>=]", d)[0].strip()
                            for d in pkg.makedepends_for()]
                eco_body_fmt += ("\n          autoreconfHook = "
                                 + str(any(x in md_names for x in
                                           ("automake", "autoconf", "libtool"))).lower()
                                 + ";")
                # anti-fantasma: VCS sin fases propias dejaba $out vacío
                # (neofetch empaquetaba 531B sin binarios)
                eco_body_fmt += INSTALL_GUARD_PHASE
            eco_body = eco_body_fmt.format(
                native_inputs=native_inputs, build_inputs=build_inputs or "glibc")
            drv = DERIV_VCS_ECO.format(
                pkgname=pname, pkgver=pkg.pkgver, url=repo, rev=rev,
                rev_short=rev[:7], hash_val="sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                eco_body=eco_body, pkgdesc=pkgdesc,
                nix_system=nix_system(cfg_arch),
            )
            derivations.append(drv)
            continue

        if _is_bin(pname, srcinfo.pkgbase, url):
            template = DERIVATION_BIN
            fmt_kwargs = dict(rpath_extra=_rpath_extra(cfg_arch))
            build_inputs_str = map_deps_to_nix(pkg.depends_for(), strict=True) or "glibc"
        else:
            eco = eco_override or detect_ecosystem(pkg)
            template = ECOSYSTEM_TEMPLATES.get(eco, DERIVATION_SOURCE)
            fmt_kwargs = {}
            if eco == "autotools":
                md_names = [re.split(r"[<>=]", d)[0].strip()
                            for d in pkg.makedepends_for()]
                # Muchos Makefiles invocan autoreconf internamente (ej. gnomato);
                # siempre habilitar autoreconfHook para ecosistema autotools.
                needs_ar = force_autoreconf or True
                fmt_kwargs["needs_autoreconf"] = str(needs_ar).lower()
                fmt_kwargs["install_guard"] = AUTORECONF_ACLOCAL
            build_inputs_str = build_inputs or "glibc"
        drv = template.format(
            pkgname=pname,
            pkgver=pkg.pkgver,
            url=url,
            hash_attr=hash_attr,
            hash_val=hash_val,
            hash_vendor=HASH_DUMMY,
            install_guard=fmt_kwargs.pop("install_guard", ""),
            build_inputs=_dedupe_inputs(build_inputs_str),
            native_inputs=_dedupe_inputs(native_inputs),
            pkgdesc=pkgdesc,
            nix_system=nix_system(cfg_arch),
            **fmt_kwargs,
        )
        derivations.append(drv)

    flake = FLAKE_TEMPLATE.format(pkgbase=srcinfo.pkgbase,
                                  nixos_ref=nixos_ref,
                                  derivations="".join(derivations),
                                  restricted_header=_restricted_header(srcinfo),
                                  nix_system=nix_system(cfg_arch))
    # renombres dentro del namespace python3Packages (attrs históricos)
    flake = re.sub(r"python3Packages\.dbus\b(?!-)",
                   "python3Packages.dbus-python", flake)
    flake_path = out_dir / "flake.nix"
    flake_path.write_text(flake)
    return flake_path


def _restricted_header(srcinfo: SrcInfo) -> str:
    """Marca `restricted=yes` en plantillas de paquetes no redistribuibles."""
    from src.aur.security import is_restricted
    restricted, reason = is_restricted(srcinfo)
    if not restricted:
        return ""
    return (f"# restricted=yes — {reason}\n"
            f"# NO distribuir el .xbps resultante; solo uso local/privado.\n")


def lock_flake(out_dir: Path) -> bool:
    """Genera/actualiza flake.lock. Retorna True si OK."""
    r = subprocess.run(
        ["nix", "flake", "lock", "--extra-experimental-features", "nix-command flakes"],
        cwd=out_dir, capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def lint_flake_patchelf(flake_path: Path) -> List[str]:
    """Reutiliza el linter anti-combinado sobre el flake generado."""
    from src.nix.patchelf import lint_fixupPhase
    return lint_fixupPhase(flake_path.read_text())


def resolve_nixos_ref(srcinfo: SrcInfo) -> str:
    """Ref de nixpkgs para este paquete: pin por-paquete (config
    [nixpkgs_pins]) > env AUR2XBPS_NIXOS_REF > NIXOS_REF global.

    Los pins permiten fijar un rev histórico ante incompatibilidades ABI
    upstream (ej. crate alpm exige libalpm v15 → rev con pacman 6.x).
    """
    import os as _os
    from src.common.config import get_config
    cfg = get_config()
    for key in (srcinfo.pkgbase, getattr(srcinfo, "pkgname", None)):
        if key and key in cfg.nixpkgs_pins:
            return cfg.nixpkgs_pins[key]
    return _os.environ.get("AUR2XBPS_NIXOS_REF") or NIXOS_REF


def _dedupe_inputs(spec: str) -> str:
    """Elimina duplicados preservando orden en listas de inputs Nix."""
    seen: dict[str, None] = {}
    for tok in (spec or "").split():
        seen.setdefault(tok, None)
    return " ".join(seen)


def transpile(srcinfo: SrcInfo, out_dir: Path,
              eco_override: str | None = None,
              force_autoreconf: bool = False) -> Path:
    """API de alto nivel: genera flake + lock + lint. Lanza si lint falla."""
    flake = generate_flake(srcinfo, out_dir,
                           nixos_ref=resolve_nixos_ref(srcinfo),
                           eco_override=eco_override,
                           force_autoreconf=force_autoreconf)
    errs = lint_flake_patchelf(flake)
    if errs:
        raise RuntimeError(f"Linter patchelf falló: {errs}")
    if not lock_flake(out_dir):
        raise RuntimeError("nix flake lock falló")
    return flake


HASH_MISMATCH_RE = re.compile(r"got:\s+(sha(?:256|512)-[A-Za-z0-9+/=]+)")
# FOD mismatch completo: permite sustituir el hash ESPECIFICADO por el real
# en cualquier posición del flake (fetchurl, fetchgit, vendorHash, npmDeps…)
SPECIFIED_GOT_RE = re.compile(
    r"specified:\s+(sha(?:256|512)-[A-Za-z0-9+/=]+).*?"
    r"got:\s+(sha(?:256|512)-[A-Za-z0-9+/=]+)",
    re.DOTALL)
# Attr inexistente en nixpkgs (renombras/eliminaciones upstream): se elimina
# del flake y se reintenta — cubre clases completas sin mapeos uno a uno
UNDEF_VAR_RE = re.compile(r"undefined variable '([A-Za-z0-9_.-]+)'")
# Attr que existe pero lanza (ej. python2): mismo tratamiento
REMOVED_RE = re.compile(r"([\w][\w.-]*)\s*=\s*throw \"[^\"]*has been removed")


# macros .m4 de dependencias (AM_GLIB_GNU_GETTEXT, PKG_PROG_PKG_CONFIG…)
# viven en <dep>/share/aclocal y aclocal NO las descubre solo. Texto FINAL
# de nix (sin pasar por .format: shell ${..} chocaría con las llaves).
AUTORECONF_ACLOCAL = """          # recolecta macros .m4 de los inputs
          preAutoreconf = pkgs.lib.optionalString true ''
            for d in $buildInputs $nativeBuildInputs; do
              if [ -d "$d/share/aclocal" ]; then
                export ACLOCAL_PATH="''${ACLOCAL_PATH:+''${ACLOCAL_PATH}:}$d/share/aclocal"
              fi
            done
          '';
"""


def _drop_undefined_var(flake_path: Path, name: str) -> bool:
    """Elimina todas las ocurrencias del token indefinido en flake.nix."""
    try:
        content = flake_path.read_text()
    except OSError:
        return False
    if name not in content:
        return False
    nuevo = re.sub(rf"(?<![\w.-]){re.escape(name)}(?![\w-])\s*", "", content)
    if nuevo == content:
        return False
    flake_path.write_text(nuevo)
    return True


def build_with_hash_fix(out_dir: Path, attr: str, max_retries: int = 5,
                        timeout: int = 600) -> tuple[bool, str]:
    """Build con auto-corrección de hash SKIP: Nix reporta el hash real en el
    error y lo parcheamos en flake.nix (equivalente a nix-prefetch-url)."""
    out_dir = Path(out_dir)
    flake = out_dir / "flake.nix"
    last_err = ""
    # Sistema del attr derivado de la arch configurada (T-3: nunca hardcodear
    # x86_64-linux; un override AUR2XBPS_ARCH sin esto era ignorado al compilar)
    system = nix_system(get_config().arch)
    for attempt in range(max_retries + 1):
        try:
            r = subprocess.run(
                ["nix", "build", f".#packages.{system}.{attr}",
                 "--extra-experimental-features", "nix-command flakes",
                 "--option", "sandbox", "true"],
                cwd=out_dir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"nix build excedió {timeout}s (intento {attempt + 1})"
        if r.returncode == 0:
            return True, "build OK"
        combined = r.stdout + r.stderr
        # post-mortem: log íntegro junto al flake (el mensaje CLI solo
        # conserva la cola)
        try:
            (out_dir / "nix-build.log").write_text(combined[-200_000:])
        except OSError:
            pass
        m = HASH_MISMATCH_RE.search(combined)
        if not m:
            # attr inexistente (renombrado/eliminado upstream): quitar y reintentar
            u = UNDEF_VAR_RE.search(combined)
            r = None if u else REMOVED_RE.search(combined)
            culprit = u.group(1) if u else (r.group(1) if r else "")
            if culprit and _drop_undefined_var(flake, culprit):
                last_err = f"attr indefinido '{culprit}' eliminado (intento {attempt + 1})"
                continue
            return False, (combined[-2000:] or "error desconocido")
        got = m.group(1)
        content = flake.read_text()
        # (a) placeholder canónico (fetchgit/fetchurl/vendorHash de cargo/go):
        #     sustitución global — cada placeholder espera SU hash real y Nix
        #     solo reporta el primer FOD fallido por intento
        if HASH_DUMMY in content:
            content_new = content.replace(HASH_DUMMY, got)
        else:
            # (b) hash especificado ≠ real: sustituir TODAS las ocurrencias
            #     del especificado por el reportado (vendor único en la práctica)
            sg = SPECIFIED_GOT_RE.search(combined)
            if sg and sg.group(1) in content:
                content_new = content.replace(sg.group(1), sg.group(2))
                got = sg.group(2)
            else:
                # (c) legado: un solo fetchurl → reescribir su hash
                n_fetch = content.count("pkgs.fetchurl")
                if n_fetch != 1:
                    return False, f"multi-fetchurl ({n_fetch}): parche manual"
                pat = r'(sha256|sha512|hash) = "[A-Za-z0-9+/=-]+"'
                content_new = re.sub(pat, f'sha256 = "{got}"', content, count=1)
        if content_new == content:
            return False, f"no se pudo parchear hash {got}"
        flake.write_text(content_new)
        last_err = f"hash corregido a {got} (intento {attempt + 1})"
    return False, last_err or "agotados reintentos de hash"
