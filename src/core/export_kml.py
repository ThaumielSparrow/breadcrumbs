# src/core/export_kml.py
from typing import List, Optional
from .load import TrackPoint

def _escape_xml(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def export_track_to_kml(
    track: List[TrackPoint],
    out_path: str,
    name: str = "Flight session",
    altitude_mode: str = "relativeToGround",  # or "absolute"
):
    if not track:
        raise ValueError("No track points to export")

    has_alt = any(p.alt_m is not None for p in track)

    coords_lines = []
    for p in track:
        alt = p.alt_m if (has_alt and p.alt_m is not None) else 0.0
        coords_lines.append(f"{p.lon},{p.lat},{alt}")

    # A marker for the last point (super useful for crash recovery)
    last = track[-1]

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{_escape_xml(name)}</name>

    <Placemark>
      <name>{_escape_xml(name)} Path</name>
      <Style>
        <LineStyle>
          <width>4</width>
        </LineStyle>
      </Style>
      <LineString>
        <tessellate>1</tessellate>
        <altitudeMode>{altitude_mode if has_alt else "clampToGround"}</altitudeMode>
        <coordinates>
          {" ".join(coords_lines)}
        </coordinates>
      </LineString>
    </Placemark>

    <Placemark>
      <name>Last known position</name>
      <Point>
        <coordinates>{last.lon},{last.lat},{last.alt_m if last.alt_m is not None else 0.0}</coordinates>
      </Point>
    </Placemark>

  </Document>
</kml>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(kml)