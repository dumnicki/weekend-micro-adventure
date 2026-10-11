"""The whole pipeline in order. PLAN.md 7.1 to 7.10.

location -> weather -> places -> candidates -> model -> validate -> retry -> fallback -> merge

The one rule that shapes this file: the app must never show an error screen when it can
show a fallback card. Every model problem degrades to the template card in 7.9, and a
weather problem degrades to "weather unknown". Only an unknown place and a bad request
are allowed to stop the request.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from app.schemas import AdventureRequest, AdventureResponse, Candidate
from app.config import settings
from app.services.candidates import (
    DaylightWindow,
    budget_for,
    build_candidates,
    maps_link,
    resolve_daylight,
)
from app.services.cache import TTLCache
from app.services.geocode import GeoLocation, geocode
from app.services.llm import generate_json
from app.services.places import queries_for_vibe, search_places
from app.services.validate import validate_card
from app.services.weather import WEATHER_UNKNOWN, fetch_weather

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# PLAN.md 7.5, verbatim.
EMPTY_MESSAGE = "Nothing good nearby for that. Try a longer time or another transport."

# When the forecast is unavailable there is no sunset to plan against. Rather than
# invent one, we stop enforcing the daylight rule and say so on the card.
NO_DAYLIGHT_RETURN = "23:59"

GenerateJson = Callable[[str, str], Awaitable[dict[str, Any]]]

# One cache for the whole process. A cache created per call would be empty every time,
# so every request and every re-roll would spend SerpApi quota for nothing.
_shared_cache: TTLCache | None = None


def get_places_cache() -> TTLCache:
    global _shared_cache
    if _shared_cache is None:
        _shared_cache = TTLCache(ttl_seconds=settings.cache_ttl_seconds)
    return _shared_cache


def reset_places_cache() -> None:
    """Drop cached searches. Used by tests and by the cache TTL in long runs."""
    global _shared_cache
    _shared_cache = None


@dataclass
class AdventureResult:
    """Either a card, or the empty answer. Never an exception for a model problem."""

    response: AdventureResponse | None = None
    empty: bool = False
    message: str | None = None
    used_fallback: bool = False


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _render(template: str, values: dict[str, str]) -> str:
    # Plain replace, not .format(): the candidates JSON is full of braces.
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _now_local(utc_offset_seconds: int, now_utc: datetime) -> time:
    """Local time at the destination, which is the clock the user cares about."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(timezone.utc) + timedelta(seconds=utc_offset_seconds)
    return local.time()


def _daylight_for(
    weather: Any,
    budget_minutes: int,
    now_utc: datetime,
) -> tuple[DaylightWindow, str]:
    """Resolve the daylight window, degrading to no limit when weather is missing."""
    if weather is None:
        now = now_utc.astimezone(timezone.utc).time()
        window = DaylightWindow(
            now_local=now.strftime("%H:%M"),
            latest_return_time=NO_DAYLIGHT_RETURN,
            max_trip_minutes=budget_minutes,
            day_label="today",
        )
        return window, WEATHER_UNKNOWN

    now = _now_local(weather.utc_offset_seconds, now_utc)
    window = resolve_daylight(
        now,
        weather.sunset_local,
        weather.sunrise_next_day,
        weather.sunset_next_day,
        budget_minutes,
    )
    return window, weather.summary


def _build_user_message(
    request: AdventureRequest,
    candidates: list[Candidate],
    window: DaylightWindow,
    weather_summary: str,
    budget_minutes: int,
) -> str:
    payload = [c.model_dump(mode="json") for c in candidates]
    values = {
        "time_budget": request.time_budget,
        "budget_minutes": str(budget_minutes),
        "transport": request.transport,
        "vibe": request.vibe,
        "now_local": window.now_local,
        "latest_return_time": window.latest_return_time,
        "weather_summary": weather_summary,
        "candidates_json": json.dumps(payload, ensure_ascii=False, indent=2),
    }
    message = _render(_load_prompt("user_template.md"), values)
    if window.day_label == "tomorrow":
        message += (
            "\n\nToday is already too late for this trip. The plan is for TOMORROW, "
            "and leave_by must be a time tomorrow.\n"
        )
    return message


def _fallback_card(candidate: Candidate, window: DaylightWindow) -> dict[str, Any]:
    """PLAN.md 7.9. A plain card, built from code, when the model fails twice."""
    duration = candidate.activity_minutes_available
    return {
        "candidate_id": candidate.id,
        "title": f"Go to {candidate.name}",
        "duration_minutes": duration,
        "getting_there": (
            f"It is {candidate.distance_km} km away, about "
            f"{round(candidate.travel_minutes_one_way)} minutes each way."
        ),
        "plan": [
            f"Head to {candidate.name} ({candidate.distance_km} km away).",
            f"Walk or relax for about {duration} minutes.",
            f"Head back before {window.latest_return_time}.",
        ],
        "tip": "Bring water and a warm layer.",
        "leave_by": window.latest_return_time,
        "check_before_you_go": "Opening hours and conditions were not checked.",
    }


