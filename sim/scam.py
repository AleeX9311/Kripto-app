"""Anti-scam checks layered on VARNOST. A hit is a mandatory veto."""

from __future__ import annotations

from typing import Any

DEAD = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000000000000000000000000",
}


def _pct(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1:
        num = num / 100.0
    return num


def lp_locked_fraction(info: dict | None) -> float | None:
    """Share of LP burned or locked. None when the payload has no LP holders."""
    if not isinstance(info, dict):
        return None
    holders = info.get("lp_holders") or []
    if not isinstance(holders, list) or not holders:
        return None
    locked = 0.0
    seen = False
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        pct = _pct(holder.get("percent"))
        if pct is None:
            continue
        seen = True
        addr = str(holder.get("address") or "").lower()
        tag = str(holder.get("tag") or holder.get("name") or "").lower()
        is_locked = str(holder.get("is_locked", "0")).lower() in ("1", "true")
        if addr in DEAD or "burn" in tag or "lock" in tag or "dead" in tag or is_locked:
            locked += pct
    return locked if seen else None


def wash_reasons(pair: dict, pool_stats: dict | None = None, trades: list | None = None) -> list[str]:
    """Heuristics from pool stats we actually have. Empty list means no hit."""
    reasons = []
    liq = 0.0
    vol = 0.0
    try:
        liq = float(((pair.get("liquidity") or {}).get("usd")) or 0)
        vol = float(((pair.get("volume") or {}).get("h24")) or 0)
    except (TypeError, ValueError):
        pass
    if liq > 0 and vol / liq > 30:
        reasons.append("volumen/likvidnost > 30x")
    created = pair.get("pairCreatedAt")
    if created:
        import time
        try:
            age_min = (time.time() * 1000 - float(created)) / 60000.0
        except (TypeError, ValueError):
            age_min = None
        if age_min is not None and age_min < 120:
            reasons.append(f"bazen mlajši od 2 h ({age_min:.0f} min)")
    tx = (pair.get("txns") or {}).get("h1") or {}
    try:
        buys = float(tx.get("buys") or 0)
        sells = float(tx.get("sells") or 0)
    except (TypeError, ValueError):
        buys, sells = 0.0, 0.0
    total = buys + sells
    if total >= 40 and (buys / total > 0.92 or sells / total > 0.92):
        reasons.append("izjemno neravnovesje nakup/prodaja")
    stats = pool_stats or {}
    try:
        buyers = float(stats.get("buyers") or 0)
        sellers = float(stats.get("sellers") or 0)
        txns = float(stats.get("txns") or total)
    except (TypeError, ValueError):
        buyers = sellers = txns = 0.0
    uniq = buyers + sellers
    if txns >= 40 and uniq > 0 and uniq / txns < 0.15:
        reasons.append("premalo edinstvenih trgovalcev")
    amounts = []
    for trade in trades or []:
        if isinstance(trade, dict):
            raw = trade.get("volume_usd") or trade.get("amount_usd") or trade.get("volume")
        else:
            raw = trade
        try:
            amounts.append(round(float(raw), 2))
        except (TypeError, ValueError):
            continue
    if len(amounts) >= 10:
        top = max(amounts.count(a) for a in set(amounts))
        if top / len(amounts) >= 0.40:
            reasons.append("ponavljajoči enaki posli")
    return reasons


def scam_reason(pair: dict, lp_fraction: float | None = None, pool_stats: dict | None = None, trades: list | None = None) -> tuple[bool, str]:
    """Return (veto, text). Unknown LP is a flag, not a veto."""
    reasons = wash_reasons(pair, pool_stats, trades)
    if lp_fraction is not None and lp_fraction < 0.80:
        reasons.append(f"LP zaklenjen {lp_fraction:.0%} < 80%")
    flag = "LP neznan" if lp_fraction is None else ""
    if reasons:
        text = "prevara: " + "; ".join(reasons)
        if flag:
            text += f" ({flag})"
        return True, text
    if flag:
        return False, flag
    return False, ""
