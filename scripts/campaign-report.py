#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Consolida y analiza los resultados de una campaña masiva remota.

Los shards del workflow ``mass-build.yml`` publican artefactos
``campaign-shard-<i>`` que contienen ``results-shard-<i>Of<N>.json``
(dict pkg → resultado con motores), binarios ``*.xbps`` y logs de
``/tmp/aur2xbps-logs``.

Subcomandos:
  organize  Fusiona los shards en DEST, ordena los .xbps por motor,
            copia logs y genera report.md, failed-only.txt y
            blocked-only.txt.
  analyze   Resumen compacto a stdout de un directorio ya organizado.

Solo stdlib de Python 3 (sin dependencias externas).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PREFIJO_BLOQUEADO = "bloqueado"


# ------------------------------------------------------------- clasificación
def detalle_motor(engine_res: dict) -> str:
    """Detalle textual de un resultado de motor ('' si ausente)."""
    d = engine_res.get("detail")
    return d if isinstance(d, str) else ""


def motor_bloqueado(engine_res: dict) -> bool:
    """True si el motor marcó el paquete como bloqueado (filtro/licencia)."""
    return detalle_motor(engine_res).startswith(PREFIJO_BLOQUEADO)


def entrada_bloqueada(entry: dict) -> bool:
    engines = entry.get("engines") or {}
    return any(motor_bloqueado(er) for er in engines.values())


def _marca_entrada(entry: dict) -> str:
    if entry.get("ok"):
        return "✅"
    return "🚫" if entrada_bloqueada(entry) else "❌"


def _marca_motor(engine_res: dict) -> str:
    if engine_res.get("ok"):
        return "✅"
    return "🚫" if motor_bloqueado(engine_res) else "❌"


def _causas_raiz(resultados: dict[str, dict]) -> Counter[str]:
    """Conteo de fallos agrupados por detalle recortado a 80 caracteres."""
    causas: Counter[str] = Counter()
    for entrada in resultados.values():
        if entrada_bloqueada(entrada):
            continue
        for er in (entrada.get("engines") or {}).values():
            if not er.get("ok"):
                causas[detalle_motor(er)[:80] or "(sin detalle)"] += 1
    return causas


# ------------------------------------------------------------------ fusión
def directorios_shard(raw: Path) -> list[Path]:
    return sorted(r for r in raw.glob("campaign-shard-*") if r.is_dir())


def fusionar_resultados(raw: Path) -> tuple[dict[str, dict], list[dict]]:
    """Fusiona los results-shard-*.json anotando el shard de origen.

    La clave especial ``@blocked`` (lista de paquetes bloqueados excluidos
    por el pool de reemplazo) se separa del dict de paquetes. Devuelve
    ``(paquetes, bloqueados_extra)``.
    """
    fusion: dict[str, dict] = {}
    bloqueados_extra: list[dict] = []
    for shard in directorios_shard(raw):
        for ruta in sorted(shard.glob("results-shard-*of*.json")):
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"aviso: no se pudo leer {ruta}: {exc}", file=sys.stderr)
                continue
            if not isinstance(datos, dict):
                continue
            for pkg, entrada in datos.items():
                if pkg == "@blocked":
                    if isinstance(entrada, list):
                        for item in entrada:
                            if isinstance(item, dict) and item not in bloqueados_extra:
                                bloqueados_extra.append(item)
                    continue
                if not isinstance(entrada, dict):
                    continue
                nueva = dict(entrada)
                nueva.setdefault("pkg", pkg)
                shards = nueva.setdefault("shards", [])
                if shard.name not in shards:
                    shards.append(shard.name)
                previa = fusion.get(pkg)
                if previa is None:
                    fusion[pkg] = nueva
                else:
                    # mitades nix/xbps-src del mismo shard: unir motores
                    previa.setdefault("engines", {}).update(
                        nueva.get("engines", {}))
                    for s in shards:
                        if s not in previa["shards"]:
                            previa["shards"].append(s)
                    previa["ok"] = bool(previa["engines"]) and all(
                        e.get("ok") for e in previa["engines"].values())
    return fusion, bloqueados_extra


