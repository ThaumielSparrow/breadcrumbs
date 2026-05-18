"""Track plotting and visualization utilities."""

import json
import statistics
from typing import List
from breadcrumbs.core.load import TrackPoint


def _is_dropout(p: TrackPoint) -> bool:
    """A sample is a 'dropout' when the link is gone or both quality channels
    are missing — LQ explicitly zero, or LQ and RSSI both unknown."""
    if p.lq is not None and p.lq <= 0:
        return True
    if p.lq is None and p.rssi_db is None:
        return True
    return False


def build_dropout_segments(track: List[TrackPoint]) -> List[List[List[float]]]:
    """Return contiguous runs of dropout samples as [[lat, lon], ...] segments.

    Each segment includes the sample immediately *before* and *after* the run
    so the rendered polyline connects to the live path on both ends. Single-
    sample dropouts produce a 3-point segment (prev, dropout, next)."""
    n = len(track)
    if n < 2:
        return []

    segments: List[List[List[float]]] = []
    i = 0
    while i < n:
        if _is_dropout(track[i]):
            start = i
            while i < n and _is_dropout(track[i]):
                i += 1
            end = i  # exclusive

            seg_start = max(0, start - 1)
            seg_end = min(n, end + 1)  # exclusive
            seg = [[track[k].lat, track[k].lon] for k in range(seg_start, seg_end)]
            if len(seg) >= 2:
                segments.append(seg)
        else:
            i += 1

    return segments


def build_timeline_seconds(track: list[TrackPoint]) -> list[float]:
    """Build timeline in seconds from track points, handling time gaps."""
    diffs = []
    prev_t = None
    for p in track:
        if p.t is None:
            continue
        if prev_t is not None:
            d = (p.t - prev_t).total_seconds()
            if 0 < d < 300:
                diffs.append(d)
        prev_t = p.t

    dt_est = statistics.median(diffs) if diffs else 1.0

    base = None
    for p in track:
        if p.t is not None:
            base = p.t
            break

    if base is None:
        return [i * 1.0 for i in range(len(track))]

    timeline = []
    last = 0.0
    for p in track:
        if p.t is None:
            last = last + dt_est
        else:
            delta = (p.t - base).total_seconds()
            if delta < last:
                delta = last + dt_est
            last = delta
        timeline.append(last)

    if not timeline or timeline[-1] < 0:
        return [i * 1.0 for i in range(len(track))]

    return timeline


def build_hotline_payload(track: list[TrackPoint], metric: str):
    """Build data and metadata for Leaflet Hotline visualization.
    
    Args:
        track: List of track points
        metric: One of "Progress", "RSSI", "Speed", "Altitude"
    
    Returns:
        Tuple of (data, meta) or None if metric has no valid data
    """
    n = len(track)
    if n < 2:
        return None

    if metric == "Progress":
        values = [(i / (n - 1)) if n > 1 else 0.0 for i in range(n)]
        vmin, vmax = 0.0, 1.0
        meta = {"title": "Progress", "min": vmin, "max": vmax, "label_min": "Start", "label_max": "End"}

    elif metric == "RSSI":
        raw = [p.rssi_db for p in track]
        present = [v for v in raw if v is not None]
        if not present:
            return None
        vmin, vmax = min(present), max(present)
        meta = {"title": "RSSI (dB)", "min": vmin, "max": vmax, "label_min": f"{vmin:.0f} dB", "label_max": f"{vmax:.0f} dB"}
        values = raw

    elif metric == "Speed":
        raw = [p.speed_kmh for p in track]
        present = [v for v in raw if v is not None]
        if not present:
            return None
        vmin, vmax = min(present), max(present)
        meta = {"title": "Speed (km/h)", "min": vmin, "max": vmax, "label_min": f"{vmin:.1f} km/h", "label_max": f"{vmax:.1f} km/h"}
        values = raw

    elif metric == "Altitude":
        raw = [p.alt_m for p in track]
        present = [v for v in raw if v is not None]
        if not present:
            return None
        vmin, vmax = min(present), max(present)
        meta = {"title": "Altitude (m)", "min": vmin, "max": vmax, "label_min": f"{vmin:.1f} m", "label_max": f"{vmax:.1f} m"}
        values = raw

    else:
        return None

    if abs(meta["max"] - meta["min"]) < 1e-12:
        meta["max"] = meta["min"] + 1e-6

    filled = []
    last_val = None
    for v in values:
        if v is None:
            filled.append(last_val)
        else:
            filled.append(float(v))
            last_val = float(v)

    first_non = next((v for v in filled if v is not None), None)
    fallback = first_non if first_non is not None else float(meta["min"])
    filled = [fallback if v is None else v for v in filled]

    data = [[p.lat, p.lon, float(z)] for p, z in zip(track, filled)]
    return data, meta
