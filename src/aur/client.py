# SPDX-License-Identifier: GPL-3.0-or-later
"""Cliente AUR RPC v5 — httpx + caché SQLite + batch 200/4443B + rate 4000/día + backoff.

Endpoints: /rpc/v5/info?arg[]=pkg (multiinfo), /rpc/v5/search/keyword?by=field
Límites: URI 4443 bytes (Nginx HTTP/2), search ≥2 chars y <5000 hits, 4000 req/día/IP.
Sync local: https://aur.archlinux.org/packages.gz con ETag/Last-Modified.
"""
from __future__ import annotations
import gzip
import io
import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

import httpx

from src.common.paths import DEFAULT_DB

AUR_RPC = "https://aur.archlinux.org/rpc/v5"
AUR_PACKAGES_GZ = "https://aur.archlinux.org/packages.gz"
MAX_URI_BYTES = 4443
MAX_BATCH = 200
MAX_SEARCH_HITS = 5000
RATE_LIMIT_PER_DAY = 4000


class RateLimitError(RuntimeError):
    pass


DB_BUSY_RETRIES = 5


class AURClient:
    def __init__(self, db_path: str | Path = DEFAULT_DB, timeout: float = 20.0,
                 max_retries: int = 4, daily_limit: int = RATE_LIMIT_PER_DAY,
                 offline: bool = False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._init_db()
        self.timeout = timeout
        self.max_retries = max_retries
        self.daily_limit = daily_limit
        self.offline = offline
        self.req_count = 0

    # ---------- SQLite ----------
    def _connect(self) -> sqlite3.Connection:
        """Conexión endurecida (H-1.1): WAL permite lectores concurrentes con
        escritura; busy_timeout absorbe locks breves de otros procesos
        (vouru + CLI comparten cache.db)."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _db_write(self, fn, *args, **kw):
        """Reintento con backoff ante SQLITE_BUSY/locked (H-1.1): la ventana
        residual que busy_timeout no cubre no debe abortar el pipeline."""
        last: Exception | None = None
        for attempt in range(DB_BUSY_RETRIES):
            try:
                return fn(*args, **kw)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                last = e
                time.sleep(0.05 * (2 ** attempt))
        raise last  # type: ignore[misc]



    # ---------- SQLite ----------
    def _init_db(self):
        cur = self._conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS rpc_cache (
            url TEXT PRIMARY KEY,
            method TEXT NOT NULL,
            response TEXT NOT NULL,
            etag TEXT,
            last_modified TEXT,
            fetched_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS packages_index (
            pkgname TEXT PRIMARY KEY,
            pkgbase TEXT,
            version TEXT,
            description TEXT,
            maintainer TEXT,
            outofdate INTEGER,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rpc_cache_fetched
            ON rpc_cache(fetched_at);
        """)
        self._db_write(self._gc)
        self._conn.commit()

    def _gc(self, cache_ttl: int = 48 * 3600, counter_keep_days: int = 7):
        """Recolector de basura (H-1.1): purga respuestas RPC caducadas y
        contadores diarios antiguos; sin esto cache.db crece sin límite y las
        búsquedas degradan. Indexado vía idx_rpc_cache_fetched → barato."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM rpc_cache WHERE fetched_at < ?",
                    (time.time() - cache_ttl,))
        # reqcount_YYYY-MM-DD: comparación lexicográfica válida para ISO
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - counter_keep_days * 86400))
        cur.execute("DELETE FROM meta WHERE key LIKE 'reqcount_%' AND substr(key, 10) < ?",
                    (cutoff,))
        # respuestas vacías envenenadas (paquete borrado de AUR vuelve a
        # existir, AUR parpadea): jamás servir 0-resultados desde caché
        cur.execute(
            "DELETE FROM rpc_cache WHERE method='GET' AND response LIKE '%\"resultcount\": 0%'")

    def _cache_get(self, url: str) -> Optional[Tuple[dict, str | None, str | None]]:
        cur = self._conn.execute(
            "SELECT response, etag, last_modified FROM rpc_cache WHERE url=? AND method='GET'", (url,))
        row = cur.fetchone()
        if row:
            return json.loads(row[0]), row[1], row[2]
        return None

    def _cache_put(self, url: str, data: dict, etag: str | None, lastmod: str | None):
        def _w():
            self._conn.execute(
                "INSERT OR REPLACE INTO rpc_cache(url,method,response,etag,last_modified,fetched_at) "
                "VALUES (?,'GET',?,?,?,?)",
                (url, json.dumps(data), etag, lastmod, time.time()))
            self._conn.commit()
        self._db_write(_w)

    def _counter_today(self) -> int:
        today = time.strftime("%Y-%m-%d")
        cur = self._conn.execute("SELECT value FROM meta WHERE key='reqcount_'||?", (today,))
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def _bump_counter(self):
        today = time.strftime("%Y-%m-%d")
        n = self._counter_today() + 1

        def _w():
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (f"reqcount_{today}", str(n)))
            self._conn.commit()
        self._db_write(_w)
        self.req_count = n
        if n > self.daily_limit:
            raise RateLimitError(f"Límite {self.daily_limit} req/día excedido ({n})")

    # ---------- HTTP ----------
    def _get_json(self, path: str, params: list | dict) -> dict:
        query = urlencode(params, doseq=True)
        url = f"{AUR_RPC}{path}?{query}"
        if len(url.encode()) > MAX_URI_BYTES:
            raise ValueError(f"URI excede {MAX_URI_BYTES} bytes: {len(url.encode())}")

        cached = self._cache_get(url)
        if self.offline:
            # Modo offline: SOLO caché local; nunca red, no consume rate-limit
            if cached:
                return cached[0]
            raise ConnectionError(
                f"offline: sin caché para {url}; ejecuta sync online primero")

        headers = {}
        if cached:
            _, etag, lastmod = cached
            if etag:
                headers["If-None-Match"] = etag
            if lastmod:
                headers["If-Modified-Since"] = lastmod

        attempt = 0
        while True:
            attempt += 1
            self._bump_counter()
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.get(url, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt <= self.max_retries:
                    time.sleep(min(60.0, 2 ** attempt))  # 2,4,8,16... máx 60s
                    continue
                break
            except httpx.TransportError:
                if attempt > self.max_retries:
                    raise
                time.sleep(min(60.0, 2 ** attempt))

        if resp.status_code == 304 and cached:
            return cached[0]
        resp.raise_for_status()
        data = resp.json()
        # NO cachear 0-resultados: un paquete borrado/parpadeo de AUR quedaría
        # "no existe" para siempre (caso yazi-bin, ago 2026). Reintentos
        # posteriores golpean red y recuperan el estado real.
        if isinstance(data.get("results"), list) and not data["results"]:
            return data
        self._cache_put(url, data, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
        return data

    # ---------- API pública ----------
    def info(self, pkgs: List[str]) -> List[Dict]:
        """Multiinfo en lotes ≤200 pkgs y ≤4443 bytes de URI."""
        results: List[Dict] = []
        batch: List[str] = []
        for pkg in pkgs:
            cand = batch + [pkg]
            q = urlencode([("arg[]", p) for p in cand], doseq=True)
            url_len = len(f"{AUR_RPC}/info?{q}".encode())
            if len(cand) > MAX_BATCH or url_len > MAX_URI_BYTES:
                if batch:
                    results.extend(self._info_batch(batch))
                    batch = []
                single_len = len(f"{AUR_RPC}/info?{urlencode([('arg[]', pkg)], doseq=True)}".encode())
                if single_len > MAX_URI_BYTES:
                    raise ValueError(f"pkg name demasiado largo para URI: {pkg}")
            batch.append(pkg)
        if batch:
            results.extend(self._info_batch(batch))
        return results

    def _info_batch(self, batch: List[str]) -> List[Dict]:
        data = self._get_json("/info", [("arg[]", p) for p in batch])
        if data.get("type") == "error":
            raise RuntimeError(f"AUR error: {data.get('error')}")
        if data.get("type") != "multiinfo":
            raise RuntimeError(f"Tipo inesperado: {data.get('type')}")
        return data.get("results", [])

    def search(self, keyword: str, by: str = "name-desc") -> List[Dict]:
        if len(keyword) < 2:
            raise ValueError("Search requiere ≥2 chars")
        data = self._get_json(f"/search/{quote(keyword, safe='')}", {"by": by})
        if data.get("type") == "error":
            raise RuntimeError(f"AUR search error: {data.get('error')}")
        results = data.get("results", [])
        if len(results) >= MAX_SEARCH_HITS:
            raise RuntimeError("Search rechaza ≥5000 hits")
        return results

    def info_one(self, pkg: str) -> Optional[Dict]:
        res = self.info([pkg])
        return res[0] if res else None

    # ---------- Sync local packages.gz ----------
    def sync_packages_index(self, force: bool = False, max_age: int = 300) -> int:
        """Descarga packages.gz si el índice local tiene >max_age segundos.
        En modo offline usa solo el índice existente. Retorna nº de paquetes."""
        cur = self._conn.execute("SELECT value FROM meta WHERE key='index_updated_at'")
        row = cur.fetchone()
        if self.offline:
            cur = self._conn.execute("SELECT COUNT(*) FROM packages_index")
            return cur.fetchone()[0]
        if not force and row and (time.time() - float(row[0])) < max_age:
            cur = self._conn.execute("SELECT COUNT(*) FROM packages_index")
            return cur.fetchone()[0]

        headers = {}
        cur = self._conn.execute("SELECT value FROM meta WHERE key='packages_gz_etag'")
        r = cur.fetchone()
        if r:
            headers["If-None-Match"] = r[0]

        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(AUR_PACKAGES_GZ, headers=headers)
        if resp.status_code == 304:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('index_updated_at',?)",
                (str(time.time()),))
            self._conn.commit()
            cur = self._conn.execute("SELECT COUNT(*) FROM packages_index")
            return cur.fetchone()[0]
        resp.raise_for_status()

        raw = gzip.decompress(resp.content) if resp.content[:2] == b"\x1f\x8b" else resp.content
        count = 0
        now = time.time()
        self._conn.execute("DELETE FROM packages_index")
        for line in raw.decode("utf-8", errors="replace").splitlines():
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            # packages.gz: un pkgname por línea
            self._conn.execute(
                "INSERT OR REPLACE INTO packages_index VALUES (?,?,?,?,?,?,?)",
                (name, name, "", "", None, 0, now))
            count += 1
        etag = resp.headers.get("ETag")
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('packages_gz_etag',?)", (etag,))
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('index_updated_at',?)",
            (str(time.time()),))
        self._conn.commit()
        return count

    def lookup_local(self, name: str) -> Optional[Dict]:
        cur = self._conn.execute(
            "SELECT pkgname,pkgbase,version,description,maintainer,outofdate "
            "FROM packages_index WHERE pkgname=?", (name,))
        row = cur.fetchone()
        if not row:
            return None
        keys = ["pkgname", "pkgbase", "version", "description", "maintainer", "outofdate"]
        return dict(zip(keys, row))

    def close(self):
        self._conn.close()


if __name__ == "__main__":
    import sys
    c = AURClient()
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        print(f"indexados: {c.sync_packages_index(force=True)}")
    elif len(sys.argv) > 1:
        for r in c.info(sys.argv[1:]):
            print(r["Name"], r["Version"])