# -------------------------------------------------------------- artefactos
_ORIGEN_MOTOR = {
    "from-nix": "nix",
    "from-xbps-src": "xbps-src",
}


def _pkg_de_nombre(nombre: str, paquetes: list[str]) -> str | None:
    """Pkg cuyo nombre es el prefijo más largo de `nombre` (split packages)."""
    for pkg in sorted(paquetes, key=len, reverse=True):
        if nombre.startswith(f"{pkg}-"):
            return pkg
    return None


def recolectar_xbps(
    raw: Path, dest: Path, resultados: dict[str, dict]
) -> dict[str, dict[str, list[str]]]:
    """Copia a DEST/xbps/<motor>/<pkg>/ los .xbps de cada shard.

    Layout del staging CI: ``xbps/from-nix/…`` y ``xbps/from-xbps-src/…``
    (el subdirectorio determina el motor, evitando colisiones de nombre
    entre motores). Fallback genérico: prefijo ``<pkg>-`` contra los
    motores que reportaron ok. Devuelve el manifiesto
    ``{pkg: {motor: [rutas relativas a dest]}}``.
    """
    manifiesto: dict[str, dict[str, list[str]]] = {}
    raiz_xbps = dest / "xbps"
    nombres_pkgs = [k for k in resultados if not k.startswith("@")]
    for shard in directorios_shard(raw):
        for binario in sorted(shard.rglob("*.xbps")):
            if not binario.is_file():
                continue
            partes = binario.relative_to(shard).parts
            motor = next((_ORIGEN_MOTOR[p] for p in partes if p in _ORIGEN_MOTOR),
                         None)
            pkg = _pkg_de_nombre(binario.name, nombres_pkgs)
            entrada = resultados.get(pkg) if pkg else None
            if motor is None:
                # fallback: asignar al primer motor exitoso del paquete
                motores_ok = [m for m, er in ((entrada or {}).get("engines")
                                              or {}).items() if er.get("ok")]
                motor = motores_ok[0] if motores_ok else "_sin-asignar"
            if pkg is None:
                salida = raiz_xbps / "_sin-asignar" / motor
                clave_pkg = None
            else:
                salida = raiz_xbps / motor / pkg
                clave_pkg = pkg
            salida.mkdir(parents=True, exist_ok=True)
            destino_final = salida / binario.name
            shutil.copy2(binario, destino_final)
            relativo = destino_final.relative_to(dest).as_posix()
            if clave_pkg:
                lista = manifiesto.setdefault(clave_pkg, {}).setdefault(motor, [])
                if relativo not in lista:
                    lista.append(relativo)
    return manifiesto


def _parece_log(relativa: Path) -> bool:
    """Heurística para ficheros procedentes de /tmp/aur2xbps-logs.

    El workflow los stages bajo ``logs/``; también encajan la convención
    original (ruta con ``aur2xbps-logs``, ``*.log``, ``log*``).
    """
    partes = relativa.parts
    if partes and partes[0] == "logs":
        return True
    if "aur2xbps-logs" in partes or "aur2xbps-logs" in relativa.as_posix():
        return True
    if relativa.suffix == ".log":
        return True
    return relativa.name.startswith("log")


def copiar_logs(raw: Path, dest: Path) -> int:
    """Copia los logs de cada shard a DEST/logs/, prefijados por shard."""
    destino_logs = dest / "logs"
    copiados = 0
    for shard in directorios_shard(raw):
        for ruta in sorted(shard.rglob("*")):
            if not ruta.is_file():
                continue
            relativa = ruta.relative_to(shard)
            if not _parece_log(relativa):
                continue
            destino_logs.mkdir(parents=True, exist_ok=True)
            nombre_plano = relativa.as_posix().replace("/", "__")
            shutil.copy2(ruta, destino_logs / f"{shard.name}__{nombre_plano}")
            copiados += 1
    return copiados


