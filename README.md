# aur2xbps

[![SPDX](https://img.shields.io/badge/SPDX-GPL--3.0--or--later-blue)](LICENSE)

Bridge **AUR (Arch) → Void Linux (XBPS)** using Nix as a hermetic build engine
— or pure `xbps-src` when Nix isn't installed.

`aur2xbps` transpiles Arch User Repository packages into reproducible, signed
Void Linux `.xbps` packages — without ever evaluating `PKGBUILD` code. It reads
only `.SRCINFO` metadata, generates pinned Nix flakes **and/or standard
`xbps-src` templates**, builds them in a sandbox, and repackages the result for
Void with automatic shared-library dependency resolution and RSA signatures.

> **Status:** working prototype, hardened end-to-end. Quality gates:
> cross-host byte-identical packaging, sandbox egress denial, loader-error
> smoke tests, 160-test suite.

## Install (Void Linux)

```bash
git clone <este-repo> aur2xbps && cd aur2xbps
./install.sh          # deps, workspace XDG, claves RSA, config.toml, servicio runit
```

`install.sh` detects Void (`/etc/os-release`), installs dependencies via
`xbps-install`, creates `$XDG_DATA_HOME/aur2xbps`, generates signing keys
(`600`) and a runit service for the local repo. **Nix is optional**: if absent you get
template-only mode (builds via `xbps-src`).

## How it works

1. **Extract** AUR metadata via RPC v5 (`httpx` + SQLite cache, batches ≤200 /
   ≤4443 B URI, 4000 req/day rate tracking, offline mode) — `PKGBUILD` is
   never executed or evaluated.
2. **Filter** supply-chain threats (Atomic Arch campaign): blocks known
   malicious packages and hashless `npm/bun install` sources *before* cloning.
3. **Transpile** per target engine:
   - **Nix flakes** (nixpkgs 24.11 locked): `buildFHSEnv` for `-bin`,
     autotools/meson/cmake/suckless templates, VCS pinned via `git ls-remote`,
     Python source-only → wheel built with Void python3
   - **xbps-src templates** (`srcpkgs/<pkg>/template`): standard Void format,
     build_style auto-detected (meta/python3-module/meson/cmake/gnu-makefile/fetch),
     VCS converted to pinned tarballs, `archs=noarch` for data/meta packages
4. **Package** deterministic XBPS (`SOURCE_DATE_EPOCH=0`, normalized tar,
   uname/gname→NUL post-processing) with auto `run_depends` from real
   `common/shlibs` + RSA-4096 signatures.
5. **Validate** in a Void chroot: install + root/no-root smoke tests +
   KPIs (TRH reproducibility, EEL execution, RDC dependency reduction,
   TIAR network isolation).

## Multi-architecture

Architecture is detected at runtime (`platform.machine()`), never hardcoded:

| Arch | Dynamic linker | Nix system |
|---|---|---|
| x86_64 | `/lib64/ld-linux-x86-64.so.2` | `x86_64-linux` |
| aarch64 | `/lib/ld-linux-aarch64.so.1` | `aarch64-linux` |
| i686 | `/lib/ld-linux.so.2` | `i686-linux` |

Override with `AUR2XBPS_ARCH`. Packages without binaries (meta/pure-python)
get `archs=noarch`.

## CLI

```bash
aur2xbps query cbonsai              # metadatos AUR → JSON
aur2xbps resolve cbonsai            # dependencias mapeadas a nombres Void → JSON
aur2xbps template cbonsai           # genera srcpkgs/cbonsai/template (xbps-src puro)
aur2xbps build cbonsai --engine auto   # nix si está; si no xbps-src
aur2xbps repo --sign                # indexa y firma el repo local
```

## Integration with vouru

[vouru](https://github.com/javiercplus/vouru) builds and installs xbps-src
templates. The generated templates are self-contained — they compile with
plain `./xbps-src pkg <pkg>`:

```bash
# 1. Generar plantillas (se sincronizan a <void-packages>/srcpkgs):
aur2xbps template <pkg>

# 2. Compilar e instalar con vouru desde su árbol void-packages:
vouru search <pkg>
vouru install <pkg>

# O compilar directamente sin vouru:
cd ~/.local/share/aur2xbps/void/void-packages && ./xbps-src pkg <pkg>
xbps-install --repository hostdir/binpkgs <pkg>
```

## Configuration

Priority: environment `AUR2XBPS_*` > user TOML > system TOML > XDG defaults.

```bash
~/.config/aur2xbps/config.toml       # usuario
/etc/aur2xbps/config.toml            # sistema
```

```toml
[paths]
data_dir = "~/.local/share/aur2xbps"
cache_dir = "~/.cache/aur2xbps"
repo_dir = "~/.local/share/aur2xbps/repo"
keys_dir = "~/.config/aur2xbps/keys"

[repo]
host = "127.0.0.1"
port = 8080

[build]
arch = "x86_64"            # o aarch64 / i686; default: detectada
restricted_mode = true     # bloquea empaquetado de no-redistribuibles
```

Environment variables: `AUR2XBPS_CONFIG`, `AUR2XBPS_DATA_DIR`,
`AUR2XBPS_CACHE_DIR`, `AUR2XBPS_REPO_DIR`, `AUR2XBPS_KEYS_DIR`,
`AUR2XBPS_OFFLINE`, `AUR2XBPS_ARCH`, `AUR2XBPS_HOST`, `AUR2XBPS_PORT`.
Helpers like vouru only need these.

## Security model

- No `PKGBUILD` evaluation — parsing is limited to declarative `.SRCINFO`.
- Supply-chain filter aborts before cloning on known-malicious packages.
- Builds run sandboxed; a canary test asserts DNS/network egress is denied (TIAR).
- Signing keys live outside the repo (`keys_dir`); nothing secret is committed.
- Non-redistributable upstream licenses are flagged `restricted=yes`;
  `restricted_mode=true` refuses to package them.

## License

This project is licensed under **GPL-3.0-or-later** — see [`LICENSE`](LICENSE).
Generated packages embed upstream software subject to its own license;
aur2xbps does not distribute binaries. Third-party components are documented
in [`THIRD_PARTY.md`](THIRD_PARTY.md).
