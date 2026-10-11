"""Tests for the PLAN.md 7.8 validation rules and the 6.1 request contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import AdventureRequest, Candidate
from app.services.validate import validate_card

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LATEST_RETURN = "17:12"


def make_candidate(
    cid: str = "ChIJgarden03",
    name: str = "Botanischer Garten",
    activity_minutes_available: int = 30,
) -> Candidate:
    return Candidate(
        id=cid,
        name=name,
        type="botanical garden",
        address="Ob. der Bastionstrasse, 1020 Wien",
        lat=48.198361,
        lng=16.363056,
        rating=4.6,
        reviews=5140,
        description="Botanical garden with labelled plant collections and a glasshouse.",
        hours_text="Open daily 09:00-18:00",
        distance_km=1.4,
        travel_minutes_one_way=15,
        activity_minutes_available=activity_minutes_available,
    )


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def base_card(**overrides) -> dict:
    """A card that passes every rule. Override one field to break one rule."""
    card = {
        "candidate_id": "ChIJgarden03",
        "title": "Botanischer Garten",
        "duration_minutes": 25,
        "getting_there": "Take transit for about 15 minutes.",
        "plan": ["Walk through the plant collections.", "Visit the glasshouse."],
        "tip": "The garden has labelled plant collections.",
        "leave_by": "16:57",
        "check_before_you_go": "Check whether the glasshouse is open today.",
    }
    card.update(overrides)
    return card


# --------------------------------------------------------------------------- fixtures


def test_real_good_fixture_is_accepted():
    """A genuine Gemma output from the Step 2 run must pass."""
    card = load_fixture("llm_good.json")
    result = validate_card(card, [make_candidate(activity_minutes_available=200)], LATEST_RETURN)
    assert result.ok, result.error_text()
    assert result.card is not None
    assert result.card.candidate_id == card["candidate_id"]
    assert result.card.plan == card["plan"]


def test_real_bad_fixture_is_rejected_for_duration():
    """The regression fixture: real model output that used budget_minutes, not the cap."""
    card = load_fixture("llm_bad.json")
    result = validate_card(card, [make_candidate(activity_minutes_available=30)], LATEST_RETURN)
    assert not result.ok
    assert "duration_minutes" in result.error_text()
    assert "exceeds activity_minutes_available" in result.error_text()


def test_base_card_passes():
    result = validate_card(base_card(), [make_candidate()], LATEST_RETURN)
    assert result.ok, result.error_text()


# ------------------------------------------------------------------- rule: candidate_id


def test_unknown_candidate_id_is_rejected():
    result = validate_card(base_card(candidate_id="nope"), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "is not one of the candidates" in result.error_text()


def test_duration_is_not_checked_against_a_different_candidate():
    """A longer duration is fine if the chosen candidate allows it."""
    result = validate_card(
        base_card(duration_minutes=180), [make_candidate(activity_minutes_available=200)], LATEST_RETURN
    )
    assert result.ok, result.error_text()


# ------------------------------------------------------------------------- rule: plan


@pytest.mark.parametrize("steps", [[], ["only one"], ["a", "b", "c", "d", "e", "f"]])
def test_plan_step_count_out_of_range(steps):
    result = validate_card(base_card(plan=steps), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "plan" in result.error_text()


@pytest.mark.parametrize("steps", [["one", "two"], ["one", "two", "three"]])
def test_plan_step_count_in_range(steps):
    result = validate_card(base_card(plan=steps), [make_candidate()], LATEST_RETURN)
    assert result.ok, result.error_text()


def test_empty_plan_step_is_rejected():
    result = validate_card(base_card(plan=["fine", "   "]), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "step 2 is empty" in result.error_text()


def test_plan_that_is_not_a_list_is_rejected():
    result = validate_card(base_card(plan="walk to the park"), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "not a list" in result.error_text()


# ------------------------------------------------------------------------ rule: title


def test_long_title_is_rejected():
    result = validate_card(base_card(title="x" * 81), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "81 characters" in result.error_text()


def test_title_at_the_limit_is_accepted():
    result = validate_card(base_card(title="x" * 80), [make_candidate()], LATEST_RETURN)
    assert result.ok, result.error_text()


# -------------------------------------------------------------------- rule: leave_by


@pytest.mark.parametrize("leave_by", ["9:05", "25:00", "17:60", "noon", "", "1700", "17:5"])
def test_bad_leave_by_format_is_rejected(leave_by):
    result = validate_card(base_card(leave_by=leave_by), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "leave_by" in result.error_text()


def test_leave_by_equal_to_latest_return_is_accepted():
    result = validate_card(base_card(leave_by=LATEST_RETURN), [make_candidate()], LATEST_RETURN)
    assert result.ok, result.error_text()


def test_leave_by_after_latest_return_is_rejected():
    result = validate_card(base_card(leave_by="17:13"), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "later than latest return time" in result.error_text()


# --------------------------------------------------------------------- rule: duration


@pytest.mark.parametrize("duration", [31, 60, 480])
def test_duration_over_the_allowance_is_rejected(duration):
    result = validate_card(base_card(duration_minutes=duration), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "exceeds activity_minutes_available" in result.error_text()


@pytest.mark.parametrize("duration", ["30", 0, -5, True, 30.5])
def test_duration_must_be_a_positive_whole_number(duration):
    result = validate_card(base_card(duration_minutes=duration), [make_candidate()], LATEST_RETURN)
    assert not result.ok


# ----------------------------------------------------------------- rule: empty fields


@pytest.mark.parametrize("field", ["title", "getting_there", "tip", "check_before_you_go"])
def test_empty_text_field_is_rejected(field):
    result = validate_card(base_card(**{field: "   "}), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert f"'{field}' is empty or a null marker" in result.error_text()


@pytest.mark.parametrize("marker", ["None", "null", "N/A", "nil", "unknown", "-", "."])
def test_literal_null_markers_are_rejected(marker):
    """Gemma writes these into prose fields. Structurally non-empty, useless on a card."""
    result = validate_card(base_card(check_before_you_go=marker), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "null marker" in result.error_text()


@pytest.mark.parametrize("marker", ["None", "N/A"])
def test_null_markers_are_rejected_in_plan_steps(marker):
    result = validate_card(base_card(plan=["Walk there.", marker]), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "null marker" in result.error_text()


def test_a_sentence_containing_none_is_fine():
    """Only a whole-field match counts. "None of the paths are lit." is real prose."""
    result = validate_card(
        base_card(check_before_you_go="None of the side paths are lit after sunset."),
        [make_candidate()],
        LATEST_RETURN,
    )
    assert result.ok, result.error_text()


# ------------------------------------------------------------------- structural cases


def test_missing_field_is_reported():
    card = base_card()
    del card["tip"]
    result = validate_card(card, [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "missing field(s): tip" in result.error_text()


@pytest.mark.parametrize("raw", ["not json", 42, None, ["a", "b"]])
def test_non_object_output_is_rejected(raw):
    result = validate_card(raw, [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "not a JSON object" in result.error_text()


def test_error_text_is_single_line_for_the_retry_prompt():
    result = validate_card(base_card(duration_minutes=99, leave_by="23:00"), [make_candidate()], LATEST_RETURN)
    assert not result.ok
    assert "\n" not in result.error_text()
    assert "duration_minutes" in result.error_text() and "leave_by" in result.error_text()


# ------------------------------------------------------- AdventureRequest (PLAN.md 6.1)


def test_request_accepts_location_text():
    req = AdventureRequest(
        time_budget="1h", transport="foot", vibe="quiet", location_text="Vienna"
    )
    assert req.lat is None and req.exclude_ids == []


def test_request_accepts_coordinates():
    req = AdventureRequest(time_budget="1h", transport="foot", vibe="quiet", lat=48.2, lng=16.37)
    assert req.lat == 48.2


def test_request_accepts_both_and_prefers_coordinates():
    req = AdventureRequest(
        time_budget="1h", transport="foot", vibe="quiet",
        location_text="Vienna", lat=48.2, lng=16.37,
    )
    assert req.lat == 48.2


def test_request_without_any_location_is_rejected():
    with pytest.raises(ValidationError):
        AdventureRequest(time_budget="1h", transport="foot", vibe="quiet")


def test_request_with_lat_but_no_lng_is_rejected():
    with pytest.raises(ValidationError, match="together"):
        AdventureRequest(time_budget="1h", transport="foot", vibe="quiet", lat=48.2)


def test_request_with_blank_location_text_is_rejected():
    with pytest.raises(ValidationError):
        AdventureRequest(time_budget="1h", transport="foot", vibe="quiet", location_text="   ")


@pytest.mark.parametrize(
    "field,value",
    [
        ("time_budget", "2h"),
        ("transport", "car"),
        ("vibe", "romantic"),
    ],
)
def test_request_rejects_unknown_enum_values(field, value):
    with pytest.raises(ValidationError):
        AdventureRequest(**{"time_budget": "1h", "transport": "foot", "vibe": "quiet", field: value})


def test_request_rejects_out_of_range_coordinates():
    with pytest.raises(ValidationError):
        AdventureRequest(time_budget="1h", transport="foot", vibe="quiet", lat=91, lng=16.37)