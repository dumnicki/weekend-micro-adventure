"""Tests for the TTL cache. PLAN.md 7.4.

A fake clock is injected so expiry is tested without sleeping.
"""

from __future__ import annotations

import pytest

from app.services.cache import TTLCache, cached_call, place_cache_key


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cache(clock: FakeClock) -> TTLCache[str]:
    return TTLCache(ttl_seconds=60, clock=clock)


# ------------------------------------------------------------------------- basics


def test_set_then_get(cache):
    cache.set("a", "value")
    assert cache.get("a") == "value"


def test_get_missing_key_returns_none(cache):
    assert cache.get("nope") is None


def test_set_overwrites(cache):
    cache.set("a", "first")
    cache.set("a", "second")
    assert cache.get("a") == "second"
    assert len(cache) == 1


# --------------------------------------------------------------------------- ttl


def test_value_survives_until_ttl(cache, clock):
    cache.set("a", "value")
    clock.advance(59.9)
    assert cache.get("a") == "value"


def test_value_expires_at_ttl(cache, clock):
    cache.set("a", "value")
    clock.advance(60)
    assert cache.get("a") is None


def test_expired_entry_is_evicted_on_read(cache, clock):
    cache.set("a", "value")
    clock.advance(61)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_refresh_on_write_restores_the_ttl(cache, clock):
    cache.set("a", "value")
    clock.advance(50)
    cache.set("a", "value2")
    clock.advance(50)
    assert cache.get("a") == "value2"


# ------------------------------------------------------------------------ eviction


def test_evicts_oldest_entry_when_full(clock):
    cache = TTLCache(ttl_seconds=60, max_entries=2, clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_read_marks_an_entry_as_recently_used(clock):
    cache = TTLCache(ttl_seconds=60, max_entries=2, clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # "a" is now the most recent, so "b" should be evicted next
    cache.set("c", 3)

    assert cache.get("a") == 1
    assert cache.get("b") is None


# --------------------------------------------------------------------- validation


@pytest.mark.parametrize("ttl", [0, -1])
def test_non_positive_ttl_is_rejected(ttl):
    with pytest.raises(ValueError, match="ttl_seconds"):
        TTLCache(ttl_seconds=ttl)


def test_zero_max_entries_is_rejected():
    with pytest.raises(ValueError, match="max_entries"):
        TTLCache(ttl_seconds=60, max_entries=0)


def test_clear_empties_the_cache(cache):
    cache.set("a", "value")
    cache.clear()
    assert cache.get("a") is None
    assert len(cache) == 0


# -------------------------------------------------------------------- cache key


def test_cache_key_rounds_coordinates():
    key = place_cache_key(48.208493, 16.372081, "quiet park")
    assert key == (48.21, 16.37, "quiet park")


def test_nearby_coordinates_share_a_key():
    """A re-roll from the same spot must not spend quota on a second search."""
    assert place_cache_key(48.208493, 16.372081, "quiet park") == place_cache_key(
        48.208489, 16.372080, "quiet park"
    )


def test_distant_coordinates_do_not_share_a_key():
    assert place_cache_key(48.21, 16.37, "quiet park") != place_cache_key(48.30, 16.37, "quiet park")


def test_different_queries_do_not_share_a_key():
    assert place_cache_key(48.21, 16.37, "quiet park") != place_cache_key(48.21, 16.37, "nature reserve")


# ------------------------------------------------------------- cached_call wrapper


async def test_cached_call_hits_the_function_once(clock):
    cache: TTLCache[int] = TTLCache(ttl_seconds=60, clock=clock)
    calls = 0

    @cached_call(cache, key_func=lambda x: x)
    async def work(x):
        nonlocal calls
        calls += 1
        return x * 2

    assert await work(2) == 4
    assert await work(2) == 4
    assert calls == 1


async def test_cached_call_refetches_after_expiry(clock):
    cache: TTLCache[int] = TTLCache(ttl_seconds=60, clock=clock)
    calls = 0

    @cached_call(cache, key_func=lambda x: x)
    async def work(x):
        nonlocal calls
        calls += 1
        return x

    await work(1)
    clock.advance(61)
    await work(1)

    assert calls == 2


async def test_cached_call_stores_empty_lists(clock):
    """An empty result is a real answer. Re-searching for it would burn quota."""
    cache: TTLCache[list] = TTLCache(ttl_seconds=60, clock=clock)
    calls = 0

    @cached_call(cache, key_func=lambda x: x)
    async def work(x):
        nonlocal calls
        calls += 1
        return []

    assert await work(1) == []
    assert await work(1) == []
    assert calls == 1


async def test_cached_call_does_not_store_none(clock):
    """None means 'nothing usable'; re-running is cheap, so it is not worth caching."""
    cache: TTLCache[object] = TTLCache(ttl_seconds=60, clock=clock)
    calls = 0

    @cached_call(cache, key_func=lambda x: x)
    async def work(x):
        nonlocal calls
        calls += 1
        return None

    assert await work(1) is None
    assert await work(1) is None
    assert calls == 2