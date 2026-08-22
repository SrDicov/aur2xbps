#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Servidor HTTPS simple para repo XBPS firmado (LAN).

Uso: https-repo-server.py [puerto] [docroot] [cert] [key]
Defaults: puerto 8443, docroot <workspace>/void/repo-local,
cert/key bajo /usr/local/lib/aur2xbps (o AUR2XBPS_TLS_CERT/AUR2XBPS_TLS_KEY).
"""
import http.server
import os
import ssl
import sys

from src.common.paths import REPO_LOCAL

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
DOCROOT = sys.argv[2] if len(sys.argv) > 2 else str(REPO_LOCAL)
CERT = os.environ.get("AUR2XBPS_TLS_CERT",
                      "/usr/local/lib/aur2xbps/https/cert.pem")
KEY = os.environ.get("AUR2XBPS_TLS_KEY",
                     "/usr/local/lib/aur2xbps/https/key.pem")

os.chdir(DOCROOT)
handler = http.server.SimpleHTTPRequestHandler
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)
server = http.server.HTTPServer(("", PORT), handler)
server.socket = ctx.wrap_socket(server.socket, server_side=True)
print(f"HTTPS repo serving {DOCROOT} on :{PORT}")
server.serve_forever()
