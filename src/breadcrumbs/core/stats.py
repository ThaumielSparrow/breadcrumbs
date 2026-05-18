"""Aggregate statistics for a loaded flight track."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from breadcrumbs.core.load import TrackPoint, _haversine_m


@dataclass(frozen=True)
class FlightStats:
    duration_s: Optional[float]
    distance_m: float
    max_dist_from_launch_m: float
    max_alt_m: Optional[float]
    max_speed_kmh: Optional[float]
    avg_rssi_db: Optional[float]
    min_rssi_db: Optional[float]
    sample_count: int


def compute_flight_stats(track: List[TrackPoint]) -> FlightStats:
    n = len(track)
    if n == 0:
        return FlightStats(None, 0.0, 0.0, None, None, None, None, 0)

    launch = track[0]
    distance_m = 0.0
    max_dist = 0.0
    max_alt: Optional[float] = None
    max_speed: Optional[float] = None
    rssi_vals: List[float] = []

    prev = launch
    for p in track:
        if p is not prev:
            distance_m += _haversine_m(prev.lat, prev.lon, p.lat, p.lon)
            prev = p

        d = _haversine_m(launch.lat, launch.lon, p.lat, p.lon)
        if d > max_dist:
            max_dist = d

        if p.alt_m is not None and (max_alt is None or p.alt_m > max_alt):
            max_alt = float(p.alt_m)

        if p.speed_kmh is not None and (max_speed is None or p.speed_kmh > max_speed):
            max_speed = float(p.speed_kmh)

        if p.rssi_db is not None:
            rssi_vals.append(float(p.rssi_db))

    # Duration: prefer real timestamps; otherwise unknown.
    duration_s: Optional[float] = None
    t0 = track[0].t
    tn = track[-1].t
    if t0 is not None and tn is not None:
        dt = (tn - t0).total_seconds()
        if dt > 0:
            duration_s = float(dt)

    avg_rssi = sum(rssi_vals) / len(rssi_vals) if rssi_vals else None
    min_rssi = min(rssi_vals) if rssi_vals else None

    return FlightStats(
        duration_s=duration_s,
        distance_m=distance_m,
        max_dist_from_launch_m=max_dist,
        max_alt_m=max_alt,
        max_speed_kmh=max_speed,
        avg_rssi_db=avg_rssi,
        min_rssi_db=min_rssi,
        sample_count=n,
    )
