"""Chart-reading gate for paper entries. It does not add a sixth vote.

A buy still needs 4 of 5 YES votes and a VARNOST yes. This module only
times the entry: confirmed setups may pass, unconfirmed ones wait,
and falling knives or extended tops are refused. It is not a prediction.
"""

from __future__ import annotations

from typing import Any

Candle = list[float]


def _closes(candles: list[Candle]) -> list[float]:
    return [float(row[4]) for row in candles if len(row) >= 5 and float(row[4]) > 0]


def ema_series(values: list[float], length: int) -> list[float | None]:
    if length <= 0 or not values:
        return []
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    k = 2.0 / (length + 1.0)
    prev = sum(values[:length]) / length
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = []
    losses = []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def reading(gain: float, loss: float) -> float:
        if loss <= 1e-12:
            return 100.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = reading(avg_g, avg_l)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out[i + 1] = reading(avg_g, avg_l)
    return out


def swing_highs(candles: list[Candle], wing: int = 2) -> list[tuple[int, float]]:
    out = []
    for i in range(wing, len(candles) - wing):
        price = candles[i][2]
        neighbors = [candles[i - k][2] for k in range(1, wing + 1)] + [candles[i + k][2] for k in range(1, wing + 1)]
        if price >= max(neighbors) and price > min(neighbors):
            out.append((i, price))
    return out


def _indexed_lows(candles: list[Candle], wing: int = 2) -> list[tuple[int, float]]:
    out = []
    for i in range(wing, len(candles) - wing):
        price = candles[i][3]
        neighbors = [candles[i - k][3] for k in range(1, wing + 1)] + [candles[i + k][3] for k in range(1, wing + 1)]
        if price <= min(neighbors) and price < max(neighbors):
            out.append((i, price))
    return out


def _green_volume_expands(candles: list[Candle]) -> bool:
    green = [row[5] for row in candles if row[4] >= row[1]]
    red = [row[5] for row in candles if row[4] < row[1]]
    if len(green) < 2 or len(red) < 2:
        return False
    return (sum(green[-3:]) / min(3, len(green))) > (sum(red[-3:]) / min(3, len(red))) * 1.15


def read_chart(
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    price: float,
    support_low: float | None = None,
    support_high: float | None = None,
) -> dict[str, Any]:
    """Score a setup. action is buy, watch, or reject. Never a forecast."""
    reasons: list[str] = []
    if price <= 0 or len(candles_15m) < 30:
        return {
            "action": "watch",
            "kind": "ni_svec",
            "label": "Čaka na potrditev",
            "score": 0,
            "reasons": ["ni dovolj 15m sveč"],
            "note": "ni napoved",
        }
    closes = _closes(candles_15m)
    ema20 = ema_series(closes, 20)
    rsi = rsi_series(closes, 14)
    lows = _indexed_lows(candles_15m)
    highs = swing_highs(candles_15m)
    hour_high = max((row[2] for row in (candles_1h or candles_15m)), default=price)
    local_high = max(hour_high, max(row[2] for row in candles_15m))
    swing = min((price for _i, price in lows), default=min(row[3] for row in candles_15m))
    depth = 1.0 - (swing / local_high) if local_high else 0.0
    last = closes[-1]
    ema_now = ema20[-1]
    ema_prev = ema20[-2] if len(ema20) > 1 else None
    rsi_now = rsi[-1]
    near = False
    if support_low:
        zone_high = float(support_high or support_low)
        zone_low = float(support_low)
        above = (price - zone_high) / price if price else 1.0
        below = (zone_low - price) / price if price else 1.0
        # Near means on the zone or still in the reversal area above it, not through it.
        near = below <= 0.012 and above <= 0.18
        reasons.append("blizu podpore" if near else "stran od podpore")
    higher_low = len(lows) >= 2 and lows[-1][1] > lows[-2][1] and lows[-1][0] > lows[-2][0]
    if higher_low:
        reasons.append("višje dno")
    reclaim = bool(ema_now and ema_prev and closes[-2] < ema_prev and last > ema_now)
    if reclaim:
        reasons.append("povratek nad EMA20")
    rsi_signal = False
    if rsi_now is not None:
        recent = [v for v in rsi[-20:] if v is not None]
        if recent and min(recent) < 30 and rsi_now > 40:
            rsi_signal = True
            reasons.append("RSI iz pod 30 nad 40")
        if len(lows) >= 2:
            i1, i2 = lows[-2][0], lows[-1][0]
            if i2 < len(rsi) and i1 < len(rsi) and rsi[i1] and rsi[i2] and lows[-1][1] < lows[-2][1] and rsi[i2] > rsi[i1]:
                rsi_signal = True
                reasons.append("bikovska divergenca RSI")
    volume_ok = _green_volume_expands(candles_15m[-12:])
    if volume_ok:
        reasons.append("zeleni volumen večji")
    structure = False
    lower_high = None
    if len(highs) >= 2 and highs[-1][1] < highs[-2][1]:
        lower_high = highs[-1][1]
    elif highs:
        lower_high = highs[-1][1]
    if lower_high and last > lower_high:
        structure = True
        reasons.append("preboj zadnjega nižjega vrha")
    recent_lows = [row[3] for row in candles_15m[-8:]]
    still_falling = (
        len(recent_lows) >= 6
        and recent_lows[-1] < recent_lows[-4]
        and recent_lows[-2] < recent_lows[-5]
        and recent_lows[-1] <= min(recent_lows[-3:-1])
    )
    freefall = bool(ema_now and last < ema_now and not higher_low and still_falling)
    overbought = bool(rsi_now is not None and rsi_now > 75 and ema_now and last > ema_now * 1.08)
    uptrend = False
    h_closes = _closes(candles_1h) if len(candles_1h) >= 50 else closes
    if len(h_closes) >= 50:
        h20 = ema_series(h_closes, 20)
        h50 = ema_series(h_closes, 50)
        if h20[-1] and h50[-1] and h20[-1] > h50[-1]:
            uptrend = True
    score = 0
    if depth >= 0.25:
        score += 20
        reasons.append(f"padec {depth:.0%} od vrha")
    if near:
        score += 15
    if higher_low:
        score += 15
    if reclaim:
        score += 15
    if rsi_signal:
        score += 15
    if volume_ok:
        score += 10
    if structure:
        score += 10
    score = max(0, min(100, score))
    kind = "nevtralno"
    label = "Čaka na potrditev"
    action = "watch"
    if freefall:
        kind, label, action = "prosti_pad", "pada, ni dna", "reject"
        score = min(score, 25)
    elif overbought:
        kind, label, action = "predrago", "predrago, ne lovim vrha", "reject"
        score = min(score, 20)
    elif depth >= 0.25 and near and higher_low and (reclaim or structure) and score >= 60:
        kind, label, action = "potrjeno_dno", "potrjeno dno", "buy"
    elif uptrend and near and (higher_low or reclaim) and not freefall and score >= 45:
        kind, label, action = "povratek", "povratek v trendu", "buy"
    elif structure and near and not freefall and not overbought and score >= 40:
        kind, label, action = "ponovni_test", "ponovni test preboja", "buy"
    elif near and (higher_low or depth >= 0.25) and not freefall and not overbought:
        kind = "dno_v_nastajanju"
    return {
        "action": action,
        "kind": kind,
        "label": label,
        "score": score,
        "reasons": reasons,
        "swing_low": swing,
        "local_high": local_high,
        "ema20": ema_now,
        "rsi": rsi_now,
        "confirm_price": last if action == "buy" else None,
        "depth": round(depth, 4),
        "note": "ni napoved",
        "support_low": support_low,
        "support_high": support_high,
    }
