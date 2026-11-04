"""Small numeric and date helpers that behave like their JavaScript counterparts.

serato-mcp started as a TypeScript server; these keep results (rounding, number formatting, ISO dates)
identical to that version.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal


def js_round(x: float) -> int:
    """Math.round: halves round towards +infinity."""
    return math.floor(x + 0.5)


def round_to(x: float, digits: int = 2) -> float:
    """Math.round(x * 10**d) / 10**d."""
    scale = 10**digits
    return js_round(x * scale) / scale


def to_fixed(x: float, digits: int) -> str:
    """Number.prototype.toFixed: rounds the exact binary value, halves away from zero."""
    return str(Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def num(x: float | int) -> float | int:
    """Whole floats as ints, so they print as JavaScript would (126, not 126.0)."""
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return int(x)
    return x


def js_str(x: object) -> str:
    """String(x) for numbers, used inside messages."""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        if math.isnan(x):
            return "NaN"
        return str(num(x))
    return str(x)


def iso(seconds: float) -> str:
    """new Date(seconds * 1000).toISOString()."""
    ms = js_round(seconds * 1000)
    dt = datetime.fromtimestamp(ms // 1000, UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


def parse_iso(s: str | None) -> float:
    """Date.parse for our own ISO strings, in milliseconds (NaN-safe: 0 when absent)."""
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0.0


def parse_float(s: object) -> float:
    """parseFloat: leading number of a string, NaN when there is none."""
    import re

    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    m = re.match(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", str(s) if s is not None else "")
    return float(m.group(1)) if m else math.nan
