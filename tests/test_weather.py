"""Tests for geocoding and weather. PLAN.md 7.1 and 7.2.

Every HTTP call is mocked with respx. Nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.services.geocode import GEOCODE_URL, UnknownPlaceError, geocode
from app.services.weather import (
    FORECAST_URL,
    WEATHER_UNKNOWN,
    build_weather,
    fetch_weather,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


@pytest.fixture
def forecast_payload() -> dict:
    return json.loads((FIXTURES / "openmeteo_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def geocode_payload() -> dict:
    return json.loads((FIXTURES / "openmeteo_geocode_sample.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------ geocoding


async def test_geocode_parses_the_real_response(router, geocode_payload):
    router.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json=geocode_payload))

    place = await geocode("Vienna")

    assert place.lat == pytest.approx(48.20849)
    assert place.lng == pytest.approx(16.37208)
    assert place.name == "Vienna, State of Vienna, Austria"
    assert place.timezone == "Europe/Vienna"


async def test_geocode_sends_the_expected_query(router, geocode_payload):
    route = router.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json=geocode_payload))

    await geocode("Vienna")

    params = route.calls.last.request.url.params
    assert params["name"] == "Vienna"
    assert params["count"] == "1"


async def test_geocode_raises_when_nothing_matches(router):
    router.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    with pytest.raises(UnknownPlaceError, match="Could not find that place"):
        await geocode("Atlantis")


async def test_geocode_raises_when_results_key_is_absent(router):
    router.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(UnknownPlaceError):
        await geocode("Atlantis")


async def test_geocode_raises_on_blank_input(router):
    with pytest.raises(UnknownPlaceError):
        await geocode("   ")


async def test_geocode_skips_results_without_coordinates(router):
    router.get(GEOCODE_URL).mock(
        return_value=httpx.Response(200, json={"results": [{"name": "Nowhere"}]})
    )

    with pytest.raises(UnknownPlaceError):
        await geocode("Nowhere")


async def test_geocode_propagates_server_errors(router):
    """A broken geocoder is a server fault, not an unknown place. It must not 404."""
    router.get(GEOCODE_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(httpx.HTTPStatusError):
        await geocode("Vienna")


# -------------------------------------------------------------------------- weather


def test_build_weather_from_the_real_response(forecast_payload):
    weather = build_weather(forecast_payload)

    assert weather is not None
    assert weather.sunset_local == "18:13"
    assert weather.sunrise_local == "07:07"
    assert weather.temp_c == 17
    assert weather.rain_chance_percent == 25
    # The degree sign is real here; the console mangles it, the string does not.
    assert weather.summary == "17°C, 25% rain chance, sunset 18:13"


def test_build_weather_uses_todays_row_not_the_first_forecast_day(forecast_payload):
    weather = build_weather(forecast_payload)
    assert weather.sunset_local == "18:13"  # daily[0], not 18:11 or 18:10


def test_build_weather_ignores_sunset_with_seconds():
    payload = {"daily": {"sunset": ["2026-10-11T18:13:44"], "sunrise": ["2026-10-11T07:07:22"],
                         "temperature_2m_max": [12.3], "precipitation_probability_max": [40]}}
    weather = build_weather(payload)
    assert weather.sunset_local == "18:13"
    assert weather.sunrise_local == "07:07"


def test_build_weather_omits_rain_when_absent():
    payload = {"daily": {"sunset": ["2026-10-11T18:13"], "sunrise": ["2026-10-11T07:07"],
                         "temperature_2m_max": [12.3]}}
    weather = build_weather(payload)
    assert weather.summary == "12°C, sunset 18:13"
    assert weather.rain_chance_percent == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"daily": {}},
        {"daily": {"temperature_2m_max": [12.0]}},                       # no sunset
        {"daily": {"sunset": ["2026-10-11T18:13"], "sunrise": ["2026-10-11T07:07"]}},  # no temp
        {"daily": {"sunset": [], "sunrise": [], "temperature_2m_max": [1.0]}},  # empty lists
        {"daily": []},                                                   # wrong type
        [],
        "not a payload",
    ],
)
def test_build_weather_returns_none_on_unusable_payloads(payload):
    assert build_weather(payload) is None


async def test_fetch_weather_parses_the_real_response(router, forecast_payload):
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))

    weather = await fetch_weather(48.20849, 16.37208)

    assert weather is not None
    assert weather.summary == "17°C, 25% rain chance, sunset 18:13"


async def test_fetch_weather_sends_the_expected_query(router, forecast_payload):
    route = router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))

    await fetch_weather(48.20849, 16.37208)

    params = route.calls.last.request.url.params
    assert params["latitude"] == "48.20849"
    assert params["longitude"] == "16.37208"
    assert params["timezone"] == "auto"
    for field in ("sunset", "sunrise", "precipitation_probability_max", "temperature_2m_max"):
        assert field in params["daily"]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(404),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"daily": {}}),
    ],
)
async def test_fetch_weather_returns_none_instead_of_raising(router, failure):
    """PLAN.md 7.2: a weather failure must never fail the request."""
    router.get(FORECAST_URL).mock(return_value=failure)

    assert await fetch_weather(48.2, 16.37) is None


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("no route to host"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("timed out"),
    ],
)
async def test_fetch_weather_returns_none_on_transport_errors(router, error):
    router.get(FORECAST_URL).mock(side_effect=error)

    assert await fetch_weather(48.2, 16.37) is None


def test_unknown_weather_phrase_is_available_for_the_prompt():
    assert WEATHER_UNKNOWN == "weather unknown"