def _merge(
    card: dict[str, Any],
    candidate: Candidate,
    weather_summary: str,
) -> AdventureResponse:
    """PLAN.md 7.10. Place facts come from the candidate, never from the model."""
    return AdventureResponse(
        candidate_id=card["candidate_id"],
        title=card["title"],
        duration_minutes=card["duration_minutes"],
        getting_there=card["getting_there"],
        plan=card["plan"],
        tip=card["tip"],
        leave_by=card["leave_by"],
        check_before_you_go=card["check_before_you_go"],
        place_name=candidate.name,
        address=candidate.address,
        lat=candidate.lat,
        lng=candidate.lng,
        maps_link=maps_link(candidate),
        weather_summary=weather_summary,
    )


async def _call_model(
    system: str,
    user: str,
    candidates: list[Candidate],
    window: DaylightWindow,
    generate: GenerateJson,
) -> tuple[dict[str, Any], bool]:
    """One attempt, then one retry with the rejection reasons. PLAN.md 7.7."""
    last_errors: list[str] = []

    for attempt in range(2):
        message = user if attempt == 0 else (
            f"{user}\n\nYour previous answer was rejected: {last_errors[0]}\n"
            "Fix those problems and return the JSON card again."
        )
        try:
            raw = await generate(system, message)
        except Exception as exc:  # noqa: BLE001 - any model failure must degrade
            last_errors = [f"the model call failed ({type(exc).__name__}: {exc})"]
            continue

        result = validate_card(raw, candidates, window.latest_return_time)
        if result.ok and result.card is not None:
            return result.card.model_dump(), False
        last_errors = result.errors or ["the answer was rejected for an unknown reason"]

    fallback = _fallback_card(candidates[0], window)
    check = validate_card(fallback, candidates, window.latest_return_time)
    # The fallback is built from our own numbers, so it should always pass. If it does
    # not, the numbers themselves are wrong and the card is returned unvalidated rather
    # than raising: a slightly off plan beats an error screen.
    return (check.card.model_dump() if check.ok and check.card else fallback), True


async def plan_adventure(
    request: AdventureRequest,
    *,
    now_utc: datetime | None = None,
    generate: GenerateJson = generate_json,
    http_client: Any | None = None,
    cache: TTLCache | None = None,
) -> AdventureResult:
    """Run the whole pipeline. Raises only UnknownPlaceError, which the route maps to 404."""
    now_utc = now_utc or datetime.now(timezone.utc)

    # 7.1 location
    if request.lat is not None and request.lng is not None:
        origin = GeoLocation(
            name=request.location_text or "your location",
            lat=request.lat,
            lng=request.lng,
        )
    else:
        origin = await geocode(request.location_text or "", client=http_client)

    # 7.2 weather. Failure is survivable by design.
    weather = await fetch_weather(origin.lat, origin.lng, client=http_client)

    # 7.6 daylight, before candidate selection, because it caps the budget.
    requested_budget = budget_for(request.time_budget)
    window, weather_summary = _daylight_for(weather, requested_budget, now_utc)
    budget_minutes = min(requested_budget, window.max_trip_minutes)

    # 7.4 places. At most two queries per request.
    raw_places: list[dict[str, Any]] = []
    cache = cache or get_places_cache()
    for query in queries_for_vibe(request.vibe):
        raw_places.extend(
            await search_places(origin.lat, origin.lng, query, cache=cache, client=http_client)
        )

    # 7.5 candidates
    candidates = build_candidates(
        raw_places,
        origin_lat=origin.lat,
        origin_lng=origin.lng,
        time_budget=request.time_budget,
        transport=request.transport,
        exclude_ids=request.exclude_ids,
        budget_minutes=budget_minutes,
    )

    if not candidates:
        return AdventureResult(empty=True, message=EMPTY_MESSAGE)

    # 7.7 and 7.8 model, retry, fallback
    system = _load_prompt("system.md")
    user = _build_user_message(request, candidates, window, weather_summary, budget_minutes)
    card, used_fallback = await _call_model(system, user, candidates, window, generate)

    # 7.10 merge
    chosen = next((c for c in candidates if c.id == card.get("candidate_id")), candidates[0])
    return AdventureResult(
        response=_merge(card, chosen, weather_summary),
        used_fallback=used_fallback,
    )