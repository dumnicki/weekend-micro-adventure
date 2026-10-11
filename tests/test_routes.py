"""Tests for the HTTP surface. PLAN.md 6.1, 7.5 and the Step 8 error mapping.

The model is replaced through a FastAPI dependency override and HTTP is mocked with
respx, so no test reaches a real API.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routes import get_generate
from app.services.planner import reset_places_cache
from app.services.places import SERPAPI_URL
from app.services.weather import FORECAST_URL

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VIENNA = {"lat": 48.20849, "lng": 16.37208}
# 14:20 UTC is 16:20 in Vienna, before the fixture's 17:43 latest return.
NOW = datetime(2026, 10, 11, 14, 20, tzinfo=timezone.utc)

VALID_BODY = {
    "time_budget": "half_day",
    "transport": "foot",
    "vibe": "quiet",
    **VIENNA,
}


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Freeze the planner's clock.

    The route passes no clock, so the planner would use the real one. That moves the
    daylight window, which moves the candidate list, which invalidates any card written
    against the saved fixture. Pinning time keeps these tests deterministic and matches
    the clock test_planner.py uses.
    """

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("app.services.planner.datetime", FrozenDatetime)


@pytest.fixture
def client():
    reset_places_cache()
    with TestClient(app) as test_client:
        yield test_client
    reset_places_cache()


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


def good_card() -> dict:
    """Valid enough to pass validation for the fixture's top candidate."""
    return {
        "candidate_id": "ChIJQ0Ey03UHbUcRRcGuE0WeIL8",
        "title": "Stadtpark",
        "duration_minutes": 45,
        "getting_there": "Walk about 12 minutes.",
        "plan": ["Walk there.", "Sit by the river."],
        "tip": "Bring water.",
        "leave_by": "16:30",
        "check_before_you_go": "Check the gates.",
    }


def use_model(card=None, calls=None):
    """Override the model dependency.

    The override must have the same shape as get_generate: a zero-argument factory that
    RETURNS the generate function. Overriding with the generate function itself makes
    FastAPI call it while resolving dependencies, before the planner runs.
    """

    async def generate(system: str, user: str) -> dict:
        if calls is not None:
            calls.append(user)
        return card if card is not None else good_card()

    def factory():
        return generate

    app.dependency_overrides[get_generate] = factory
    return calls


def mock_upstream(router, serpapi_payload, forecast_payload):
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


# ------------------------------------------------------------------------- health


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ------------------------------------------------------------------------ good path


