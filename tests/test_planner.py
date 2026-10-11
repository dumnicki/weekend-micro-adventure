"""Tests for the full pipeline. PLAN.md 7.1 to 7.10.

HTTP is mocked with respx and the model is mocked by injecting a generate function.
No test touches a live API.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.schemas import AdventureRequest, Candidate
from app.services.candidates import build_candidates, resolve_daylight
from app.services.geocode import GEOCODE_URL, UnknownPlaceError
from app.services.planner import EMPTY_MESSAGE, plan_adventure
from app.services.places import SERPAPI_URL, parse_local_results
from app.services.weather import FORECAST_URL

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VIENNA = (48.20849, 16.37208)
# 14:20 UTC is 16:20 in Vienna (UTC+2), before the fixture's 18:13 sunset.
NOW = datetime(2026, 10, 11, 14, 20, tzinfo=timezone.utc)
# Comfortably before every latest_return the tests produce (17:43 today, 17:41 tomorrow).
SAFE_LEAVE_BY = "16:30"


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


@pytest.fixture
def serpapi_payload() -> dict:
    return json.loads((FIXTURES / "serpapi_maps_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def forecast_payload() -> dict:
    return json.loads((FIXTURES / "openmeteo_sample.json").read_text(encoding="utf-8"))


def _local_time(now_utc: datetime, offset_seconds: int):
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return (now_utc + timedelta(seconds=offset_seconds)).time()


def default_candidates() -> list[Candidate]:
    """The exact candidate list the planner builds for the default request.

    Computed the same way the planner does, including the daylight cap on the budget.
    Handing the model a guessed candidate id or a guessed duration is what this file
    originally did, and validate_card rejected those cards for the right reason.
    """
    places = parse_local_results(
        json.loads((FIXTURES / "serpapi_maps_sample.json").read_text(encoding="utf-8"))
    )
    forecast = json.loads((FIXTURES / "openmeteo_sample.json").read_text(encoding="utf-8"))
    daily = forecast["daily"]

    def hhmm(iso: str) -> str:
        return iso.split("T")[1][:5]

    window = resolve_daylight(
        _local_time(NOW, forecast["utc_offset_seconds"]),
        hhmm(daily["sunset"][0]),
        hhmm(daily["sunrise"][1]),
        hhmm(daily["sunset"][1]),
        240,
    )
    return build_candidates(
        places,
        origin_lat=VIENNA[0],
        origin_lng=VIENNA[1],
        time_budget="half_day",
        transport="foot",
        budget_minutes=min(240, window.max_trip_minutes),
    )


def make_request(**overrides) -> AdventureRequest:
    payload = {
        "time_budget": "half_day",
        "transport": "foot",
        "vibe": "quiet",
        "lat": VIENNA[0],
        "lng": VIENNA[1],
    }
    payload.update(overrides)
    return AdventureRequest(**payload)


def good_card(candidate: Candidate | None = None, **overrides) -> dict[str, Any]:
    """A card that passes validation, built from a real candidate."""
    candidate = candidate or default_candidates()[0]
    card = {
        "candidate_id": candidate.id,
        "title": candidate.name,
        # Exactly the allowance, so it can never exceed it.
        "duration_minutes": candidate.activity_minutes_available,
        "getting_there": f"Walk about {round(candidate.travel_minutes_one_way)} minutes.",
        "plan": ["Walk there.", "Stay for a while."],
        "tip": "Bring water.",
        "leave_by": SAFE_LEAVE_BY,
        "check_before_you_go": "Check the gates are open.",
    }
    card.update(overrides)
    return card


def ok_llm(candidate: Candidate | None = None, **overrides):
    async def generate(system: str, user: str) -> dict:
        return good_card(candidate, **overrides)

    return generate


def mock_upstream(router, serpapi_payload, forecast_payload):
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))


# ------------------------------------------------------------------------ good path


async def test_good_path_returns_a_full_card(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)

    result = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm())

    assert result.empty is False
    assert result.used_fallback is False
    assert result.response is not None
    assert len(result.response.plan) == 2


async def test_the_model_picks_from_the_offered_candidates(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    target = default_candidates()[2]

    result = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(target))

    assert result.response.candidate_id == target.id
    assert result.response.place_name == target.name


async def test_response_carries_place_facts_from_code_not_the_model(
    router, serpapi_payload, forecast_payload
):
    mock_upstream(router, serpapi_payload, forecast_payload)
    target = default_candidates()[0]

    result = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(target))

    response = result.response
    assert response.place_name == target.name
    assert response.address == target.address
    assert response.maps_link.startswith("https://www.google.com/maps/search/?api=1")
    assert f"query_place_id={target.id}" in response.maps_link
    assert response.weather_summary == "17°C, 25% rain chance, sunset 18:13"


async def test_model_cannot_redirect_the_map_link(router, serpapi_payload, forecast_payload):
    """A hallucinated title must not move the user somewhere else."""
    mock_upstream(router, serpapi_payload, forecast_payload)
    target = default_candidates()[1]

    result = await plan_adventure(
        make_request(), now_utc=NOW, generate=ok_llm(target, title="Somewhere Else Entirely")
    )

    assert result.response.place_name == target.name
    assert result.response.lat == target.lat
    assert f"query_place_id={target.id}" in result.response.maps_link


# ------------------------------------------------------------------- model failures


async def test_bad_json_then_good_retry_succeeds(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    attempts: list[str] = []

    async def generate(system: str, user: str) -> dict:
        attempts.append(user)
        if len(attempts) == 1:
            return {"nope": "not a card"}
        return good_card()

    result = await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert len(attempts) == 2
    assert result.used_fallback is False
    assert result.response.candidate_id == default_candidates()[0].id


async def test_retry_prompt_carries_the_rejection_reason(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    attempts: list[str] = []

    async def generate(system: str, user: str) -> dict:
        attempts.append(user)
        if len(attempts) == 1:
            return good_card(duration_minutes=9999)
        return good_card()

    await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert "rejected" in attempts[1]
    assert "duration_minutes" in attempts[1]


async def test_two_bad_answers_fall_back_without_raising(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    attempts: list[str] = []

    async def generate(system: str, user: str) -> dict:
        attempts.append(user)
        return {"garbage": True}

    result = await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert len(attempts) == 2
    assert result.used_fallback is True
    assert result.response.title.startswith("Go to ")
    assert len(result.response.plan) == 3


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("Backboard is down"), TimeoutError("timed out"), ValueError("bad json")],
)
async def test_any_model_exception_falls_back(router, serpapi_payload, forecast_payload, failure):
    mock_upstream(router, serpapi_payload, forecast_payload)

    async def generate(system: str, user: str) -> dict:
        raise failure

    result = await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert result.used_fallback is True
    assert result.response is not None


async def test_fallback_card_is_valid_output(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)

    async def generate(system: str, user: str) -> dict:
        raise RuntimeError("down")

    result = await plan_adventure(make_request(), now_utc=NOW, generate=generate)
    response = result.response

    assert 2 <= len(response.plan) <= 5
    assert response.duration_minutes > 0
    assert response.check_before_you_go


# ------------------------------------------------------------------ empty results


async def test_empty_candidates_never_call_the_model(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    calls: list[str] = []

    async def generate(system: str, user: str) -> dict:
        calls.append(user)
        return good_card()

    nothing_outdoors = json.loads(json.dumps(serpapi_payload))
    nothing_outdoors["local_results"] = [
        {**r, "type": "Restaurant", "types": ["Restaurant"]}
        for r in nothing_outdoors["local_results"]
    ]
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=nothing_outdoors))

    result = await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert result.empty is True
    assert result.message == EMPTY_MESSAGE
    assert result.response is None
    assert calls == [], "the model must not be called when there is nothing to choose"


async def test_reroll_promotes_the_next_candidate(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    cands = default_candidates()
    assert len(cands) >= 2

    first = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(cands[0]))
    assert first.response.candidate_id == cands[0].id

    second = await plan_adventure(
        make_request(exclude_ids=[cands[0].id]),
        now_utc=NOW,
        generate=ok_llm(cands[1]),
    )

    assert second.response.candidate_id == cands[1].id
    assert second.response.candidate_id != first.response.candidate_id


# -------------------------------------------------------------------- weather down


async def test_weather_failure_still_returns_a_card(router, serpapi_payload):
    router.get(FORECAST_URL).mock(side_effect=httpx.ConnectError("down"))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    result = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm())

    assert result.empty is False
    assert result.response.weather_summary == "weather unknown"


async def test_weather_failure_relaxes_the_leave_by_check(router, serpapi_payload):
    """Without a sunset there is nothing to enforce, so the rule stops applying."""
    router.get(FORECAST_URL).mock(side_effect=httpx.ConnectError("down"))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    result = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(leave_by="22:30"))

    assert result.response.leave_by == "22:30"
    assert result.used_fallback is False


async def test_weather_failure_passes_unknown_to_the_prompt(router, serpapi_payload):
    router.get(FORECAST_URL).mock(return_value=httpx.Response(500))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))
    prompts: list[str] = []

    async def generate(system: str, user: str) -> dict:
        prompts.append(user)
        return good_card(leave_by="22:00")

    await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert "weather unknown" in prompts[0]


async def test_weather_failure_does_not_break_the_fallback(router, serpapi_payload):
    router.get(FORECAST_URL).mock(side_effect=httpx.ConnectError("down"))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))

    async def generate(system: str, user: str) -> dict:
        raise RuntimeError("down")

    result = await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert result.used_fallback is True
    assert result.response is not None


# -------------------------------------------------------------------- geocoding


async def test_location_text_is_geocoded(router, serpapi_payload, forecast_payload):
    router.get(GEOCODE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"name": "Vienna", "latitude": VIENNA[0], "longitude": VIENNA[1],
                               "country": "Austria", "timezone": "Europe/Vienna"}]},
        )
    )
    mock_upstream(router, serpapi_payload, forecast_payload)

    result = await plan_adventure(
        make_request(lat=None, lng=None, location_text="Vienna"), now_utc=NOW, generate=ok_llm()
    )

    assert result.empty is False


async def test_unknown_place_raises_for_the_route_to_turn_into_404(router):
    router.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    with pytest.raises(UnknownPlaceError, match="Could not find that place"):
        await plan_adventure(
            make_request(lat=None, lng=None, location_text="Atlantis"), now_utc=NOW, generate=ok_llm()
        )


async def test_coordinates_skip_geocoding_entirely(router, serpapi_payload, forecast_payload):
    geocode_route = router.get(GEOCODE_URL).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    mock_upstream(router, serpapi_payload, forecast_payload)

    await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm())

    assert geocode_route.call_count == 0


# ------------------------------------------------------------------------ daylight


async def test_leave_by_before_latest_return_is_kept(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)

    result = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm())

    assert result.response.leave_by == SAFE_LEAVE_BY
    assert result.used_fallback is False


async def test_leave_by_after_latest_return_falls_back(router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)

    result = await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(leave_by="21:00"))

    assert result.used_fallback is True
    # The fallback uses the daylight limit itself as the leave-by time.
    assert result.response.leave_by == "17:43"  # sunset 18:13 minus 30 minutes


async def test_daylight_caps_the_budget_passed_to_the_model(
    router, serpapi_payload, forecast_payload
):
    """At 16:20 local only 83 minutes remain before the 17:43 cutoff."""
    mock_upstream(router, serpapi_payload, forecast_payload)
    prompts: list[str] = []

    async def generate(system: str, user: str) -> dict:
        prompts.append(user)
        return good_card()

    await plan_adventure(make_request(), now_utc=NOW, generate=generate)

    assert "half_day (83 minutes total, including travel)" in prompts[0]


async def test_tomorrow_is_announced_when_todays_window_has_passed(
    router, serpapi_payload, forecast_payload
):
    mock_upstream(router, serpapi_payload, forecast_payload)
    prompts: list[str] = []

    async def generate(system: str, user: str) -> dict:
        prompts.append(user)
        return good_card()

    # 18:00 UTC is 20:00 in Vienna, past today's 17:43 latest return.
    evening = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
    await plan_adventure(make_request(), now_utc=evening, generate=generate)

    assert "TOMORROW" in prompts[0]


async def test_early_morning_is_still_today(router, serpapi_payload, forecast_payload):
    """01:00 local has not yet passed today's sunset, so the trip is today."""
    mock_upstream(router, serpapi_payload, forecast_payload)
    prompts: list[str] = []

    async def generate(system: str, user: str) -> dict:
        prompts.append(user)
        return good_card()

    small_hours = datetime(2026, 10, 11, 23, 0, tzinfo=timezone.utc)  # 01:00 Vienna
    await plan_adventure(make_request(), now_utc=small_hours, generate=generate)

    assert "TOMORROW" not in prompts[0]
    assert "half_day (240 minutes total, including travel)" in prompts[0]


# ------------------------------------------------------------------------- caching


async def test_repeated_requests_reuse_the_cached_search(router, serpapi_payload, forecast_payload):
    serp_route = router.get(SERPAPI_URL)
    serp_route.mock(return_value=httpx.Response(200, json=serpapi_payload))
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))

    from app.services.cache import TTLCache

    cache = TTLCache(ttl_seconds=3600)
    await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(), cache=cache)
    first_calls = serp_route.call_count
    await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(), cache=cache)

    assert serp_route.call_count == first_calls, "a reroll must not spend quota"


async def test_at_most_two_queries_per_request(router, serpapi_payload, forecast_payload):
    serp_route = router.get(SERPAPI_URL)
    serp_route.mock(return_value=httpx.Response(200, json=serpapi_payload))
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))

    from app.services.cache import TTLCache

    await plan_adventure(make_request(), now_utc=NOW, generate=ok_llm(), cache=TTLCache(3600))

    assert serp_route.call_count <= 2