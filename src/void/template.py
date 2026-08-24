# SPDX-License-Identifier: GPL-3.0-or-later
"""Generador de plantillas xbps-src estándar desde .SRCINFO del AUR.

Salida compatible con ``xbps-src`` puro (sin aur2xbps en el medio) y por
tanto consumible por vouru: ``srcpkgs/<pkgname>/template``.

Contrato con vouru:
  - el árbol generado se copia/sincroniza a ``<void-packages>/srcpkgs/``
  - compila con ``./xbps-src pkg <pkgname>``
  - binario resultante en ``hostdir/binpkgs/``

build_style elegido por heurística (misma detección que el transpilador Nix):
  meta | python3-module | cmake | meson | gnu-makefile | fetch (-bin)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.common.config import get_config
from src.aur.parser import SrcInfo
from src.aur.security import is_restricted
from src.nix.generator import detect_ecosystem
from src.void.mapping import map_dep, map_deps

MAINTAINER = "aur2xbps <aur2xbps@local>"

#: sufijos inequívocos de BINARIO precompilado (plantilla fetch)
BIN_SUFFIXES = (".deb", ".rpm", ".appimage", ".pkg.tar.zst", ".exe")

HEADER_RE = re.compile(r"[^A-Za-z0-9._+-]")


def _clean(s: str | None, default: str = "") -> str:
    s = (s or default).replace('"', "'").replace("\\", "").strip()
    return " ".join(s.split())


def _copy_install_target(pkg: SrcInfoPackage) -> str | None:
    """Destino de instalación por copia para familias sin sistema de build
    (plugins/themes): zsh-*, vim-*… Retorna ruta relativa bajo /usr/share o None."""
    n = pkg.pkgname.lower()
    for suf in ("-git", "-bin"):
        if n.endswith(suf):
            n = n[:-len(suf)]
    if n.startswith("zsh-") or n.endswith("-zsh-plugin"):
        base = n[4:] if n.startswith("zsh-") else n.replace("-zsh-plugin", "")
        return f"/usr/share/zsh/plugins/{base}"
    if n.startswith("vim-") or n.startswith("nvim-"):
        return "/usr/share/vim/vimfiles"
    return None


def choose_build_style(pkg: SrcInfoPackage, urls: list[str]) -> str:
    """Heurística de build_style Void equivalente a la del transpilador Nix.

    Doctrina del repo: solo nombre ``-bin`` o formatos inequívocos
    (.deb/.rpm/.AppImage) son precompilados; tarballs/zip son FUENTE.
    """
    name = pkg.pkgname.lower()
    looks_prebuilt = (
        name.endswith("-bin")
        or any(u.lower().split("?")[0].split("#")[0].endswith(BIN_SUFFIXES)
               for u in urls))
    if looks_prebuilt:
        return ""  # fetch: sin build_style, do_install manual
    if not urls:
        return "meta"  # sin distfiles: paquete meta/solo dependencias
    eco = detect_ecosystem(pkg)
    return {
        "meson": "meson",
        "cmake": "cmake",
        "python-pep517": "python3-module",
        "python-legacy": "python3-module",
        "autotools": "gnu-makefile",
        "suckless": "gnu-makefile",
    }.get(eco, "gnu-makefile")


def _strip_name_prefix(u: str) -> str:
    """AUR permite 'nombre::url' — separa el prefijo de archivo."""
    return u.split("::", 1)[1] if "::" in u else u


def _void_distfile(raw: str) -> str:
    """Convierte fuente AUR a sintaxis distfiles de xbps-src.

    - Sin prefijo: URL tal cual.
    - Con prefijo ``nombre::url``: Void usa ``url>nombre.ext`` donde ext se
      hereda de la URL original (00-distfiles exige sufijo conocido).
    """
    u = _strip_name_prefix(raw)
    if "::" not in raw:
        return u
    name = raw.split("::", 1)[0]
    base = u.rsplit("/", 1)[-1].split("?")[0].split("#")[0]
    if "." in name:            # el nombre AUR ya trae sufijo válido
        return f"{u}>{name}"
    head, _, ext = base.partition(".")   # extensión desde la URL
    return f"{u}>{name}.{ext}" if ext else f"{u}>{name}.tar.gz"


def _split_vcs_fragment(url: str) -> tuple[str, str | None]:
    """Separa fragmento AUR de URL VCS: ``…git#tag=v1.2`` → (url, 'v1.2').

    Fragmentos soportados: ``tag=``, ``commit=``, ``branch=``; un fragmento
    plano se trata como tag/rev. La referencia se usa DIRECTA para el tarball
    de GitHub — determinista y sin ls-remote.
    """
    if "#" not in url:
        return url, None
    base, frag = url.split("#", 1)
    for prefix in ("tag=", "commit=", "branch="):
        if frag.startswith(prefix):
            ref = frag[len(prefix):]
            break
    else:
        ref = frag
    return base, (ref or None)


def _vcs_to_tarball(url: str) -> tuple[str | None, str | None]:
    """Convierte fuente VCS a tarball pineado cuando es posible.

    xbps-src no soporta ``git+https://`` en distfiles; para GitHub se usa el
    tarball del commit: si el AUR fija referencia vía ``#tag=/#commit=`` se usa
    esa directamente; si no, rev de HEAD vía ls-remote. Otros hosts → None.
    """
    url, ref = _split_vcs_fragment(url.rstrip("/"))
    m = re.match(r"(?:git\+)?(https?)://(?:www\.)?github\.com/([^/]+)/([^/.]+?)(?:\.git)?$", url)
    if not m:
        return None, None
    scheme, owner, repo = m.groups()
    if not ref:
        try:
            from src.nix.generator import pin_git_rev
            ref = pin_git_rev(f"https://github.com/{owner}/{repo}.git")
        except Exception:
            return None, None
    # sha de 40 hex → ruta directa; cualquier otra ref → tag
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        path = ref
    else:
        path = f"refs/tags/{ref}"
    return (f"{scheme}://github.com/{owner}/{repo}/archive/{path}.tar.gz", ref)


def _checksums(pkg: SrcInfoPackage, arch: str) -> list[str]:
    for algo in ("sha256", "sha512", "b2"):
        vals = pkg.sums_for(algo, arch) or pkg.sums_for(algo, "")
        if vals:
            return vals
    return []


@dataclass
class TemplateResult:
    pkgname: str
    version: str
    template_path: Path
    restricted: bool
    warnings: list[str]


def _compute_hashes(void_distfiles: list[str], timeout: int = 120) -> list[str]:
    """Descarga cada distfile y calcula su SHA-256 real (best-effort).

    Necesario para fuentes VCS (tarballs pineados) y .SRCINFO sin hashes:
    xbps-src NO acepta ``checksum=SKIP`` en builds normales. Retorna [] si
    alguna descarga falla (plantilla queda con SKIP + warning).
    """
    import hashlib as _hl

    import httpx
    hashes: list[str] = []
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          headers={"User-Agent": "aur2xbps-template/0.2"}) as cl:
            for entry in void_distfiles:
                url = entry.split(">", 1)[0]
                h = _hl.sha256()
                got = False
                with cl.stream("GET", url) as r:
                    r.raise_for_status()
                    for chunk in r.iter_bytes(65536):
                        h.update(chunk)
                        got = True
                if not got:
                    return []
                hashes.append(h.hexdigest())
    except Exception:
        return []
    return hashes


def generate_template(srcinfo: SrcInfo, out_dir: Path,
                      revision: int = 1) -> list[TemplateResult]:
    """Genera ``<out_dir>/<pkgname>/template`` por cada subpaquete.

    Retorna la lista de resultados (uno por subpaquete). Las plantillas son
    autónomas: xbps-src puede compilarlas sin aur2xbps instalado.
    """
    cfg = get_config()
    out_dir = Path(out_dir)
    results: list[TemplateResult] = []

    for pname, pkg in srcinfo.packages.items():
        arch = cfg.arch
        urls_raw = pkg.sources_for(arch) or pkg.sources_for("")
        # Alinear checksums con las URLs filtradas por ÍNDICE de fuente original
        # (las fuentes locales sin :// tienen checksum que debe descartarse también)
        src_sums = _checksums(pkg, arch)
        kept_idx: list[int] = []
        urls = []
        warnings: list[str] = []
        vcs_notes: list[str] = []
        for i, u in enumerate(urls_raw):
            raw = u
            u = _strip_name_prefix(u)
            if "://" not in u:
                continue  # fuentes locales no son distfiles
            kept_idx.append(i)
            if "git+" in u or u.endswith(".git"):
                tb, rev = _vcs_to_tarball(u)
                if tb:
                    urls.append(_void_distfile(f"{raw.split('::', 1)[0]}::{tb}"
                                               if "::" in raw else tb))
                    vcs_notes.append(f"VCS pineado a {rev[:7] if rev else '?'} vía tarball")
                else:
                    warnings.append(f"fuente VCS sin soporte tarball ({u}); añadir do_fetch manual")
            else:
                urls.append(_void_distfile(raw))
        # checksum alineado por índice; SKIP cuenta como ausente
        checksums = [src_sums[i] for i in kept_idx
                     if i < len(src_sums) and src_sums[i] != "SKIP"]
        needs_hash = len(checksums) != len(urls)
        # Variantes de microarquitectura (-baseline vs moderna): quedarse SOLO
        # con las baseline — máxima compatibilidad de CPU. Instalar ambas sobre
        # la misma ruta termina ejecutando la que exige AVX2 → SIGILL.
        if any("-baseline" in u for u in urls) and len(checksums) == len(urls):
            pairs = [(u, c) for u, c in zip(urls, checksums) if "-baseline" in u]
            if 0 < len(pairs) < len(urls):
                urls = [u for u, _ in pairs]
                checksums = [c for _, c in pairs]
                warnings.append(
                    "variantes de microarch: se empaqueta solo -baseline "
                    "(compatibilidad CPU máxima)")

        build_style = choose_build_style(pkg, urls)
        copy_target = None
        if not urls:
            pass  # meta real: sin distfiles
        elif (ct := _copy_install_target(pkg)) and build_style == "gnu-makefile":
            # familia sin Makefile (plugins/themes): instalación por copia
            copy_target = ct
            build_style = ""
        restricted, reason = is_restricted(srcinfo)
        if restricted and cfg.restricted_mode:
            warnings.append(f"restringido ({reason}): NO distribuir el binario")
        hash_source = "srcinfo" if checksums else None
        if needs_hash and not cfg.offline:
            computed = _compute_hashes(urls)
            if computed:
                checksums = computed
                needs_hash = False
                hash_source = "calculado"
        # Refresh opt-in de hashes stale (p.ej. google-chrome re-publica el
        # .deb bajo la misma URL y el .SRCINFO queda desfasado). NUNCA es
        # silencioso: exige AUR2XBPS_REFRESH_HASHES=1 y avisa en la plantilla.
        if (checksums and not cfg.offline
                and os.environ.get("AUR2XBPS_REFRESH_HASHES") == "1"):
            computed = _compute_hashes(urls)
            if computed and computed != checksums:
                checksums = computed
                hash_source = "recalculado (AUR2XBPS_REFRESH_HASHES)"
                warnings.append(
                    "hashes del .SRCINFO desfasados: recalculados por "
                    "AUR2XBPS_REFRESH_HASHES=1; revisar el origen")
        if needs_hash:
            warnings.append(
                f"{len(urls)} distfiles vs {len(checksums)} checksums: revisar antes de compilar")

        depends = map_deps(pkg.depends_for())
        mapped_makedepends = map_deps(pkg.makedepends_for())
        # En el chroot xbps-src solo se instalan hostmakedepends/makedepends
        # ANTES de compilar: los headers (*-devel) son dependencia de build.
        # Las libs compartidas runtime las añade xbps automáticamente vía
        # common/shlibs al crear el paquete, así que depends queda limpio.
        # OJO: comparar el nombre base SIN operador/versión ("foo-devel>=1")
        # también es header; endswith sobre el string completo falla ahí.
        import re as _re

        def _base(d: str) -> str:
            return _re.split(r"[<>=]+", d, maxsplit=1)[0]

        # xbps-src rechaza versiones en hostmakedepends/makedepends ("template
        # version is used always"): emitir siempre el nombre base pelado.
        make_libs = sorted({_base(d) for d in dict.fromkeys(mapped_makedepends + depends)
                            if _base(d).endswith("-devel")})
        make_tools = [d for d in dict.fromkeys(mapped_makedepends)
                      if not _base(d).endswith("-devel")]
        depends = [d for d in depends if not _base(d).endswith("-devel")]
        if make_libs and "pkg-config" not in make_tools:
            # Los builds que enlazan librerías invocan pkg-config para obtener
            # flags/libs; sin él los Makefiles caen a LDLIBS hardcodeados estilo
            # Debian (-ltinfo) que no existen en Void (tinfo va en libncursesw).
            make_tools.append("pkg-config")

        lines: list[str] = []
        lines.append(f"# Template file for '{pname}'")
        lines.append(f'pkgname="{pname}"')
        ver = pkg.pkgver
        # XBPS rechaza underscores en versiones; reemplazar con puntos
        if "_" in ver:
            ver = ver.replace("_", ".")
        lines.append(f'version="{ver}"')
        lines.append(f"revision={revision}")
        if build_style == "meta":
            # sintaxis Void vigente (build_style=meta está deprecado)
            lines.append("metapackage=yes")
        elif build_style:
            lines.append(f'build_style="{build_style}"')
        elif urls:
            lines.append("create_wrksrc=yes")
            # binarios precompilados: nopie evita el rechazo de ELFs non-PIE
            # (bun, etc.) y nostrip (convención xbps-src, NO dontStrip de Nix)
            # evita que el hook strip toque/corrompa binarios upstream.
            lines.append("nopie=yes")
            lines.append("nostrip=yes")
        # NOTA: no emitir 'archs=noarch' — xbps-src lo trata como restricción
        # y falla ("cannot be built for x86_64"); los paquetes arch-independientes
        # simplemente no restringen archs.
        if make_tools:
            lines.append(f'hostmakedepends="{" ".join(make_tools)}"')
        if make_libs:
            lines.append(f'makedepends="{" ".join(make_libs)}"')
        if depends:
            lines.append(f'depends="{" ".join(depends)}"')
        lines.append(f'short_desc="{_clean(pkg.pkgdesc, pname)}"')
        lines.append(f'maintainer="{MAINTAINER}"')
        # normalizar licencias: soporta "MIT", "(MIT)", "license = (GPL LGPL)"
        lic_parts = []
        for l in pkg.license:
            l = _clean(l.strip('()"\t '))
            if not l:
                continue
            lic_parts.extend(l.split())
        lic = " ".join(lic_parts) or "custom:unknown"
        lines.append(f'license="{_clean(lic, "custom:unknown")}"')
        for note in vcs_notes:
            lines.append(f"# {note}")
        if pkg.url:
            lines.append(f'homepage="{_clean(pkg.url)}"')
        if urls:
            lines.append(f'distfiles="{" ".join(urls)}"')
            # distfiles que NO son archivos comprimidos (metadatos de repo,
            # firmas .gpg/.sig, Release/Packages de apt, etc.): xbps-src no
            # sabe extraerlos → skip_extraction (se descargan y verifican
            # checksum igualmente).
            _ARCH_EXTS = (".zip", ".tgz", ".tar.gz", ".tar.bz2", ".tbz2",
                          ".tar.xz", ".txz", ".tar.zst", ".tzst", ".tar",
                          ".deb", ".rpm", ".appimage")
            _skip = []
            for u in urls:
                # nombre local: parte tras '>' o basename de la URL
                if ">" in u:
                    name = u.rsplit(">", 1)[-1]
                else:
                    name = u.split("://", 1)[-1].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
                if not name.lower().endswith(_ARCH_EXTS):
                    _skip.append(name)
            if _skip:
                lines.append(f'skip_extraction="{" ".join(_skip)}"')
            if checksums:
                if hash_source in ("calculado", "recalculado (AUR2XBPS_REFRESH_HASHES)"):
                    lines.append("# hashes SHA-256 calculados al generar la plantilla")
                lines.append(f'checksum="{" ".join(checksums)}"')
            else:
                lines.append('checksum="SKIP"')
        else:
            lines.append("# sin distfiles: paquete meta o solo dependencias")

        # ---- cuerpo según estilo ----
        if copy_target:
            # plugins/temas sin sistema de build: copiar árbol al share dir
            lines += [
                "",
                "do_install() {",
                f"\tvmkdir {copy_target}",
                f'\tvcopy . {copy_target}',
                "}",
            ]
        elif build_style == "":
            # -bin precompilado: instalación genérica conservadora
            lines += [
                "",
                "_doinstall() {",
                "\t# Instalación genérica de artefactos precompilados.",
                "\t# Ajustar rutas al layout real del tarball/deb.",
                "\t# layout Debian (.deb): fusionar usr/ y opt/ en DESTDIR",
                '\tif [ -d usr ]; then mkdir -p "${DESTDIR}/usr"; cp -a usr/. "${DESTDIR}/usr/"; fi',
                '\tif [ -d opt ]; then mkdir -p "${DESTDIR}/opt"; cp -a opt/. "${DESTDIR}/opt/"; fi',
                "\t# FHS/pkglint: los ELF no pueden vivir en /usr/share (apps Debian",
                "\t# como spotify/vscode los meten ahí). Reubicar el directorio a",
                "\t# /usr/lib y reescribir las referencias de los lanzadores.",
                '\t_MAGIC=$(printf "\\177ELF")',
                '\tfor _d in "${DESTDIR}/usr/share"/*; do',
                '\t\t[ -d "$_d" ] || continue',
                '\t\t_haself=0',
                '\t\tfor f in $(find "$_d" -type f); do',
                '\t\t\thead -c 4 "$f" 2>/dev/null | grep -q "$_MAGIC" && { _haself=1; break; }',
                '\t\tdone',
                '\t\t[ "$_haself" = 1 ] || continue',
                '\t\t_rel=${_d#"${DESTDIR}/usr/share/"}',
                '\t\tmkdir -p "${DESTDIR}/usr/lib"',
                '\t\tmv "$_d" "${DESTDIR}/usr/lib/${_rel}"',
                "\t\t# lanzadores anidados <app>/bin/* (convención Debian vía",
                "\t\t# postinst, que aquí no existe) → enlazarlos en /usr/bin",
                '\t\tif [ -d "${DESTDIR}/usr/lib/${_rel}/bin" ]; then',
                '\t\t\tmkdir -p "${DESTDIR}/usr/bin"',
                '\t\t\tfor _b in "${DESTDIR}/usr/lib/${_rel}/bin"/*; do',
                '\t\t\t\t[ -f "$_b" ] || continue',
                '\t\t\t\t_bn=$(basename "$_b")',
                '\t\t\t\tln -sfn "/usr/lib/${_rel}/bin/${_bn}" "${DESTDIR}/usr/bin/${_bn}"',
                "\t\t\tdone",
                "\t\tfi",
                "\t\t# re-enlazar symlinks de usr/bin que apuntaban a share/",
                '\t\tfor _l in "${DESTDIR}/usr/bin"/*; do',
                '\t\t\t[ -L "$_l" ] || continue',
                '\t\t\t_lt=$(readlink "$_l")',
                '\t\t\tcase "$_lt" in',
                '\t\t\t\t*/share/${_rel}*)',
                '\t\t\t\t\t_nt=$(printf "%s" "$_lt" | sed -e "s|\\.\\./share/${_rel}|../lib/${_rel}|g" -e "s|/usr/share/${_rel}|/usr/lib/${_rel}|g")',
                '\t\t\t\t\tln -sfn "$_nt" "$_l"',
                '\t\t\t\t\t;;',
                '\t\t\tesac',
                '\t\tdone',
                '\t\tfor b in "${DESTDIR}/usr/bin"/*; do',
                '\t\t\t[ -f "$b" ] || continue',
                '\t\t\thead -c 4 "$b" 2>/dev/null | grep -q "$_MAGIC" && continue',
                '\t\t\tsed -i "s|/usr/share/${_rel}|/usr/lib/${_rel}|g" "$b"',
                '\t\tdone',
                '\tdone',
                '\tfor f in *.AppImage; do',
                "\t\t[ -e \"$f\" ] || continue",
                "\t\tvinstall \"$f\" 755 usr/bin \"${f%.AppImage}\"",
                "\tdone",
                "\t# Apps tipo Chromium/Electron (zip plano o anidado): el binario necesita",
                "\t# sus recursos (locales/, *.pak, icudtl.dat) EN EL MISMO directorio;",
                "\t# instalar árbol completo en /usr/lib/<pkg> y enlazar /usr/bin.",
                '\t_main="${pkgname%-bin}"',
                '\tif [ ! -f "$_main" ] && [ -f "$_main/$_main" ]; then',
                '\t\tcd "$_main"',
                "\tfi",
                '\tif [ -f "$_main" ] && { [ -d locales ] || [ -n "$(ls *.pak 2>/dev/null)" ]; }; then',
                '\t\tvmkdir "usr/lib/${pkgname}"',
                '\t\tvcopy . "usr/lib/${pkgname}"',
                '\t\tmkdir -p "${DESTDIR}/usr/bin"',
                '\t\tln -sf "../lib/${pkgname}/${_main}" "${DESTDIR}/usr/bin/${_main}"',
                "\t\treturn",
                "\tfi",
                "\t# fallback: ejecutables ELF sueltos en la raíz del tarball",
                "\tfor f in *; do",
                '\t\t[ -f "$f" ] || continue',
                '\t\tmagic=$(head -c 4 "$f" | od -An -tx1 | tr -d " \\n")',
                '\t\tcase "$magic" in',
                '\t\t\t7f454c46*) ;;',
                "\t\t\t*) continue ;;",
                "\t\tesac",
                '\t\t# normalizar nombres tipo release Go: gdu_linux_amd64 -> gdu',
                "\t\t# vinstall es alias con ';': usar if/else, nunca '&& x || y'",
                '\t\tcase "$f" in',
                '\t\t\t*_linux_*|*_amd64*|*_x86_64*|*-linux-*|*-linux-amd64)',
                '\t\t\tclean=$(echo "$f" | sed "s/_linux_.*$//; s/[._-]amd64.*$//; s/[._-]x86_64.*$//; s/-linux-amd64$//")',
                '\t\t\tif [ -n "$clean" ] && [ "$clean" != "$f" ]; then',
                '\t\t\t\tvinstall "$f" 755 usr/bin "$clean"',
                '\t\t\telse',
                '\t\t\t\tvinstall "$f" 755 usr/bin',
                "\t\t\tfi",
                "\t\t\t;;",
                "\t\t\t*) vinstall \"$f\" 755 usr/bin ;;",
                "\t\tesac",
                "\tdone",
                "\t# último recurso: descubrir ELFs recursivamente cuando el layout",
                "\t# es <dir-cualquiera>/<binario> y ninguna estrategia previa instaló",
                "\t# nada. La guarda es 'cero archivos regulares en DESTDIR' porque los",
                '\t# hooks de xbps-src pre-crean $DESTDIR/usr/lib vacío.',
                '\tif [ -z "$(find "${DESTDIR}" -type f 2>/dev/null | head -n 1)" ]; then',
                "\t\t# ¿bundle de app (Electron/Chromium)? Si junto a los ELFs hay",
                "\t\t# ficheros NO-ELF (icudtl.dat, *.pak, resources/), el árbol entero",
                "\t\t# es la app: copiarlo completo a /usr/lib/<pkg> y enlazar cada",
                "\t\t# ejecutable de la raíz. Si no, instalar ELFs uno a uno.",
                "\t\t_first=\"\"",
                "\t\tfor f in $(find . -type f | sort); do",
                '\t\t\tmagic=$(head -c 4 "$f" | od -An -tx1 | tr -d " \\n")',
                "\t\t\tcase \"$magic\" in 7f454c46*) _first=\"$f\"; break ;; esac",
                "\t\tdone",
                '\t\t[ -n "$_first" ] || return',
                '\t\t_top=$(printf "%s" "$_first" | sed "s|^\\./||; s|/.*||")',
                "\t\t_bundle=0",
                '\t\tif [ -n "$_top" ] && [ -d "$_top" ]; then',
                '\t\t\tfor f in $(find "$_top" -type f); do',
                '\t\t\t\tmagic=$(head -c 4 "$f" | od -An -tx1 | tr -d " \\n")',
                "\t\t\t\tcase \"$magic\" in 7f454c46*) ;; *) _bundle=1; break ;; esac",
                "\t\t\tdone",
                "\t\tfi",
                '\t\tmkdir -p "${DESTDIR}/usr/bin"',
                '\t\tif [ "$_bundle" = 1 ]; then',
                '\t\t\tcp -a "$_top"/. "${DESTDIR}/usr/lib/${pkgname}/"',
                '\t\t\tfor f in "${DESTDIR}/usr/lib/${pkgname}"/*; do',
                '\t\t\t\t[ -f "$f" ] || continue',
                '\t\t\t\t[ -x "$f" ] || continue',
                '\t\t\t\tb=$(basename "$f")',
                '\t\t\t\tcase "$b" in',
                "\t\t\t\t\t*.so|*.so.*|*.pak|*.dat|*.bin|*.json|*.node|chrome-sandbox|chrome_crashpad_handler) continue ;;",
                "\t\t\t\tesac",
                '\t\t\t\tln -sf "../lib/${pkgname}/${b}" "${DESTDIR}/usr/bin/${b}"',
                "\t\t\t\t# alias en minúsculas: los tarballs upstream suelen traer",
                "\t\t\t\t# capitalización tipo 'Postman' y el usuario espera 'postman'",
                '\t\t\t\t_lc=$(printf "%s" "$b" | tr "A-Z" "a-z")',
                '\t\t\t\tif [ "$_lc" != "$b" ] && [ ! -e "${DESTDIR}/usr/bin/${_lc}" ]; then',
                '\t\t\t\t\tln -sf "../lib/${pkgname}/${b}" "${DESTDIR}/usr/bin/${_lc}"',
                "\t\t\t\tfi",
                "\t\t\tdone",
                "\t\telse",
                '\t\tvmkdir "usr/lib/${pkgname}"',
                '\t\tfor f in $(find . -type f | sort); do',
                '\t\t\tmagic=$(head -c 4 "$f" | od -An -tx1 | tr -d " \\n")',
                '\t\t\tcase "$magic" in',
                '\t\t\t\t7f454c46*) ;;',
                '\t\t\t\t*) continue ;;',
                '\t\t\tesac',
                '\t\t\tb=$(basename "$f")',
                '\t\t\tcase "$b" in',
                '\t\t\t\t*.so|*.so.*) vinstall "$f" 755 "usr/lib/${pkgname}"; continue ;;',
                '\t\t\tesac',
                '\t\t\tcase "$f" in',
                '\t\t\t\t*/bin/*) vinstall "$f" 755 usr/bin "$b" ;;',
                '\t\t\t\t*)',
                '\t\t\t\t\tvinstall "$f" 755 "usr/lib/${pkgname}"',
                '\t\t\t\t\tmkdir -p "${DESTDIR}/usr/bin"',
                '\t\t\t\t\tln -sf "../lib/${pkgname}/${b}" "${DESTDIR}/usr/bin/${b}"',
                '\t\t\t\t\t;;',
                '\t\t\tesac',
                '\t\tdone',
                '\t\tfor l in $(find . -type l); do',
                '\t\t\ttb=$(basename "$(readlink "$l")")',
                '\t\t\tb=$(basename "$l")',
                '\t\tif [ -e "${DESTDIR}/usr/bin/$tb" ] && [ ! -e "${DESTDIR}/usr/bin/$b" ]; then',
                '\t\t\tln -sf "$tb" "${DESTDIR}/usr/bin/$b"',
                '\t\tfi',
                '\tdone',
                "\t\tfi",
                "\tfi",
                "\t# Compat Debian→Void: apps que enlazan libcurl-gnutls.so.4",
                "\t# (Debian/Arch) corren con la libcurl de Void (OpenSSL); crear",
                '\t# el shim junto a los ELFs de la app solo si se referencia.',
                '\tfor _d in "${DESTDIR}"/opt/* "${DESTDIR}"/usr/share/* "${DESTDIR}"/usr/lib/*; do',
                '\t\t[ -d "$_d" ] || continue',
                '\t\tgrep -rls "libcurl-gnutls" "$_d" >/dev/null 2>&1 || continue',
                '\t\t[ -e "$_d/libcurl-gnutls.so.4" ] || ln -s /usr/lib/libcurl.so.4 "$_d/libcurl-gnutls.so.4"',
                "\tdone",
                "}",
                "",
                "do_install() {",
                "\t_doinstall",
                "}",
            ]
        elif build_style == "python3-module":
            lines += [
                "",
                "# módulo python: python3-module usa pip/build automático",
                "# make_check=no si la suite requiere deps no empaquetadas",
                "make_check=no",
            ]
        elif build_style == "gnu-makefile":
            lines += [
                "",
                "# make_install_args puede necesitar PREFIX=/usr",
                'make_install_args="PREFIX=/usr"',
                'make_build_args="CC=${CC} CFLAGS=${CFLAGS} LDFLAGS=${LDFLAGS}"',
            ]
        elif build_style == "meta":
            lines.append("# paquete meta: solo depends, sin build")

        if restricted:
            lines = ([f"# restricted=yes — {reason}",
                      f"# NO redistribuir el binario resultante."]
                     + lines)

        tdir = out_dir / HEADER_RE.sub("_", pname)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "template").write_text("\n".join(lines) + "\n", encoding="utf-8")
        results.append(TemplateResult(pkgname=pname, version=ver,
                                      template_path=tdir / "template",
                                      restricted=restricted,
                                      warnings=warnings))
    return results


def sync_to_void_srcpkgs(out_dir: Path) -> int:
    """Copia las plantillas generadas a <void_packages_dir>/srcpkgs/.

    No sobrescribe plantillas existentes distintas (seguridad): solo añade.
    Retorna número de plantillas copiadas.
    """
    import shutil
    cfg = get_config()
    dst_root = cfg.void_packages_dir / "srcpkgs"
    copied = 0
    for tdir in Path(out_dir).iterdir():
        if not (tdir / "template").is_file():
            continue
        dst = dst_root / tdir.name
        if dst.exists():
            continue
        shutil.copytree(tdir, dst)
        copied += 1
    return copied
