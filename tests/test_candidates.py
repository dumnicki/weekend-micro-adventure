"""Tests for candidate filtering, distance, time math, scoring and daylight. PLAN.md 7.3, 7.5, 7.6.

Runs against the real saved SerpApi response from tests/fixtures/serpapi_maps_sample.json.
No network calls.
"""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path

import pytest

from app.services.candidates import (
    BUDGET_MINUTES,
    MIN_ACTIVITY_MINUTES,
    OUTDOOR_KEYWORDS,
    RADIUS_KM,
    build_candidates,
    haversine_km,
    maps_link,
    resolve_daylight,
)
from app.services.places import parse_local_results

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VIENNA = (48.20849, 16.37208)


@pytest.fixture
def places():
    payload = json.loads((FIXTURES / "serpapi_maps_sample.json").read_text(encoding="utf-8"))
    return parse_local_results(payload)


def make_place(**overrides):
    base = {
        "id": "p1", "name": "Test Park", "type": "Park", "types": ["Park"],
        "address": "Somewhere", "lat": VIENNA[0], "lng": VIENNA[1],
        "rating": 4.5, "reviews": 100, "description": None,
        "hours": None, "operating_hours": None, "open_state": None,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- haversine


def test_haversine_is_zero_for_the_same_point():
    assert haversine_km(48.2, 16.37, 48.2, 16.37) == pytest.approx(0.0)


def test_haversine_known_distance():
    # Vienna to Salzburg is about 251 km.
    assert haversine_km(48.20849, 16.37208, 47.80945, 13.05503) == pytest.approx(251, abs=5)


def test_haversine_is_symmetric():
    a = haversine_km(48.2, 16.37, 47.8, 13.05)
    b = haversine_km(47.8, 13.05, 48.2, 16.37)
    assert a == pytest.approx(b)


# ----------------------------------------------------------------- real fixture


def test_fixture_produces_between_one_and_eight_candidates(places):
    result = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="half_day", transport="foot")
    assert 1 <= len(result) <= 8


@pytest.mark.parametrize("time_budget", BUDGET_MINUTES)
@pytest.mark.parametrize("transport", ["foot", "bike", "transit"])
def test_never_more_than_eight_for_any_combination(places, time_budget, transport):
    result = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget=time_budget, transport=transport)
    assert len(result) <= 8


def test_all_returned_candidates_satisfy_the_rules(places):
    result = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="half_day", transport="foot")
    radius = RADIUS_KM["foot"]["half_day"]
    for candidate in result:
        assert candidate.distance_km <= radius
        assert candidate.activity_minutes_available >= MIN_ACTIVITY_MINUTES
        assert candidate.id
        assert any(k in f"{candidate.type} {' '.join(candidate.__dict__.get('types', []) or [])}".lower()
                   for k in OUTDOOR_KEYWORDS)


def test_results_are_sorted_by_score_descending(places):
    result = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    from math import log

    scores = [
        (c.rating or 3.5) * log(1 + (c.reviews or 0)) for c in result
    ]
    assert scores == sorted(scores, reverse=True)


# ------------------------------------------------------------------ radius filter


def test_radius_drops_distant_places():
    near = make_place(id="near", lat=VIENNA[0], lng=VIENNA[1])
    far = make_place(id="far", lat=48.40, lng=16.60)
    result = build_candidates([near, far], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="1h", transport="foot")
    assert [c.id for c in result] == ["near"]


def test_smaller_budget_yields_a_smaller_or_equal_set(places):
    small = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                             time_budget="1h", transport="foot")
    large = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                             time_budget="full_day", transport="foot")
    assert len(small) <= len(large)


# -------------------------------------------------------------- exclude / dedupe


