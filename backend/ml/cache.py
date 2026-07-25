"""Repeat-query caching for the data-reading tools (spec.md "Caching" row).

`functools.lru_cache` cannot wrap these directly: every tool takes a dict, and dicts are not
hashable. This keys on a canonical JSON rendering of the arguments instead, which also makes
`{"account_id": "X", "role": "both"}` and `{"role": "both", "account_id": "X"}` the same entry.

Safe to cache because the tools are pure over data that is read-only after the build scripts
run (ml_spec.md interface contract: "pure / side-effect-free except reading the Parquet
files"). Nothing here caches `risk` or `explain` — their arguments are whole result dicts, so
building the cache key would cost more than recomputing the answer.
"""
import copy
import json
from functools import lru_cache, wraps
from typing import Callable

DEFAULT_MAXSIZE = 64


def cached_tool(maxsize: int = DEFAULT_MAXSIZE) -> Callable:
    """Memoize a tool function on its JSON-canonicalized arguments."""

    def decorate(func: Callable[..., dict]) -> Callable[..., dict]:
        @lru_cache(maxsize=maxsize)
        def _by_key(key: str) -> dict:
            args, kwargs = json.loads(key)
            return func(*args, **kwargs)

        @wraps(func)
        def wrapper(*args, **kwargs) -> dict:
            try:
                key = json.dumps([args, kwargs], sort_keys=True, default=str)
            except (TypeError, ValueError):
                # Unserializable input is a caller error the tool itself reports better than
                # the cache can. Pass it straight through.
                return func(*args, **kwargs)
            # Deep-copied on the way out: a caller that mutates a returned dict — the agent
            # loop annotating a result before handing it on, say — must not corrupt what the
            # next identical call sees.
            return copy.deepcopy(_by_key(key))

        wrapper.cache_clear = _by_key.cache_clear
        wrapper.cache_info = _by_key.cache_info
        wrapper.__wrapped__ = func
        return wrapper

    return decorate
