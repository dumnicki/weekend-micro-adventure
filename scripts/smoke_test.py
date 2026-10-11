"""Call a running instance once and report what came back.

    python scripts/smoke_test.py https://micro-adventure.onrender.com
    python scripts/smoke_test.py                       # defaults to localhost:8000

Exits non-zero on failure, so it is usable as a deploy gate. Reports used_fallback
because that is the difference between "Gemma wrote this" and "the template did",
which is the whole claim of the demo.
"""

from __future__ import annotations

import json
import sys
import time
from urllib.parse import urlparse

import httpx

DEFAULT_BASE = "http://127.0.0.1:8000"
CITY = "Vienna"
COLD_BUDGET_SECONDS = 15.0
WARM_BUDGET_SECONDS = 8.0


def _print(icon: str, text: str) -> None:
    # Keep the degree sign readable on Windows consoles.
    try:
        print(f"{icon} {text}")
    except UnicodeEncodeError:
        print(f"{icon} {text.encode('ascii', 'replace').decode()}")


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE
    base = base.rstrip("/")

    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https"):
        _print("x", f"Not a valid URL: {base}")
        return 2

    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            _print("->", f"{base}/api/health")
            health = client.get(f"{base}/api/health")
            health.raise_for_status()
            _print("ok", f"health: {health.json()}")

            body = {
                "time_budget": "half_day",
                "transport": "foot",
                "vibe": "quiet",
                "location_text": CITY,
            }

            _print("->", f"POST /api/adventure  ({CITY})")
            started = time.perf_counter()
            response = client.post(f"{base}/api/adventure", json=body)
            cold = time.perf_counter() - started
            response.raise_for_status()
            data = response.json()

            _print("ok", f"first call {cold:.2f}s  (budget {COLD_BUDGET_SECONDS:.0f}s)")
            if data.get("empty"):
                _print("x", f"empty result: {data.get('message')}")
                return 1

            started = time.perf_counter()
            client.post(f"{base}/api/adventure", json=body)
            warm = time.perf_counter() - started
            _print("ok", f"cached call {warm:.2f}s  (budget {WARM_BUDGET_SECONDS:.0f}s)")

    except httpx.HTTPStatusError as exc:
        _print("x", f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        return 1
    except httpx.HTTPError as exc:
        _print("x", f"could not reach {base}: {exc}")
        return 1

    _print("", "--- card ---")
    _print("", json.dumps(
        {k: data.get(k) for k in (
            "title", "place_name", "duration_minutes", "leave_by",
            "plan", "tip", "check_before_you_go", "weather_summary", "used_fallback",
        )},
        indent=2, ensure_ascii=False,
    ))

    problems = []
    if data.get("used_fallback"):
        problems.append("used_fallback is true: Gemma did not write this card")
    if cold > COLD_BUDGET_SECONDS:
        problems.append(f"first call {cold:.2f}s over the {COLD_BUDGET_SECONDS:.0f}s budget")
    if warm > WARM_BUDGET_SECONDS:
        problems.append(f"cached call {warm:.2f}s over the {WARM_BUDGET_SECONDS:.0f}s budget")

    if problems:
        for problem in problems:
            _print("x", problem)
        return 1

    _print("ok", "Smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())