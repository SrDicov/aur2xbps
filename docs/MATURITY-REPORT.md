# Reporte de madurez — AUDIT-2026-08 (agosto 2026)

Implementación del plan de madurez a partir de `docs/AUDIT-2026-08.md`
(verificación manual de cada hallazgo contra el código, luego fix + test).
Suite: **227/227 ✓** (162 iniciales → 65 tests nuevos).

## Resumen por hallazgo

| ID | Hallazgo | Estado inicial | Fix | Tests |
|----|----------|----------------|-----|-------|
| H-1.1 | SQLite sin WAL/busy_timeout/retry/GC/índices | vigente | `client.py`: WAL+busy_timeout+`_db_write` backoff×5, GC 48h/7d, índice | concurrencia real 8 procesos, GC, retry |
| H-1.2 | Parser sin sanitización léxica | vigente | control chars/homoglifos/límites/DoS caps rechazados; claves desconocidas → `SrcInfo.warnings` | fixtures maliciosos + fuzzer determinista 450 inputs |
| — | pkgver/pkgrel/epoch sin validar | vigente | regex formato + epoch numérico | incluidos arriba |
| H-2.1 | Regex JS estrecha y desconectada | vigente | `JS_INSTALL_RE` (yarn/pnpm/deno/bower/i/ci/add/ofuscación); pipeline pasa texto PKGBUILD estático | paramétricos ×8 |
| H-2.2 | Allowlist hardcodeada sin PGP | vigente | `[security] trusted_pgp_keys` (+env); allowlist deprecada con warning | PGP exime/no-exime, deprecación |
| H-3.1 | Symlinks absolutos preservados en BIN | vigente | post-unpack: relativizar dentro de `$out`, rotos fuera | shell real sobre árbol sintético |
| H-3.2 | Oráculo nix eval cascada | parcial | solo ante inaccesibilidad HTTP total | aserción fuente |
| H-4.1 | validate_elf código muerto; sin ldd oracle | parcial | `verify_patched_elf()` wired en cada parcheo; chmod restaurado | ELF corrupto → RuntimeError |
| H-4.2 | PaxHeaders/xattrs pasan; zstd sin pinear | vigente | strip pax x/g, uid/gid=0 en cabecera ustar, zstd vía `AUR2XBPS_ZSTD` | tar sintético idempotente |
| H-5.1 | Servidor sin timeouts/traversal/TLS débil | vigente | timeout 30s, guard realpath, TLS≥1.2, warning no-loopback; legacy 0.0.0.0 eliminado | servidor vivo: escape symlink→404, interno→200 |
| H-5.2 | `--privkey` en argv/logs/excepciones | vigente | `redact_cmd()` en builder._run/pipeline/signing | anti-fuga en stdout y excepción |
| X-1 | Stage contaminado | resuelto ya | — | — |
| X-2 | _StderrOnly sin tests | bajo | timeouts zstd/git-pull/xbps-src (`build_timeout`) | restauración fds éxito/excepción |
| T-1 | Canary TIAR solo-DNS | pendiente* | *queda para corrida en PC grande (requiere sandbox Nix) | — |
| T-2 | Sin fuzzing parser | vigente | fuzzer casero determinista (seed fija) | 450 mutaciones |
| T-3 | Hardcode x86_64-linux en build | bug duro | attr desde `nix_system(cfg.arch)` | monkeypatch aarch64 |
| T-4 | Batch secuencial O(N) | mitigado | prepare paralelo planificado; builds serializados por lock xbps-src (documentado) | pilot |
| T-6 | Subprocess sin timeouts | vigente | techos duros en shlibs/cli/determinize/builder | propagación timeout |

\* T-1 y la corrida completa 100+100 requieren Nix instalado → máquina destino.

## Harness dual-engine (Fase 6)

`scripts/mass-validate.py`:
- muestreo reproducible (seed) desde packages.gz: `-bin` vs fuente
- motores: `both` corre xbps-src **y** nix por paquete (degrada sin Nix)
- smoke funcional SIN instalar: extrae `.xbps` a tmp (`filter="data"`),
  resuelve symlinks `/usr/bin→../lib`, ejecuta `--version/--help`,
  clasifica exit_ok / loader_error / segfault / signal
- rotación de disco (`--min-free-mb`), reanudable (`mass-results.json`),
  reporte markdown con causas raíz agrupadas

**Pilot local (xbps-src): 4/4 OK** — cbonsai, neofetch-git, gdu-bin,
yazi-nightly-bin (build+smoke verde, ~25-130s c/u).

## Corrida completa (en PC destino)

```bash
pip install -e . && ./scripts/ci-local.sh          # sanity
python3 scripts/mass-validate.py --count-bin 100 --count-src 100 \
    --engine both --seed 20260823 --min-free-mb 3000
# iterar: revisar mass-results.md → fixes dirigidos → re-ejecutar (reanuda)
```

Meta acordada: iterar hasta que todo paquete esté OK o clasificado
broken-upstream con causa documentada.

## Commits

| Fase | Commit | Contenido |
|------|--------|-----------|
| 0 | `324f764`→`a4bc886` | AUDIT importado a docs/, rama feat/maturity |
| 1 | `dcec3f4` | timeouts, oráculo ELF, arch-aware build |
| 2 | `4581a69` | SQLite endurecido + sanitización parser |
| 3 | `928f842` | heurística JS + anclaje PGP |
| 4 | `316f7f2` | hermeticidad symlinks + TRH canónico |
| 5 | `2098a8a` | redacción secretos + servidor endurecido |
| 6 | (este) | harness masivo dual-engine + docs |
