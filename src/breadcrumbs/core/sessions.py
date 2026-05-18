import csv
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from breadcrumbs.core.parser import parse_gps_field


def _rev_tail(filename: str, read_bytes: int = 128 * 1024) -> List[str]:
    size = os.path.getsize(filename)
    if size <= 0:
        return []
    with open(filename, "rb") as f:
        start = max(0, size - read_bytes)
        f.seek(start)
        data = f.read()
    text = data.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    # drop partial first line if we started mid-file
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def _parse_dt(date_s: str, time_s: str) -> Optional[datetime]:
    if not date_s or not time_s:
        return None
    dt_str = f"{date_s.strip()} {time_s.strip()}"

    fmts = (
        "%Y-%m-%d %H:%M:%S.%f",  # your sample
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(dt_str, fmt)
        except Exception:
            pass
    return None


def _find_alt_col(col_map: Dict[str, int]) -> Optional[str]:
    # exact common matches
    for k in ("Alt", "Alt(m)", "Altitude", "Alt (m)"):
        if k in col_map:
            return k
    # fallback: first column starting with "Alt"
    for k in col_map.keys():
        if k.strip().lower().startswith("alt"):
            return k
    return None


@dataclass(frozen=True)
class SessionMeta:
    file_path: str
    day_key: str  # "YYYY-MM-DD"
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    has_alt: bool

    def label(self) -> str:
        base = os.path.basename(self.file_path)
        st = self.start_time.strftime("%H:%M:%S") if self.start_time else "?"
        et = self.end_time.strftime("%H:%M:%S") if self.end_time else "?"
        alt_flag = " (Alt)" if self.has_alt else ""

        dur = ""
        if self.start_time and self.end_time:
            secs = (self.end_time - self.start_time).total_seconds()
            if secs > 0:
                s = int(secs)
                if s >= 3600:
                    dur = f" · {s // 3600}h{(s % 3600) // 60:02d}m"
                else:
                    dur = f" · {s // 60}m {s % 60:02d}s"

        return f"[{st}–{et}]{dur}{alt_flag}  {base}"


def scan_one_log(csv_path: str) -> Optional[SessionMeta]:
    # Single text open: header + first data row in one pass.
    day_key: Optional[str] = None
    start_time: Optional[datetime] = None
    has_alt = False
    gps_idx: Optional[int] = None
    date_idx: Optional[int] = None
    time_idx: Optional[int] = None

    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return None

            col_map = {name.strip(): idx for idx, name in enumerate(header)}
            if "GPS" not in col_map:
                return None

            gps_idx = col_map["GPS"]
            has_alt = _find_alt_col(col_map) is not None
            date_idx = col_map.get("Date")
            time_idx = col_map.get("Time")

            for row in reader:
                if not row:
                    continue
                if date_idx is not None and date_idx < len(row):
                    day_key = row[date_idx].strip() or None
                if (
                    date_idx is not None and time_idx is not None
                    and date_idx < len(row) and time_idx < len(row)
                ):
                    start_time = _parse_dt(row[date_idx], row[time_idx])
                break
    except Exception:
        return None

    if not day_key:
        day_key = datetime.fromtimestamp(os.path.getmtime(csv_path)).strftime("%Y-%m-%d")

    # Tail scan (binary, separate open by necessity — seeks to end of file).
    end_time: Optional[datetime] = None
    try:
        for line in reversed(_rev_tail(csv_path, read_bytes=256 * 1024)):
            try:
                row = next(csv.reader([line]))
            except Exception:
                continue
            if len(row) <= gps_idx:
                continue
            if parse_gps_field(row[gps_idx]) is None:
                continue
            if (
                date_idx is not None and time_idx is not None
                and date_idx < len(row) and time_idx < len(row)
            ):
                end_time = _parse_dt(row[date_idx], row[time_idx])
            break
    except Exception:
        pass

    return SessionMeta(
        file_path=csv_path,
        day_key=day_key,
        start_time=start_time,
        end_time=end_time,
        has_alt=has_alt,
    )


def scan_logs_dir(
    logs_dir: str,
    *,
    max_workers: int = 8,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, List[SessionMeta]]:
    """Index a folder of EdgeTX CSV logs, grouped by day.

    `progress(done, total)` is called once per scanned file from worker threads
    — wire it to a Qt signal's `.emit` to surface progress on the main thread.
    """
    csv_paths = [
        os.path.join(logs_dir, name)
        for name in os.listdir(logs_dir)
        if name.lower().endswith(".csv")
    ]
    sessions_by_day: Dict[str, List[SessionMeta]] = {}
    total = len(csv_paths)
    if total == 0:
        return sessions_by_day

    if total <= max_workers:
        # Skip pool spin-up overhead for tiny folders — it can dominate.
        for done, p in enumerate(csv_paths, 1):
            meta = scan_one_log(p)
            if meta:
                sessions_by_day.setdefault(meta.day_key, []).append(meta)
            if progress is not None:
                progress(done, total)
    else:
        # File I/O dominates here — threads release the GIL during open/read,
        # so a modest pool gives a real 3-6x speedup on cold caches.
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(scan_one_log, p): p for p in csv_paths}
            for done, fut in enumerate(as_completed(futures), 1):
                meta = fut.result()
                if meta:
                    sessions_by_day.setdefault(meta.day_key, []).append(meta)
                if progress is not None:
                    progress(done, total)

    # Sort sessions within each day by start_time (fallback to mtime)
    for metas in sessions_by_day.values():
        metas.sort(
            key=lambda m: m.start_time
            or datetime.fromtimestamp(os.path.getmtime(m.file_path))
        )

    return sessions_by_day
