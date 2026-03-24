import os
import sqlite3
import threading
import time
import requests
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Cache configuration
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".edgetx_map_cache")
DB_PATH = os.path.join(CACHE_DIR, "cache.db")
MAX_CACHE_BYTES = 500 * 1024 * 1024  # 500 MB

os.makedirs(CACHE_DIR, exist_ok=True)

# We use a lock because ThreadingHTTPServer processes multiple tile requests concurrently
db_lock = threading.Lock()

osm_connection_limit = threading.Semaphore(4)

tile_session = requests.Session()
tile_session.headers.update({
    'User-Agent': 'Breadcrumbs_EdgeTX_LogViewer',
    'Referer': 'https://github.com/ThaumielSparrow/breadcrumbs'
})

def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS tiles (
                url_key TEXT PRIMARY KEY,
                file_path TEXT,
                size INTEGER,
                last_access REAL
            )
        ''')
        conn.commit()
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
                # Switch between OSM servers to lessen load
                sub = random.choice(['a', 'b', 'c'])
                real_url = f"https://{sub}.tile.openstreetmap.org/{z}/{x}/{y}.png"
            elif provider == "esri":
                real_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            else:
                self.send_error(404, "Unknown Provider")
                return

            url_key = f"{provider}/{z}/{x}/{y}"
            tile_data = self.get_from_cache(url_key)

            if not tile_data:
                tile_data = self.fetch_and_cache(real_url, url_key)

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
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT file_path FROM tiles WHERE url_key=?", (url_key,))
            row = c.fetchone()
            
            if row:
                file_path = row[0]
                # Update last access time
                import time
                c.execute("UPDATE tiles SET last_access=? WHERE url_key=?", (time.time(), url_key))
                conn.commit()
                conn.close()
                
                try:
                    with open(file_path, "rb") as f:
                        return f.read()
                except FileNotFoundError:
                    # File was deleted manually outside the DB, remove from DB
                    conn = sqlite3.connect(DB_PATH)
                    conn.execute("DELETE FROM tiles WHERE url_key=?", (url_key,))
                    conn.commit()
                    conn.close()
                    return None
            conn.close()
        return None

    def fetch_and_cache(self, real_url, url_key):
        try:
            provider = url_key.split('/')[0]
            
            # Queue the requests 2 at a time using the semaphore
            if provider == "osm":
                with osm_connection_limit:
                    response = tile_session.get(real_url, timeout=10)
            else:
                # Burst requests on Ersi because it has a much higher rate limit
                response = tile_session.get(real_url, timeout=10)
            
            # If the server returns a 403 or 404, immediately abort so we don't cache it
            response.raise_for_status() 
            
            tile_data = response.content

            file_path = os.path.join(CACHE_DIR, url_key.replace("/", "_") + ".png")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            with open(file_path, "wb") as f:
                f.write(tile_data)

            size = len(tile_data)

            with db_lock:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO tiles (url_key, file_path, size, last_access) VALUES (?, ?, ?, ?)",
                    (url_key, file_path, size, time.time())
                )
                conn.commit()
                conn.close()
            
            self.enforce_lru()
            return tile_data
            
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch tile {real_url}: {e}")
            return None
        
    def enforce_lru(self):
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT SUM(size) FROM tiles")
            total_size = c.fetchone()[0] or 0

            if total_size > MAX_CACHE_BYTES:
                # Get the oldest 50 tiles
                c.execute("SELECT url_key, file_path FROM tiles ORDER BY last_access ASC LIMIT 50")
                oldest_tiles = c.fetchall()

                for key, path in oldest_tiles:
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass
                    c.execute("DELETE FROM tiles WHERE url_key=?", (key,))
                conn.commit()
            conn.close()

def start_cache_server():
    # port=0 tells the OS to automatically assign an available free port
    server = ThreadingHTTPServer(("127.0.0.1", 0), TileProxyHandler)
    port = server.server_address[1]
    
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    return server, port