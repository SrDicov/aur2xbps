#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Servidor HTTP(S) del repositorio local firmado — configuración vía config.

Endurecido (AUDIT-2026-08 H-5.1):
- timeout de socket por conexión (mitiga slowloris sobre ThreadingHTTPServer)
- guard anti-traversal: symlinks dentro del docroot que escapen a rutas
  externas (ej. privkey.pem) responden 404, jamás se sirve fuera de la raíz
- TLS con mínimo TLSv1.2 cuando --tls
- warning explícito si el bind no es loopback

Uso: serve-repo.py [--http] [--host HOST] [--port PORT] [--docroot DIR]
Defaults desde src/common/config.py (host/port del TOML/env).
"""
from __future__ import annotations

import argparse
import functools
import http.server
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.config import get_config


class RepoHandler(http.server.SimpleHTTPRequestHandler):
    """Handler endurecido para servir el repo de paquetes."""

    # Slowloris: conexiones que gotean bytes mueren a los 30s en vez de
    # agotar el pool de threads indefinidamente.
    timeout = 30

    def __init__(self, *a, docroot: str | None = None, **kw):
        self._docroot = Path(docroot).resolve() if docroot else None
        if self._docroot is not None:
            kw["directory"] = str(self._docroot)   # servir DESDE el docroot
        super().__init__(*a, **kw)

    def translate_path(self, path: str) -> str:
        """Resuelve la petición y NUNCA devuelve rutas fuera del docroot:
        encadenar symlinks dentro de repo_dir no puede exponer ficheros
        externos (claves, configs del host)."""
        real = Path(super().translate_path(path)).resolve()
        if self._docroot is not None:
            try:
                real.relative_to(self._docroot)
            except ValueError:
                return str(self._docroot / ".inexistente-bloqueado-por-seguridad")
        return str(real)

    def end_headers(self):   # cabeceras mínimas de higiene
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def main() -> int:
    cfg = get_config()
    ap = argparse.ArgumentParser(prog="serve-repo")
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--docroot", default=str(cfg.repo_dir))
    ap.add_argument("--tls", action="store_true",
                    help="habilita TLS con cert/key de AUR2XBPS_TLS_CERT/KEY")
    args = ap.parse_args()

    handler = functools.partial(RepoHandler, docroot=args.docroot)
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    if args.tls:
        import os
        cert = os.environ.get("AUR2XBPS_TLS_CERT", str(get_config().keys_dir / "https" / "cert.pem"))
        key = os.environ.get("AUR2XBPS_TLS_KEY", str(get_config().keys_dir / "https" / "key.pem"))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2   # H-5.1: sin degradación
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"aur2xbps repo sirviendo {args.docroot} en "
          f"{'https' if args.tls else 'http'}://{args.host}:{args.port}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: bind NO-loopback; expón solo tras proxy validado "
              "(nginx/lighttpd) o VPN — este servidor es de desarrollo",
              file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
