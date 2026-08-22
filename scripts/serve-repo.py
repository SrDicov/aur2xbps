#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Servidor HTTP(S) del repositorio local firmado — configuración vía config.

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


def main() -> int:
    cfg = get_config()
    ap = argparse.ArgumentParser(prog="serve-repo")
    ap.add_argument("--host", default=cfg.host)
    ap.add_argument("--port", type=int, default=cfg.port)
    ap.add_argument("--docroot", default=str(cfg.repo_dir))
    ap.add_argument("--tls", action="store_true",
                    help="habilita TLS con cert/key de AUR2XBPS_TLS_CERT/KEY")
    args = ap.parse_args()

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=args.docroot)
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    if args.tls:
        import os
        cert = os.environ.get("AUR2XBPS_TLS_CERT", str(get_config().keys_dir / "https" / "cert.pem"))
        key = os.environ.get("AUR2XBPS_TLS_KEY", str(get_config().keys_dir / "https" / "key.pem"))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"aur2xbps repo sirviendo {args.docroot} en "
          f"{'https' if args.tls else 'http'}://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
