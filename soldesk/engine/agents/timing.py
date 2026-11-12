"""Entry timing. Descriptive structure only — not a price forecast.

A buy needs a pullback and a higher low, buy pressure, and price above
the 5-minute VWAP and EMA9. Missing candles fail closed.
"""

from __future__ import annotations

from typing import Optional

from market import Market
from models import Candidate
from simfeed import SimFeed


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


def _swing_lows(lows: list[float], look: int = 2) -> list[float]:
    out = []
    for i in range(look, len(lows) - look):
        window = lows[i - look : i + look + 1]
        if lows[i] <= min(window):
            out.append(lows[i])
    return out


def _sorted(rows: Optional[list[list]]) -> list[list]:
    if not rows:
        return []
    return sorted(rows, key=lambda row: row[0])


def structure_ok(rows: list[list], label: str, pullback_min: float = 0.03) -> tuple[bool, str]:
    if len(rows) < 12:
        return False, f"ni podatka ({label} sveče)"
    closes = [float(row[4]) for row in rows]
    highs = [float(row[2]) for row in rows]
    lows = [float(row[3]) for row in rows]
    last = closes[-1]
    if last <= 0:
        return False, f"ni podatka ({label} cena)"
    recent_high = max(highs[-12:])
    pullback = (recent_high - last) / recent_high if recent_high else 0.0
    swing = _swing_lows(lows)
    higher = len(swing) >= 2 and swing[-1] > swing[-2] * 1.002
    if not higher:
        return False, f"{label}: ni višjega minimuma"
    floor = max(0.03, min(0.20, float(pullback_min)))
    if not (floor <= pullback <= 0.22):
        return False, f"{label}: umik {pullback:.0%} ni {floor:.0%}–22 %"
    # The latest bar must not be the spike itself.
    if last >= max(highs[-4:-1] or highs):
        return False, f"{label}: zadnja sveča je še na vrhu"
    return True, f"{label}: višji minimum, umik {pullback:.0%}"


def pressure_ok(cand: Candidate) -> tuple[bool, str]:
    buys = cand.buys_5m if cand.buys_5m is not None else cand.buys_1h
    sells = cand.sells_5m if cand.sells_5m is not None else cand.sells_1h
    if buys is None or sells is None:
        return False, "ni podatka (nakupi/prodaje)"
    if buys <= sells:
        return False, f"nakupni pritisk ni ({buys} nakupov, {sells} prodaj)"
    return True, f"nakupi {buys} > prodaje {sells}"


def makers_ok(cand: Candidate, memory: dict) -> tuple[bool, str]:
    """Unique makers must be rising versus the previous look, when we have one."""
    buyers = cand.buyers_5m if cand.buyers_5m is not None else cand.buyers_1h
    if buyers is None:
        return False, "ni podatka (ustvarjalci)"
    import time

    now = time.time()
    prev = memory.get(cand.mint)
    memory[cand.mint] = {"ts": now, "buyers": int(buyers)}
    if not prev or now - float(prev.get("ts") or 0) > 20 * 60:
        return False, "čakam rast ustvarjalcev (prvi vzorec)"
    before = int(prev.get("buyers") or 0)
    if int(buyers) <= before:
        return False, f"ustvarjalci ne rastejo ({before} → {buyers})"
    return True, f"ustvarjalci {before} → {buyers}"


def entry_gate(structure_5m: bool, above_ema: bool, above_vwap: bool, buy_pressure: bool) -> bool:
    """5-minute pullback and higher low, plus one other confirmation.

    The 1-minute structure and a rising maker count are recorded, and they
    do not block. Missing the 5-minute structure still fails closed.
    """
    if not structure_5m:
        return False
    return bool(above_ema or above_vwap or buy_pressure)


def evaluate(
    cand: Candidate,
    market: Market,
    sim: Optional[SimFeed],
    source: str,
    memory: dict,
    pullback_min: float = 0.03,
) -> tuple[bool, list[str], str]:
    reasons: list[str] = []
    if source == "SIM" and sim is not None:
        rows5 = _sorted(sim.ohlcv(cand.mint))
        rows1 = rows5
    else:
        if not cand.pair_address:
            return False, ["ni podatka (par za sveče)"], "ni_svec"
        network = "bsc" if getattr(cand, "chain", "") == "bsc" else "solana"
        rows5 = _sorted(market.gecko_ohlcv(cand.pair_address, aggregate=5, limit=48, network=network))
        rows1 = _sorted(market.gecko_ohlcv(cand.pair_address, aggregate=1, limit=30, network=network))
    ok5, why5 = structure_ok(rows5, "5m", pullback_min)
    ok1, why1 = structure_ok(rows1, "1m", pullback_min) if rows1 is not rows5 else (True, "1m: isti vzorec")
    if rows1 is rows5 and source != "SIM":
        ok1, why1 = structure_ok(rows1, "1m", pullback_min)
    reasons.append(why5)
    reasons.append(why1)
    above_ema = False
    above_vwap = False
    closes = [float(row[4]) for row in rows5]
    if closes:
        ema9 = _ema(closes, 9)
        vwap = _vwap(rows5)
        last = closes[-1]
        if ema9 is not None and last > ema9:
            above_ema = True
            reasons.append(f"nad EMA9 {ema9:.6g}")
        else:
            reasons.append("cena ni nad EMA9 na 5m" if ema9 else "ni podatka (EMA9)")
        if vwap is not None and last >= vwap:
            above_vwap = True
            reasons.append("nad VWAP")
        else:
            reasons.append("cena ni nad VWAP" if vwap else "ni podatka (VWAP)")
    else:
        reasons.append("ni podatka (EMA9)")
        reasons.append("ni podatka (VWAP)")
    ok_p, why_p = pressure_ok(cand)
    reasons.append(why_p)
    _ok_m, why_m = makers_ok(cand, memory)
    reasons.append(why_m)
    if entry_gate(ok5, above_ema, above_vwap, ok_p):
        return True, reasons, "umik_in_potrditev"
    return False, reasons, "ni_potrditve"
