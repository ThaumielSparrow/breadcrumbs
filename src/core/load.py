from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class TrackPoint:
    """A single telemetry sample suitable for plotting and playback."""
    t: Optional[datetime]
    lat: float
    lon: float
    alt_m: Optional[float] = None
    speed_kmh: Optional[float] = None
    rssi_db: Optional[float] = None


def _parse_float(s: object) -> Optional[float]:
    if s is None:
        return None
    try:
        st = str(s).strip()
        if st == "" or st.lower() in {"nan", "none"}:
            return None
        return float(st)
    except Exception:
        return None


def _parse_dt(date_str: str, time_str: str) -> Optional[datetime]:
    """
    EdgeTX logs often include:
      Date: YYYY-MM-DD
      Time: HH:MM:SS.mmm  (or .uuuuuu)
    """
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip()
    if not date_str or not time_str:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(f"{date_str} {time_str}", fmt)
        except Exception:
            pass
    return None


def parse_gps_field(gps_value: object) -> Optional[Tuple[float, float]]:
    """
    EdgeTX CSV GPS cell commonly looks like:
        "37.521103 -77.403145"
    Returns (lat, lon) or None.
    """
    if gps_value is None:
        return None
    s = str(gps_value).strip().strip('"')
    if not s:
        return None

    parts = s.replace(",", " ").split()
    if len(parts) < 2:
        return None

    lat = _parse_float(parts[0])
    lon = _parse_float(parts[1])
    if lat is None or lon is None:
        return None

    # Filter out invalid / uninitialized GPS
    if abs(lat) < 1e-9 and abs(lon) < 1e-9:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    return float(lat), float(lon)


def _find_col(header_map: Dict[str, int], candidates: List[str]) -> Optional[int]:
    # Exact match first
    for name in candidates:
        if name in header_map:
            return header_map[name]

    # Case-insensitive match
    lower = {k.lower(): v for k, v in header_map.items()}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _find_alt_col(header_map: Dict[str, int]) -> Optional[int]:
    return _find_col(header_map, ["Alt(m)", "Alt", "GPS Alt", "GPSAlt", "GAlt(m)"])


def _find_speed_col(header_map: Dict[str, int]) -> Optional[int]:
    return _find_col(header_map, ["GSpd(kmh)", "GSpd", "Speed(kmh)", "Speed"])


def _find_rssi_cols(header_map: Dict[str, int]) -> List[int]:
    cols: List[int] = []
    for name in ("TRSS(dB)", "1RSS(dB)", "RSSI(dB)", "RSSI"):
        idx = _find_col(header_map, [name])
        if idx is not None:
            cols.append(idx)

    # de-dup while preserving order
    seen = set()
    out = []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _combine_rssi(row: List[str], rssi_cols: List[int]) -> Optional[float]:
    vals: List[float] = []
    for idx in rssi_cols:
        if idx < 0 or idx >= len(row):
            continue
        v = _parse_float(row[idx])
        if v is None:
            continue
        # Many logs have a "0" for missing RSSI. It's safer to ignore 0 here.
        if abs(v) < 1e-12:
            continue
        vals.append(float(v))
    if not vals:
        return None
    # Prefer the "best" (highest / least negative) RSSI if multiple are present
    return max(vals)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    if lat1 == lat2 and lon1 == lon2:
        return 0.0

    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def repair_stale_gps(
    points: List[TrackPoint],
    *,
    pos_epsilon_m: float = 0.5,
    speed_threshold_kmh: float = 2.0,
    min_run_len: int = 2,
    min_anchor_dist_m: float = 2.0,
) -> List[TrackPoint]:
    """
    Repair a common EdgeTX/telemetry quirk:

      - GPS lat/lon can repeat for multiple samples (stale position),
        even while GSpd(kmh) indicates the vehicle is moving.

    This causes playback to "pause then jump" because interpolation between identical
    positions produces no motion until the next distinct position arrives.

    Strategy:
      - Detect *runs* where successive samples are within pos_epsilon_m meters.
      - If during that run the speed is >= speed_threshold_kmh AND there is a future
        sample with a meaningfully different position (>= min_anchor_dist_m),
        redistribute the duplicate samples' lat/lon by linearly interpolating
        between the last non-stale point and the next distinct point, using time
        proportion when timestamps are available (else index proportion).

    Notes:
      - Only modifies samples in the duplicate run (not the anchor points).
      - Leaves low-speed duplicates alone (so genuine hovering/stop remains a stop).
    """
    n = len(points)
    if n < 3:
        return points

    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    times = [p.t for p in points]
    speeds = [p.speed_kmh for p in points]

    def dist(i: int, j: int) -> float:
        return _haversine_m(lats[i], lons[i], lats[j], lons[j])

    changed_any = False
    i = 1
    while i < n:
        if dist(i - 1, i) <= pos_epsilon_m:
            # duplicate run starts at i (anchors are i-1 and run_end+1)
            run_start = i
            run_end = i
            while run_end + 1 < n and dist(run_end, run_end + 1) <= pos_epsilon_m:
                run_end += 1

            run_len = run_end - run_start + 1
            anchor_start = run_start - 1
            anchor_end = run_end + 1 if (run_end + 1) < n else None

            if anchor_end is None:
                break  # no future anchor to interpolate toward

            anchor_dist = dist(anchor_start, anchor_end)

            max_speed: Optional[float] = None
            for s in speeds[run_start : run_end + 1]:
                if s is None:
                    continue
                if max_speed is None or s > max_speed:
                    max_speed = float(s)

            do_repair = (
                run_len >= min_run_len
                and anchor_dist >= min_anchor_dist_m
                and max_speed is not None
                and max_speed >= speed_threshold_kmh
            )

            if do_repair:
                t0 = times[anchor_start]
                t1 = times[anchor_end]
                total_dt: Optional[float] = None
                if t0 is not None and t1 is not None:
                    dt = (t1 - t0).total_seconds()
                    if dt > 0:
                        total_dt = float(dt)

                denom = float(anchor_end - anchor_start) if (anchor_end - anchor_start) != 0 else None

                for k in range(run_start, run_end + 1):
                    # Default: index proportion
                    r = 0.0
                    if denom is not None:
                        r = (k - anchor_start) / denom

                    # Prefer time proportion when possible
                    if total_dt is not None and times[k] is not None:
                        r = (times[k] - t0).total_seconds() / total_dt

                    # Clamp in case of weird time ordering
                    if r < 0.0:
                        r = 0.0
                    elif r > 1.0:
                        r = 1.0

                    lats[k] = lats[anchor_start] + r * (lats[anchor_end] - lats[anchor_start])
                    lons[k] = lons[anchor_start] + r * (lons[anchor_end] - lons[anchor_start])

                changed_any = True

            i = run_end + 1
        else:
            i += 1

    if not changed_any:
        return points

    repaired: List[TrackPoint] = []
    for idx, p in enumerate(points):
        if p.lat != lats[idx] or p.lon != lons[idx]:
            repaired.append(
                TrackPoint(
                    t=p.t,
                    lat=float(lats[idx]),
                    lon=float(lons[idx]),
                    alt_m=p.alt_m,
                    speed_kmh=p.speed_kmh,
                    rssi_db=p.rssi_db,
                )
            )
        else:
            repaired.append(p)

    return repaired