# ---------------------------------------------------------------- informes
def escribir_informe(
    dest: Path,
    resultados: dict[str, dict],
    manifiesto: dict[str, dict[str, list[str]]],
    logs_copiados: int,
    run_id: str | None,
    bloqueados_extra: list[dict] | None = None,
) -> None:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    extra_n = len(bloqueados_extra or [])
    ok = sum(1 for e in resultados.values() if e.get("ok"))
    bloqueados = sum(1 for e in resultados.values() if entrada_bloqueada(e))
    fallos = len(resultados) - ok - bloqueados

    lineas: list[str] = [
        "# Informe de campaña",
        "",
        f"- Fecha: {ahora}",
        f"- Run: {run_id if run_id else "(desconocido)"}",
        f"- Paquetes: {len(resultados)}",
        f"- ✅ OK: {ok}",
        f"- ❌ Fallo: {fallos}",
        f"- 🚫 Bloqueados: {bloqueados} (+{extra_n} excluidos por pool)",
        f"- Logs copiados: {logs_copiados}",
        "",
        "## Causas raíz (agrupadas)",
        "",
    ]
    if bloqueados_extra:
        nombres = ", ".join(str(b.get("pkg", "?")) for b in bloqueados_extra[:12])
        lineas += [f"- Excluidos por pool de reemplazo: { nombres }", ""]
    causas = _causas_raiz(resultados)
    if causas:
        lineas += [
            "| Motores afectados | Causa (primeros 80 caracteres) |",
            "|------------------:|--------------------------------|",
        ]
        for causa, n in causas.most_common():
            causa_md = causa.replace("|", "\\|")
            lineas.append(f"| {n} | `{causa_md}` |")
    else:
        lineas.append("(sin fallos)")

    lineas += ["", "## Detalle por paquete", ""]
    for pkg in sorted(resultados):
        entrada = resultados[pkg]
        shards = ", ".join(entrada.get("shards") or [])
        lineas.append(f"### {pkg} [{entrada.get('kind', '?')}] — {_marca_entrada(entrada)}")
        if shards:
            lineas.append(f"- shards: {shards}")
        for motor, er in (entrada.get("engines") or {}).items():
            det = detalle_motor(er)
            sufijo = f" — {det}" if det else ""
            lineas.append(f"  - {motor}: {_marca_motor(er)}{sufijo}")
        for motor, rutas in sorted((manifiesto.get(pkg) or {}).items()):
            for ruta in rutas:
                lineas.append(f"  - artefacto ({motor}): `{ruta}`")
        lineas.append("")

    lineas += ["## Artefactos .xbps organizados", ""]
    total = 0
    for pkg in sorted(manifiesto):
        for motor, rutas in sorted(manifiesto[pkg].items()):
            for ruta in rutas:
                total += 1
                lineas.append(f"- `{ruta}`")
    if total == 0:
        lineas.append("(ninguno)")

    (dest / "report.md").write_text("\n".join(lineas) + "\n", encoding="utf-8")


def escribir_listas(
    dest: Path,
    resultados: dict[str, dict],
    bloqueados_extra: list[dict] | None = None,
) -> tuple[list[str], list[str]]:
    """Genera failed-only.txt y blocked-only.txt (espacio-separados)."""
    fallidos = sorted(
        p for p, e in resultados.items()
        if not e.get("ok") and not entrada_bloqueada(e)
    )
    nombres_extra = {str(b.get("pkg"))
                     for b in (bloqueados_extra or []) if b.get("pkg")}
    bloqueados = sorted(
        {p for p, e in resultados.items() if entrada_bloqueada(e)}
        | nombres_extra)
    (dest / "failed-only.txt").write_text(
        (" ".join(fallidos) + "\n") if fallidos else "", encoding="utf-8")
    (dest / "blocked-only.txt").write_text(
        (" ".join(bloqueados) + "\n") if bloqueados else "", encoding="utf-8")
    return fallidos, bloqueados


