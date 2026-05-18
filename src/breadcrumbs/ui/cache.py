import os
import sqlite3
import threading
import asyncio
import time
import httpx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Cache configuration
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".edgetx_map_cache")
DB_PATH = os.path.join(CACHE_DIR, "cache.db")
MAX_CACHE_BYTES = 500 * 1024 * 1024  # 500 MB

os.makedirs(CACHE_DIR, exist_ok=True)

# Per-thread SQLite connections. SQLite forbids sharing a connection across
# threads without check_same_thread=False; ThreadingHTTPServer hands each
# request to a fresh thread, so opening a new connection per request (the
# previous approach) added noticeable overhead on cache hits. A thread-local
# keeps each worker's connection alive for the life of that thread.
_tls = threading.local()
_db_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _tls.conn = conn
    return conn


# Shared async infrastructure
_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()

_HEADERS = {
    'User-Agent': 'Breadcrumbs_EdgeTX_LogViewer',
    'Referer': 'https://github.com/ThaumielSparrow/breadcrumbs'
}

_client = httpx.AsyncClient(
    http2=True,
    headers=_HEADERS,
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
    timeout=httpx.Timeout(10.0)
)


def init_db():
    # Bootstrap connection — runs on the importing thread and is not stored in
    # the TLS slot for the request workers.
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tiles (
                url_key TEXT PRIMARY KEY,
                file_path TEXT,
                size INTEGER,
                last_access REAL
            )
        ''')
    finally:
        conn.close()


init_db()


class TileProxyHandler(BaseHTTPRequestHandler):
    # Suppress console spam from HTTP requests
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        try:
            # Expected path: /{provider}/{z}/{x}/{y}.png
            parts = self.path.strip("/").split("/")
            if len(parts) != 4:
                self.send_error(400, "Bad Request")
                return

            provider, z, x, y_ext = parts
            y = y_ext.split('.')[0]

            # Map the provider to the real URL
            if provider == "osm":
                real_url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            elif provider == "esri":
                real_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            else:
                self.send_error(404, "Unknown Provider")
                return

            url_key = f"{provider}/{z}/{x}/{y}"
            tile_data = self.get_from_cache(url_key)

            if not tile_data:
                # Bridge async fetch onto the shared event loop, block this handler thread until coroutine completes
                future = asyncio.run_coroutine_threadsafe(
                    self.fetch_and_cache(real_url, url_key),
                    _loop,
                )
                tile_data = future.result()

            if tile_data:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(tile_data)
            else:
                self.send_error(502, "Bad Gateway - Could not fetch tile")

        except Exception as e:
            self.send_error(500, str(e))

    def get_from_cache(self, url_key):
        conn = _get_conn()
        row = conn.execute(
            "SELECT file_path FROM tiles WHERE url_key=?", (url_key,)
        ).fetchone()
        if not row:
            return None

        file_path = row[0]
        # Cheap write — WAL keeps readers unblocked. Serialize writers to
        # avoid 'database is locked' surprises on bursty playback.
        with _db_write_lock:
            conn.execute(
                "UPDATE tiles SET last_access=? WHERE url_key=?",
                (time.time(), url_key),
            )

        try:
            with open(file_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            # File was deleted manually outside the DB, remove from DB
            with _db_write_lock:
                conn.execute("DELETE FROM tiles WHERE url_key=?", (url_key,))
            return None

    async def fetch_and_cache(self, real_url, url_key):
        try:
            response = await _client.get(real_url)
            response.raise_for_status()
            tile_data = response.content

            file_path = os.path.join(CACHE_DIR, url_key.replace("/", "_") + ".png")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            with open(file_path, "wb") as f:
                f.write(tile_data)

            size = len(tile_data)

            conn = _get_conn()
            with _db_write_lock:
                conn.execute(
                    "INSERT OR REPLACE INTO tiles (url_key, file_path, size, last_access) VALUES (?, ?, ?, ?)",
                    (url_key, file_path, size, time.time()),
                )

            self.enforce_lru()
            return tile_data

        except httpx.HTTPStatusError as e:
            print(f"Tile server error {e.response.status_code} for {real_url}")
            return None
        except httpx.RequestError as e:
            print(f"Failed to fetch tile {real_url}: {e}")
            return None

    def enforce_lru(self):
        conn = _get_conn()
        total_size = conn.execute("SELECT SUM(size) FROM tiles").fetchone()[0] or 0
        if total_size <= MAX_CACHE_BYTES:
            return

        oldest = conn.execute(
            "SELECT url_key, file_path FROM tiles ORDER BY last_access ASC LIMIT 50"
        ).fetchall()
        with _db_write_lock:
            for key, path in oldest:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
                conn.execute("DELETE FROM tiles WHERE url_key=?", (key,))


def start_cache_server():
    # port=0 tells the OS to automatically assign an available free port
    server = ThreadingHTTPServer(("127.0.0.1", 0), TileProxyHandler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, port


def shutdown_cache_server(server):
    """Graceful teardown of the local tile proxy. Safe to call from any thread."""
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass
    # Close the shared httpx client and stop the asyncio loop.
    try:
        fut = asyncio.run_coroutine_threadsafe(_client.aclose(), _loop)
        fut.result(timeout=2.0)
    except Exception:
        pass
    try:
        _loop.call_soon_threadsafe(_loop.stop)
    except Exception:
        pass