def test_excluded_ids_are_dropped(places):
    baseline = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                                time_budget="full_day", transport="transit")
    assert len(baseline) >= 2

    rerolled = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                                time_budget="full_day", transport="transit",
                                exclude_ids=[baseline[0].id])

    assert baseline[0].id not in {c.id for c in rerolled}
    # Excluding one frees a slot, so the place that was previously cut off the top 8
    # is promoted into it. The reroll draws from the backlog rather than just shuffling.
    assert len(rerolled) == len(baseline)
    assert baseline[0].id not in {c.id for c in rerolled}


def test_excluding_every_place_gives_an_empty_list(places):
    """Exhaust the whole pool, not just the top 8 that were returned."""
    every_id = [p["id"] for p in places]
    result = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit",
                              exclude_ids=every_id)
    assert result == []


def test_reroll_pulls_from_beyond_the_top_eight(places):
    baseline = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                                time_budget="full_day", transport="transit")
    assert len(baseline) == 8, "expected the top-8 cap to be in play"

    rerolled = build_candidates(places, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                                time_budget="full_day", transport="transit",
                                exclude_ids=[baseline[0].id])

    promoted = {c.id for c in rerolled} - {c.id for c in baseline}
    assert promoted, "expected a place from outside the top 8 to be promoted"


