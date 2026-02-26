# src/core/edgetx_track.py
import csv
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .parser import parse_gps_field


def _parse_dt(date_s: str, time_s: str) -> Optional[datetime]:
    if not date_s or not time_s:
        return None
    dt_str = f"{date_s.strip()} {time_s.strip()}"
    fmts = (
        "%Y-%m-%d %H:%M:%S.%f",
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
    for k in ("Alt", "Alt(m)", "Altitude", "Alt (m)"):
        if k in col_map:
            return k
    for k in col_map.keys():
        if k.strip().lower().startswith("alt"):
            return k
    return None


@dataclass(frozen=True)
class TrackPoint:
    t: Optional[datetime]
    lat: float
    lon: float
    alt_m: Optional[float]


def load_track(csv_path: str, max_points: int = 10000) -> List[TrackPoint]:
    """
    Loads GPS track points from a single EdgeTX CSV file.
    Downsamples by stride if > max_points to keep UI fast.
    """
    with open(csv_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []

        col_map = {name.strip(): idx for idx, name in enumerate(header)}
        if "GPS" not in col_map:
            return []

        gps_idx = col_map["GPS"]
        alt_key = _find_alt_col(col_map)
        alt_idx = col_map.get(alt_key) if alt_key else None

        date_idx = col_map.get("Date")
        time_idx = col_map.get("Time")

        points: List[TrackPoint] = []
        for row in reader:
            if not row or len(row) <= gps_idx:
                continue
            coords = parse_gps_field(row[gps_idx])
            if not coords:
                continue
            lat, lon = coords

            alt_m = None
            if alt_idx is not None and alt_idx < len(row):
                try:
                    alt_m = float(row[alt_idx])
                except Exception:
                    alt_m = None

            t = None
            if date_idx is not None and time_idx is not None and date_idx < len(row) and time_idx < len(row):
                t = _parse_dt(row[date_idx], row[time_idx])

            points.append(TrackPoint(t=t, lat=lat, lon=lon, alt_m=alt_m))

    if len(points) <= max_points:
        return points

    # stride downsample to keep the map UI responsive
    step = max(1, len(points) // max_points)
    slim = points[::step]
    if slim[-1] != points[-1]:
        slim.append(points[-1])
    return slim