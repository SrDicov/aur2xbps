# AGENTS.md — aur2xbps

Instrucciones para agentes IA y desarrolladores que trabajen en este repositorio.

## Project
Bridge AUR (Arch) → Void Linux (XBPS) usando Nix como motor hermético. Pipeline completo: RPC AUR → filtro seguridad → transpile flakes → build sandbox Nix → empaquetado XBPS determinista firmado → validación chroot. Fuente de verdad ejecutable: `src/`, `tests/`, `scripts/`, `docs/`.

## Configuración de entorno
- **Workspace** (cachés, fuentes, derivaciones, repo): variable `AUR2XBPS_ROOT` (default `~/.local/share/aur2xbps`; alternativamente archivo `~/.config/aur2xbps/root` con la ruta). Resolución centralizada en `src/common/paths.py` — NUNCA hardcodear rutas absolutas personales.
- **Claves de firma**: FUERA del árbol, en `AUR2XBPS_KEYDIR` (default `/etc/xbps/keys/aur2xbps`). Nunca commitear `.pem`/`.key`/`.sig2`.
- **Requisitos**: Python ≥3.11, Nix ≥2.35 (flakes + sandbox), xbps estáticos, patchelf, void-packages bootstrapeado (submódulo `common/void-packages`; fuente de `common/shlibs`). Elevador de privilegios: sudo o doas (Void) — override `AUR2XBPS_PRIV`; guía operativa: `docs/USAGE.md`.

## Código — entrypoints
- `src/common/config.py` — configuración central (env AUR2XBPS_* > ~/.config/aur2xbps/config.toml > /etc/aur2xbps/config.toml > defaults XDG). Claves: data_dir/cache_dir/repo_dir/keys_dir/masterdir/void_packages_dir/host/port/arch/python_version/signing_key/log_level/restricted_mode/offline. `dynamic_linker()`/`nix_system()` para multi-arch.
- `src/common/paths.py` — capa fina sobre config (compat imports).
- `src/common/tools.py` — resolución de binarios xbps sin rutas fijas (`find_xbps_tool`, `has_nix`).
- `src/cli.py` — CLI estable para humanos y helpers: `query|resolve|template|build|repo`, JSON por stdout. Entry point `[project.scripts] aur2xbps`.
- `src/void/mapping.py` — mapeo Arch→Void (manual → prefijos → directo; descarta lib32-*).
- `src/void/template.py` — generador plantillas xbps-src estándar (build_style auto, VCS→tarball pineado, archs=noarch, restricted=yes). Contrato vouru: compilan con ./xbps-src puro.
- `src/aur/parser.py` — parser `.SRCINFO`: split packages, arch-qualified (`depends_x86_64`, `source_i686`, `sha256sums_aarch64`, `b2sums_*`), validpgpkeys, noextract. Nunca evalúa `PKGBUILD`.
- `src/aur/client.py` — RPC v5 httpx + SQLite cache + batch ≤200/≤4443B + rate 4000/día persistente + ETag/304 + backoff + modo `offline=True`.
- `src/aur/pipeline.py` — `prepare_package(pkgname, offline=False)`: RPC → clone → filtro Atomic → licencias → staleness.
- `src/aur/security.py` — filtro Atomic Arch (maliciosos exactos por nombre/dependencia + npm/bun sin hash salvo allowlist) + `validate_license` + `is_restricted` (marca no-redistribuibles).
- `src/nix/generator.py` — `transpile(srcinfo, out_dir)` → flake.nix + flake.lock + lint patchelf. Una derivación por subpaquete (attrs quoted). Detección ecosistema: meson/cmake/python-pep517/python-legacy(experimental)/autotools/suckless/VCS(fetchgit rev pinneado + fetchSubmodules). Mapeo Arch→Nix en capas: manual → repology JSON → prefijos → raw. Header `restricted=yes` para licencias propietarias.
- `src/nix/patchelf.py` — linter anti-combinado + orden rpath→interpreter separados + `validate_elf`.
- `src/xbps/builder.py` — stage determinista (touch @0 + chown 0:0) + `create_xbps` + `stage_from_nix_result` + `rindex_add`.
- `src/xbps/determinize.py` — normaliza tar interno (uname/gname→NUL, checksum recalculado, zstd -T1). Obligatorio post-créate para reproducibilidad cross-host.
- `src/xbps/shlibs.py` — `ShlibsDB` desde `common/shlibs` real. `soname_to_dep()` emite `pkg>=ver`.
- `src/xbps/pipeline.py` — `full_pipeline(...)`: stage → patchelf Void selectivo → shebangs Nix→FHS → console_scripts → TRH normalize → run_depends auto → create determinista + determinize + firma → chroot install → smoke root/no-root. `build_python_in_void()`: wheel con python3 de Void.
- `src/xbps/signing.py` — par RSA-4096 + `sign_repo`. CI genera clave EFÍMERA en runner.

