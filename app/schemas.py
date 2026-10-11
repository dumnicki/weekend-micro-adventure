"""Pydantic models for the request, the internal candidate, and the model output card.

Contracts come from PLAN.md section 6. The rules in section 7.8 are deliberately NOT
encoded here: validate.py is the single place that decides whether a card is rejected,
so that the retry and fallback paths have one source of truth.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

TimeBudget = Literal["1h", "half_day", "full_day"]
Transport = Literal["foot", "bike", "transit"]
Vibe = Literal["quiet", "active", "curious"]


class AdventureRequest(BaseModel):
    """POST /api/adventure body. 422 on anything this rejects."""

    time_budget: TimeBudget
    transport: Transport
    vibe: Vibe
    location_text: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    exclude_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _location_is_required(self) -> AdventureRequest:
        """PLAN.md 6.1: location_text OR both lat and lng. Coordinates win if both given."""
        has_text = bool((self.location_text or "").strip())
        has_lat = self.lat is not None
        has_lng = self.lng is not None

        if has_lat != has_lng:
            raise ValueError("lat and lng must be given together, or not at all")
        if not has_text and not has_lat:
            raise ValueError("Provide location_text, or both lat and lng")
        return self


class Candidate(BaseModel):
    """Internal candidate, already filtered and scored. PLAN.md 6.2."""

    id: str
    name: str
    type: str
    address: str | None = None
    lat: float
    lng: float
    rating: float | None = None
    reviews: int | None = None
    description: str | None = None
    hours_text: str | None = None
    distance_km: float
    travel_minutes_one_way: float
    activity_minutes_available: int


class AdventureCard(BaseModel):
    """What the model must produce. PLAN.md 6.3."""

    candidate_id: str
    title: str
    duration_minutes: int
    getting_there: str
    plan: list[str]
    tip: str
    leave_by: str
    check_before_you_go: str


class AdventureResponse(BaseModel):
    """The card plus the fields code contributes. PLAN.md 6.3 and 7.10.

    Place name, address, coordinates and map link come from the candidate, never from
    the model, so a hallucinated card cannot move the user to the wrong place.
    """

    candidate_id: str
    title: str
    duration_minutes: int
    getting_there: str
    plan: list[str]
    tip: str
    leave_by: str
    check_before_you_go: str

    place_name: str
    address: str | None = None
    lat: float
    lng: float
    maps_link: str
    weather_summary: str | None = None
    # True when the model failed twice and PLAN.md 7.9's template card was used instead.
    # Not in the section 6.3 contract, but without it there is no way to tell from the
    # outside whether Gemma actually wrote the card. The demo story depends on that.
    used_fallback: bool = False


class EmptyResponse(BaseModel):
    """PLAN.md 7.5. HTTP 200 with no card, so the client shows a message not an error."""

    empty: bool = True
    message: str