"""Geolocation helpers for On_Site clock-in validation."""

from __future__ import annotations

import math

# Mean Earth radius in meters (used by the haversine formula).
EARTH_RADIUS_METERS = 6_371_008.8


def haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Return the great-circle distance between two points in meters.

    Uses the haversine formula, which is accurate for the short distances
    relevant to a 20-meter geofence.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_METERS * c


def is_within_radius(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    radius_meters: float,
) -> bool:
    """Return True if the two coordinates are within ``radius_meters``."""
    return haversine_distance_meters(lat1, lon1, lat2, lon2) <= radius_meters