## Tests y CI
```bash
pytest tests/ -q --timeout=120          # suite completa
./scripts/ci-local.sh                   # CI local completo (~4 min)
./scripts/ci-local.sh --notify          # con notificación de fallos
SMOKE_TARGET=<pkg-bin> ./scripts/ci-local.sh   # activa smoke chroot (sin env se omite)
python3 -m src.nix.patchelf             # linter de flakes generados (standalone)
```
- Imports SIEMPRE como `from src.…`: el `conftest.py` en la raíz inserta el repo en `sys.path`. Si corres pytest desde otro directorio, rompe.
- Tests marcados `@pytest.mark.network` acceden a AUR real; el resto no usa red. En CI público se ejecutan con `-m "not network"`.
- Fixtures: `tests/fixtures/srcinfo/` (56 `.SRCINFO` reales + maliciosos sintéticos), `tests/fixtures/flakes/`.
- CI GitHub `.github/workflows/ci.yml`: jobs lint/trh/tiar, sin secretos (clave RSA efímera generada en runtime; workspace en `/tmp` del runner).

## KPIs
| KPI | Verificación | Umbral |
|-----|--------------|--------|
| TRH cross-host | host glibc vs contenedor musl, post-determinize | SHA-256 idénticos |
| EEL | smoke root + no-root | exit 0 sin errores cargador |
| RDC | run_depends XBPS vs depends Arch | >0% reducción |
| TIAR | canary curl en sandbox Nix | 0 bytes egress (exit 6 DNS) |

## Gotchas críticos
- **dash = /bin/sh en Void**: POSIX estricto, SIN expansión de llaves `{a,b}` ni bashismos en scripts con shebang `#!/bin/sh`.
- **xbps-install sin `-y`**: con stdin en EOF imprime `Aborting!` y retorna exit 0 → éxito falso. Siempre `-Sy`.
- **Stamps de xbps-src**: dobuild toca `*_build_done` ANTES de instalar; un fallo posterior (pkglint) lo deja puesto y TODOS los retries saltan fetch/extract/install en silencio (incluso con `-f`, que solo re-ejecuta el target `build`). Siempre `./xbps-src clean <pkg>` antes de reintentar.
- **`nostrip=yes`, NO `dontStrip=true`**: dontStrip es convención Nix; xbps-src la ignora y el hook strip corrompe/rechaza binarios upstream.
- **`provides` exige revisión**: xbps 0.60 rechaza `foo-1.0` (los ejemplos de su propia help fallan); usar `foo-1.0_1`.
- **`xbps-rindex -f -a`**: sin `-f` conserva silenciosamente la entrada previa si el pkgver ya estaba indexado → repodata stale tras re-empaquetar.
- **Headers `-devel` son build-time**: en el chroot solo se instalan hostmakedepends/makedepends antes de compilar; las libs runtime las resuelve xbps vía shlibs. Nunca emitir `-devel` en `depends=`. Sin versión en makedepends ("template version is used always").
- **pkg-config obligatorio al enlazar libs**: sin él los Makefiles caen a LDLIBS estilo Debian (`-ltinfo`) inexistentes en Void (tinfo va fusionado en libncursesw).
- **patchelf orden mandatorio**: rpath primero, interpreter después, invocaciones separadas. Combinados → ELF corrupto. Linter falla si detecta ambos en misma línea. `chmod +w` antes/después (Nix store read-only).
- **patchelf sobre binarios Go es destructivo**: saltarse ELFs que ya no referencian /nix/store (check readelf antes de parchear).
- **strip de stdenv corrompe binarios Go/precompilados**: `dontStrip = true` en template BIN; **autoPatchelfHook corrompe binarios Go** — no usarlo.
- **`.SRCINFO` stale**: validar tag git vs pkgver; abortar si diverge.
- **AUR RPC v5**: URI ≤4443 bytes, batch ≤200, rate 4000/día/IP. Cache SQLite + offline mode.
- **Atomic Arch**: bloquear solo maliciosos exactos + npm/bun install sin hash (salvo allowlist JS). No bloquear todo npm/bun.
- **buildFHSEnv**: solo `-bin`. PKG_CONFIG_PATH rompe aislamiento.
- **TRH cross-host**: libarchive escribe uname/gname como STRING cuando getpwuid resuelve → hash varía entre hosts. `determinize_xbps()` obligatorio.
- **TIAR**: tcpdump global captura ruido del host. Prueba válida = canary curl falla DNS (exit 6).
- **Electron `$ORIGIN`**: RUNPATH = `$ORIGIN:/usr/lib:/usr/lib64`.
- **Chrome-sandbox setuid**: `dpkg-deb --fsys-tarfile $src | tar --no-same-owner --no-same-permissions`.
- **xbps-uchroot exige root**: envolver con sudo; montar dev/proc/sys para GUI.
- **Shebangs Nix→FHS**: `_fix_shebangs()` reescribe `#!/nix/store/...` → FHS. patchelf solo ELF.
- **shlibs dep format**: emitir `pkg>=ver` (NO tupla literal que xbps trata como versión exacta).
- **Python cross-versión**: wheel con python sandbox ≠ python destino. Usar `build_python_in_void()` (python3 del masterdir Void).
- **dpkg-deb en zip**: `case "$src" in *.deb)... *.zip) unzip...` en unpackPhase.
- **`file` command**: necesario en nativeBuildInputs para detección ELF.
- **Secretos en argv**: comandos con `--privkey`/`--sign-key` SIEMPRE vía `builder._run` (redacta en logs/excepciones); nunca `subprocess.run` directo ni print del cmd crudo.
- **Smoke de paquetes**: extraer el `.xbps` a tmp y ejecutar resolviendo symlinks (`/usr/bin/x → ../lib/app/x` es patrón habitual post-reubicación); LD_LIBRARY_PATH al lib propio del paquete.

