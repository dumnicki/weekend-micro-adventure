"""Place search via SerpApi google_maps. PLAN.md 7.4.

One search costs one unit of the monthly quota, so this module is deliberately narrow:
at most two queries per request, every response cached, and nothing retried.

Field names come from https://serpapi.com/google-maps-api and were checked against a
real saved response in tests/fixtures/serpapi_maps_sample.json.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.services.cache import TTLCache, place_cache_key

SERPAPI_URL = "https://serpapi.com/search.json"
DEFAULT_TIMEOUT = 15.0

# PLAN.md 7.4: two queries per vibe, at most two per request.
VIBE_QUERIES: dict[str, tuple[str, ...]] = {
    "quiet": ("quiet park", "nature reserve"),
    "active": ("hiking trail", "scenic viewpoint"),
    "curious": ("botanical garden", "nature trail"),
}
MAX_QUERIES_PER_REQUEST = 2
MAP_ZOOM = "14z"

logger = logging.getLogger(__name__)


class SerpApiError(Exception):
    """SerpApi could not be reached or returned an error response."""


class RawPlace(dict):
    """One local_results entry, normalised to the names used downstream.

    Kept as a plain dict so it can be compared and sliced in tests without a pydantic
    round trip. Step 6 turns these into Candidate objects.
    """

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> RawPlace | None:
        """Return None when the entry lacks the id or coordinates candidates.py needs."""
        if not isinstance(result, dict):
            return None

        place_id = result.get("place_id")
        coords = result.get("gps_coordinates") or {}
        lat = coords.get("latitude")
        lng = coords.get("longitude")

        if not place_id or lat is None or lng is None:
            return None

        types = result.get("types") or []
        if not isinstance(types, list):
            types = [types]

        rating = result.get("rating")
        reviews = result.get("reviews")

        return cls(
            id=place_id,
            name=result.get("title") or "",
            type=result.get("type") or "",
            types=[str(t) for t in types],
            address=result.get("address"),
            lat=float(lat),
            lng=float(lng),
            rating=float(rating) if isinstance(rating, (int, float)) else None,
            reviews=int(reviews) if isinstance(reviews, (int, float)) else None,
            # Present but sparse: only 4 of 20 results in the saved response carry one.
            # None is the normal case, not an error, and the prompt already tells the
            # model to say so in check_before_you_go rather than invent something.
            description=result.get("description") or None,
            hours=result.get("hours") or None,
            operating_hours=result.get("operating_hours") or None,
            open_state=result.get("open_state") or None,
        )


def parse_local_results(payload: dict[str, Any]) -> list[RawPlace]:
    """Pull usable places out of a SerpApi response. Never raises on a missing field."""
    if not isinstance(payload, dict):
        return []

    results = payload.get("local_results")
    if not isinstance(results, list):
        return []

    places: list[RawPlace] = []
    for result in results:
        place = RawPlace.from_result(result)
        if place is not None:
            places.append(place)
    return places


async def search_places(
    lat: float,
    lng: float,
    query: str,
    cache: TTLCache[list[RawPlace]] | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[RawPlace]:
    """Run one google_maps search. Returns [] rather than raising on failure.

    An empty list is a legitimate answer and is cached, so a repeated query for the same
    spot does not spend quota to rediscover that there is nothing there.
    """
    key = place_cache_key(lat, lng, query)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            logger.debug("cache hit for %s", query)
            return hit

    if not settings.serpapi_api_key:
        logger.warning("SERPAPI_API_KEY is missing; returning no places")
        return []

    params = {
        "engine": "google_maps",
        "type": "search",
        "q": query,
        "ll": f"@{lat},{lng},{MAP_ZOOM}",
        "hl": "en",
        "api_key": settings.serpapi_api_key,
    }

    try:
        if client is not None:
            response = await client.get(SERPAPI_URL, params=params)
        else:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as owned:
                response = await owned.get(SERPAPI_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # An errored search is not billed by SerpApi, but we still return empty so one
        # failed query cannot take down the whole pipeline.
        logger.warning("SerpApi query %r failed (%s: %s)", query, type(exc).__name__, exc)
        return []

    places = parse_local_results(payload)
    logger.info("SerpApi %r returned %d usable places", query, len(places))

    if cache is not None:
        cache.set(key, places)
    return places


def queries_for_vibe(vibe: str) -> tuple[str, ...]:
    """The query words for a vibe, capped at MAX_QUERIES_PER_REQUEST."""
    queries = VIBE_QUERIES.get(vibe, VIBE_QUERIES["curious"])
    return queries[:MAX_QUERIES_PER_REQUEST]