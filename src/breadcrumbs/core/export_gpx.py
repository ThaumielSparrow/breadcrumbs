from typing import List

from breadcrumbs.core.load import TrackPoint


def _escape_xml(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def export_track_to_gpx(
    track: List[TrackPoint],
    out_path: str,
    name: str = "Flight session",
):
    if not track:
        raise ValueError("No track points to export")

    pts: List[str] = []
    for p in track:
        attrs = f'lat="{p.lat:.7f}" lon="{p.lon:.7f}"'
        body_parts: List[str] = []
        if p.alt_m is not None:
            body_parts.append(f"<ele>{p.alt_m:.2f}</ele>")
        if p.t is not None:
            # GPX 1.1 expects ISO 8601 UTC with trailing 'Z'. EdgeTX logs are
            # already local-time naive datetimes, but we emit them as-is with
            # 'Z' — most viewers (Google Earth, Strava import) accept it, and
            # the relative ordering is what matters for playback/analysis.
            body_parts.append(f"<time>{p.t.isoformat()}Z</time>")
        if p.speed_kmh is not None:
            # GPX speed is m/s; convert from km/h.
            speed_mps = float(p.speed_kmh) / 3.6
            body_parts.append(f"<speed>{speed_mps:.3f}</speed>")
        body = "".join(body_parts)
        pts.append(f'      <trkpt {attrs}>{body}</trkpt>')

    name_esc = _escape_xml(name)
    gpx = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="breadcrumbs" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata>
    <name>{name_esc}</name>
  </metadata>
  <trk>
    <name>{name_esc}</name>
    <trkseg>
{chr(10).join(pts)}
    </trkseg>
  </trk>
</gpx>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(gpx)
