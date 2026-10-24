"""Routing tool adapter.

Two modes, always explicitly labelled via DataSource:
  * ROUTING_API_URL configured  -> real OSRM-compatible route (external_api)
  * not configured              -> haversine-based estimate (calculated_estimate)

The adapter never claims an external call succeeded when the call failed;
callers receive (estimate, error) and decide the failure state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from ..models.common import DataSource
from ..models.pickup import TravelEstimate

logger = logging.getLogger("lastmile.routing")

# Demo calibration constants (documented, clearly not real-world guarantees).
EARTH_RADIUS_KM = 6371.0088
ROAD_FACTOR = 1.3        # straight-line -> road distance correction
URBAN_AVG_SPEED_KMH = 24.0


@dataclass
class RoutingResult:
    estimate: TravelEstimate
    error: str = ""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return EARTH_RADIUS_KM * 2 * asin(sqrt(a))


def estimate_between(
    donor_lat: float | None,
    donor_lon: float | None,
    recipient_lat: float,
    recipient_lon: float,
    routing_api_url: str = "",
    timeout_seconds: float = 8.0,
) -> RoutingResult:
    """Return a TravelEstimate, from the real API when configured+reachable."""
    if donor_lat is None or donor_lon is None:
        return RoutingResult(
            estimate=TravelEstimate(
                distance_km=0.0,
                duration_minutes=0.0,
                data_source=DataSource.CALCULATED_ESTIMATE,
                note="donor coordinates unavailable - distance unknown",
            ),
            error="no donor coordinates available; distance cannot be estimated",
        )

    if routing_api_url:
        result = _call_osrm(
            routing_api_url,
            donor_lat,
            donor_lon,
            recipient_lat,
            recipient_lon,
            timeout_seconds,
        )
        if result.error:
            # Explicit failure path - do NOT silently pretend it is external data.
            fallback = _road_estimate(donor_lat, donor_lon, recipient_lat, recipient_lon)
            return RoutingResult(
                estimate=fallback,
                error=f"routing API unavailable ({result.error}); "
                "fell back to a clearly-labelled calculated estimate",
            )
        return result

    return RoutingResult(estimate=_road_estimate(donor_lat, donor_lon, recipient_lat, recipient_lon))


def _road_estimate(
    donor_lat: float, donor_lon: float, recipient_lat: float, recipient_lon: float
) -> TravelEstimate:
    straight = haversine_km(donor_lat, donor_lon, recipient_lat, recipient_lon)
    road = straight * ROAD_FACTOR
    duration_minutes = (road / URBAN_AVG_SPEED_KMH) * 60.0
    return TravelEstimate(
        distance_km=round(road, 2),
        duration_minutes=round(duration_minutes, 1),
        data_source=DataSource.CALCULATED_ESTIMATE,
        note=(
            "calculated estimate: straight-line distance x road factor 1.3, "
            "urban average speed 24 km/h - not a verified route"
        ),
    )


def _call_osrm(
    base_url: str,
    donor_lat: float,
    donor_lon: float,
    recipient_lat: float,
    recipient_lon: float,
    timeout_seconds: float,
) -> RoutingResult:
    """Query an OSRM-compatible /route endpoint. Never raises."""
    url = (
        f"{base_url.rstrip('/')}/route/v1/driving/"
        f"{donor_lon:.6f},{donor_lat:.6f};{recipient_lon:.6f},{recipient_lat:.6f}"
        "?overview=false"
    )
    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.RequestException as exc:
        return RoutingResult(estimate=TravelEstimate(distance_km=0.0, duration_minutes=0.0), error=str(exc))

    if response.status_code != 200:
        return RoutingResult(
            estimate=TravelEstimate(distance_km=0.0, duration_minutes=0.0),
            error=f"HTTP {response.status_code}",
        )
    try:
        data = response.json()
    except ValueError:
        return RoutingResult(estimate=TravelEstimate(distance_km=0.0, duration_minutes=0.0), error="non-JSON response")

    routes = data.get("routes") or []
    if not routes:
        return RoutingResult(
            estimate=TravelEstimate(distance_km=0.0, duration_minutes=0.0),
            error=f"no route found (OSRM code={data.get('code')})",
        )
    route = routes[0]
    # OSRM returns metres / seconds.
    km = route.get("distance", 0.0) / 1000.0
    minutes = route.get("duration", 0.0) / 60.0
    return RoutingResult(
        estimate=TravelEstimate(
            distance_km=round(km, 2),
            duration_minutes=round(minutes, 1),
            data_source=DataSource.EXTERNAL_API,
            note="distance/duration from configured routing API",
        )
    )