## Flujo operativo (CLI)
```bash
aur2xbps query <pkg>              # metadatos JSON
aur2xbps resolve <pkg>            # deps mapeadas a Void JSON
aur2xbps template <pkg>           # srcpkgs/<pkg>/template → vouru/xbps-src
aur2xbps build <pkg> [--engine auto|nix|xbps-src]
aur2xbps repo --sign
```

## Flujo interno
1. **Extraer**: `prepare_package('yay-bin')` → RPC + clone + parse + Atomic + licencias.
2. **Derivar**: `transpile(si, Path(...))` → flake.nix + lock + lint. Build: `nix build .#packages.x86_64-linux.<attr> --option sandbox true`.
3. **Empaquetar**: `full_pipeline(...)` → stage → patchelf selectivo → shlibs auto → create_signed → chroot → smoke.
4. **Lote**: `python3 scripts/batch-validate.py [pkg...]` — registra `<workspace>/batch-results.json`.
5. **VCS refresh**: `./scripts/vcs-refresh.sh [--offline|--force|--submodules]`.
6. **Lote masivo**: `python3 scripts/mass-validate.py [--count-bin N --count-src M] [--engine both|nix|xbps-src] [--seed S]` — muestreo reproducible desde packages.gz, smoke funcional extrayendo el .xbps (resuelve symlinks `/usr/bin→../lib`), reanudable vía `mass-results.json`, reporte markdown con causas raíz agrupadas.

## Convenciones repo
- **Rutas**: SIEMPRE vía `src/common/config.py` (Python) o `${AUR2XBPS_*}` en bash. Prohibido rutas absolutas personales, usuarios fijos, IPs o puertos en código.
- **Arquitectura**: nunca hardcodear x86_64; usar config.arch/dynamic_linker()/uname -m.
- **Paquetes**: cero paquetes fijos en scripts/CI — argumentos CLI o env con skip elegante.
- **Nix opcional**: todo flujo debe funcionar sin Nix (motor xbps-src); detectar con tools.has_nix().
- **Split packages**: una derivación por pkgname; campos arch-qualified priorizan.
- **Hash SKIP**: auto-corregido en build (Nix reporta hash real).
- **Atributos Nix quoted**: `"{pkgname}"` necesario para guiones.
- **Deps híbridas**: nominal + SONAME real desde shlibs.
- **_is_bin**: solo nombre `-bin` o formatos inequívocos (.deb/.rpm/.AppImage); tarballs son FUENTE.
- **restricted=yes**: licencias custom/commercial/EULA o binarios propietarios upstream → NO distribuir el .xbps.
- **Licencias**: repo GPL-3.0-or-later; cabeceras SPDX obligatorias en archivos de código (no en fixtures de datos).

## Integración vouru
Helper bash que envuelve xbps-src en `~/.local/share/pkgs/void-packages` (config ~/.voururc). Consume plantillas ESTÁNDAR de `srcpkgs/<pkg>/template`; instala desde hostdir/binpkgs. aur2xbps genera las plantillas (`aur2xbps template`) y vouru compila/instala. La plantilla debe ser suficiente SIN aur2xbps en el medio.

## Servicio del repo (runit, NO systemd)
Void usa runit. El instalador crea `/etc/runit/runsvdir/default/aur2xbps-repo/run` (root) o `~/.config/aur2xbps/runit/` (usuario con `runsvdir`). PROHIBIDO reintroducir unidades systemd: mentalidad Arch no aplica en Void. Control: `sv up|down|status aur2xbps-repo`.

## Limitaciones conocidas
- python-legacy setup.py con cmdclass custom: experimental.
- `build_python_in_void()` requiere deps build instaladas en el masterdir Void.
- Cross-compile ARM (boringssl-git) desde host x86_64 único: no soportado.
- Repology API restringe bots: oráculo offline cubre caso básico sin nombres alternativos automáticos.

## Documentación externa
- **DeepWiki** (MCP `deepwiki` en opencode): repo indexado como `SrDicov/aur2xbps`. Para arquitectura/pipeline usar `ask_question(repoName="SrDicov/aur2xbps", ...)` o `read_wiki_structure`; web: https://deepwiki.com/SrDicov/aur2xbps
