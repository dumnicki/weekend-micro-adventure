"""Filtering, distance, time math, scoring and the daylight limit. PLAN.md 7.3, 7.5, 7.6.

Pure code, no network and no model. Every decision that can be made from numbers is
made here, so the model is only left with choosing one place and writing sentences.

The filter runs in the order given in 7.5 and that order is load-bearing: dedupe before
scoring so a duplicated place cannot occupy two of the eight slots.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from math import asin, cos, log, radians, sin, sqrt
from typing import Any

from app.schemas import Candidate

# ------------------------------------------------------------------ PLAN.md 7.3

BUDGET_MINUTES: dict[str, int] = {"1h": 60, "half_day": 240, "full_day": 480}

RADIUS_KM: dict[str, dict[str, float]] = {
    "foot": {"1h": 2.0, "half_day": 6.0, "full_day": 12.0},
    "bike": {"1h": 6.0, "half_day": 20.0, "full_day": 40.0},
    "transit": {"1h": 5.0, "half_day": 25.0, "full_day": 50.0},
}

SPEED_KMH: dict[str, float] = {"foot": 4.5, "bike": 14.0, "transit": 20.0}

# Fixed wait for a transit ride, applied once each way.
TRANSIT_OVERHEAD_MINUTES = 10

# Straight-line distance understates the real route.
ROUTE_FACTOR = 1.3

# Below this there is no trip, only a commute.
MIN_ACTIVITY_MINUTES = 20

DEFAULT_RATING = 3.5
TOP_N = 8

# PLAN.md 7.5 step 4. Matched case-insensitively against type and types.
OUTDOOR_KEYWORDS: tuple[str, ...] = (
    "park", "trail", "garden", "forest", "lake",
    "reserve", "viewpoint", "nature", "hiking", "beach", "river", "woods",
)

# PLAN.md 7.6: come back half an hour before dark.
SUNSET_SAFETY_MINUTES = 30


# ---------------------------------------------------------------------- geometry


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km. PLAN.md 15."""
    r = 6371.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * r * asin(sqrt(a))


def budget_for(time_budget: str) -> int:
    return BUDGET_MINUTES.get(time_budget, BUDGET_MINUTES["half_day"])


def radius_for(transport: str, time_budget: str) -> float:
    return RADIUS_KM.get(transport, RADIUS_KM["foot"]).get(
        time_budget, RADIUS_KM.get(transport, RADIUS_KM["foot"])["half_day"]
    )


def speed_for(transport: str) -> float:
    return SPEED_KMH.get(transport, SPEED_KMH["foot"])


def travel_minutes(distance_km: float, transport: str) -> int:
    """One-way travel, route-corrected, with the transit wait added."""
    speed = speed_for(transport)
    overhead = TRANSIT_OVERHEAD_MINUTES if transport == "transit" else 0
    return round(distance_km * ROUTE_FACTOR / speed * 60) + overhead


def activity_minutes(distance_km: float, transport: str, budget_minutes: int) -> int:
    """Time actually spent at the place once both legs are subtracted."""
    return budget_minutes - 2 * travel_minutes(distance_km, transport)


# ------------------------------------------------------------------ PLAN.md 7.6


@dataclass(frozen=True)
class DaylightWindow:
    """How long the trip may run, after daylight is taken into account."""

    now_local: str
    latest_return_time: str
    max_trip_minutes: int
    day_label: str  # "today" or "tomorrow"


def _hhmm_to_minutes(value: str) -> int:
    """'17:42' -> 1062. datetime.time cannot be offset by a timedelta, so the daylight
    maths runs on plain minutes since midnight."""
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _minutes_to_hhmm(total: int) -> str:
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def resolve_daylight(
    now: time,
    sunset: str,
    sunrise_next_day: str | None,
    sunset_next_day: str | None,
    budget_minutes: int,
) -> DaylightWindow:
    """Trim the budget so the user is home before dark, or move the trip to tomorrow.

    PLAN.md 7.6. Falls back to tomorrow's window when today's latest return has passed.
    Without tomorrow's sunrise and sunset in the forecast it returns a zero-length
    window, which lets the caller return the empty message instead of inventing a plan.
    """
    latest_today = _hhmm_to_minutes(sunset) - SUNSET_SAFETY_MINUTES
    now_label = _minutes_to_hhmm(now.hour * 60 + now.minute)

    if now.hour * 60 + now.minute >= latest_today:
        if not sunrise_next_day or not sunset_next_day:
            return DaylightWindow(now_label, _minutes_to_hhmm(latest_today), 0, "tomorrow")
        latest = _hhmm_to_minutes(sunset_next_day) - SUNSET_SAFETY_MINUTES
        # The trip happens tomorrow, so today's clock does not shorten it. It is still
        # bounded by tomorrow's daylight and by what the user asked for.
        return DaylightWindow(now_label, _minutes_to_hhmm(latest), budget_minutes, "tomorrow")

    minutes_left = latest_today - (now.hour * 60 + now.minute)
    return DaylightWindow(
        now_label,
        _minutes_to_hhmm(latest_today),
        max(0, min(budget_minutes, minutes_left)),
        "today",
    )