# ------------------------------------------------------------ subcomandos
def cmd_organize(args: argparse.Namespace) -> int:
    raw = Path(args.raw)
    dest = Path(args.dest)
    if not raw.is_dir():
        print(f"error: no existe el directorio RAW: {raw}", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    resultados, bloqueados_extra = fusionar_resultados(raw)
    (dest / "campaign-results.json").write_text(
        json.dumps({"packages": resultados, "blocked": bloqueados_extra},
                   indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")

    manifiesto = recolectar_xbps(raw, dest, resultados)
    (dest / "manifest.json").write_text(
        json.dumps(manifiesto, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")

    logs_n = copiar_logs(raw, dest)
    escribir_informe(dest, resultados, manifiesto, logs_n, args.run,
                     bloqueados_extra)
    fallidos, bloqueados = escribir_listas(dest, resultados, bloqueados_extra)

    if not args.keep_raw:
        shutil.rmtree(raw)

    ok = sum(1 for e in resultados.values() if e.get("ok"))
    bloq_detect = sum(1 for e in resultados.values() if entrada_bloqueada(e))
    n_xbps = sum(len(rs) for pkg in manifiesto.values() for rs in pkg.values())
    print(f"paquetes procesados: {len(resultados)} "
          f"(OK: {ok}, fallo: {len(fallidos)}, "
          f"bloqueados: {bloq_detect}+{len(bloqueados_extra)} pool)")
    print(f".xbps organizados: {n_xbps} | logs copiados: {logs_n}")
    print(f"informe: {dest / 'report.md'}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    ruta_resultados = d / "campaign-results.json"
    if not ruta_resultados.is_file():
        print(f"error: falta {ruta_resultados}; ejecuta antes 'organize'",
              file=sys.stderr)
        return 1
    datos = json.loads(ruta_resultados.read_text(encoding="utf-8"))
    if isinstance(datos, dict) and "packages" in datos:
        resultados: dict[str, dict] = datos.get("packages") or {}
        bloqueados_extra: list[dict] = datos.get("blocked") or []
    else:  # formato legacy (dict plano pkg→entrada)
        resultados = datos
        bloqueados_extra = []

    manifiesto: dict[str, dict[str, list[str]]] = {}
    ruta_manifest = d / "manifest.json"
    if ruta_manifest.is_file():
        manifiesto = json.loads(ruta_manifest.read_text(encoding="utf-8"))

    ok = sum(1 for e in resultados.values() if e.get("ok"))
    bloqueados = sum(1 for e in resultados.values() if entrada_bloqueada(e))
    print(f"== campaña: {d}")
    print(f"paquetes: {len(resultados)} | OK: {ok} | "
          f"fallo: {len(resultados) - ok - bloqueados} | "
          f"bloqueados: {bloqueados} (+{len(bloqueados_extra)} pool)")

    causas = _causas_raiz(resultados)
    if causas:
        print("top causas raíz:")
        for i, (causa, n) in enumerate(causas.most_common(10), 1):
            print(f"  {i:>2}. [{n:>3}] {causa}")

    sin_artefacto = sorted(p for p in resultados if not (manifiesto.get(p) or {}))
    if sin_artefacto:
        print(f"sin ningún .xbps organizado ({len(sin_artefacto)}): "
              + " ".join(sin_artefacto))

    raiz_xbps = d / "xbps"
    if raiz_xbps.is_dir():
        partes = []
        for motor_dir in sorted(p for p in raiz_xbps.iterdir() if p.is_dir()):
            bytes_tot = sum(f.stat().st_size
                            for f in motor_dir.rglob("*") if f.is_file())
            partes.append(f"{motor_dir.name}={bytes_tot / (1024 * 1024):.2f} MB")
        if partes:
            print("tamaño .xbps por motor: " + ", ".join(partes))
    return 0


# ------------------------------------------------------------------- CLI
def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="campaign-report.py",
        description="Consolida y analiza resultados de campañas masivas remotas.")
    sub = parser.add_subparsers(dest="comando", required=True)

    p_org = sub.add_parser(
        "organize", help="fusiona shards y organiza xbps/logs/informes")
    p_org.add_argument("--raw", required=True,
                       help="directorio con los artefactos descargados (_raw)")
    p_org.add_argument("--dest", required=True,
                       help="directorio de destino organizado")
    p_org.add_argument("--keep-raw", action="store_true",
                       help="no borrar el directorio _raw al terminar")
    p_org.add_argument("--run", default=None,
                       help="identificador del run de GitHub (para el informe)")
    p_org.set_defaults(func=cmd_organize)

    p_an = sub.add_parser(
        "analyze", help="resumen compacto de un directorio ya organizado")
    p_an.add_argument("--dir", required=True,
                      help="directorio organizado (con campaign-results.json)")
    p_an.set_defaults(func=cmd_analyze)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
