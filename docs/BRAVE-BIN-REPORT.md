# Reporte de build — brave-bin (2026-08-22)

Build de `brave-bin` 1.93.138 (AUR) e instalación directa en el sistema Void
x86_64 del usuario (sin chroot de prueba, según lo acordado). Suite de tests
del proyecto tras los fixes: **162/162 ✓**.

## Resultado

| Paso | Resultado |
|---|---|
| Descarga zip oficial + SHA-256 | ✓ coincide |
| Empaquetado `.xbps` determinista (SOURCE_DATE_EPOCH=0) | ✓ 168 MB |
| Indexado + firma RSA del repo local | ✓ |
| Instalación en el host (`xbps-install`) | ✓ 448 MB |
| Smoke nativo | ✓ `brave --version` → "Brave Browser 151.1.93.138" exit 0 |

## Errores encontrados y arreglados

### 1. `ttf-font` no existe en Void — `src/void/mapping.py`
- **Síntoma**: `ERROR: target dependency 'ttf-font' does not exist!`
- **Causa**: nombre virtual de Arch sin equivalente directo; el mapeo lo dejaba pasar tal cual.
- **Fix**: `"ttf-font": "fontconfig"` en la tabla MANUAL (runtime, sin `-devel`, cae en `depends=`).

### 2. Instalador genérico `-bin` rompe apps tipo Chromium — `src/void/template.py`
- **Síntoma** (por inspección): el zip de Brave es **plano** (binario + `locales/` + `*.pak` + recursos al mismo nivel). El fallback genérico habría esparcido ELFs sueltos (incluso `.so`) en `/usr/bin` y perdido los recursos → navegador sin `locales/` no arranca.
- **Fix**: rama nueva en `_doinstall`: detecta binario `${pkgname%-bin}` (+ layout anidado `<dir>/<binario>`) junto a `locales/` o `*.pak` → instala árbol completo en `/usr/lib/<pkg>` y symlink `/usr/bin`. Verificado que la plantilla generada emite la rama correctamente.

### 3. `ShlibsDB.deps_for_elf()` devolvía tuplas crudas — `src/xbps/shlibs.py`
- **Síntoma**: run_depends tipo `alsa-lib-1.0.20_1` (sin operador).
- **Causa**: usaba `self.lookup()` en vez de `self.soname_to_dep()` — contradice el gotcha documentado del propio repo ("xbps trata literal como versión exacta").
- **Fix**: usar `soname_to_dep()`. Suite sigue verde.

### 4. xbps-create 0.60.7: `provides` exige revisión `_N`
- **Síntoma**: `provides: invalid value: brave-1.93.138` — y también fallan los ejemplos de la propia help (`foo-9999`).
- **Hallazgo empírico**: solo acepta versión con revisión (`foo-1_1` OK).
- **Workaround aplicado**: `provides="brave-1.93.138_1 brave-browser-1.93.138_1"`.

### 5. `create_xbps()`: rename entre filesystems — `src/xbps/builder.py`
- **Síntoma**: `OSError: Invalid cross-device link` al mover el .xbps desde el tmpdir de `/tmp` (tmpfs) hacia `~/.../repo`.
- **Fix**: `shutil.move()` + soporte de destino directorio o archivo completo.

### 6. Repo firmado pide importar clave con TTY
- **Síntoma**: `Do you want to import this public key? [Y/n]` → con stdin EOF: `Resource temporarily unavailable` (y `$?` enmascarado por pipes).
- **Solución**: registrar la clave a mano en `/var/db/xbps/keys/<huella>.plist` (formato plist con PEM en base64). Candidato a feature: comando `aur2xbps repo --trust` que genere ese plist automáticamente.

### 7. `generate_keypair()` nunca escribía `pubkey.pem` — `src/xbps/signing.py`
- **Síntoma**: `install.sh` intenta `chmod 644 pubkey.pem` pero el archivo no existía (solo se generaba si se pasaba explícito, y no se pasaba).
- **Fix**: default `priv.parent/"pubkey.pem"` + derivarla también cuando solo existe la privada.

### 8. Repodata stale al re-indexar mismo pkgver — `src/xbps/builder.py::rindex_add`
- **Síntoma**: re-empaqueté brave-bin con dependencias corregidas, pero `xbps-query --show` seguía mostrando las viejas (`gtk+3` pelado) aunque el `.xbps` interno era correcto.
- **Causa**: `xbps-rindex -a` **sin `-f` conserva silenciosamente la entrada previa** cuando el pkgver ya estaba indexado.
- **Fix**: `xbps-rindex -f -a` en `rindex_add()`.

## Observaciones menores (no requieren fix)

- La **epoch** de Arch (`1:1.93.138-1`) ya se recorta bien al generar la plantilla (`version="1.93.138"`).
- Los shims Qt del zip (`libqt5_shim.so`, `libqt6_shim.so`) generaban deps duras `qt5-*`/`qt6-*`; son dlopen opcionales → filtradas.
- Deps "extras" sin SONAME (gtk+3, fontconfig…) deben ir **versionadas** (`pkg>=ver`) para resolver en transacción; nombres pelados provocaron `can't guess pkgname for dependency 'gtk+3'` durante el diagnóstico.
- Licencia `custom:chromium` → paquete marcado **restricted** (`restricted=yes`): NO redistribuir el `.xbps`; uso local OK.

## Archivos modificados (sin commitear)

| Archivo | Cambio |
|---|---|
| `src/void/mapping.py` | ttf-font → fontconfig |
| `src/void/template.py` | rama Chromium/Electron en `_doinstall` |
| `src/xbps/shlibs.py` | `soname_to_dep()` en `deps_for_elf()` |
| `src/xbps/builder.py` | `shutil.move` cross-device; `rindex -f -a` |
| `src/xbps/signing.py` | pubkey.pem siempre generada |
| `install.sh` | pasa ruta de pubkey a generate_keypair |