def test_returns_a_full_card(client, router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    use_model()

    response = client.post("/api/adventure", json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["place_name"]
    assert body["title"]
    assert len(body["plan"]) >= 2
    assert body["maps_link"].startswith("https://www.google.com/maps/search/?api=1")
    assert body["weather_summary"]


def test_used_fallback_is_false_on_the_happy_path(client, router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    use_model()

    body = client.post("/api/adventure", json=VALID_BODY).json()

    assert body["used_fallback"] is False


def test_reroll_excludes_the_previous_place(client, router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    use_model()

    first = client.post("/api/adventure", json=VALID_BODY).json()
    second = client.post(
        "/api/adventure", json={**VALID_BODY, "exclude_ids": [first["candidate_id"]]}
    ).json()

    assert second["candidate_id"] != first["candidate_id"]


def test_reroll_does_not_spend_serpapi_quota(client, router, serpapi_payload, forecast_payload):
    """PLAN.md 7.4: a re-roll must be served from the cache."""
    serp_route = router.get(SERPAPI_URL)
    serp_route.mock(return_value=httpx.Response(200, json=serpapi_payload))
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))
    use_model()

    client.post("/api/adventure", json=VALID_BODY)
    first_calls = serp_route.call_count
    client.post("/api/adventure", json={**VALID_BODY, "exclude_ids": ["something"]})

    assert serp_route.call_count == first_calls


def test_location_text_works_too(client, router, serpapi_payload, forecast_payload):
    from app.services.geocode import GEOCODE_URL

    router.get(GEOCODE_URL).mock(
        return_value=httpx.Response(
            200, json={"results": [{"name": "Vienna", "latitude": VIENNA["lat"],
                                    "longitude": VIENNA["lng"]}]}
        )
    )
    mock_upstream(router, serpapi_payload, forecast_payload)
    use_model()

    response = client.post(
        "/api/adventure",
        json={"time_budget": "1h", "transport": "foot", "vibe": "curious", "location_text": "Vienna"},
    )

    assert response.status_code == 200
    assert response.json()["place_name"]


# ------------------------------------------------------------------------ fallback


def test_model_failure_returns_200_with_a_fallback_card(client, router, serpapi_payload, forecast_payload):
    """PLAN.md 7.9: never an error screen when a fallback card is possible."""
    mock_upstream(router, serpapi_payload, forecast_payload)

    async def broken(system: str, user: str) -> dict:
        raise RuntimeError("Backboard is down")

    app.dependency_overrides[get_generate] = lambda: broken

    response = client.post("/api/adventure", json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["used_fallback"] is True
    assert body["title"].startswith("Go to ")


def test_garbage_from_the_model_still_returns_200(client, router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    use_model(card={"totally": "wrong"})

    response = client.post("/api/adventure", json=VALID_BODY)

    assert response.status_code == 200
    assert response.json()["used_fallback"] is True


# ------------------------------------------------------------------- empty result


def test_no_candidates_returns_200_with_empty_true(client, router, serpapi_payload, forecast_payload):
    mock_upstream(router, serpapi_payload, forecast_payload)
    nothing = json.loads(json.dumps(serpapi_payload))
    nothing["local_results"] = [
        {**r, "type": "Restaurant", "types": ["Restaurant"]} for r in nothing["local_results"]
    ]
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=nothing))
    calls: list[str] = []
    use_model(calls=calls)

    response = client.post("/api/adventure", json=VALID_BODY)

    assert response.status_code == 200
    body = response.json()
    assert body["empty"] is True
    assert "Nothing good nearby" in body["message"]
    assert calls == []


# ------------------------------------------------------------------------- errors


def test_unknown_place_returns_404(client, router):
    from app.services.geocode import GEOCODE_URL

    router.get(GEOCODE_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    response = client.post(
        "/api/adventure",
        json={"time_budget": "1h", "transport": "foot", "vibe": "quiet", "location_text": "Atlantis"},
    )

    assert response.status_code == 404
    assert "Could not find that place" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {},                                                              # everything missing
        {"time_budget": "1h", "transport": "foot", "vibe": "quiet"},     # no location
        {"time_budget": "2h", "transport": "foot", "vibe": "quiet", **VIENNA},   # bad enum
        {"time_budget": "1h", "transport": "car", "vibe": "quiet", **VIENNA},   # bad transport
        {"time_budget": "1h", "transport": "foot", "vibe": "quiet", "lat": 48.2},  # lng missing
        {"time_budget": "1h", "transport": "foot", "vibe": "quiet", "lat": 91, "lng": 16.3},  # range
    ],
)
def test_bad_input_returns_422(client, body):
    assert client.post("/api/adventure", json=body).status_code == 422


def test_weather_outage_does_not_fail_the_request(client, router, serpapi_payload):
    router.get(FORECAST_URL).mock(side_effect=httpx.ConnectError("down"))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json=serpapi_payload))
    use_model()

    response = client.post("/api/adventure", json=VALID_BODY)

    assert response.status_code == 200
    assert response.json()["weather_summary"] == "weather unknown"


def test_serpapi_outage_returns_200_not_500(client, router, forecast_payload):
    """With no search results there is nothing to offer, which is an empty answer."""
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))
    router.get(SERPAPI_URL).mock(side_effect=httpx.ConnectError("down"))
    use_model()

    response = client.post("/api/adventure", json=VALID_BODY)

    assert response.status_code == 200
    assert response.json()["empty"] is True


def test_no_key_configured_does_not_crash(client, router, forecast_payload, monkeypatch):
    router.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload))
    router.get(SERPAPI_URL).mock(return_value=httpx.Response(200, json={"local_results": []}))
    use_model()

    response = client.post("/api/adventure", json=VALID_BODY)

    assert response.status_code == 200
    assert response.json()["empty"] is True


# ------------------------------------------------------------------------ caching


def test_cache_ttl_comes_from_settings():
    from app.services.planner import get_places_cache

    reset_places_cache()
    cache = get_places_cache()
    assert cache.ttl_seconds == settings.cache_ttl_seconds
    reset_places_cache()