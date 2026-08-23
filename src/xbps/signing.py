# SPDX-License-Identifier: GPL-3.0-or-later
"""Firma XBPS — xbps-rindex --sign con privkey desde primer repo.

Genera par RSA si no existe.
"""
from __future__ import annotations
import subprocess
from pathlib import Path

def generate_keypair(priv: Path, pub: Path | None = None, bits: int = 4096):
    priv = Path(priv)
    if pub is None:
        # default: pubkey junto a la privada (clientes del repo la necesitan)
        pub = priv.parent / "pubkey.pem"
    pub = Path(pub)
    priv.parent.mkdir(parents=True, exist_ok=True)
    pub.parent.mkdir(parents=True, exist_ok=True)
    if not priv.exists():
        # xbps usa pem RSA; puede generarse con openssl
        subprocess.run(["openssl", "genrsa", "-out", str(priv), str(bits)], check=True)
        subprocess.run(["chmod", "600", str(priv)], check=True)
    if not pub.exists():
        # extraer pubkey también si solo existía la privada
        subprocess.run(["openssl", "rsa", "-in", str(priv), "-pubout", "-out", str(pub)], check=True)
        print(f"pubkey generada: {pub}")
    return priv

def sign_repo(repo_dir: Path, privkey: Path, signedby: str = "aur2xbps <aur2xbps@local>"):
    repo_dir = Path(repo_dir)
    from src.xbps.builder import XBPS_RINDEX
    subprocess.run([XBPS_RINDEX(), "--privkey", str(privkey), "--signedby", signedby, "--sign", str(repo_dir)], check=True)

if __name__ == "__main__":
    import sys
    from src.common.paths import PRIVKEY
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else PRIVKEY
    generate_keypair(p)
    print(f"key {p}")