def load_track(
    csv_path: str,
    max_points: int = 10000,
    *,
    repair_stale: bool = True,
    stale_pos_epsilon_m: float = 0.5,
    stale_speed_threshold_kmh: float = 2.0,
    stale_min_run_len: int = 2,
    stale_min_anchor_dist_m: float = 2.0,
) -> List[TrackPoint]:
    """
    Load GPS/RSSI telemetry from an EdgeTX CSV log.

    - Extracts only the data needed for plotting:
        time, lat/lon, altitude, speed, RSSI
    - Optionally repairs "stale" GPS samples (recommended) to avoid pause/jump playback.
    - Downsamples if more than max_points.

    Tuning:
    - Repair more aggressively: decrease `stale_speed_threshold_kmh`
    - Compensate for GPS jitter: increase `stale_pos_epsilon_m`

    Returns list of TrackPoint.
    """
    points: List[TrackPoint] = []

    with open(csv_path, "r", newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []

        header_map = {name.strip(): i for i, name in enumerate(header)}

        date_idx = _find_col(header_map, ["Date"])
        time_idx = _find_col(header_map, ["Time"])
        gps_idx = _find_col(header_map, ["GPS"])
        alt_idx = _find_alt_col(header_map)
        spd_idx = _find_speed_col(header_map)
        rssi_cols = _find_rssi_cols(header_map)

        if gps_idx is None:
            return []

        for row in reader:
            if not row or gps_idx >= len(row):
                continue

            gps = parse_gps_field(row[gps_idx])
            if gps is None:
                continue
            lat, lon = gps

            t: Optional[datetime] = None
            if (
                date_idx is not None
                and time_idx is not None
                and date_idx < len(row)
                and time_idx < len(row)
            ):
                t = _parse_dt(row[date_idx], row[time_idx])

            alt_m = None
            if alt_idx is not None and alt_idx < len(row):
                alt_m = _parse_float(row[alt_idx])

            speed_kmh = None
            if spd_idx is not None and spd_idx < len(row):
                speed_kmh = _parse_float(row[spd_idx])

            rssi_db = _combine_rssi(row, rssi_cols) if rssi_cols else None

            points.append(
                TrackPoint(
                    t=t,
                    lat=float(lat),
                    lon=float(lon),
                    alt_m=alt_m,
                    speed_kmh=speed_kmh,
                    rssi_db=rssi_db,
                )
            )

    # Repair stale GPS (pause/jump fix)
    if repair_stale and points:
        points = repair_stale_gps(
            points,
            pos_epsilon_m=stale_pos_epsilon_m,
            speed_threshold_kmh=stale_speed_threshold_kmh,
            min_run_len=stale_min_run_len,
            min_anchor_dist_m=stale_min_anchor_dist_m,
        )

    # Downsample if needed
    if max_points > 0 and len(points) > max_points:
        step = max(1, len(points) // max_points)
        points = points[::step]

    return points