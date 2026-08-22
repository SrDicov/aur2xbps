# Guía de uso — aur2xbps

Herramienta que transpila paquetes del AUR (Arch) a `.xbps` de Void Linux usando
Nix como motor hermético — o `xbps-src` puro si Nix no está instalado.

## Instalación en Void Linux

```bash
git clone <repo> aur2xbps && cd aur2xbps
./install.sh
```

El instalador:
1. Verifica `ID=void` en `/etc/os-release`.
2. Instala dependencias con `xbps-install` (`git python3 jq bubblewrap patchelf curl zstd tar openssl xbps xtools`). Usa `doas`/`sudo` solo si es necesario.
3. Crea el workspace en `$AUR2XBPS_DATA_DIR` (default `~/.local/share/aur2xbps`) y clona void-packages (depth 1) + `binary-bootstrap`.
4. **Nix es opcional**: si falta, avisa, ofrece instalarlo, o continúa en modo solo-plantillas (`xbps-src` como motor).
5. Genera claves RSA en `$XDG_CONFIG_HOME/aur2xbps/keys` (privkey 600).
6. Escribe `~/.config/aur2xbps/config.toml` con defaults portables.
7. Instala servicio **runit**: como root en `/etc/runit/runsvdir/default/aur2xbps-repo` (activo con `sv up aur2xbps-repo`); como usuario en `~/.config/aur2xbps/runit/` (arranca con `runsvdir ~/.config/aur2xbps/runit &` o ejecuta el wrapper `~/.local/bin/aur2xbps-serve-repo`).
8. Deja el CLI en `~/.local/bin/aur2xbps`.

## Configuración

Prioridad: **env `AUR2XBPS_*` > TOML usuario > TOML sistema > defaults XDG**.

| Clave | Env | Default |
|---|---|---|
| `data_dir` | `AUR2XBPS_DATA_DIR` | `$XDG_DATA_HOME/aur2xbps` |
| `cache_dir` | `AUR2XBPS_CACHE_DIR` | `$XDG_CACHE_HOME/aur2xbps` |
| `repo_dir` | `AUR2XBPS_REPO_DIR` | `<data_dir>/repo` |
| `keys_dir` | `AUR2XBPS_KEYS_DIR` | `$XDG_CONFIG_HOME/aur2xbps/keys` |
| `masterdir` | `AUR2XBPS_MASTERDIR` | `<data_dir>/void/masterdir` |
| `void_packages_dir` | `AUR2XBPS_VOID_DIR` | `<data_dir>/void/void-packages` |
| `host` / `port` | `AUR2XBPS_HOST` / `AUR2XBPS_PORT` | `127.0.0.1` / `8080` |
| `arch` | `AUR2XBPS_ARCH` | detectada (`platform.machine()`) |
| offline | `AUR2XBPS_OFFLINE=1` | `false` |

Ejemplo `config.toml`:

```toml
[paths]
data_dir = "~/.local/share/aur2xbps"
keys_dir = "~/.config/aur2xbps/keys"

[repo]
host = "0.0.0.0"
port = 8080

[build]
arch = "aarch64"
restricted_mode = true
```

## CLI

Salida de máquina siempre JSON por stdout; logs por stderr.

### query — metadatos del AUR
```bash
aur2xbps query cbonsai --sources   # --sources añade fuentes+hashes del .SRCINFO
```

### resolve — dependencias mapeadas a Void
```bash
aur2xbps resolve cbonsai
# {"package": "cbonsai", "depends": ["gcc", "ncurses-devel"], "makedepends": ["scdoc"], …}
```
Mapeo Arch→Void en capas: tabla manual → reglas (`python-*→python3-*`, `gtk3→gtk+3`, descarta `lib32-*`) → nombre directo. Best-effort; la plantilla resultante es revisable antes de compilar.

### template — plantilla xbps-src estándar
```bash
aur2xbps template cbonsai [--out DIR] [--no-sync]
```
Genera `srcpkgs/<pkg>/template` autónomo (compila con `./xbps-src pkg <pkg>` sin aur2xbps). Por defecto se sincroniza a `<void-packages>/srcpkgs/`. `build_style` auto-detectado: `meta` (sin fuentes) · `python3-module` · `meson` · `cmake` · `gnu-makefile` · fetch manual para `-bin` (.deb/.rpm/.AppImage). VCS `-git` se convierte a tarball pineado por rev cuando el host es GitHub. Paquetes sin binarios → `archs=noarch`. Licencias no redistribuibles → comentario `restricted=yes`.

