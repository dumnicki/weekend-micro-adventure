"""Step 2 check: run the prompt through the model 10 times with fake data.

Does exactly ONE generate_json call per run. No retries, no fallback. The point is to
measure the prompt, not the recovery path.

Applies the PLAN.md 7.3 time math and 7.5 filtering to the hand-written fake candidates
so each run sends a realistic shortlist, then checks the reply against the 7.8 rules.

Usage: python scripts/step2_prompt_check.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from math import asin, cos, log, radians, sin, sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.llm import generate_json  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "fake_candidates.json"
PROMPTS = ROOT / "app" / "prompts"

# PLAN.md 7.3. radius_km is the max straight-line radius; speed_kmh drives the time math.
BUDGET_MINUTES = {"1h": 60, "half_day": 240, "full_day": 480}
RADIUS_KM = {
    "foot": {"1h": 2, "half_day": 6, "full_day": 12},
    "bike": {"1h": 6, "half_day": 20, "full_day": 40},
    "transit": {"1h": 5, "half_day": 25, "full_day": 50},
}
SPEED_KMH = {"foot": 4.5, "bike": 14.0, "transit": 20.0}
ROUTE_FACTOR = 1.3  # straight-line understates real routes

# 10 runs covering every transport x a spread of budgets and vibes.
RUNS = [
    {"time_budget": "1h", "transport": "foot", "vibe": "quiet", "weather": "14°C, 10% rain chance, sunset 17:42"},
    {"time_budget": "1h", "transport": "bike", "vibe": "active", "weather": "14°C, 10% rain chance, sunset 17:42"},
    {"time_budget": "1h", "transport": "transit", "vibe": "curious", "weather": "14°C, 10% rain chance, sunset 17:42"},
    {"time_budget": "half_day", "transport": "foot", "vibe": "curious", "weather": "16°C, 20% rain chance, sunset 17:42"},
    {"time_budget": "half_day", "transport": "bike", "vibe": "quiet", "weather": "16°C, 20% rain chance, sunset 17:42"},
    {"time_budget": "half_day", "transport": "transit", "vibe": "active", "weather": "16°C, 20% rain chance, sunset 17:42"},
    {"time_budget": "half_day", "transport": "foot", "vibe": "active", "weather": "9°C, 75% rain chance, sunset 17:42"},
    {"time_budget": "full_day", "transport": "foot", "vibe": "active", "weather": "16°C, 20% rain chance, sunset 17:42"},
    {"time_budget": "full_day", "transport": "bike", "vibe": "curious", "weather": "16°C, 20% rain chance, sunset 17:42"},
    {"time_budget": "full_day", "transport": "transit", "vibe": "quiet", "weather": "16°C, 20% rain chance, sunset 17:42"},
]

NOW_LOCAL = "2026-10-11 14:20"
LATEST_RETURN = "17:12"  # sunset 17:42 minus 30 min, per 7.6


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * r * asin(sqrt(a))


def build_candidates(places: list[dict], origin: dict, transport: str, time_budget: str) -> list[dict]:
    """PLAN.md 7.3 + 7.5: radius filter, time math, drop short-activity, score."""
    radius = RADIUS_KM[transport][time_budget]
    speed = SPEED_KMH[transport]
    budget = BUDGET_MINUTES[time_budget]
    kept: list[dict] = []

    for p in places:
        distance = haversine_km(origin["lat"], origin["lng"], p["lat"], p["lng"])
        if distance > radius:
            continue
        routed = distance * ROUTE_FACTOR
        travel = routed / speed * 60 + (10 if transport == "transit" else 0)
        activity = budget - 2 * travel
        if activity < 20:
            continue
        rating = p["rating"] if p["rating"] is not None else 3.5
        activity = round(activity)
        kept.append(
            {
                **p,
                "distance_km": round(distance, 2),
                "travel_minutes_one_way": round(travel),
                "activity_minutes_available": activity,
                # Restated as a copy-target next to the raw number. Small models
                # copy strings more reliably than they re-derive integers.
                "max_duration_minutes": str(activity),
                "score": rating * log(1 + p["reviews"]),
            }
        )

    kept.sort(key=lambda c: -c["score"])
    return [{k: v for k, v in c.items() if k != "score"} for c in kept[:8]]


def check(card: dict, candidates: list[dict], latest_return: str) -> list[str]:
    """The PLAN.md 7.8 rules, as a list of failure reasons."""
    failures: list[str] = []
    required = [
        "candidate_id", "title", "duration_minutes",
        "getting_there", "plan", "tip", "leave_by", "check_before_you_go",
    ]

    for field in required:
        if field not in card:
            failures.append(f"missing field '{field}'")
    if failures:
        return failures

    by_id = {c["id"]: c for c in candidates}
    chosen = by_id.get(str(card["candidate_id"]))
    if chosen is None:
        failures.append(f"candidate_id '{card['candidate_id']}' not in the shortlist")
    else:
        available = chosen["activity_minutes_available"]
        duration = card["duration_minutes"]
        if not isinstance(duration, int):
            failures.append(f"duration_minutes not an int ({duration!r})")
        elif duration > available:
            failures.append(f"duration {duration} > activity_minutes_available {available}")

    title = str(card["title"])
    if len(title) > 80:
        failures.append(f"title is {len(title)} chars (max 80)")

    plan = card["plan"]
    if not isinstance(plan, list):
        failures.append("plan is not a list")
    elif not 2 <= len(plan) <= 5:
        failures.append(f"plan has {len(plan)} steps (need 2-5)")

    leave_by = str(card["leave_by"])
    if not (len(leave_by) == 5 and leave_by[2] == ":" and leave_by[:2].isdigit() and leave_by[3:].isdigit()):
        failures.append(f"leave_by '{leave_by}' is not HH:MM")
    elif leave_by > latest_return:
        failures.append(f"leave_by {leave_by} is later than latest_return_time {latest_return}")

    for field in ["title", "getting_there", "tip", "check_before_you_go"]:
        if not str(card.get(field, "")).strip():
            failures.append(f"empty field '{field}'")

    return failures


def render(template: str, values: dict) -> str:
    # Plain replace, not .format(): candidates_json is full of braces.
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val)
    return out


async def main() -> None:
    system = (PROMPTS / "system.md").read_text(encoding="utf-8")
    template = (PROMPTS / "user_template.md").read_text(encoding="utf-8")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    places, origin = data["places"], data["_origin"]

    passed = 0
    latencies: list[float] = []
    good_cards: list[dict] = []
    bad_cards: list[dict] = []

    for i, run in enumerate(RUNS, start=1):
        cands = build_candidates(places, origin, run["transport"], run["time_budget"])
        if not cands:
            print(f"\n--- run {i}: NO CANDIDATES after filtering ---")
            continue

        user = render(
            template,
            {
                "time_budget": run["time_budget"],
                "budget_minutes": str(BUDGET_MINUTES[run["time_budget"]]),
                "transport": run["transport"],
                "vibe": run["vibe"],
                "now_local": NOW_LOCAL,
                "latest_return_time": LATEST_RETURN,
                "weather_summary": run["weather"],
                "candidates_json": json.dumps(cands, ensure_ascii=False, indent=2),
            },
        )

        label = f"run {i:>2}  {run['time_budget']:<9} {run['transport']:<8} {run['vibe']:<8}"
        print(f"\n=== {label} | {len(cands)} candidates ===")

        t0 = time.perf_counter()
        try:
            card = await generate_json(system, user)
        except Exception as exc:  # noqa: BLE001 - measuring the prompt, any failure counts
            dt = time.perf_counter() - t0
            latencies.append(dt)
            print(f"  PARSE/NETWORK FAILURE ({dt:.2f}s): {type(exc).__name__}: {exc}")
            bad_cards.append({"_run": i, "_error": str(exc)})
            continue

        dt = time.perf_counter() - t0
        latencies.append(dt)

        failures = check(card, cands, LATEST_RETURN)
        if failures:
            print(f"  FAIL ({dt:.2f}s)")
            for f in failures:
                print(f"    - {f}")
            bad_cards.append({"_run": i, "_failures": failures, "card": card})
        else:
            passed += 1
            print(f"  PASS ({dt:.2f}s)  {card['title']!r}")
            good_cards.append({"_run": i, "card": card})

    out = ROOT / "tests" / "fixtures"
    # All passing cards, so hallucination can be audited across the whole batch
    # instead of just whichever one happened to be last.
    (out / "llm_good_all.json").write_text(
        json.dumps(good_cards, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "llm_good.json").write_text(
        json.dumps(good_cards[-1]["card"] if good_cards else {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Only overwrite the bad fixture when this run actually produced a failure,
    # so a clean 10/10 cannot silently clobber the regression fixture.
    if bad_cards:
        card = bad_cards[-1].get("card") or {"_error": str(bad_cards[-1].get("_error", ""))}
        (out / "llm_bad.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    elif not (out / "llm_bad.json").exists():
        (out / "llm_bad.json").write_text("{}", encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{len(RUNS)} runs passed the 7.8 rules")
    if latencies:
        print(f"latency: min {min(latencies):.2f}s  max {max(latencies):.2f}s")
    print(f"fixtures written: llm_good_all.json ({len(good_cards)} cards), llm_good.json")
    if bad_cards:
        print("fixtures written: llm_bad.json (this run had failures)")


if __name__ == "__main__":
    asyncio.run(main())