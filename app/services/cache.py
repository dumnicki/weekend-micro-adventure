"""Tiny in-memory TTL cache. PLAN.md 7.4.

SerpApi's free quota is small enough that a re-roll must never trigger a second search.
This caches raw search responses by rounded coordinates and query, so the second look at
the same area costs nothing and returns immediately.

SerpApi keeps its own cache for an hour and serves cached searches for free, but that
only helps for byte-identical queries and still costs a round trip. This one is local.

Process-local and unbounded in count but capped in entries: a single-user demo app does
not need Redis, and the plan forbids a database.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Hashable
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Expiry is lazy: an entry past its TTL is dropped when read, not by a timer."""

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        # Insertion order doubles as LRU order: reads move an entry to the end.
        self._entries: dict[Hashable, tuple[float, T]] = {}

    def get(self, key: Hashable) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at >= self.ttl_seconds:
            del self._entries[key]
            return None
        # Re-insert rather than overwrite: assigning to an existing key leaves its
        # position in the dict unchanged, which would silently make this FIFO.
        del self._entries[key]
        self._entries[key] = (stored_at, value)
        return value

    def set(self, key: Hashable, value: T) -> None:
        if key in self._entries:
            self._entries.pop(key)
        elif len(self._entries) >= self.max_entries:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = (self._clock(), value)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        """Entry count, expired ones included. For tests and diagnostics."""
        return len(self._entries)


def place_cache_key(lat: float, lng: float, query: str) -> tuple[float, float, str]:
    """Cache key from PLAN.md 7.4: coordinates rounded to 2dp, plus the query.

    Rounding means a re-roll from the same spot reuses the first search instead of
    paying for a second one, even if the client sends slightly different floats.
    """
    return (round(lat, 2), round(lng, 2), query)


def cached_call(
    cache: TTLCache[Any],
    key_func: Callable[..., Hashable],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Memoise an async function on the cache. Results of None are not stored.

    None means "no usable result" for our callers, and re-running those is cheap and
    rare enough that storing them would cost more than it saves. Empty lists are stored,
    because those are a real answer and re-searching for them burns quota.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = key_func(*args, **kwargs)
            hit = cache.get(key)
            if hit is not None:
                return hit
            value = await func(*args, **kwargs)
            if value is not None:
                cache.set(key, value)
            return value

        return wrapper

    return decorator