### build — compilar con el motor disponible
```bash
aur2xbps build cbonsai                # auto: nix si está; si no xbps-src
aur2xbps build cbonsai --engine xbps-src
aur2xbps build yay-bin  --engine nix
```
- Motor **nix**: transpile → `nix build` sandbox → stage → patchelf Void selectivo → shlibs auto → `xbps-create` determinista + firma → chroot install + smoke root/no-root.
- Motor **xbps-src** (sin Nix): genera plantilla en `srcpkgs/`, `binary-bootstrap` si hace falta, `./xbps-src -A <arch> pkg <pkg>`; resultado en `<void-packages>/hostdir/binpkgs/`.

### repo — repositorio local firmado
```bash
aur2xbps repo --sign     # indexa repo_dir/*.xbps + firma con keys_dir/privkey.pem
```

## Integración con vouru

[vouru](https://github.com/javiercplus/vouru) busca/compila/instala plantillas de `srcpkgs/`. Flujo recomendado:

```bash
# 1) Generar plantillas (se sincronizan al árbol de vouru o al propio):
aur2xbps template <pkg>
cp -r ~/.local/share/aur2xbps/srcpkgs/<pkg> ~/.local/share/pkgs/void-packages/srcpkgs/

# 2) Con vouru:
vouru search <pkg>
vouru install <pkg>          # opción [source] compila con ./xbps-src pkg

# 3) Sin vouru, directo:
cd <void-packages> && ./xbps-src pkg <pkg>
xbps-install -S --repository hostdir/binpkgs <pkg>
```

Variables que un helper debe respetar: `AUR2XBPS_CONFIG`, `AUR2XBPS_DATA_DIR`,
`AUR2XBPS_REPO_DIR`, `AUR2XBPS_KEYS_DIR`, `AUR2XBPS_OFFLINE`, `AUR2XBPS_ARCH`.

## Multi-arquitectura

Detectada en runtime, nunca hardcodeada:

| Arch | Intérprete ELF | Nix system |
|---|---|---|
| x86_64 | `/lib64/ld-linux-x86-64.so.2` | `x86_64-linux` |
| aarch64 | `/lib/ld-linux-aarch64.so.1` | `aarch64-linux` |
| i686 | `/lib/ld-linux.so.2` | `i686-linux` |

Override: `AUR2XBPS_ARCH=aarch64 aur2xbps template <pkg>`.

## Modo sin Nix

Si `nix --version` falla, todo el flujo funciona con plantillas + `xbps-src`:

```bash
AUR2XBPS_OFFLINE=0 aur2xbps template <pkg>
cd <void-packages> && ./xbps-src pkg <pkg>
```

Limitaciones vs Nix: sin aislamiento de red TIAR durante build, sin hash-lock
de fuentes (usa checksums del .SRCINFO), smoke EEL reducido.

## CI local

```bash
./scripts/ci-local.sh                    # completo (~4 min)
SMOKE_TARGET=yay-bin ./scripts/ci-local.sh          # smoke root opcional
NOROOT_SMOKE_PKG=<pkg> TRH_STAGE_REAL=<pkg> ./scripts/ci-local.sh
```

Todos los paquetes son **argumentos/env**, nunca constantes: sin ellos los checks se omiten con aviso (no fallan). Lote end-to-end:

```bash
python3 scripts/batch-validate.py "pkg1:/usr/bin/pkg1" pkg2[:bin1,bin2] …
```

## KPIs implementados
| KPI | Verificación | Umbral |
|-----|--------------|--------|
| TRH cross-host | SHA-256 host glibc vs Alpine/musl post-determinize | idénticos |
| EEL | chroot root+no-root sin errores de cargador | exit 0 |
| RDC | run_depends XBPS vs depends Arch | >0% reducción |
| TIAR | canary curl sandbox Nix | 0 bytes egress (exit 6) |

## Clave privada
- En `keys_dir` (config), FUERA del árbol; permisos 600.
- Exportar pública: `scripts/export-key.sh [destino]`.
- CI GitHub genera clave RSA **efímera** en el runner; nunca usa una real.

## Limitaciones conocidas
- python-legacy setup.py con cmdclass custom: experimental.
- `build_python_in_void()` requiere deps build instaladas en masterdir.
- Cross-compile ARM desde host x86_64 único: no soportado (build nativo aarch64 sí vía `AUR2XBPS_ARCH`).
- Repology API restringe bots: oráculo offline básico.
- Plantillas VCS no-GitHub requieren `do_fetch` manual (aviso en generación).
