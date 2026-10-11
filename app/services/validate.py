"""Checks a model card against the candidate data. PLAN.md 7.8.

This is the only place that decides whether a card is accepted. The planner retries
once on failure and then falls back to a template card, so the reasons this collects
are also the text fed back into the retry prompt.

Type coercion is left to pydantic at the end. The manual rules run first so that a
badly typed field produces a readable reason instead of a traceback from a comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import ValidationError

from app.schemas import AdventureCard, Candidate

MAX_TITLE_CHARS = 80
MIN_PLAN_STEPS = 2
MAX_PLAN_STEPS = 5

# PLAN.md 7.8 says leave_by must be HH:MM in 24h time. Zero padded, so the front end
# can print it verbatim and string comparison against latest_return_time stays correct.
LEAVE_BY_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

CARD_FIELDS = (
    "candidate_id",
    "title",
    "duration_minutes",
    "getting_there",
    "plan",
    "tip",
    "leave_by",
    "check_before_you_go",
)
TEXT_FIELDS = ("title", "getting_there", "tip", "check_before_you_go")


@dataclass
class ValidationResult:
    """Outcome of validating one card."""

    ok: bool
    card: AdventureCard | None = None
    errors: list[str] = field(default_factory=list)

    def error_text(self) -> str:
        """Single line for the retry prompt."""
        return "; ".join(self.errors)


def _hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python, and True would sail through a naive check.
    return isinstance(value, int) and not isinstance(value, bool)


def validate_card(
    raw: Any,
    candidates: Sequence[Candidate],
    latest_return_time: str,
) -> ValidationResult:
    """Accept a card only if it satisfies every rule in PLAN.md 7.8."""
    if not isinstance(raw, dict):
        return ValidationResult(False, errors=["Model output is not a JSON object"])

    missing = [name for name in CARD_FIELDS if name not in raw]
    if missing:
        return ValidationResult(False, errors=[f"missing field(s): {', '.join(missing)}"])

    errors: list[str] = []
    by_id = {c.id: c for c in candidates}
    chosen = by_id.get(str(raw["candidate_id"]))
    if chosen is None:
        known = ", ".join(sorted(by_id)[:5]) or "none"
        errors.append(
            f"candidate_id '{raw['candidate_id']}' is not one of the candidates ({known})"
        )

    for name in TEXT_FIELDS:
        if not str(raw[name] or "").strip():
            errors.append(f"'{name}' is empty")

    title = str(raw["title"] or "")
    if len(title) > MAX_TITLE_CHARS:
        errors.append(f"title is {len(title)} characters, max {MAX_TITLE_CHARS}")

    plan = raw["plan"]
    if not isinstance(plan, list):
        errors.append("'plan' is not a list")
    elif not MIN_PLAN_STEPS <= len(plan) <= MAX_PLAN_STEPS:
        errors.append(f"'plan' has {len(plan)} steps, must be {MIN_PLAN_STEPS}-{MAX_PLAN_STEPS}")
    else:
        for index, step in enumerate(plan, start=1):
            if not str(step or "").strip():
                errors.append(f"'plan' step {index} is empty")
                break

    duration = raw["duration_minutes"]
    if not _is_int(duration):
        errors.append(f"'duration_minutes' is not a whole number ({duration!r})")
    elif duration <= 0:
        errors.append(f"'duration_minutes' must be positive, got {duration}")
    elif chosen is not None and duration > chosen.activity_minutes_available:
        # The failure this app exists to prevent: the model uses budget_minutes, or a
        # number copied from a different candidate, instead of the allowance it chose.
        errors.append(
            f"duration_minutes {duration} exceeds activity_minutes_available "
            f"{chosen.activity_minutes_available} for {chosen.name}"
        )

    leave_by = str(raw["leave_by"] or "")
    if not LEAVE_BY_RE.match(leave_by):
        errors.append(f"leave_by '{leave_by}' is not HH:MM in 24 hour time")
    elif not LEAVE_BY_RE.match(str(latest_return_time or "")):
        errors.append(f"latest_return_time '{latest_return_time}' is not a valid HH:MM")
    elif _hhmm_to_minutes(leave_by) > _hhmm_to_minutes(str(latest_return_time)):
        errors.append(
            f"leave_by {leave_by} is later than latest return time {latest_return_time}"
        )

    if errors:
        return ValidationResult(False, errors=errors)

    try:
        card = AdventureCard.model_validate(raw)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return ValidationResult(False, errors=[f"schema rejected the card: {detail}"])

    return ValidationResult(True, card=card)