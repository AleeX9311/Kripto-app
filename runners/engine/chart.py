"""1h and 4h structure for the paper runner desk. Descriptive only."""

from __future__ import annotations

from typing import Optional


def _num(rows: list[list]) -> tuple[list[float], list[float], list[float], list[float]]:
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    vols = [float(r[5] or 0) for r in rows]
    return highs, lows, closes, vols


def swings(values: list[float], kind: str, look: int = 2) -> list[tuple[int, float]]:
    out = []
    for i in range(look, len(values) - look):
        window = values[i - look : i + look + 1]
        if kind == "high" and values[i] >= max(window):
            out.append((i, values[i]))
        if kind == "low" and values[i] <= min(window):
            out.append((i, values[i]))
    return out


def _zone(price: float, pad: float = 0.035) -> dict:
    return {"lo": round(price * (1 - pad), 8), "hi": round(price * (1 + pad), 8)}


def read_chart(rows: Optional[list[list]], label: str) -> dict:
    """Higher highs, two higher lows, trendline, zones, R:R. Missing candles fail closed."""
    empty = {
        "label": label,
        "ok": False,
        "reason": "ni podatka (sveče)",
        "higher_highs": False,
        "higher_lows": False,
        "structure": False,
        "ath": None,
        "swing_highs": [],
        "swing_lows": [],
        "trendline_break": False,
        "pullback": None,
        "zones": None,
        "rr": None,
        "in_buy_zone": False,
    }
    rows = sorted(rows or [], key=lambda r: r[0])
    if len(rows) < 16:
        return empty
    highs, lows, closes, _vols = _num(rows)
    if min(closes) <= 0:
        return empty
    hi_sw = swings(highs, "high")
    lo_sw = swings(lows, "low")
    ath = max(highs)
    ath_i = highs.index(ath)
    higher_highs = len(hi_sw) >= 2 and hi_sw[-1][1] > hi_sw[-2][1]
    higher_lows = len(lo_sw) >= 2 and lo_sw[-1][1] > lo_sw[-2][1]
    structure = higher_highs and higher_lows
    trend_break = False
    if len(lo_sw) >= 2 and lo_sw[-1][0] != lo_sw[-2][0]:
        x1, y1 = lo_sw[-2]
        x2, y2 = lo_sw[-1]
        slope = (y2 - y1) / (x2 - x1)
        line_now = y2 + slope * (len(closes) - 1 - x2)
        trend_break = closes[-1] > line_now and slope > 0
    last = closes[-1]
    pullback = (ath - last) / ath if ath else None
    deep = pullback is not None and pullback >= 0.25 and last < ath * 0.85
    zones = None
    rr = None
    in_zone = False
    if len(lo_sw) >= 1 and ath > 0:
        low_a = lo_sw[-1][1]
        low_b = lo_sw[-2][1] if len(lo_sw) >= 2 else low_a * 0.82
        zone_a = _zone(low_a, 0.03)
        zone_b = _zone(min(low_b, low_a * 0.9), 0.04)
        stop_a = round(zone_a["lo"] * 0.985, 8)
        stop_b = round(zone_b["lo"] * 0.985, 8)
        no_entry = {"lo": round(ath * 0.82, 8), "hi": round(ath * 0.98, 8)}
        invalidation = round(min(low_a, low_b), 8)
        entry = last
        stop = stop_a if zone_a["lo"] <= last <= zone_a["hi"] else stop_b
        reward = max(ath - entry, entry * 2)  # toward ATH or about 3x from a deep dip
        risk = max(entry - stop, entry * 0.01)
        rr = round(reward / risk, 2) if risk > 0 else None
        in_zone = (zone_a["lo"] <= last <= zone_a["hi"] or zone_b["lo"] <= last <= zone_b["hi"]) and structure and deep
        zones = {
            "ath": round(ath, 8),
            "resistance": [{"lo": round(ath * 0.98, 8), "hi": round(ath, 8), "name": "ATH"}],
            "no_entry": {**no_entry, "name": "No-entry zone – počakaj na dip"},
            "buy_a": {**zone_a, "stop": stop_a, "name": "Buy Zone A"},
            "buy_b": {**zone_b, "stop": stop_b, "name": "Buy Zone B"},
            "invalidation": invalidation,
            "deep_pullback": deep,
        }
    return {
        "label": label,
        "ok": True,
        "reason": "struktura" if structure else "ni višjih vrhov in dveh višjih dni",
        "higher_highs": higher_highs,
        "higher_lows": higher_lows,
        "structure": structure,
        "ath": round(ath, 8),
        "ath_index": ath_i,
        "swing_highs": [{"i": i, "p": round(p, 8)} for i, p in hi_sw[-4:]],
        "swing_lows": [{"i": i, "p": round(p, 8)} for i, p in lo_sw[-4:]],
        "trendline_break": trend_break,
        "pullback": None if pullback is None else round(pullback, 4),
        "zones": zones,
        "rr": rr,
        "in_buy_zone": bool(in_zone),
        "last": round(last, 8),
    }
