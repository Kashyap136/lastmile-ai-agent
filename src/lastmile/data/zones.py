"""The selectable intake zones.

These are FICTIONAL demo districts that pair with the demo recipient
dataset so distances can be calculated without a geocoding service.
Real deployments should replace this with a verified geolocation source.
"""

from __future__ import annotations

DEMO_ZONES: dict[str, tuple[float, float]] = {
    "Downtown": (40.7220, -74.0020),
    "Riverside": (40.7265, -74.0118),
    "Harbor East": (40.7160, -73.9975),
    "Maple Park": (40.7335, -74.0262),
    "North Heights": (40.7412, -74.0131),
    "Greenfield": (40.7145, -74.0310),
    "South Bay": (40.7080, -74.0098),
}


def zone_center(zone: str) -> tuple[float, float] | None:
    label = zone.strip().title()
    return DEMO_ZONES.get(label)
