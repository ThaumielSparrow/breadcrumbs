# src/core/parser.py
import re
from typing import Optional, Tuple

# Matches: "37.123 -77.456" or "37.123,-77.456"
_GPS_RE_DECIMAL = re.compile(r"(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)")

# Matches: "N 37.123 W 77.456"
_GPS_RE_CARDINAL = re.compile(
    r"([NS])\s*(\d+(?:\.\d+)?)\s*([EW])\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE
)

def parse_gps_field(gps_str: str) -> Optional[Tuple[float, float]]:
    """
    EdgeTX 'GPS' field is commonly a single CSV cell containing "lat lon".
    Returns (lat, lon) or None.
    """
    if not gps_str:
        return None
    s = gps_str.strip()
    if not s:
        return None

    m = _GPS_RE_DECIMAL.search(s)
    if m:
        a = float(m.group(1))
        b = float(m.group(2))

        # Most common: lat, lon
        lat, lon = a, b
        # Some sources might emit lon, lat; attempt swap if needed
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) and (-90 <= b <= 90 and -180 <= a <= 180):
            lat, lon = b, a

        # Drop obvious invalid fix (0,0) when GPS has not acquired enough satellites
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            return None

        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    m = _GPS_RE_CARDINAL.search(s)
    if m:
        ns, lat_s, ew, lon_s = m.groups()
        lat = float(lat_s)
        lon = float(lon_s)
        if ns.upper() == "S":
            lat = -lat
        if ew.upper() == "W":
            lon = -lon
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            return None
        return lat, lon

    return None
