"""Market regime from hourly candles. Paper labels only, no orders."""

from __future__ import annotations

import math
from typing import Sequence


def ema(values: Sequence[float], length: int) -> list[float]:
    if not values or length <= 0:
        return []
    k = 2.0 / (length + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1.0 - k))
    return out


def classify_closes(closes: Sequence[float]) -> str:
    """BIK, NEVTRALNO, MEDVED, or VISOKA VOLATILNOST from one close series."""
    vals = [float(v) for v in closes if v and v > 0]
    if len(vals) < 50:
        return "NEVTRALNO"
    ema20 = ema(vals, 20)
    ema50 = ema(vals, 50)
    slope = (ema20[-1] - ema20[-6]) / ema20[-6] if ema20[-6] else 0.0
    ch24 = vals[-1] / vals[-25] - 1.0 if len(vals) >= 25 else 0.0
    rets = []
    for i in range(max(1, len(vals) - 24), len(vals)):
        if vals[i - 1] > 0 and vals[i] > 0:
            rets.append(math.log(vals[i] / vals[i - 1]))
    if len(rets) >= 8:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        daily = math.sqrt(var) * math.sqrt(24)
    else:
        daily = 0.0
    if daily >= 0.08:
        return "VISOKA VOLATILNOST"
    if ema20[-1] > ema50[-1] and slope > 0 and ch24 > 0:
        return "BIK"
    if ema20[-1] < ema50[-1] and slope < 0 and ch24 < 0:
        return "MEDVED"
    return "NEVTRALNO"


def classify_chain(closes_1h: Sequence[float], closes_4h: Sequence[float]) -> str:
    """1h decides. A 4h disagreement downgrades BIK or MEDVED to NEVTRALNO."""
    primary = classify_closes(closes_1h)
    if primary in ("VISOKA VOLATILNOST", "NEVTRALNO") or len(closes_4h) < 50:
        return primary
    higher = classify_closes(closes_4h)
    if higher == "VISOKA VOLATILNOST":
        return higher
    if primary == "BIK" and higher == "MEDVED":
        return "NEVTRALNO"
    if primary == "MEDVED" and higher == "BIK":
        return "NEVTRALNO"
    return primary


def size_multiplier(regime: str, category: str) -> float | None:
    """None means the entry is blocked. Otherwise a size factor."""
    if regime == "MEDVED" and category == "MEME":
        return None
    if regime == "MEDVED":
        return 0.5
    if regime == "VISOKA VOLATILNOST":
        return 0.5
    return 1.0
