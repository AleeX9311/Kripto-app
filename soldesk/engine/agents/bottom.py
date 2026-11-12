"""Bottom and reversal detection for the DNO paper lane.

A coin must already be well off its local high, then show a confirmed
reversal or a base. A new low is a falling knife and does not enter.
This module does not size trades and does not choose the stop or target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import config
from models import Candidate


@dataclass
class BottomRead:
    confirmed: bool
    score: float
    drawdown: Optional[float]
    kind: str
    reasons: list[str] = field(default_factory=list)


def _sorted(rows: Optional[list]) -> list[list]:
    if not rows:
        return []
    return sorted(rows, key=lambda row: row[0])


def _ema(values: list[float], length: int) -> Optional[float]:
    if len(values) < length:
        return None
    k = 2.0 / (length + 1.0)
    prev = sum(values[:length]) / length
    for value in values[length:]:
        prev = value * k + prev * (1.0 - k)
    return prev


def _vwap(rows: list[list]) -> Optional[float]:
    num = 0.0
    den = 0.0
    for row in rows:
        vol = float(row[5] or 0)
        if vol <= 0:
            continue
        typical = (float(row[2]) + float(row[3]) + float(row[4])) / 3.0
        num += typical * vol
        den += vol
    if den <= 0:
        return None
    return num / den


def _rsi(closes: list[float], length: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= length:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / length
    avg_loss = losses / length
    for i in range(length, len(closes)):
        if i > length:
            delta = closes[i] - closes[i - 1]
            avg_gain = (avg_gain * (length - 1) + max(delta, 0.0)) / length
            avg_loss = (avg_loss * (length - 1) + max(-delta, 0.0)) / length
        if avg_loss <= 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def _dump(rows: list[list]) -> Optional[dict]:
    if len(rows) < 12:
        return None
    highs = [float(row[2]) for row in rows]
    lows = [float(row[3]) for row in rows]
    closes = [float(row[4]) for row in rows]
    vols = [float(row[5] or 0) for row in rows]
    peak_i = max(range(len(highs)), key=lambda i: highs[i])
    if peak_i > len(rows) - 4:
        return None
    trough_rel = min(range(peak_i, len(lows)), key=lambda i: lows[i])
    peak = highs[peak_i]
    trough = lows[trough_rel]
    last = closes[-1]
    if peak <= 0 or trough <= 0 or last <= 0:
        return None
    drawdown = (peak - trough) / peak
    rebound = (last - trough) / trough
    return {
        "peak_i": peak_i,
        "trough_i": trough_rel,
        "peak": peak,
        "trough": trough,
        "last": last,
        "drawdown": drawdown,
        "rebound": rebound,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "vols": vols,
    }


def _new_low(info: dict) -> bool:
    lows = info["lows"]
    if len(lows) < 5:
        return True
    return lows[-1] <= min(lows[-5:-1]) * 0.995


def _higher_low(info: dict) -> bool:
    lows = info["lows"]
    trough_i = info["trough_i"]
    if trough_i >= len(lows) - 2:
        return False
    later = lows[trough_i + 1 :]
    if not later:
        return False
    return min(later) > info["trough"] * 1.01


def _volume_back(vols: list[float]) -> bool:
    if len(vols) < 6:
        return False
    recent = sum(vols[-3:])
    prior = sum(vols[-6:-3])
    return prior > 0 and recent >= prior * 0.90


def _bullish_divergence(closes: list[float], lows: list[float]) -> bool:
    rsi = _rsi(closes)
    pairs = [i for i, value in enumerate(rsi) if value is not None]
    if len(pairs) < 6:
        return False
    first = min(pairs[-8:-3] or pairs[:1], key=lambda i: lows[i])
    second = min(pairs[-3:], key=lambda i: lows[i])
    if second <= first:
        return False
    return lows[second] <= lows[first] and (rsi[second] or 0) > (rsi[first] or 0) + 2


def _base_break(info: dict) -> bool:
    n = config.DNO_BASE_CANDLES
    highs = info["highs"]
    lows = info["lows"]
    closes = info["closes"]
    if len(closes) < n + 2:
        return False
    window_h = highs[-(n + 1) : -1]
    window_l = lows[-(n + 1) : -1]
    if not window_h or not window_l:
        return False
    mid = (max(window_h) + min(window_l)) / 2.0
    if mid <= 0:
        return False
    span = (max(window_h) - min(window_l)) / mid
    near_bottom = min(window_l) <= info["trough"] * 1.08
    flat = span <= config.DNO_BASE_RANGE
    broke = closes[-1] > max(window_h)
    held = lows[-1] >= min(window_l) * 0.99
    return near_bottom and flat and broke and held


def assess(
    rows_15m: Optional[list],
    rows_1h: Optional[list] = None,
    *,
    buys: Optional[int] = None,
    sells: Optional[int] = None,
    min_drawdown: float = config.DNO_MIN_DRAWDOWN,
    min_score: float = config.DNO_MIN_SCORE,
) -> BottomRead:
    """Score a bottom. Confirmation is required; the score alone is not an entry."""
    primary = _dump(_sorted(rows_15m))
    hourly = _dump(_sorted(rows_1h))
    info = primary or hourly
    reasons: list[str] = []
    if info is None:
        return BottomRead(False, 0.0, None, "ni_svec", ["ni podatka (sveče za dno)"])
    drawdown = info["drawdown"]
    if hourly and hourly["drawdown"] > drawdown:
        drawdown = hourly["drawdown"]
        reasons.append(f"1h padec {hourly['drawdown']:.0%}")
    reasons.append(f"15m padec {info['drawdown']:.0%}" if primary else f"padec {drawdown:.0%}")
    floor = max(config.DNO_MIN_DRAWDOWN, float(min_drawdown))
    if drawdown < floor:
        reasons.append(f"padec {drawdown:.0%} < {floor:.0%}")
        return BottomRead(False, 0.0, drawdown, "preplitvo", reasons)
    if info["rebound"] > 0.55:
        reasons.append(f"že odskočil {info['rebound']:.0%} od dna")
        return BottomRead(False, 0.0, drawdown, "zamujen", reasons)

    knife = _new_low(info)
    higher = _higher_low(info)
    closes = info["closes"]
    source_rows = _sorted(rows_15m) or _sorted(rows_1h)
    start = info["trough_i"]
    window = closes[start:] if len(closes) - start >= 8 else closes[-min(20, len(closes)):]
    ema = _ema(window, min(20, max(5, len(window) - 1)))
    vwap_rows = source_rows[start:] if len(source_rows) - start >= 4 else source_rows[-12:]
    vwap = _vwap(vwap_rows)
    last = info["last"]
    reclaim = (ema is not None and last > ema) or (vwap is not None and last > vwap)
    pressure = buys is not None and sells is not None and buys > sells
    volume = _volume_back(info["vols"])
    divergence = _bullish_divergence(closes, info["lows"])
    base = _base_break(info)
    reversal = (not knife) and higher and reclaim and (pressure or volume)
    confirmed = (not knife) and (reversal or base)
    if knife:
        reasons.append("nov minimum, še pada")
    if higher:
        reasons.append("višji minimum po dnu")
    if reclaim:
        reasons.append("povratek nad EMA20 ali VWAP")
    elif ema is not None:
        reasons.append("cena še pod EMA20 in VWAP")
    if pressure:
        reasons.append(f"nakupi {buys} > prodaje {sells}")
    elif buys is not None and sells is not None:
        reasons.append(f"nakupi {buys} niso nad prodajami {sells}")
    if volume:
        reasons.append("volumen se vrača")
    if divergence:
        reasons.append("bikovska razlika RSI")
    if base:
        reasons.append("baza in zaprtje nad razponom")
    elif not reversal:
        reasons.append("ni potrjenega obrata ali baze")

    dd_score = min(1.0, max(0.0, (drawdown - 0.40) / 0.40))
    structure = 0.85 if reversal else 0.75 if base else 0.25
    flow = 1.0 if pressure else 0.35
    vol_score = 1.0 if volume else 0.40
    confirm_score = 1.0 if confirmed else 0.0
    score = 0.30 * dd_score + 0.30 * structure + 0.15 * flow + 0.10 * vol_score + 0.15 * confirm_score
    if divergence and confirmed:
        score = min(1.0, score + 0.05)
    score = round(score, 4)
    kind = "obrat" if reversal else "baza" if base else "brez"
    need = max(config.DNO_MIN_SCORE, float(min_score))
    ok = confirmed and score >= need
    if confirmed and score < need:
        reasons.append(f"ocena {score:.2f} < {need:.2f}")
    return BottomRead(ok, score, drawdown, kind, reasons)


def evaluate(cand: Candidate, market, sim, source: str, view: dict) -> BottomRead:
    """Load candles and assess. Missing candles fail closed."""
    buys = cand.buys_5m if cand.buys_5m is not None else cand.buys_1h
    sells = cand.sells_5m if cand.sells_5m is not None else cand.sells_1h
    kwargs = {
        "buys": buys,
        "sells": sells,
        "min_drawdown": view.get("dno_min_drawdown", config.DNO_MIN_DRAWDOWN),
        "min_score": view.get("dno_min_score", config.DNO_MIN_SCORE),
    }
    if source == "SIM" and sim is not None:
        rows = sim.ohlcv(cand.mint)
        return assess(rows, rows, **kwargs)
    if not cand.pair_address:
        return BottomRead(False, 0.0, None, "ni_para", ["ni podatka (par za sveče)"])
    network = "bsc" if getattr(cand, "chain", "") == "bsc" else "solana"
    rows15 = market.gecko_ohlcv(cand.pair_address, aggregate=15, limit=48, network=network, timeframe="minute")
    rows1h = market.gecko_ohlcv(cand.pair_address, aggregate=1, limit=48, network=network, timeframe="hour")
    return assess(rows15, rows1h, **kwargs)
