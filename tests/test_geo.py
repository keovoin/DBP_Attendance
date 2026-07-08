"""Tests for the geofence distance calculation."""

from attendance_bot import geo


def test_zero_distance_for_same_point():
    assert geo.haversine_distance_meters(11.5, 104.9, 11.5, 104.9) == 0.0


def test_known_short_distance_is_within_a_few_meters():
    # ~0.0001 degrees of latitude is about 11.1 meters near the equator.
    d = geo.haversine_distance_meters(11.5000, 104.9000, 11.5001, 104.9000)
    assert 10.5 <= d <= 11.5


def test_is_within_radius_true_and_false():
    # 10 m north -> within 20 m
    assert geo.is_within_radius(11.5, 104.9, 11.500090, 104.9, 20)
    # ~100 m away -> outside 20 m
    assert not geo.is_within_radius(11.5, 104.9, 11.5009, 104.9, 20)


def test_boundary_is_inclusive():
    d = geo.haversine_distance_meters(0.0, 0.0, 0.0, 0.0)
    assert geo.is_within_radius(0.0, 0.0, 0.0, 0.0, d)
