"""HTTP surface. PLAN.md 7.1, 7.5 and the error mapping from the Step 8 build notes.

Error policy, in one place:
  422  the request itself is wrong (pydantic, automatic)
  404  the place could not be found
  200  an empty result, with a message
  200  a fallback card when the model fails, never a 500
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.schemas import AdventureRequest, AdventureResponse, EmptyResponse
from app.services.geocode import UnknownPlaceError
from app.services.llm import generate_json
from app.services.planner import GenerateJson, plan_adventure

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def get_generate() -> GenerateJson:
    """Seam for tests to swap the model out. Only llm.py knows about Backboard."""
    return generate_json


@router.post(
    "/api/adventure",
    response_model=None,
    responses={
        200: {
            "model": AdventureResponse,
            "description": "One adventure card, or an empty result.",
        },
        404: {"model": EmptyResponse, "description": "Place not found."},
    },
)
async def adventure(
    request: AdventureRequest,
    generate: GenerateJson = Depends(get_generate),
) -> AdventureResponse | EmptyResponse:
    try:
        result = await plan_adventure(request, generate=generate)
    except UnknownPlaceError as exc:
        raise HTTPException(status_code=404, detail="Could not find that place") from exc

    if result.empty:
        # 200, not an error: there simply was nothing good nearby.
        return EmptyResponse(empty=True, message=result.message or "Nothing good nearby.")

    assert result.response is not None  # guaranteed unless empty
    response = result.response.model_copy(update={"used_fallback": result.used_fallback})
    return response