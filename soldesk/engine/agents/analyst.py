"""Analyst — scores 0..1 from current market STRUCTURE. Not a price forecast.

Under 0.6 = no trade. Missing inputs score as 0 (capital preservation).
"""

from __future__ import annotations

from typing import Optional

import config
from market import Market
from models import AnalystResult, Candidate, Event, FilterResult, ScoreComponent, iso
from simfeed import SimFeed


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class Analyst:
    def __init__(self, market: Market, sim: Optional[SimFeed] = None, data_source: str = "LIVE") -> None:
        self.market = market
        self.sim = sim
        self.data_source = data_source

    def score(self, cand: Candidate, filt: FilterResult) -> tuple[AnalystResult, Event]:
        comps: list[ScoreComponent] = []
        weights = config.ANALYST_WEIGHTS

        comps.append(self._liquidity(cand, weights["liquidity"]))
        comps.append(self._turnover(cand, weights["turnover"]))
        comps.append(self._flow(cand, weights["flow"]))
        structure, label = self._structure(cand, weights["structure"])
        comps.append(structure)
        comps.append(self._age(cand, weights["age"]))
        comps.append(self._holders(filt, weights["holders"]))

        total = sum(c.weight * c.score for c in comps)
        total = round(_clamp(total), 4)
        # Never chase a vertical pump, even if other components pull the average up.
        pump_veto = "vertikalni pump" in label.lower()
        passed = total >= config.MIN_ANALYST_SCORE and not pump_veto
        result = AnalystResult(
            mint=cand.mint,
            symbol=cand.symbol,
            total=total,
            passed=passed,
            components=comps,
            structure_label=label,
        )
        bits = ", ".join(f"{c.name}={c.score:.2f}" for c in comps)
        if passed:
            msg = f"{cand.symbol}  score {total:.2f} ≥ {config.MIN_ANALYST_SCORE:.2f}  [{bits}]  {label}"
            level = "ok"
        elif pump_veto:
            msg = f"{cand.symbol}  score {total:.2f}  VETO  {label}  [{bits}]"
            level = "kill"
        else:
            msg = f"{cand.symbol}  score {total:.2f} < {config.MIN_ANALYST_SCORE:.2f}  no trade  [{bits}]  {label}"
            level = "kill"
        ev = Event(ts=iso(), agent="ANALYST", level=level, message=msg, mint=cand.mint, symbol=cand.symbol)
        return result, ev

    def _liquidity(self, cand: Candidate, w: float) -> ScoreComponent:
        liq = cand.liquidity_usd
        if liq <= 0:
            return ScoreComponent("liquidity", w, 0.0, "ni podatka")
        # $30k ~ 0.1, $50k ~ 0.3, $200k ~ 0.7, $1m ~ 1.0
        if liq < 30_000:
            s = 0.05
        elif liq < 50_000:
            s = 0.15
        else:
            s = _clamp((liq - 50_000) / (1_000_000 - 50_000) * 0.7 + 0.3)
        return ScoreComponent("liquidity", w, round(s, 4), f"${liq:,.0f}")

    def _turnover(self, cand: Candidate, w: float) -> ScoreComponent:
        if cand.liquidity_usd <= 0 or cand.volume_1h_usd is None:
            return ScoreComponent("turnover", w, 0.0, "ni podatka")
        ratio = cand.volume_1h_usd / cand.liquidity_usd
        # Healthy memecoin turnover roughly 0.3–3x liquidity per hour.
        # Very high (>8) looks like a wash / vertical pump.
        if ratio < 0.05:
            s, note = 0.15, f"{ratio:.2f}x (thin)"
        elif ratio < 0.3:
            s, note = 0.45 + (ratio - 0.05) / 0.25 * 0.2, f"{ratio:.2f}x"
        elif ratio <= 3.0:
            s, note = 0.75 + min(ratio, 1.5) / 1.5 * 0.2, f"{ratio:.2f}x"
        elif ratio <= 8.0:
            s, note = 0.55, f"{ratio:.2f}x (hot)"
        else:
            s, note = 0.15, f"{ratio:.2f}x (wash/pump-like)"
        return ScoreComponent("turnover", w, round(_clamp(s), 4), note)

    def _flow(self, cand: Candidate, w: float) -> ScoreComponent:
        buys, sells = cand.buys_1h, cand.sells_1h
        if buys is None or sells is None:
            return ScoreComponent("flow", w, 0.0, "ni podatka")
        total = buys + sells
        if total <= 0:
            return ScoreComponent("flow", w, 0.0, "no trades in 1h")
        buy_ratio = buys / total
        makers = None
        if cand.buyers_1h is not None and cand.sellers_1h is not None:
            makers = cand.buyers_1h + cand.sellers_1h
        # Prefer balanced-to-slightly-positive bid, with more than a handful of makers
        s = _clamp((buy_ratio - 0.35) / 0.35)  # 0.35 -> 0, 0.70 -> 1
        if buy_ratio > 0.85 and (cand.price_change_5m or 0) > 30:
            s *= 0.4  # one-sided melt-up, not healthy flow
            note = f"buy {buy_ratio:.0%} (one-sided into a spike)"
        else:
            note = f"buy {buy_ratio:.0%} of {total} tx"
        if makers is not None:
            if makers < 15:
                s *= 0.5
                note += f", {makers} makers (thin)"
            else:
                note += f", {makers} makers"
                s = _clamp(s + 0.05)
        return ScoreComponent("flow", w, round(_clamp(s), 4), note)

    def _structure(self, cand: Candidate, w: float) -> tuple[ScoreComponent, str]:
        """Describe current price structure. This is not a forecast."""
        candles = self._ohlcv(cand)
        label = "struktura: ni podatka"
        if candles and len(candles) >= 20:
            s, label = self._structure_from_candles(candles)
            return ScoreComponent("structure", w, round(s, 4), label), label

        # Fallback: DexScreener percent changes only (still descriptive, not a prediction)
        m5 = cand.price_change_5m
        h1 = cand.price_change_1h
        if m5 is None and h1 is None:
            return ScoreComponent("structure", w, 0.0, "ni podatka"), label

        m5 = m5 if m5 is not None else 0.0
        h1 = h1 if h1 is not None else 0.0
        if m5 >= 40 or h1 >= 80:
            label = "struktura: vertikalni pump — ne lovim"
            return ScoreComponent("structure", w, 0.1, label), label
        if h1 <= -20 and m5 < 0:
            label = "struktura: nižji minimi, še pada"
            return ScoreComponent("structure", w, 0.15, label), label
        if 4 <= h1 <= 40 and -12 <= m5 <= 3:
            label = "struktura: umik po dvigu (približek, brez sveč)"
            return ScoreComponent("structure", w, 0.75, label), label
        if h1 > 0 and m5 < 0:
            label = "struktura: blag umik, brez sveč za višji minimum"
            return ScoreComponent("structure", w, 0.55, label), label
        label = f"struktura: 5m {m5:+.1f}% / 1h {h1:+.1f}% (brez sveč)"
        s = 0.4 if abs(h1) < 15 else 0.3
        return ScoreComponent("structure", w, s, label), label

    def _structure_from_candles(self, candles: list[list]) -> tuple[float, str]:
        # candles: [ts, o, h, l, c, vol] oldest -> newest (normalize order)
        rows = sorted(candles, key=lambda r: r[0])
        closes = [float(r[4]) for r in rows]
        lows = [float(r[3]) for r in rows]
        highs = [float(r[2]) for r in rows]
        last = closes[-1]
        if last <= 0:
            return 0.0, "struktura: ni podatka"

        window = closes[-12:]
        change_recent = (window[-1] - window[0]) / window[0] if window[0] else 0.0
        # vertical pump: last 8-12 candles mostly up and large range
        ups = sum(1 for i in range(1, len(window)) if window[i] > window[i - 1])
        if change_recent >= 0.50 and ups >= 8:
            return 0.08, "struktura: vertikalni pump — ne lovim"

        swing_lows = _swing_lows(lows, look=3)
        higher_low = False
        if len(swing_lows) >= 2:
            higher_low = swing_lows[-1][1] > swing_lows[-2][1] * 1.002
        recent_high = max(highs[-24:]) if len(highs) >= 8 else max(highs)
        pullback = 0.0
        if recent_high > 0:
            pullback = (recent_high - last) / recent_high

        if higher_low and 0.03 <= pullback <= 0.25:
            return 0.88, "struktura: višji minimum in umik pod lokalnim vrhom"
        if higher_low:
            return 0.72, "struktura: višji minimum"
        if 0.05 <= pullback <= 0.20 and change_recent > -0.1:
            return 0.60, "struktura: umik 5–20% pod lokalnim vrhom"
        if len(swing_lows) >= 2 and swing_lows[-1][1] < swing_lows[-2][1]:
            return 0.18, "struktura: nižji minimi, še pada"
        if change_recent <= -0.15:
            return 0.20, "struktura: padajoč razpon"
        return 0.40, "struktura: brez jasnega višjega minimuma"

    def _age(self, cand: Candidate, w: float) -> ScoreComponent:
        age = cand.age_seconds()
        if age is None:
            return ScoreComponent("age", w, 0.0, "ni podatka")
        minutes = age / 60.0
        hours = minutes / 60.0
        if minutes < 15:
            s, note = 0.10, f"{minutes:.0f} min (zelo sveže)"
        elif minutes < 60:
            s, note = 0.40, f"{minutes:.0f} min"
        elif hours < 24:
            s, note = 0.80, f"{hours:.1f} h"
        elif hours < 24 * 14:
            s, note = 0.90, f"{hours/24:.1f} d"
        else:
            s, note = 0.70, f"{hours/24:.0f} d"
        return ScoreComponent("age", w, s, note)

    def _holders(self, filt: FilterResult, w: float) -> ScoreComponent:
        if filt.top10_pct is None:
            return ScoreComponent("holders", w, 0.0, "ni podatka")
        pct = filt.top10_pct
        # Filter already caps this. Tighter concentration still scores higher.
        cap = float(config.MAX_TOP10_HOLDER_PCT)
        s = _clamp((cap - pct) / cap * 0.5 + 0.5)
        return ScoreComponent("holders", w, round(s, 4), f"top10 {pct:.1f}%")

    def _ohlcv(self, cand: Candidate) -> Optional[list[list]]:
        if self.data_source == "SIM" and self.sim is not None:
            return self.sim.ohlcv(cand.mint)
        if not cand.pair_address:
            return None
        return self.market.gecko_ohlcv(cand.pair_address, aggregate=5, limit=80)


def _swing_lows(lows: list[float], look: int = 3) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for i in range(look, len(lows) - look):
        window = lows[i - look : i + look + 1]
        if lows[i] <= min(window):
            out.append((i, lows[i]))
    return out
