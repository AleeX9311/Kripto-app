"""Five entry agents. A paper buy requires every vote to be YES.

KIT looks at buy concentration in DexScreener transaction counts.
TREND looks at momentum and whether the token is boosted.
VARNOST looks at GoPlus plus liquidity and age.
BIK/MEDVED vetoes a 5-minute reversal and a blown-out 1-hour move.
IZVAJANJE checks that the route is PancakeSwap and slippage looks small.
"""

from __future__ import annotations

import time
from typing import Any

import config
from strategy import _f, _txns

AGENT_IDS = ("kit", "trend", "varnost", "bik_medved", "izvajanje")

# Classification only. Scanning is not limited to these names.
MAJOR_SYMBOLS = {"wbnb", "bnb", "btcb", "eth", "weth", "cake"}
MAJOR_ADDRS = {
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",  # BTCB
    "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH
    "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82",  # CAKE
}
STABLE_SYMBOLS = {"usdt", "usdc", "busd", "dai", "fdusd", "tusd", "usdp", "usd1"}
MEME_HINTS = (
    "doge", "pepe", "floki", "shib", "inu", "elon", "baby", "meme", "trump",
    "bonk", "wojak", "frog", "moon", "safemoon", "neiro", "wif", "chad", "cat",
)

AGENT_LABELS = {
    "kit": "KIT",
    "trend": "TREND",
    "varnost": "VARNOST",
    "bik_medved": "BIK/MEDVED",
    "izvajanje": "IZVAJANJE",
}

MAX_CANDIDATES = 30
MAX_ACTIVITY = 40
MAX_EQUITY_POINTS = 500


def is_stable(symbol: str) -> bool:
    return (symbol or "").strip().lower() in STABLE_SYMBOLS


def classify(symbol: str, name: str = "", address: str = "", mcap: float = 0, boosted: bool = False, source: str = "") -> str:
    """MEME, ALT, or MAJOR. Majors are a label, not a scan allow-list."""
    sym = (symbol or "").strip().lower()
    addr = (address or "").strip().lower()
    blob = f"{sym} {(name or '').lower()}"
    if addr in MAJOR_ADDRS or sym in MAJOR_SYMBOLS:
        return "MAJOR"
    if any(hint in blob for hint in MEME_HINTS):
        return "MEME"
    if source == "new" and (not mcap or mcap < 100_000_000):
        return "MEME"
    if boosted and (not mcap or mcap < 80_000_000):
        return "MEME"
    return "ALT"


def _bars(category: str) -> dict:
    """Altcoins and majors do not need meme-sized spikes."""
    if category == "MAJOR":
        return {"ch5": -1.0, "ch1h": -1.5, "vol_frac": 0.35, "share": 0.42, "ratio": 1.0, "vol24": 20_000, "max_mcap": 0}
    if category == "ALT":
        return {"ch5": -0.35, "ch1h": -0.6, "vol_frac": 0.5, "share": 0.48, "ratio": 1.1, "vol24": 30_000, "max_mcap": 5_000_000_000}
    return {
        "ch5": 0.0, "ch1h": 0.0, "vol_frac": 0.8, "share": 0.56,
        "ratio": config.MIN_BUY_SELL_RATIO, "vol24": config.MIN_VOL_24H, "max_mcap": config.MAX_FDV,
    }


def _vote(yes: bool, reason: str, **numbers: Any) -> dict:
    clean = {}
    for key, value in numbers.items():
        if isinstance(value, float):
            clean[key] = round(value, 4)
        else:
            clean[key] = value
    return {"vote": "YES" if yes else "NO", "reason": reason, "numbers": clean}


def agent_kit(pair: dict, ctx: dict | None = None) -> dict:
    """Whale / smart-wallet proxy: buy-count concentration and size."""
    b5, s5, t5 = _txns(pair, "m5")
    b1, s1, t1 = _txns(pair, "h1")
    vol5 = _f((pair.get("volume") or {}).get("m5"))
    share = (b5 / t5) if t5 else 0.0
    bars = _bars((ctx or {}).get("category") or "MEME")
    r5 = (b5 / s5) if s5 > 0 else (999.0 if b5 > 0 else 0.0)
    r1 = (b1 / s1) if s1 > 0 else (999.0 if b1 > 0 else 0.0)
    avg_buy = (vol5 * share / b5) if b5 else 0.0
    numbers = {
        "buys_5m": b5,
        "sells_5m": s5,
        "txns_1h": t1,
        "buy_share": share,
        "ratio_5m": r5,
        "avg_buy_usd": avg_buy,
    }
    if t5 < config.MIN_TXNS_5M:
        return _vote(False, f"premalo 5m poslov ({t5})", **numbers)
    if t1 < config.MIN_TXNS_1H:
        return _vote(False, f"premalo 1h poslov ({t1})", **numbers)
    if r5 < bars["ratio"] or r1 < bars["ratio"]:
        return _vote(False, f"nakupi niso skoncentrirani (5m {r5:.2f})", **numbers)
    if share < bars["share"]:
        return _vote(False, f"delež nakupov {share:.0%} je prenizek", **numbers)
    return _vote(
        True,
        f"veliki nakupi, delež {share:.0%}, ~${avg_buy:.0f}/nakup",
        **numbers,
    )


