"""The only module that talks to Backboard."""

from __future__ import annotations

import json
import re
from typing import Any

from backboard import BackboardClient

from app.config import settings

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_json_content(raw: str) -> dict[str, Any]:
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Model output is not a JSON object")
    return parsed


async def generate_json(system: str, user: str) -> dict[str, Any]:
    if not settings.backboard_api_key:
        raise RuntimeError("BACKBOARD_API_KEY is missing")
    if not settings.backboard_model:
        raise RuntimeError("BACKBOARD_MODEL is missing")

    provider = settings.backboard_provider or None
    timeout = settings.llm_timeout_seconds

    async with BackboardClient(
        api_key=settings.backboard_api_key,
        timeout=timeout,
    ) as client:
        response = await client.send_message(
            user,
            system_prompt=system,
            llm_provider=provider,
            model_name=settings.backboard_model,
            json_output=True,
            memory="off",
            stream=False,
        )

    content = getattr(response, "content", None)
    if not content:
        raise ValueError("Empty model response")
    return _parse_json_content(content)
