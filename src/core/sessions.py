# src/core/sessions.py
import csv
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .parser import parse_gps_field


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
    day_key: str # "YYYY-MM-DD"
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    has_alt: bool
    last_lat: Optional[float]
    last_lon: Optional[float]
    file_size: int

    def label(self) -> str:
        base = os.path.basename(self.file_path)
        st = self.start_time.strftime("%H:%M:%S") if self.start_time else "?"
        et = self.end_time.strftime("%H:%M:%S") if self.end_time else "?"
        alt_flag = " (Alt)" if self.has_alt else ""
        return f"[{st}–{et}]{alt_flag}  {base}"


def scan_one_log(csv_path: str) -> Optional[SessionMeta]:
    # header
    try:
        with open(csv_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return None
    except Exception:
        return None

    col_map = {name.strip(): idx for idx, name in enumerate(header)}
    if "GPS" not in col_map:
        return None

    gps_idx = col_map["GPS"]
    alt_key = _find_alt_col(col_map)
    has_alt = alt_key is not None

    date_idx = col_map.get("Date")
    time_idx = col_map.get("Time")

    # first data row for day/start
    day_key: Optional[str] = None
    start_time: Optional[datetime] = None
    try:
        with open(csv_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if not row:
                    continue
                if date_idx is not None and date_idx < len(row):
                    day_key = row[date_idx].strip() or None
                if date_idx is not None and time_idx is not None and date_idx < len(row) and time_idx < len(row):
                    start_time = _parse_dt(row[date_idx], row[time_idx])
                break
    except Exception:
        pass

    if not day_key:
        day_key = datetime.fromtimestamp(os.path.getmtime(csv_path)).strftime("%Y-%m-%d")

    # tail scan for last GPS/end time
    end_time: Optional[datetime] = None
    last_lat = last_lon = None

    try:
        for line in reversed(_rev_tail(csv_path, read_bytes=256 * 1024)):
            try:
                row = next(csv.reader([line]))
            except Exception:
                continue
            if len(row) <= gps_idx:
                continue
            coords = parse_gps_field(row[gps_idx])
            if not coords:
                continue
            last_lat, last_lon = coords

            if date_idx is not None and time_idx is not None and date_idx < len(row) and time_idx < len(row):
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
        last_lat=last_lat,
        last_lon=last_lon,
        file_size=os.path.getsize(csv_path),
    )


def scan_logs_dir(logs_dir: str) -> Dict[str, List[SessionMeta]]:
    """
    Returns:
      day_key -> [SessionMeta...]
    """
    sessions_by_day: Dict[str, List[SessionMeta]] = {}

    for name in os.listdir(logs_dir):
        if not name.lower().endswith(".csv"):
            continue
        fp = os.path.join(logs_dir, name)
        meta = scan_one_log(fp)
        if not meta:
            continue
        sessions_by_day.setdefault(meta.day_key, []).append(meta)

    # sort sessions within each day by start_time (fallback to mtime)
    for day, metas in sessions_by_day.items():
        def sort_key(m: SessionMeta):
            return m.start_time or datetime.fromtimestamp(os.path.getmtime(m.file_path))
        metas.sort(key=sort_key)

    return sessions_by_day
