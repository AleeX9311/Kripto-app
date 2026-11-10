"""Support-based stop for the paper book. No orders, no wallet.

The stop sits under the nearest repeated swing low, then a buffer.
Distance is clamped to -5% .. -15%. Deeper support skips the trade.
"""

from __future__ import annotations

from typing import Any

import config

Candle = list[float]


def atr14(candles: list[Candle]) -> float | None:
    if len(candles) < 2:
        return None
    ranges = []
    prev_close = candles[0][4]
    for row in candles[1:]:
        high, low, close = row[2], row[3], row[4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        ranges.append(tr)
        prev_close = close
    window = ranges[-14:]
    if not window:
        return None
    return sum(window) / len(window)


def swing_lows(candles: list[Candle], wing: int = 2) -> list[tuple[float, float]]:
    """Fractal lows confirmed by `wing` candles on each side. Returns (price, volume)."""
    out = []
    for i in range(wing, len(candles) - wing):
        price = candles[i][3]
        left = [candles[i - k][3] for k in range(1, wing + 1)]
        right = [candles[i + k][3] for k in range(1, wing + 1)]
        neighbors = left + right
        if price <= min(neighbors) and price < max(neighbors):
            out.append((price, candles[i][5]))
    return out


def _clusters(levels: list[tuple[float, float]], width: float) -> list[dict[str, float]]:
    if not levels:
        return []
    ordered = sorted(levels, key=lambda item: item[0])
    groups: list[list[tuple[float, float]]] = [[ordered[0]]]
    for price, volume in ordered[1:]:
        anchor = sum(p for p, _v in groups[-1]) / len(groups[-1])
        if price - anchor <= width:
            groups[-1].append((price, volume))
        else:
            groups.append([(price, volume)])
    clusters = []
    for group in groups:
        prices = [p for p, _v in group]
        volumes = [v for _p, v in group]
        clusters.append({
            "low": min(prices),
            "high": max(prices),
            "touches": float(len(group)),
            "volume": sum(volumes),
        })
    return clusters


def find_support(entry: float, candles_15m: list[Candle], candles_1h: list[Candle]) -> dict[str, float] | None:
    """Nearest meaningful support strictly below entry. None when the candles have no swing."""
    if entry <= 0:
        return None
    atr = atr14(candles_15m) or atr14(candles_1h) or 0.0
    width = max(entry * 0.01, 0.5 * atr)
    levels = swing_lows(candles_15m) + swing_lows(candles_1h)
    # High-volume lows add weight when they sit on the same zone.
    pool = candles_15m or candles_1h
    if pool:
        ranked = sorted(pool, key=lambda row: row[5], reverse=True)
        cutoff = max(1, len(ranked) // 5)
        for row in ranked[:cutoff]:
            if row[3] < entry:
                levels.append((row[3], row[5]))
    below = [(price, volume) for price, volume in levels if 0 < price < entry]
    clusters = [c for c in _clusters(below, width) if c["high"] < entry]
    if not clusters:
        return None
    repeated = [c for c in clusters if c["touches"] >= 2]
    chosen_pool = repeated or clusters
    # Nearest zone, with a tie broken toward more touches.
    chosen = min(chosen_pool, key=lambda c: (entry - c["high"], -c["touches"]))
    return {
        "low": chosen["low"],
        "high": chosen["high"],
        "touches": chosen["touches"],
        "atr": atr,
    }


def plan_stop(entry: float, candles_15m: list[Candle], candles_1h: list[Candle]) -> dict[str, Any]:
    """Return a stop plan. `ok` is false when the trade must be skipped."""
    support = find_support(entry, candles_15m, candles_1h)
    atr = (support or {}).get("atr") or atr14(candles_15m) or 0.0
    buffer = max(entry * 0.01, 0.5 * atr)
    if not support:
        distance = float(config.STOP_FALLBACK)
        stop_price = entry * (1.0 + distance)
        zone_low = zone_high = None
        touches = 0
    else:
        zone_low = float(support["low"])
        zone_high = float(support["high"])
        touches = int(support["touches"])
        stop_price = zone_low - buffer
        distance = stop_price / entry - 1.0
        if distance < float(config.STOP_LOSS):
            return {
                "ok": False,
                "reason": "podpora predaleč",
                "support_low": zone_low,
                "support_high": zone_high,
                "touches": touches,
                "buffer": buffer,
                "atr": atr,
                "stop_price": stop_price,
                "stop_pct": distance * 100.0,
            }
        if distance > float(config.STOP_MIN):
            distance = float(config.STOP_MIN)
            stop_price = entry * (1.0 + distance)
    reward = float(config.TAKE_PROFIT) / abs(distance) if distance else 0.0
    if reward < float(config.MIN_REWARD_RISK):
        return {
            "ok": False,
            "reason": "slabo razmerje",
            "support_low": zone_low,
            "support_high": zone_high,
            "touches": touches,
            "buffer": buffer,
            "atr": atr,
            "stop_price": stop_price,
            "stop_pct": distance * 100.0,
            "reward_risk": reward,
        }
    return {
        "ok": True,
        "reason": "podpora" if support else "ni podpore",
        "support_low": zone_low,
        "support_high": zone_high,
        "touches": touches,
        "buffer": buffer,
        "atr": atr,
        "stop_price": stop_price,
        "stop_pct": distance * 100.0,
        "stop_frac": distance,
        "reward_risk": reward,
    }


def size_for_stop(equity: float, stop_frac: float) -> float:
    """min(10% of equity, 1.2% of equity / stop distance)."""
    distance = abs(stop_frac)
    if equity <= 0 or distance <= 0:
        return 0.0
    capped = equity * config.POSITION_PCT
    risk = equity * config.RISK_PCT / distance
    return min(capped, risk)