# ------------------------------------------------------------------ PLAN.md 7.5


def _is_outdoor_like(place: Mapping[str, Any]) -> bool:
    haystack = " ".join(
        str(part)
        for part in (place.get("type"), *(place.get("types") or []))
        if part
    ).lower()
    return any(keyword in haystack for keyword in OUTDOOR_KEYWORDS)


def _score(place: Mapping[str, Any]) -> float:
    """rating * log(1 + reviews). A missing rating counts as 3.5 per 7.5."""
    rating = place.get("rating")
    rating = float(rating) if isinstance(rating, (int, float)) else DEFAULT_RATING
    reviews = place.get("reviews")
    reviews = float(reviews) if isinstance(reviews, (int, float)) else 0.0
    if rating <= 0 or reviews < 0:
        return 0.0
    return rating * log(1 + reviews)


def _hours_text(place: Mapping[str, Any]) -> str | None:
    """Prefer the status string; fall back to operating_hours only when unambiguous.

    All seven weekdays identical is a safe one-line summary. Anything more varied is
    left as None so the model says "check before you go" instead of summarising hours
    it cannot verify.
    """
    hours = place.get("hours")
    if hours:
        return str(hours)

    operating = place.get("operating_hours")
    if isinstance(operating, Mapping) and operating:
        values = {str(v) for v in operating.values() if v}
        if len(values) == 1:
            return values.pop()
    return None


def build_candidates(
    places: Sequence[Mapping[str, Any]],
    *,
    origin_lat: float,
    origin_lng: float,
    time_budget: str,
    transport: str,
    exclude_ids: Iterable[str] = (),
    budget_minutes: int | None = None,
    top_n: int = TOP_N,
) -> list[Candidate]:
    """Filter, score and rank. Returns at most top_n, best first.

    An empty list is a legitimate result: the caller answers with the empty message
    from 7.5 and never calls the model.
    """
    excluded = {str(pid) for pid in exclude_ids}
    budget = budget_for(time_budget) if budget_minutes is None else budget_minutes
    radius = radius_for(transport, time_budget)

    scored: list[tuple[float, Candidate]] = []
    seen: set[str] = set()

    for place in places:
        # 1. usable coordinates and an id
        place_id = place.get("id")
        lat, lng = place.get("lat"), place.get("lng")
        if not place_id or lat is None or lng is None:
            continue

        # 2. no duplicate place_ids
        place_id = str(place_id)
        if place_id in seen:
            continue
        seen.add(place_id)

        # 3. re-roll exclusions
        if place_id in excluded:
            continue

        # 4. outdoor-like only
        if not _is_outdoor_like(place):
            continue

        # 5. distance
        distance = haversine_km(origin_lat, origin_lng, float(lat), float(lng))
        if distance > radius:
            continue

        # 6. time math
        available = activity_minutes(distance, transport, budget)
        if available < MIN_ACTIVITY_MINUTES:
            continue

        candidate = Candidate(
            id=place_id,
            name=str(place.get("name") or ""),
            type=str(place.get("type") or ""),
            address=place.get("address"),
            lat=float(lat),
            lng=float(lng),
            rating=place.get("rating") if isinstance(place.get("rating"), (int, float)) else None,
            reviews=place.get("reviews") if isinstance(place.get("reviews"), int) else None,
            description=place.get("description") or None,
            hours_text=_hours_text(place),
            distance_km=round(distance, 2),
            travel_minutes_one_way=travel_minutes(distance, transport),
            activity_minutes_available=available,
        )
        scored.append((_score(place), candidate))

    # 7 and 8: score descending, then keep the top N.
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [candidate for _, candidate in scored[:top_n]]


def maps_link(candidate: Candidate) -> str:
    """PLAN.md 7.10. Built from the candidate, never from model output."""
    return (
        "https://www.google.com/maps/search/?api=1"
        f"&query={candidate.lat},{candidate.lng}"
        f"&query_place_id={candidate.id}"
    )