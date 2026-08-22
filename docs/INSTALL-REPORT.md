# Reporte de instalación — aur2xbps (2026-08-22)

Instalación en Void Linux x86_64 (`doas` disponible, sin Nix → modo solo-plantillas).
Logs completos en `/tmp/opencode/aur2xbps-install/` (`install.log`, `reinstall.log`, `build*.err/json`).

## Resumen ejecutivo

- **5 bugs detectados y corregidos** (3 en `install.sh`, 2 en `src/void/template.py`).
- Suite de tests: **162/162 pasan** (antes de los fixes: 8 fallos).
- Validación end-to-end real: build de `cbonsai` → `.xbps` generado → dependencias auto-resueltas vía shlibs → instalado en chroot → smoke OK → repo firmado.

## Bugs encontrados y arreglados

### BUG 1 — Crítico: expansión de llaves bajo dash (`install.sh`)
- **Síntoma**: se creó un directorio literal `{sources,derivations,srcpkgs,fake-root}`; los 4 directorios reales del workspace no existían.
- **Causa raíz**: shebang `#!/bin/sh` → **dash** en Void. POSIX sh no soporta brace expansion `{a,b}`.
- **Fix**: `mkdir -p` con rutas explícitas. Estado reparado a mano (borrado el dir literal, creados los reales).

### BUG 2 — Medio: masterdir mal resuelto (`install.sh`)
- **Síntoma**: `config.toml` apuntaba a `<data_dir>/void/masterdir`, que nunca existía; xbps-src moderno crea `masterdir-x86_64` dentro del árbol void-packages.
- **Consecuencia**: cada re-run intentaba re-bootstrapear; el smoke chroot habría fallado.
- **Fix**: `find_masterdir()` detecta `masterdir[-$ARCH]` dentro de VP; symlink de compatibilidad `<data_dir>/void/masterdir` → real. Re-run verificado idempotente.

### BUG 3 — Crítico silencioso: `xbps-install` sin `-y` → falso éxito (`install.sh`)
- **Síntoma**: el log decía `ok: dependencias base`, pero `jq patchelf zstd binutils python3-httpx python3-yaml` **no se instalaron**. 8 tests fallaban (`readelf` ausente, `httpx` no importable).
- **Causa raíz**: con stdin en EOF, xbps-install imprime `Aborting!` en el prompt `Do you want to continue?` y retorna **exit 0**.
- **Fix**: `xbps-install -Sy` siempre. Dependencias instaladas a mano después; suite verde.

### BUG 4 — Medio: headers `-devel` emitidos como `depends=` runtime (`src/void/template.py`)
- **Síntoma**: build de cbonsai falló: `fatal error: curses.h: No such file or directory`.
- **Causa raíz**: el mapeo ponía `ncurses-devel` en `depends`; el chroot solo instala hostmakedepends/makedepends antes de compilar.
- **Fix**: split en el generador — `-devel` → `makedepends`, herramientas → `hostmakedepends`, runtime limpio (las libs las añade xbps vía shlibs).

### BUG 5 — Medio: falta `pkg-config` al enlazar librerías (`src/void/template.py`)
- **Síntoma**: tras el fix 4, el enlace falló: `cannot find -ltinfo`.
- **Causa raíz**: el Makefile hace `$(shell pkg-config --libs ncursesw panelw || echo "-lncursesw -ltinfo -lpanelw")`; sin pkg-config cae al fallback estilo Debian, pero Void fusiona tinfo dentro de libncursesw. La plantilla oficial de Void declara `hostmakedepends="pkg-config scdoc"`.
- **Fix**: si hay libs de build (`make_libs`), añadir `pkg-config` a hostmakedepends automáticamente.

### Observación menor (documentada, no corregida)
- `depends="gcc"` del `.SRCINFO` del AUR pasa tal cual a runtime (`gcc>=0` en run_depends). Inocuo pero innecesario para la mayoría de paquetes; es parte del mapeo best-effort documentado.

## Archivos modificados

| Archivo | Cambio |
|---|---|
| `install.sh` | mkdir POSIX-safe; detección/symlink de masterdir; `-Sy` en xbps_sync |
| `src/void/template.py` | split devel/tools/runtime deps; pkg-config automático al enlazar |
| `AGENTS.md` | 4 gotchas nuevos (dash, xbps -y, -devel build-time, pkg-config) |

## Validación final

| Prueba | Resultado |
|---|---|
| `pytest tests/ --timeout=120` | **162/162 ✓** (~10 s) |
| `aur2xbps query cbonsai` | JSON válido ✓ |
| `aur2xbps resolve cbonsai` | deps mapeadas a Void ✓ |
| `aur2xbps template cbonsai` | plantilla estándar con makedepends correctos ✓ |
| `aur2xbps build cbonsai --engine xbps-src` | `cbonsai-1.4.2_1.x86_64.xbps` ✓ |
| run_depends auto-shlibs | `glibc>=2.41_1`, `ncurses-libs>=5.8_1` ✓ |
| Instalación + smoke en chroot | `Usage: cbonsai [OPTION]...` sin errores de cargador ✓ |
| `aur2xbps repo --sign` | 1 paquete indexado + firmado ✓ |

## Estado post-instalación

- CLI en `~/.local/bin/aur2xbps` (en PATH) ✓
- Workspace `~/.local/share/aur2xbps` completo (sources/derivations/srcpkgs/fake-root/repo) ✓
- void-packages clonado + binary-bootstrap ✓
- Claves RSA en `~/.config/aur2xbps/keys` (600) ✓ · config.toml ✓

## Pendientes / recomendaciones

1. **Nix no instalado** (opcional): para el motor hermético completo, re-ejecutar `./install.sh` y aceptar el prompt, o `curl -sSL https://install.determinate.systems/nix | sh -s -- install`. Sin Nix todo funciona vía xbps-src (sin aislamiento de red TIAR durante build ni hash-lock Nix).
2. **Servicio del repo no arrancado**: `runsvdir ~/.config/aur2xbps/runit &` o directo `~/.local/bin/aur2xbps-serve-repo &` (sirve `~/.local/share/aur2xbps/repo` en :8080).
3. Los cambios quedan **sin commitear** — revisar con `git diff` antes de hacer commit.
