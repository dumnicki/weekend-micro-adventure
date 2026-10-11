"""Step 4 check: geocode a city and print its weather summary.

    python scripts/preview_weather.py Vienna
    python scripts/preview_weather.py Vienna --save     # also refresh the fixtures

Open-Meteo needs no API key, so this is safe to run as often as needed. Tests never
call it: they read the saved fixtures through respx mocks.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.services.geocode import GEOCODE_URL, UnknownPlaceError, geocode  # noqa: E402
from app.services.weather import FORECAST_URL, DAILY_FIELDS, HOURLY_FIELDS, fetch_weather  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


async def main() -> int:
    city = sys.argv[1] if len(sys.argv) > 1 else "Vienna"
    save = "--save" in sys.argv

    try:
        place = await geocode(city)
    except UnknownPlaceError:
        print(f"Could not find that place: {city}")
        return 1

    print(f"city        : {city}")
    print(f"resolved to : {place.name}")
    print(f"lat / lng   : {place.lat}, {place.lng}")
    print(f"timezone    : {place.timezone}")

    weather = await fetch_weather(place.lat, place.lng)
    if weather is None:
        print("weather     : weather unknown (forecast unavailable)")
    else:
        print(f"sunrise     : {weather.sunrise_local}")
        print(f"sunset      : {weather.sunset_local}")
        print(f"temp_c      : {weather.temp_c}")
        print(f"rain chance : {weather.rain_chance_percent}%")
        print(f"summary     : {weather.summary}")

    if save:
        async with httpx.AsyncClient(timeout=15) as client:
            geo_raw = (await client.get(GEOCODE_URL, params={"name": city, "count": 1, "format": "json"})).json()
            fc_raw = (
                await client.get(
                    FORECAST_URL,
                    params={
                        "latitude": place.lat,
                        "longitude": place.lng,
                        "daily": DAILY_FIELDS,
                        "hourly": HOURLY_FIELDS,
                        "timezone": "auto",
                    },
                )
            ).json()
        (FIXTURES / "openmeteo_geocode_sample.json").write_text(
            json.dumps(geo_raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (FIXTURES / "openmeteo_sample.json").write_text(
            json.dumps(fc_raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nsaved fixtures to {FIXTURES}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))