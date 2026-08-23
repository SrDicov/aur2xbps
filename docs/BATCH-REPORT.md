# Reporte de lote — 12 paquetes AUR → Void (2026-08-23)

Reconstrucción y validación end-to-end del repo portátil Ventoy
(`repo-void`, 12 paquetes) usando aur2xbps con motor xbps-src en esta
máquina. Cada paquete: build → instalación real → smoke test.

## Resultado final: 12/12 compilan

| # | Paquete | Smoke | Fixes necesarios |
|---|---------|-------|------------------|
| 1 | gdu-bin | `gdu --version` v5.36.1 ✓ | — |
| 2 | bun-bin | `bun --version` + ejecuta JS ✓ | F1, F2, F3, F4 |
| 3 | yazi-nightly-bin | `yazi --version` (+`ya`) ✓ | M1 |
| 4 | neofetch-git | `neofetch --version` ✓ | — |
| 5 | yay-bin | binario carga; falla ALPM* | — |
| 6 | brave-bin | GUI validada sesión previa ✓ | (ver BRAVE-BIN-REPORT) |
| 7 | anydesk-bin | `anydesk --version` 8.0.4 ✓ | M2, M3 |
| 8 | spotify | `spotify --version` exit 0 + shim ✓ | G1–G6 |
| 9 | telegram-desktop-bin | `Telegram` arranca sin errores cargador ✓ | G5(basename) |
| 10 | postman-bin | app viva, sin errores ICU ✓ | G7(bundle), G8(alias) |
| 11 | visual-studio-code-bin | `code --version` 1.134.0 ✓ | F5(nostrip), G9(lanzadores bin/) |
| 12 | google-chrome | `google-chrome-stable --version` ✓ | M4(ttf-liberation), F6(refresh hashes) |

\* yay exige pacman/ALPM de Arch: empaquetado correcto, funcionalidad
limitada por diseño de plataforma.

## Fixes genéricos (en el generador, nunca por-paquete)

### src/void/template.py
- **G1** split `-devel`: compara nombre base SIN operador (`alsa-lib-devel>=1`
  también es header); versiones peladas al emitir makedepends ("template
  version is used always").
- **G2** `skip_extraction` para distfiles no-archivo (.png, Release, .gpg…);
  nombre local = parte tras `>` o basename de URL.
- **G3** fusión Debian `.deb`: `cp -a usr/. DESTDIR/usr/` (vcopy anidaba
  `/usr/usr`).
- **G4** shim `libcurl-gnutls.so.4 → /usr/lib/libcurl.so.4` junto a la app,
  solo si algún ELF la referencia (patrón del spotify oficial de Void).
- **G5** ELF bajo `/usr/share` prohibidos por pkglint → reubicar directorio a
  `/usr/lib/<app>`, reescribir lanzadores y re-enlazar symlinks relativos.
- **G6** variantes de microarch (`*-baseline`): empaquetar solo baseline
  (bun moderno exige AVX2 → SIGILL en CPUs viejas).
- **G7** fallback recursivo bundle-aware: si junto a los ELFs hay ficheros
  NO-ELF (icudtl.dat, *.pak) copiar árbol completo a /usr/lib/<pkg> y enlazar
  cada ejecutable de raíz; alias minúsculas (Postman→postman).
- **G8** lanzadores anidados `<app>/bin/*` (vscode usa postinst de Debian que
  aquí no existe) → enlazar a /usr/bin.
- **F5** `nostrip=yes` + `nopie=yes` en plantillas -bin (`dontStrip=true` es
  convención Nix: xbps-src la ignora y strip corrompe/rechaza upstream).
- **F6** `AUR2XBPS_REFRESH_HASHES=1`: recálculo opt-in de hashes stale
  (google-chrome re-publica el .deb bajo misma URL), siempre con aviso.

### src/void/mapping.py
- **M1** ttf-nerd-fonts-symbols → nerd-fonts-symbols-ttf
- **M2** gcc-libs → libstdc++
- **M3** lsb-release, systemd-libs → None (sin equivalente/dlopen opcional)
- **M4** ttf-liberation → liberation-fonts-ttf
- libcurl-gnutls → libcurl · libsm → libSM · libcups → cups-devel

### src/cli.py / src/xbps/
- **F2** `./xbps-src clean <pkg>` antes de cada build: el stamp `_build_done`
  se toca ANTES de instalar; un fallo posterior dejaba todos los retries
  saltándose fetch/extract/install en silencio (incluso con `-f`).
- **F3** `xbps-rindex -f -a`: sin `-f` la repodata quedaba stale al
  re-indexar mismo pkgver.
- `create_xbps()`: `shutil.move` cross-device (EXDEV /tmp→home).

## Estado del sistema tras la validación

Los 12 paquetes de prueba fueron desinstalados a petición (los originales
siguen en el USB Ventoy). Workspace reducido a lo mínimo (sin masterdir);
quedan los fixes en `src/` sin commitear y AGENTS.md actualizado con los
gotchas nuevos. Suite: **162/162 ✓**.
