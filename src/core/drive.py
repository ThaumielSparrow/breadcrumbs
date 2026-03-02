# src/core/drive.py
import psutil
import os
import time
from typing import Optional

EDGE_TX_MARKERS = {"LOGS", "MODELS", "SOUNDS", "SCRIPTS"}

def is_edgetx_root(path: str) -> bool:
    try:
        entries = {e.name.upper() for e in os.scandir(path)}
    except Exception:
        return False
    return any(marker in entries for marker in EDGE_TX_MARKERS)

def find_mounted_edgetx_once() -> Optional[str]:
    """Scan mounted partitions once. Return mount point path if found."""
    for part in psutil.disk_partitions(all=False):
        mount = part.mountpoint
        if is_edgetx_root(mount):
            return mount
    return None

def wait_for_radio(timeout: Optional[float] = None, poll_interval: float = 1.0) -> Optional[str]:
    """Wait for a radio to be connected (blocking). Returns mount path or None if timeout."""
    start = time.time()
    while True:
        mp = find_mounted_edgetx_once()
        if mp:
            return mp
        if timeout is not None and (time.time() - start) > timeout:
            return None
        time.sleep(poll_interval)

if __name__ == "__main__":
    print("Looking for radio...")
    mp = wait_for_radio(timeout=30)
    print("Found:", mp)
    