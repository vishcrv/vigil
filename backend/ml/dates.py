"""Date-bound normalization for every tool that filters on `Timestamp`.

`Timestamp` is stored as a fixed-width VARCHAR in `YYYY/MM/DD HH:MM` form (phase3.md §3a), so
DuckDB compares it lexicographically. That makes the format of a caller-supplied bound
load-bearing: `'2022/09/01 00:00' <= '2022-09-05'` is **false**, because `/` (0x2F) sorts above
`-` (0x2D). An ISO-8601 bound — which is what an LLM emits for "last week" every time — matched
zero rows and returned no error, so `anomaly` scored nothing and `risk` reported LOW/MONITOR on
an account it had never looked at.

Everything here converts a caller bound to the stored format before it reaches SQL, and returns
a structured error instead of a silently-empty result when it cannot.
"""
import re
from typing import Any

STORED_FORMAT = "YYYY/MM/DD HH:MM"

# Date part with either separator, optional time part with either 'T' or a space.
_FULL = re.compile(
    r"^(\d{4})[-/](\d{2})[-/](\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::\d{2})?(?:\.\d+)?Z?)?$"
)

# Partial prefixes: "2022", "2022-09", "2022/09". Used by prefix-match filters only.
_PREFIX = re.compile(r"^(\d{4})(?:[-/](\d{2}))?(?:[-/](\d{2}))?$")

_DAY_START = "00:00"
_DAY_END = "23:59"


def normalize_bound(value: Any, upper: bool = False) -> tuple[str | None, str | None]:
    """Convert one comparison bound to `YYYY/MM/DD HH:MM`. Returns (normalized, error).

    A date without a time is widened to cover the whole day — `00:00` for a lower bound,
    `23:59` for an upper one. Without that widening, `<= '2022-09-05'` would silently exclude
    every transaction on the 5th, which is not what a caller asking for "up to the 5th" means.
    """
    if not isinstance(value, str) or not value.strip():
        return None, f"date bound must be a non-empty string, got {value!r}"

    match = _FULL.match(value.strip())
    if not match:
        return None, (
            f"unrecognized date {value!r} - expected YYYY-MM-DD, YYYY/MM/DD, or either with a "
            f"HH:MM time"
        )

    year, month, day, hh, mm = match.groups()
    if not 1 <= int(month) <= 12 or not 1 <= int(day) <= 31:
        return None, f"invalid calendar date {value!r}"
    if hh is not None and (int(hh) > 23 or int(mm) > 59):
        return None, f"invalid time in {value!r}"

    if hh is None:
        hh, mm = (_DAY_END.split(":") if upper else _DAY_START.split(":"))
    return f"{year}/{month}/{day} {hh}:{mm}", None


def normalize_range(value: Any) -> tuple[list[str] | None, str | None]:
    """Normalize a [start, end] pair, widening each end outward. Returns (pair, error)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None, "date range must be a [start, end] pair"

    low, err = normalize_bound(value[0], upper=False)
    if err:
        return None, err
    high, err = normalize_bound(value[1], upper=True)
    if err:
        return None, err
    if low > high:
        return None, f"date range start {value[0]!r} is after end {value[1]!r}"
    return [low, high], None


def normalize_prefix(value: Any) -> tuple[str | None, str | None]:
    """Normalize a partial date used for prefix/substring matching (`2022-09` → `2022/09`).

    Non-date text passes through untouched — this filter is also used against free text.
    """
    if not isinstance(value, str):
        return None, "prefix match value must be a string"
    match = _PREFIX.match(value.strip())
    if not match:
        return value, None
    return "/".join(p for p in match.groups() if p is not None), None