def agent_trend(pair: dict, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    pc = pair.get("priceChange") or {}
    vol = pair.get("volume") or {}
    ch5 = _f(pc.get("m5"))
    ch1h = _f(pc.get("h1"))
    v5 = _f(vol.get("m5"))
    v1h = _f(vol.get("h1"))
    avg5 = v1h / 12.0 if v1h > 0 else 0.0
    bars = _bars(ctx.get("category") or "MEME")
    boosted = bool(ctx.get("boosted"))
    numbers = {"ch5": ch5, "ch1h": ch1h, "vol_5m": v5, "boost": boosted, "category": ctx.get("category") or "MEME"}
    if ch5 <= bars["ch5"]:
        return _vote(False, f"5m {ch5:.1f}% ne raste", **numbers)
    if ch1h <= bars["ch1h"]:
        return _vote(False, f"1h {ch1h:.1f}% ne raste", **numbers)
    if v5 <= 0:
        return _vote(False, "ni 5m prometa", **numbers)
    if avg5 > 0 and v5 < avg5 * bars["vol_frac"]:
        return _vote(False, f"5m promet ${v5:.0f} pod povprečjem", **numbers)
    boost = ", DexScreener boost" if boosted else ""
    return _vote(True, f"momentum 5m {ch5:+.1f}% / 1h {ch1h:+.1f}%{boost}", **numbers)


def agent_varnost(pair: dict, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    liq = _f((pair.get("liquidity") or {}).get("usd"))
    fdv = _f(pair.get("fdv"))
    mcap = _f(pair.get("marketCap")) or fdv
    created = pair.get("pairCreatedAt")
    age_min = None
    if created:
        age_min = (time.time() * 1000 - _f(created)) / 60000.0
    numbers = {"liq_usd": liq, "mcap_usd": mcap, "age_min": age_min if age_min is not None else -1}
    if liq < config.MIN_LIQUIDITY_USD:
        return _vote(False, f"likvidnost ${liq:.0f} prenizka", **numbers)
    max_mcap = _bars(ctx.get("category") or "MEME")["max_mcap"]
    if mcap and mcap < config.MIN_FDV:
        return _vote(False, f"mcap ${mcap:.0f} izven meja", **numbers)
    if max_mcap and mcap and mcap > max_mcap:
        return _vote(False, f"mcap ${mcap:.0f} izven meja", **numbers)
    if mcap and liq > 0 and (mcap / liq) > config.MAX_MCAP_LIQ_RATIO:
        return _vote(False, f"mcap/likv {mcap / liq:.1f} previsok", **numbers)
    if age_min is None:
        return _vote(False, "starost para ni znana", **numbers)
    if age_min < config.MIN_PAIR_AGE_MIN:
        return _vote(False, f"par star {age_min:.0f} min", **numbers)

    if ctx.get("run_goplus", True):
        preset = ctx.get("goplus")
        if preset is not None:
            ok, reason = preset
        else:
            from api import goplus_ok

            addr = ((pair.get("baseToken") or {}).get("address") or "")
            ok, reason = goplus_ok(addr) if addr else (False, "no_address")
        numbers["goplus"] = reason
        if not ok:
            return _vote(False, f"GoPlus zavrača ({reason})", **numbers)
        if str(reason).startswith("goplus_unavailable") or str(reason).startswith("goplus_code"):
            return _vote(True, f"likvidnost OK, GoPlus ni dosegljiv", **numbers)
        return _vote(True, f"likvidnost ${liq:.0f}, {reason}", **numbers)
    return _vote(True, f"likvidnost ${liq:.0f}, brez GoPlus", **numbers)


def agent_bik_medved(pair: dict, ctx: dict | None = None) -> dict:
    pc = pair.get("priceChange") or {}
    ch5 = _f(pc.get("m5"))
    ch1h = _f(pc.get("h1"))
    vol24 = _f((pair.get("volume") or {}).get("h24"))
    bars = _bars((ctx or {}).get("category") or "MEME")
    numbers = {"ch5": ch5, "ch1h": ch1h, "vol_24h": vol24}
    if ch1h >= config.MAX_CHANGE_1H * 100:
        return _vote(False, f"pregreto, 1h {ch1h:+.0f}%", **numbers)
    if vol24 < bars["vol24"]:
        return _vote(False, f"24h promet ${vol24:.0f} prešibek", **numbers)
    if ch5 < min(0.0, bars["ch5"]):
        return _vote(False, f"5m obrat navzdol ({ch5:.1f}%)", **numbers)
    return _vote(True, f"ni pregretja, 5m {ch5:+.1f}%", **numbers)


def agent_izvajanje(pair: dict, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    dex = str(pair.get("dexId") or "")
    liq = _f((pair.get("liquidity") or {}).get("usd"))
    px = _f(pair.get("priceUsd"))
    size = float(ctx.get("size_usd") or config.MIN_POSITION_USD)
    slip = (size / liq) if liq > 0 else 1.0
    numbers = {"dex": dex or "?", "liq_usd": liq, "slip": slip, "price": px}
    if px <= 0:
        return _vote(False, "ni cene za pot", **numbers)
    if not dex:
        return _vote(False, "ni BSC DEX poti", **numbers)
    if liq < config.MIN_LIQUIDITY_USD:
        return _vote(False, f"likvidnost ${liq:.0f} preplitka za vstop", **numbers)
    if slip > 0.02:
        return _vote(False, f"ocena zdrsa {slip:.1%} previsoka", **numbers)
    return _vote(True, f"{dex}, zdrs ~{slip:.2%}, liq ${liq:.0f}", **numbers)


_FUNCS = {
    "kit": agent_kit,
    "trend": agent_trend,
    "varnost": agent_varnost,
    "bik_medved": agent_bik_medved,
    "izvajanje": agent_izvajanje,
}


def evaluate_agents(pair: dict, ctx: dict | None = None) -> tuple[bool, dict]:
    """Return (all_yes, votes keyed by agent id). Every agent always votes."""
    ctx = ctx or {}
    votes = {agent_id: fn(pair, ctx) for agent_id, fn in _FUNCS.items()}
    all_yes = all(v["vote"] == "YES" for v in votes.values())
    return all_yes, votes


def first_veto(votes: dict) -> str | None:
    for agent_id in AGENT_IDS:
        if votes.get(agent_id, {}).get("vote") != "YES":
            return agent_id
    return None


def ensure_books(st: dict) -> None:
    agents = st.setdefault("agents", {})
    for agent_id in AGENT_IDS:
        bucket = agents.setdefault(agent_id, {})
        bucket.setdefault("evaluated", 0)
        bucket.setdefault("approved", 0)
        bucket.setdefault("vetoed", 0)
    st.setdefault("candidates", [])
    st.setdefault("activity", [])
    st.setdefault("equity_history", [])


def note_votes(st: dict, symbol: str, votes: dict, when: str) -> None:
    ensure_books(st)
    for agent_id in AGENT_IDS:
        vote = votes[agent_id]
        bucket = st["agents"][agent_id]
        bucket["evaluated"] = int(bucket.get("evaluated") or 0) + 1
        if vote["vote"] == "YES":
            bucket["approved"] = int(bucket.get("approved") or 0) + 1
        else:
            bucket["vetoed"] = int(bucket.get("vetoed") or 0) + 1
        bucket["latest"] = {
            "vote": vote["vote"],
            "reason": vote["reason"],
            "symbol": symbol,
            "time": when,
        }


def push_candidate(st: dict, record: dict) -> None:
    ensure_books(st)
    st["candidates"].append(record)
    if len(st["candidates"]) > MAX_CANDIDATES:
        st["candidates"] = st["candidates"][-MAX_CANDIDATES:]


def push_activity(st: dict, event: dict) -> None:
    ensure_books(st)
    st["activity"].append(event)
    if len(st["activity"]) > MAX_ACTIVITY:
        st["activity"] = st["activity"][-MAX_ACTIVITY:]


def push_equity(st: dict, point: dict) -> None:
    ensure_books(st)
    st["equity_history"].append(point)
    if len(st["equity_history"]) > MAX_EQUITY_POINTS:
        st["equity_history"] = st["equity_history"][-MAX_EQUITY_POINTS:]
