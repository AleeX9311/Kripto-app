"""Entry filters — all must pass (any veto blocks). PAPER ONLY."""

from __future__ import annotations

import time
from typing import Any

import config
from api import goplus_ok


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _txns(pair: dict, window: str) -> tuple[int, int, int]:
    t = pair.get("txns") or {}
    w = t.get(window) or {}
    buys = int(_f(w.get("buys")))
    sells = int(_f(w.get("sells")))
    return buys, sells, buys + sells


def prefer_pancakeswap(pairs: list[dict]) -> list[dict]:
    """Prefer PancakeSwap pairs; keep others as fallback."""
    pcs = [p for p in pairs if "pancake" in str(p.get("dexId", "")).lower()]
    return pcs if pcs else pairs


def best_pair_for_token(pairs: list[dict], token_addr: str) -> dict | None:
    token_addr = token_addr.lower()
    cands = []
    for p in pairs:
        base = (p.get("baseToken") or {}).get("address", "").lower()
        quote = (p.get("quoteToken") or {}).get("address", "").lower()
        if base != token_addr and quote != token_addr:
            continue
        # Prefer token as base
        if base == token_addr:
            cands.append(p)
        else:
            cands.append(p)
    if not cands:
        return None
    cands = prefer_pancakeswap(cands)
    # Highest liquidity
    cands.sort(key=lambda p: _f((p.get("liquidity") or {}).get("usd")), reverse=True)
    return cands[0]


def check_trend(pair: dict) -> tuple[bool, str]:
    pc = pair.get("priceChange") or {}
    ch5 = _f(pc.get("m5"))
    ch1h = _f(pc.get("h1"))
    vol = pair.get("volume") or {}
    v5 = _f(vol.get("m5"))
    v1h = _f(vol.get("h1"))
    if ch5 <= 0:
        return False, f"trend: 5m change {ch5:.2f}% <= 0"
    if ch1h <= 0:
        return False, f"trend: 1h change {ch1h:.2f}% <= 0"
    # Increasing 5m volume relative to average 5m of the hour
    avg5 = v1h / 12.0 if v1h > 0 else 0
    if avg5 > 0 and v5 < avg5 * 0.8:
        return False, f"trend: 5m vol {v5:.0f} < 80% of avg5 {avg5:.0f}"
    if v5 <= 0:
        return False, "trend: no 5m volume"
    return True, f"trend ok ch5={ch5:.1f}% ch1h={ch1h:.1f}% v5={v5:.0f}"


def check_flow(pair: dict) -> tuple[bool, str]:
    b5, s5, t5 = _txns(pair, "m5")
    b1, s1, t1 = _txns(pair, "h1")
    if t5 < config.MIN_TXNS_5M:
        return False, f"flow: txns5m={t5} < {config.MIN_TXNS_5M}"
    if t1 < config.MIN_TXNS_1H:
        return False, f"flow: txns1h={t1} < {config.MIN_TXNS_1H}"
    r5 = (b5 / s5) if s5 > 0 else (999.0 if b5 > 0 else 0.0)
    r1 = (b1 / s1) if s1 > 0 else (999.0 if b1 > 0 else 0.0)
    if r5 < config.MIN_BUY_SELL_RATIO:
        return False, f"flow: buy/sell 5m={r5:.2f} < {config.MIN_BUY_SELL_RATIO}"
    if r1 < config.MIN_BUY_SELL_RATIO:
        return False, f"flow: buy/sell 1h={r1:.2f} < {config.MIN_BUY_SELL_RATIO}"
    return True, f"flow ok r5={r5:.2f} r1={r1:.2f} t5={t5} t1={t1}"


def check_safety(pair: dict, run_goplus: bool = True) -> tuple[bool, str]:
    liq = _f((pair.get("liquidity") or {}).get("usd"))
    if liq < config.MIN_LIQUIDITY_USD:
        return False, f"safety: liq ${liq:.0f} < {config.MIN_LIQUIDITY_USD}"

    fdv = _f(pair.get("fdv"))
    mcap = _f(pair.get("marketCap")) or fdv
    if mcap and (mcap < config.MIN_FDV or mcap > config.MAX_FDV):
        return False, f"safety: mcap/fdv ${mcap:.0f} out of bounds"
    if mcap and liq > 0 and (mcap / liq) > config.MAX_MCAP_LIQ_RATIO:
        return False, f"safety: mcap/liq={mcap/liq:.1f} > {config.MAX_MCAP_LIQ_RATIO}"

    created = pair.get("pairCreatedAt")
    if created:
        age_min = (time.time() * 1000 - _f(created)) / 60000.0
        if age_min < config.MIN_PAIR_AGE_MIN:
            return False, f"safety: pair age {age_min:.0f}m < {config.MIN_PAIR_AGE_MIN}m"
    else:
        return False, "safety: unknown pair age"

    notes = [f"liq=${liq:.0f}", f"mcap=${mcap:.0f}"]
    if run_goplus:
        base = pair.get("baseToken") or {}
        addr = base.get("address") or ""
        if addr:
            ok, reason = goplus_ok(addr)
            notes.append(f"goplus={reason}")
            if not ok:
                return False, f"safety: goplus veto ({reason})"
    return True, "safety ok " + " ".join(notes)


def check_bear_veto(pair: dict) -> tuple[bool, str]:
    """Return (pass, reason). False = veto. DexScreener priceChange is in percent."""
    pc = pair.get("priceChange") or {}
    ch5 = _f(pc.get("m5"))
    ch1h = _f(pc.get("h1"))
    vol24 = _f((pair.get("volume") or {}).get("h24"))
    # config.MAX_CHANGE_1H is a fraction (3.0 = 300%)
    if ch1h >= config.MAX_CHANGE_1H * 100:
        return False, f"bear: 1h +{ch1h:.0f}% overextended (>=300%)"
    if vol24 < config.MIN_VOL_24H:
        return False, f"bear: vol24h ${vol24:.0f} < {config.MIN_VOL_24H}"
    if ch5 < 0:
        return False, f"bear: 5m falling ({ch5:.2f}%)"
    return True, "bear ok"


def all_checks(pair: dict, run_goplus: bool = True) -> tuple[bool, list[str]]:
    results = []
    for name, fn in (
        ("trend", check_trend),
        ("flow", check_flow),
        ("safety", lambda p: check_safety(p, run_goplus=run_goplus)),
        ("bear", check_bear_veto),
    ):
        ok, reason = fn(pair)
        results.append(f"{'PASS' if ok else 'VETO'} {name}: {reason}")
        if not ok:
            return False, results
    # Extra: must have a usable price
    if _f(pair.get("priceUsd")) <= 0:
        results.append("VETO price: no priceUsd")
        return False, results
    return True, results