def test_duplicate_ids_are_dropped():
    twice = [make_place(id="dup", reviews=10), make_place(id="dup", reviews=10)]
    result = build_candidates(twice, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert len(result) == 1


# --------------------------------------------------------------- allow-list


@pytest.mark.parametrize("bad_type", ["Restaurant", "Cafe", "Hotel", "Shopping mall", "Bakery"])
def test_shops_restaurants_and_hotels_are_dropped(bad_type):
    result = build_candidates([make_place(type=bad_type, types=[bad_type])],
                              origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result == []


@pytest.mark.parametrize("good_type", ["Park", "Hiking trail", "Botanical garden", "Lake", "Forest"])
def test_outdoor_types_are_kept(good_type):
    result = build_candidates([make_place(type=good_type, types=[good_type])],
                              origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert len(result) == 1


def test_type_matching_is_case_insensitive():
    result = build_candidates([make_place(type="BIRDWATCHING AREA", types=["Birdwatching area"])],
                              origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result == []  # contains no outdoor keyword, so it is correctly dropped


def test_types_list_is_searched_even_when_type_is_generic():
    result = build_candidates([make_place(type="Establishment", types=["Scenic viewpoint"])],
                              origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert len(result) == 1


# --------------------------------------------------------------- time math


def test_short_activity_places_are_dropped():
    """A 40 km round trip inside a 60 minute budget leaves nothing to do."""
    far = make_place(id="far", lat=VIENNA[0] + 0.30, lng=VIENNA[1])
    result = build_candidates([far], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="1h", transport="foot")
    assert result == []


def test_activity_minutes_matches_the_plan_formula():
    """budget - 2 * (distance * 1.3 / speed * 60), transit wait added each way."""
    place = make_place(lat=VIENNA[0], lng=VIENNA[1])  # distance 0
    result = build_candidates([place], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="half_day", transport="foot")
    assert result[0].activity_minutes_available == 240


def test_transit_overhead_reduces_available_time():
    place = make_place(lat=VIENNA[0], lng=VIENNA[1])
    result = build_candidates([place], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="half_day", transport="transit")
    # 240 - 2 * 10 minutes of waiting
    assert result[0].activity_minutes_available == 220


def test_explicit_budget_overrides_the_table():
    place = make_place()
    result = build_candidates([place], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="foot",
                              budget_minutes=60)
    assert result[0].activity_minutes_available == 60


# ------------------------------------------------------------------ scoring


def test_missing_rating_counts_as_three_point_five():
    from math import log

    unrated = make_place(id="u", rating=None, reviews=1000)
    rated_35 = make_place(id="r", rating=3.5, reviews=1000)
    result = build_candidates([unrated, rated_35], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    order = [c.id for c in result]
    # Equal scores, so the name tiebreak decides; both are kept.
    assert set(order) == {"u", "r"}


def test_higher_rating_wins_when_reviews_match():
    low = make_place(id="low", name="Low", rating=3.0, reviews=500)
    high = make_place(id="high", name="High", rating=4.9, reviews=500)
    result = build_candidates([low, high], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result[0].id == "high"


def test_zero_reviews_does_not_crash_scoring():
    result = build_candidates([make_place(reviews=0)], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert len(result) == 1


# ------------------------------------------------------------------ hours text


def test_hours_prefers_the_status_string():
    place = make_place(hours="Open 24 hours",
                       operating_hours={d: "Open 24 hours" for d in
                                        ("monday", "tuesday", "wednesday", "thursday",
                                         "friday", "saturday", "sunday")})
    result = build_candidates([place], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result[0].hours_text == "Open 24 hours"


def test_operating_hours_are_summarised_when_uniform():
    place = make_place(hours=None,
                       operating_hours={d: "6 AM–10 PM" for d in
                                        ("monday", "tuesday", "wednesday", "thursday",
                                         "friday", "saturday", "sunday")})
    result = build_candidates([place], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result[0].hours_text == "6 AM–10 PM"


def test_varied_operating_hours_are_left_unknown_rather_than_summarised():
    """Summarising irregular hours would be inventing facts. None is the honest answer."""
    place = make_place(hours=None,
                       operating_hours={"monday": "9 AM–5 PM", "saturday": "10 AM–4 PM"})
    result = build_candidates([place], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result[0].hours_text is None


# ------------------------------------------------------------------- daylight


def test_daylight_trims_the_budget_to_fit_before_sunset():
    # Now 15:00, sunset 18:13 -> latest return 17:43, so 163 minutes remain.
    window = resolve_daylight(time(15, 0), "18:13", "07:09", "18:11", 480)
    assert window.latest_return_time == "17:43"
    assert window.max_trip_minutes == 163
    assert window.day_label == "today"


def test_daylight_keeps_the_budget_when_there_is_room():
    window = resolve_daylight(time(9, 0), "18:13", "07:09", "18:11", 60)
    assert window.max_trip_minutes == 60


def test_daylight_switches_to_tomorrow_after_sunset_minus_thirty():
    window = resolve_daylight(time(18, 0), "18:13", "07:09", "18:11", 240)
    assert window.day_label == "tomorrow"
    assert window.latest_return_time == "17:41"  # tomorrow's sunset 18:11 minus 30
    assert window.max_trip_minutes == 240


def test_daylight_without_tomorrows_data_returns_zero_minutes():
    window = resolve_daylight(time(22, 0), "18:13", None, None, 240)
    assert window.max_trip_minutes == 0
    assert window.day_label == "tomorrow"


def test_daylight_can_reduce_the_trip_to_nothing_late_in_the_day():
    window = resolve_daylight(time(17, 30), "18:13", "07:09", "18:11", 240)
    assert window.max_trip_minutes == 13


# ------------------------------------------------------------------ maps link


def test_maps_link_uses_the_candidate_fields():
    result = build_candidates([make_place(id="abc")], origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    link = maps_link(result[0])

    assert link.startswith("https://www.google.com/maps/search/?api=1")
    assert f"query={result[0].lat},{result[0].lng}" in link
    assert "query_place_id=abc" in link


# ---------------------------------------------------------------- empty result


def test_no_survivors_gives_an_empty_list():
    result = build_candidates([make_place(type="Restaurant", types=["Restaurant"])],
                              origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result == []


def test_places_without_coordinates_are_skipped():
    broken = [make_place(id="a", lat=None, lng=None), make_place(id="b")]
    result = build_candidates(broken, origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert [c.id for c in result] == ["b"]


def test_places_without_an_id_are_skipped():
    result = build_candidates([make_place(id=None), make_place(id="")],
                              origin_lat=VIENNA[0], origin_lng=VIENNA[1],
                              time_budget="full_day", transport="transit")
    assert result == []