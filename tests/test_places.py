"""Tests for SerpApi place search and parsing. PLAN.md 7.4.

Every HTTP call is mocked. The saved fixture is the real response from the single
hand call recorded in tests/fixtures/serpapi_maps_sample.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.services.cache import TTLCache
from app.services.places import (
    MAX_QUERIES_PER_REQUEST,
    SERPAPI_URL,
    VIBE_QUERIES,
    parse_local_results,
    queries_for_vibe,
    search_places,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VIENNA = (48.20849, 16.37208)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


@pytest.fixture
def serpapi_payload() -> dict:
    return json.loads((FIXTURES / "serpapi_maps_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def cache() -> TTLCache[list]:
    return TTLCache(ttl_seconds=3600)


# ------------------------------------------------------------------------ parsing


def test_parses_the_real_response(serpapi_payload):
    places = parse_local_results(serpapi_payload)

    assert len(places) == 20
    first = places[0]
    assert first["id"] == "ChIJU0SX9JEHbUcRReINMpMU-oc"
    assert first["name"] == "Planquadratpark"
    assert first["lat"] == pytest.approx(48.1966253)
    assert first["lng"] == pytest.approx(16.3640801)
    assert first["rating"] == 4.8
    assert first["reviews"] == 31
    assert first["address"]


def test_every_parsed_place_has_the_fields_step6_needs(serpapi_payload):
    for place in parse_local_results(serpapi_payload):
        assert place["id"]
        assert place["name"]
        assert isinstance(place["lat"], float)
        assert isinstance(place["lng"], float)


def test_description_is_read_when_present(serpapi_payload):
    places = {p["name"]: p for p in parse_local_results(serpapi_payload)}
    # Real data carries a description on some results, not all.
    assert places["Stadtpark"]["description"]
    assert "Johann Strauss" in places["Stadtpark"]["description"]


def test_missing_description_becomes_none(serpapi_payload):
    """Most results have none. None is normal and must not become invented prose."""
    places = parse_local_results(serpapi_payload)
    without = [p for p in places if p["description"] is None]

    assert without, "expected the real response to have results without a description"
    assert all(p["description"] is None for p in without)


def test_operating_hours_are_captured(serpapi_payload):
    places = {p["name"]: p for p in parse_local_results(serpapi_payload)}
    hours = places["Planquadratpark"]["operating_hours"]

    assert hours["sunday"] == "Open 24 hours"
    assert "monday" in hours


def test_hours_and_open_state_are_captured(serpapi_payload):
    places = {p["name"]: p for p in parse_local_results(serpapi_payload)}
    assert places["Planquadratpark"]["hours"] == "Open 24 hours"
    assert places["Planquadratpark"]["open_state"] == "Open 24 hours"


@pytest.mark.parametrize(
    "result",
    [
        {"title": "No id", "gps_coordinates": {"latitude": 1, "longitude": 2}},
        {"place_id": "x", "title": "No coords"},
        {"place_id": "x", "title": "Partial coords", "gps_coordinates": {"latitude": 1}},
        {"place_id": "", "title": "Blank id", "gps_coordinates": {"latitude": 1, "longitude": 2}},
    ],
)
def test_entries_without_id_or_coordinates_are_dropped(result):
    assert parse_local_results({"local_results": [result]}) == []


def test_zero_coordinates_are_kept():
    """0 is a real coordinate. A falsy check would wrongly drop Null Island."""
    places = parse_local_results(
        {"local_results": [{"place_id": "x", "gps_coordinates": {"latitude": 0, "longitude": 0}}]}
    )
    assert len(places) == 1
    assert places[0]["lat"] == 0.0


@pytest.mark.parametrize(
    "payload",
    [{}, {"local_results": None}, {"local_results": []}, {"local_results": "nope"}, [], "text", None],
)
def test_unusable_payloads_yield_no_places(payload):
    assert parse_local_results(payload) == []


def test_non_numeric_rating_becomes_none():
    places = parse_local_results(
        {
            "local_results": [
                {
                    "place_id": "x",
                    "gps_coordinates": {"latitude": 1, "longitude": 2},
                    "rating": "excellent",
                    "reviews": "many",
                }
            ]
        }
    )
    assert places[0]["rating"] is None
    assert places[0]["reviews"] is None


def test_types_is_always_a_list():
    places = parse_local_results(
        {"local_results": [{"place_id": "x", "gps_coordinates": {"latitude": 1, "longitude": 2}}]}
    )
    assert places[0]["types"] == []


# -------------------------------------------------------------------------- search


@respx.mock
async def test_search_sends_the_documented_parameters(serpapi_payload):
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    await search_places(*VIENNA, "quiet park")

    params = route.calls.last.request.url.params
    assert params["engine"] == "google_maps"
    assert params["type"] == "search"
    assert params["q"] == "quiet park"
    assert params["ll"] == f"@{VIENNA[0]},{VIENNA[1]},14z"
    assert params["hl"] == "en"
    assert params["api_key"]


@respx.mock
async def test_search_returns_parsed_places(serpapi_payload):
    respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    places = await search_places(*VIENNA, "quiet park")

    assert len(places) == 20


@respx.mock
async def test_second_call_is_served_from_cache_without_http(serpapi_payload, cache):
    """The re-roll guarantee from PLAN.md 7.4: no second search, no quota spent."""
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    first = await search_places(*VIENNA, "quiet park", cache=cache)
    second = await search_places(*VIENNA, "quiet park", cache=cache)

    assert first == second
    assert len(first) == 20
    assert route.call_count == 1


@respx.mock
async def test_cache_key_rounding_means_a_nearby_reroll_reuses_the_search(serpapi_payload, cache):
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    await search_places(48.208493, 16.372081, "quiet park", cache=cache)
    await search_places(48.208489, 16.372080, "quiet park", cache=cache)

    assert route.call_count == 1


@respx.mock
async def test_a_different_query_does_not_hit_the_cache(serpapi_payload, cache):
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    await search_places(*VIENNA, "quiet park", cache=cache)
    await search_places(*VIENNA, "nature reserve", cache=cache)

    assert route.call_count == 2


@respx.mock
async def test_empty_result_is_cached_so_repeats_do_not_burn_quota(cache):
    route = respx.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={"local_results": []}))

    assert await search_places(*VIENNA, "nowhere", cache=cache) == []
    assert await search_places(*VIENNA, "nowhere", cache=cache) == []

    assert route.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(401), httpx.Response(429), httpx.Response(500), httpx.Response(200, text="<html>")],
)
@respx.mock
async def test_failures_return_empty_instead_of_raising(failure):
    """One dead query must not take down the pipeline."""
    respx.get(SERPAPI_URL).mock(return_value=failure)

    assert await search_places(*VIENNA, "quiet park") == []


@respx.mock
async def test_transport_errors_return_empty():
    respx.get(SERPAPI_URL).mock(side_effect=httpx.ConnectError("no route"))

    assert await search_places(*VIENNA, "quiet park") == []


# ------------------------------------------------------------------------- vibes


def test_each_vibe_has_the_planned_queries():
    assert queries_for_vibe("quiet") == ("quiet park", "nature reserve")
    assert queries_for_vibe("active") == ("hiking trail", "scenic viewpoint")
    assert queries_for_vibe("curious") == ("botanical garden", "nature trail")


def test_never_more_than_two_queries_per_vibe():
    for vibe in VIBE_QUERIES:
        assert len(queries_for_vibe(vibe)) <= MAX_QUERIES_PER_REQUEST


def test_unknown_vibe_falls_back_instead_of_raising():
    assert queries_for_vibe("romantic") == VIBE_QUERIES["curious"]