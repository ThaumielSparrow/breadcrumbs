# src/core/load.py
import csv
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

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


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except Exception:
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


def _find_speed_col(col_map: Dict[str, int]) -> Optional[str]:
    # common EdgeTX GPS speed name
    for k in ("GSpd(kmh)", "GSpd", "Speed(kmh)", "Speed"):
        if k in col_map:
            return k
    # fallback: anything that looks like speed
    for k in col_map.keys():
        lk = k.strip().lower()
        if lk.startswith("gspd") or "speed" in lk:
            return k
    return None


def _find_rssi_cols(col_map: Dict[str, int]) -> List[int]:
    """
    Prefer explicit 'RSSI' if present; otherwise use common ELRS/EdgeTX columns:
      1RSS(dB), 2RSS(dB), TRSS(dB)
    """
    if "RSSI" in col_map:
        return [col_map["RSSI"]]

    keys = list(col_map.keys())
    idxs: List[int] = []
    for pref in ("1RSS", "2RSS", "TRSS"):
        for k in keys:
            if k.upper().startswith(pref):
                idxs.append(col_map[k])

    # unique preserve order
    out: List[int] = []
    seen = set()
    for i in idxs:
        if i not in seen:
            out.append(i)
            seen.add(i)
    return out


def _combine_rssi(values: List[Optional[float]]) -> Optional[float]:
    """
    For dB-like RSSI columns, 0 is often a 'not present' placeholder.
    Use the strongest (highest / least-negative) available value.
    """
    cleaned = []
    for v in values:
        if v is None:
            continue
        if abs(v) < 1e-9:  # treat 0 as missing in dB RSSI context
            continue
        cleaned.append(v)
    if not cleaned:
        return None
    return max(cleaned)


@dataclass(frozen=True)
class TrackPoint:
    t: Optional[datetime]
    lat: float
    lon: float
    alt_m: Optional[float]
    speed_kmh: Optional[float]
    rssi_db: Optional[float]


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
        date_idx = col_map.get("Date")
        time_idx = col_map.get("Time")

        alt_key = _find_alt_col(col_map)
        alt_idx = col_map.get(alt_key) if alt_key else None

        spd_key = _find_speed_col(col_map)
        spd_idx = col_map.get(spd_key) if spd_key else None

        rssi_idxs = _find_rssi_cols(col_map)

        points: List[TrackPoint] = []
        for row in reader:
            if not row or len(row) <= gps_idx:
                continue

            coords = parse_gps_field(row[gps_idx])
            if not coords:
                continue
            lat, lon = coords

            t = None
            if date_idx is not None and time_idx is not None and date_idx < len(row) and time_idx < len(row):
                t = _parse_dt(row[date_idx], row[time_idx])

            alt_m = None
            if alt_idx is not None and alt_idx < len(row):
                alt_m = _parse_float(row[alt_idx])

            speed_kmh = None
            if spd_idx is not None and spd_idx < len(row):
                speed_kmh = _parse_float(row[spd_idx])

            rssi_db = None
            if rssi_idxs:
                vals = []
                for i in rssi_idxs:
                    vals.append(_parse_float(row[i]) if i < len(row) else None)
                rssi_db = _combine_rssi(vals)

            points.append(
                TrackPoint(
                    t=t,
                    lat=lat,
                    lon=lon,
                    alt_m=alt_m,
                    speed_kmh=speed_kmh,
                    rssi_db=rssi_db,
                )
            )

    if len(points) <= max_points:
        return points

    step = max(1, len(points) // max_points)
    slim = points[::step]
    if slim and slim[-1] != points[-1]:
        slim.append(points[-1])
    return slim