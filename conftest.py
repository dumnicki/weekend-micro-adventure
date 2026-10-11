"""Makes the project root importable so tests can `from app... import ...`.

tests/ has no __init__.py, so pytest prepends tests/ to sys.path rather than the
project root. Without this file every test that touches app.* fails at import.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_shared_places_cache():
    """The planner keeps one process-wide SerpApi cache.

    Left alone, a cached search from one test satisfies the HTTP mock in the next one,
    so tests would pass or fail depending on execution order. Every test starts with a
    clean cache unless it passes one in explicitly.
    """
    from app.services.planner import reset_places_cache

    reset_places_cache()
    yield
    reset_places_cache()