"""Today's weather and daylight. PLAN.md 7.2.

Open-Meteo, no API key. Every failure path here returns None instead of raising:
weather is context, never a reason to refuse a trip. The planner substitutes
WEATHER_UNKNOWN and the request still produces a card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT = 10.0

# Shown to the model and on the card when the forecast could not be read.
WEATHER_UNKNOWN = "weather unknown"

DAILY_FIELDS = "sunset,sunrise,precipitation_probability_max,temperature_2m_max,temperature_2m_min"
HOURLY_FIELDS = "precipitation_probability,temperature_2m"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Weather:
    """What the planner and the card need. All times are local to the location.

    The next-day values exist for PLAN.md 7.6, which falls back to tomorrow's window
    once today's latest return time has passed.
    """

    sunset_local: str
    sunrise_local: str
    temp_c: float
    rain_chance_percent: int
    summary: str
    sunrise_next_day: str | None = None
    sunset_next_day: str | None = None
    # Seconds to add to UTC for the location's local time. The daylight maths in 7.6
    # needs "now" as the user experiences it, which is the destination's clock, not the
    # server's. Without this the trip would be planned against the wrong sunset.
    utc_offset_seconds: int = 0


def _hhmm(iso_value: str) -> str:
    """'2026-10-11T17:42' or '...T17:42:33' -> '17:42'."""
    return str(iso_value).split("T")[1][:5]


def _first(series: object) -> object | None:
    """First value of an Open-Meteo daily list, or None when empty or absent."""
    if isinstance(series, list) and series:
        return series[0]
    return None


def _second(series: object) -> object | None:
    """Second value of an Open-Meteo daily list. Used for the next-day window."""
    if isinstance(series, list) and len(series) > 1:
        return series[1]
    return None


def build_weather(payload: dict) -> Weather | None:
    """Turn a forecast payload into a Weather, or None if the fields we need are absent."""
    if not isinstance(payload, dict):
        return None
    daily = payload.get("daily") or {}
    if not isinstance(daily, dict):
        return None

    sunset_raw = _first(daily.get("sunset"))
    sunrise_raw = _first(daily.get("sunrise"))
    if sunset_raw is None or sunrise_raw is None:
        return None

    temp_raw = _first(daily.get("temperature_2m_max"))
    if temp_raw is None:
        return None

    rain_raw = _first(daily.get("precipitation_probability_max"))

    sunset = _hhmm(sunset_raw)
    sunrise = _hhmm(sunrise_raw)
    temp_c = round(float(temp_raw))
    rain = int(rain_raw) if rain_raw is not None else None

    # Daily high, not the current reading: the question is what the day feels like,
    # not what the thermometer says at request time.
    summary = f"{temp_c}°C"
    if rain is not None:
        summary += f", {rain}% rain chance"
    summary += f", sunset {sunset}"

    sunrise_next = _second(daily.get("sunrise"))
    sunset_next = _second(daily.get("sunset"))

    return Weather(
        sunset_local=sunset,
        sunrise_local=sunrise,
        temp_c=temp_c,
        rain_chance_percent=rain if rain is not None else 0,
        summary=summary,
        sunrise_next_day=_hhmm(sunrise_next) if sunrise_next else None,
        sunset_next_day=_hhmm(sunset_next) if sunset_next else None,
        utc_offset_seconds=int(payload.get("utc_offset_seconds") or 0),
    )


async def fetch_weather(
    lat: float,
    lng: float,
    client: httpx.AsyncClient | None = None,
) -> Weather | None:
    """Fetch today's forecast. Returns None on any failure, by design.

    Callers must handle None. The log line is the only trace a failure leaves, which
    is why it is at warning level rather than debug.
    """
    params = {
        "latitude": lat,
        "longitude": lng,
        "daily": DAILY_FIELDS,
        "hourly": HOURLY_FIELDS,
        "timezone": "auto",
    }

    try:
        if client is not None:
            response = await client.get(FORECAST_URL, params=params)
        else:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as owned:
                response = await owned.get(FORECAST_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Open-Meteo forecast unavailable (%s: %s)", type(exc).__name__, exc)
        return None

    weather = build_weather(payload)
    if weather is None:
        logger.warning("Open-Meteo payload had no usable daily fields; treating as unknown")
    return weather