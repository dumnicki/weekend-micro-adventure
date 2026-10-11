"""Free-text place to coordinates. PLAN.md 7.1.

Open-Meteo geocoding, which needs no API key. A text that matches nothing raises
UnknownPlaceError so the route can turn it into a 404 with a clear message, rather
than passing None down the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
DEFAULT_TIMEOUT = 10.0


class UnknownPlaceError(Exception):
    """Geocoding found no match. Routes map this to 404 "Could not find that place"."""


@dataclass(frozen=True)
class GeoLocation:
    """A resolved starting point."""

    name: str
    lat: float
    lng: float
    country: str | None = None
    timezone: str | None = None


async def geocode(
    location_text: str,
    client: httpx.AsyncClient | None = None,
) -> GeoLocation:
    """Resolve location_text to a GeoLocation.

    Raises UnknownPlaceError when the service returns no usable result. Transport
    errors are deliberately not swallowed: a dead geocoder is a server fault, and
    pretending the place does not exist would send the user to a 404 that lies.
    """
    text = location_text.strip()
    if not text:
        raise UnknownPlaceError("Could not find that place")

    params = {"name": text, "count": 1, "format": "json"}
    if client is not None:
        response = await client.get(GEOCODE_URL, params=params)
    else:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as owned:
            response = await owned.get(GEOCODE_URL, params=params)

    response.raise_for_status()
    results: list[dict[str, Any]] = response.json().get("results") or []

    for result in results:
        lat, lng = result.get("latitude"), result.get("longitude")
        if lat is None or lng is None:
            continue
        name = result.get("name") or text
        region = result.get("admin1")
        country = result.get("country")
        label = ", ".join(part for part in (name, region, country) if part)
        return GeoLocation(
            name=label or text,
            lat=float(lat),
            lng=float(lng),
            country=country,
            timezone=result.get("timezone"),
        )

    raise UnknownPlaceError("Could not find that place")