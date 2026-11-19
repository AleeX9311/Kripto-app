"""Paper-only fund. No exchange keys, no live orders, no withdrawals.

Capital is 1000 EUR. Risk per trade is at most 1% of the account.
Size = account × risk / distance to the stop, capped at 1x. A 3% UTC-day
loss blocks new entries. A 15% drop from the equity high is the kill
switch: stops stay, new entries stop, and the fund waits.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
START_EUR = 1000.0
RISK_PCT = 0.01
DAILY_LOSS = 0.03
KILL_DD = 0.15
LEVERAGE_CAP = 1.0
SLIPPAGE_LIMIT = 0.025
CEX_SLIPPAGE = 0.0005
FEE = 0.001  # 0.10% per side when the fee tier is unknown (spot)
FAIL_BUCKET = 8
MIN_AGE_H = 48.0
MIN_MC = 1_000_000.0
MIN_VOL = 2_000_000.0
TOP10_MAX = 0.30
BUNDLE_MAX = 0.10
LJ = ZoneInfo("Europe/Ljubljana")

MAJORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
ALTS = ["XRPUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "TONUSDT"]
UNIVERSE = MAJORS + ALTS

VISION = "https://data-api.binance.vision"
BINANCE = "https://api.binance.com"
UA = {"User-Agent": "kripto-fund/1", "Accept": "application/json"}

PROTECTED = ("risk_pct", "position_pct", "daily_loss", "kill_dd", "weekly_loss", "leverage", "vetoes")
TRADE_FIELDS = [
    "trade_id",
    "strategy",
    "mode",
    "market",
    "side",
    "entry_time",
    "entry_price",
    "stop",
    "target",
    "exit_time",
    "exit_price",
    "fees",
    "funding",
    "slippage",
    "pnl",
    "r_multiple",
    "reason_in",
    "reason_out",
    "regime",
    "backtest_expected_r",
    "notes",
]
ALERT_KINDS = ("kill", "missing_stop", "failed_order", "exchange", "daily")

STRATEGIES = (
    "trend",
    "mean_reversion",
    "vol_breakout",
    "relative_value",
    "xsection",
    "funding",
    "regime_trend",
    "donchian",
    "dominance",
    "vol_target",
    "session",
)

STRATEGY_CARDS = [
    {
        "id": "trend",
        "name": "Sledenje trendu",
        "text": "Dolgi samo če je EMA20 nad EMA50, zadnja sveča se dotakne povprečja in višji minimum drži. Stop pod swing low.",
    },
    {
        "id": "mean_reversion",
        "name": "Vrnitev k povprečju",
        "text": "Dolgi samo globoko pod 20-svečnim povprečjem, in samo če 50-svečno povprečje ne pada. Brez lovljenja zloma.",
    },
    {
        "id": "vol_breakout",
        "name": "Preboj volatilnosti",
        "text": "Ozek razpon, preboj, vstop šele na ponovnem testu. Stop pod robom razpona.",
    },
    {
        "id": "relative_value",
        "name": "Relativna vrednost",
        "text": "Alt, ki v 24 urah jasno prehiti BTC in hkrati drži trend. BTC sam nima tega signala.",
    },
    {
        "id": "xsection",
        "name": "Prečni moment",
        "text": "Eden, najmočnejši tekoči alt v zmernem plusu. Pregretih ne vzamemo.",
    },
    {
        "id": "funding",
        "name": "Funding kot signal",
        "text": "Hyperliquid funding je samo podatek. Ekstremno negativen funding lahko podpre dolgi trend. Vzvod ni vklopljen.",
    },
]

GOLDEN = [
    "Brez stopa ni posla.",
    "Brez backtesta s polnimi stroški ni bota.",
    "Brez 30 ujemajočih se papirnih poslov in pisnega 'go live' ni pravega denarja.",
    "Tveganje na posel je največ 1% računa. Velikost = račun × tveganje / razdalja do stopa.",
    "Dnevna izguba 3% ustavi nove vstope do naslednjega UTC dne.",
    "Izklop je −15% od vrha knjige. Stopi ostanejo. Čakamo na Aleksa.",
    "Vzvod je 1x. Shorti so izklopljeni. Marta in širjenja stopa ni.",
    "CPI in FOMC: 30 minut prej in potem ni vstopov, potem do konca dneva polovična velikost.",
    "Manjkajoč podatek je veto. Številk ne ugibamo.",
]


@dataclass
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class Signal:
    symbol: str
    strategy: str
    entry: float
    stop: float
    reason: str
    kind: str = "cex"


def iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lj_day(now: datetime) -> str:
    return now.astimezone(LJ).strftime("%Y-%m-%d")


def lj_week(now: datetime) -> str:
    local = now.astimezone(LJ)
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def atr(bars: list[Bar], n: int = 14) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, bar in zip(bars[-n - 1 : -1], bars[-n:]):
        trs.append(max(bar.h - bar.l, abs(bar.h - prev.c), abs(bar.l - prev.c)))
    return sum(trs) / len(trs)


def bars_fresh(bars: list[Bar], now: datetime, interval_ms: int = 3_600_000) -> bool:
    if not bars:
        return False
    age_ms = now.timestamp() * 1000 - bars[-1].t
    return 0 <= age_ms <= interval_ms * 2


def utc_day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def position_eur(equity: float, entry: float, stop: float, risk_pct: float = RISK_PCT) -> float:
    """Section 8: size = account × risk fraction / distance to the stop, capped at 1x."""
    if equity <= 0 or entry <= 0 or stop <= 0 or stop >= entry or risk_pct <= 0:
        return 0.0
    distance = (entry - stop) / entry
    if distance <= 0:
        return 0.0
    size = equity * min(risk_pct, RISK_PCT) / distance
    return round(min(size, equity * LEVERAGE_CAP), 2)


def max_drawdown(curve: list[float]) -> float:
    if not curve:
        return 0.0
    peak = curve[0]
    worst = 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, equity / peak - 1)
    return worst


def tx_fails(key: str) -> bool:
    digest = hashlib.sha256(key.encode()).hexdigest()
    return int(digest[:8], 16) % 100 < FAIL_BUCKET


def r_result(entry: float, stop: float, exit_px: float) -> Optional[float]:
    risk = abs(entry - stop) / entry
    if risk <= 0:
        return None
    return ((exit_px - entry) / entry) / risk


def stop_ok(entry: float, stop: float) -> bool:
    if entry <= 0 or stop <= 0 or stop >= entry:
        return False
    dist = (entry - stop) / entry
    return 0.008 <= dist <= 0.06


def trend_signal(symbol: str, bars: list[Bar], funding: Optional[float] = None) -> Optional[Signal]:
    if len(bars) < 60:
        return None
    closes = [b.c for b in bars]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    price = closes[-1]
    if e20[-1] <= e50[-1] or price <= e20[-1]:
        return None
    if bars[-1].l > e20[-1] * 1.006:
        return None
    swing = min(b.l for b in bars[-8:])
    prev = min(b.l for b in bars[-16:-8])
    if swing <= prev:
        return None
    a = atr(bars)
    if not a:
        return None
    stop = min(swing, price - 1.2 * a)
    if not stop_ok(price, stop):
        return None
    if price / closes[-24] - 1 > 0.25:
        return None
    if funding is not None and funding > 0.0005:
        return None
    return Signal(symbol, "trend", price, stop, "pullback na EMA20, višji minimum")


def mean_signal(symbol: str, bars: list[Bar]) -> Optional[Signal]:
    if len(bars) < 60:
        return None
    closes = [b.c for b in bars]
    a = atr(bars)
    if not a:
        return None
    sma = sum(closes[-20:]) / 20
    price = closes[-1]
    if price > sma - 1.2 * a:
        return None
    e50 = ema(closes, 50)
    if e50[-1] < e50[-10] * 0.98:
        return None
    if price / closes[-24] - 1 < -0.12:
        return None
    stop = min(b.l for b in bars[-5:]) - 0.25 * a
    if not stop_ok(price, stop):
        return None
    return Signal(symbol, "mean_reversion", price, stop, "pod 20-svečnim povprečjem, brez zloma")


def breakout_signal(symbol: str, bars: list[Bar]) -> Optional[Signal]:
    if len(bars) < 40:
        return None
    window = bars[-18:-3]
    hi = max(b.h for b in window)
    lo = min(b.l for b in window)
    mid = (hi + lo) / 2
    if mid <= 0 or (hi - lo) / mid > 0.045:
        return None
    if bars[-2].c <= hi or bars[-1].c <= hi or bars[-1].l > hi * 1.004:
        return None
    a = atr(bars) or (hi - lo)
    stop = hi - a
    price = bars[-1].c
    if not stop_ok(price, stop):
        return None
    return Signal(symbol, "vol_breakout", price, stop, "preboj tesnega razpona, retest drži")


def relative_signal(symbol: str, bars: list[Bar], btc: list[Bar]) -> Optional[Signal]:
    if symbol == "BTCUSDT" or len(bars) < 60 or len(btc) < 60:
        return None
    rel = (bars[-1].c / bars[-24].c) - (btc[-1].c / btc[-24].c)
    if rel < 0.02:
        return None
    base = trend_signal(symbol, bars)
    if base is None:
        return None
    return Signal(symbol, "relative_value", base.entry, base.stop, f"prehiti BTC za {rel:.1%}")


def funding_signal(symbol: str, bars: list[Bar], funding: Optional[float]) -> Optional[Signal]:
    if funding is None or funding > -0.0002:
        return None
    if len(bars) < 60:
        return None
    closes = [b.c for b in bars]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    price = closes[-1]
    if e20[-1] <= e50[-1] or price <= e50[-1]:
        return None
    a = atr(bars)
    if not a:
        return None
    stop = min(b.l for b in bars[-8:])
    stop = min(stop, price - 1.2 * a)
    if not stop_ok(price, stop):
        return None
    return Signal(symbol, "funding", price, stop, f"funding {funding:.5f} in trend")


def xsection_signal(books: dict[str, list[Bar]]) -> Optional[Signal]:
    best: Optional[tuple[float, Signal]] = None
    for symbol, bars in books.items():
        if symbol == "BTCUSDT" or len(bars) < 60:
            continue
        change = bars[-1].c / bars[-24].c - 1
        if not 0.02 <= change <= 0.20:
            continue
        base = trend_signal(symbol, bars)
        if base is None:
            continue
        signal = Signal(symbol, "xsection", base.entry, base.stop, f"najmočnejši {change:.1%}")
        if best is None or change > best[0]:
            best = (change, signal)
    return None if best is None else best[1]


def champion_of(closed: list[dict]) -> tuple[str, str]:
    grouped: dict[str, list[float]] = {}
    for trade in closed:
        try:
            grouped.setdefault(trade["strategy"], []).append(float(trade["result_r"]))
        except (KeyError, TypeError, ValueError):
            continue
    eligible = {name: vals for name, vals in grouped.items() if len(vals) >= 20}
    if not eligible:
        return "trend", "začasni prvak, premalo zaprtih poslov"
    best = max(eligible, key=lambda name: (sum(eligible[name]) / len(eligible[name]), len(eligible[name])))
    return best, "prvak po pričakovanju zaprtih poslov"


def in_event_window(now: datetime, events: list[dict], minutes: int = 30) -> list[dict]:
    hit = []
    span = timedelta(minutes=minutes)
    for event in events:
        at = event.get("at")
        if not isinstance(at, datetime):
            continue
        if at - span <= now <= at + span:
            hit.append(event)
    return hit


def is_macro_event(title: str) -> bool:
    text = title.upper()
    if "CPI" in text:
        return True
    if "FOMC MEMBER" in text or "SPEAKS" in text:
        return False
    return "FOMC" in text


def meme_verdict(sec: Optional[dict]) -> tuple[bool, str]:
    """Fail closed. Bundle data we do not have is a veto, never a pass."""
    if not isinstance(sec, dict) or not sec:
        return False, "ni podatka (varnost)"
    if sec.get("honeypot") in (None, ""):
        return False, "ni podatka (honeypot)"
    if str(sec.get("honeypot")).lower() in ("1", "true"):
        return False, "honeypot"
    if sec.get("mintable") in (None, ""):
        return False, "ni podatka (mint)"
    if str(sec.get("mintable")).lower() in ("1", "true"):
        return False, "lastnik lahko kuje"
    if sec.get("freezable") in (None, ""):
        return False, "ni podatka (freeze)"
    if str(sec.get("freezable")).lower() in ("1", "true"):
        return False, "račune se da zamrzniti"
    if sec.get("top10") is None:
        return False, "ni podatka (top 10)"
    try:
        top10 = float(sec["top10"])
    except (TypeError, ValueError):
        return False, "ni podatka (top 10)"
    if top10 > TOP10_MAX:
        return False, f"top 10 {top10:.0%} > 30%"
    if sec.get("bundle") is None:
        return False, "ni podatka (bundle)"
    try:
        bundle = float(sec["bundle"])
    except (TypeError, ValueError):
        return False, "ni podatka (bundle)"
    if bundle > BUNDLE_MAX:
        return False, f"bundle {bundle:.0%} > 10%"
    if sec.get("reserve_usd") in (None, "", 0):
        return False, "ni podatka (rezerva)"
    return True, "varnostni pregled gre skozi"


def consider_change(state: dict, proposal: dict) -> dict:
    """Learning may not touch size, brakes, or vetoes. Nothing is applied automatically."""
    touched = [key for key in PROTECTED if key in proposal]
    closed = len(state.get("closed") or [])
    refusal = {
        "applied": False,
        "proposal": proposal.get("name", "sprememba"),
        "reason": "",
    }
    if touched:
        refusal["reason"] = "zavrnjeno: velikost, zavore in vetoji se ne spreminjajo"
        return refusal
    if closed < 30:
        refusal["reason"] = f"zavrnjeno: {closed}/30 zaprtih poslov"
        return refusal
    changes = (state.get("learning") or {}).get("changes") or []
    version = (state.get("learning") or {}).get("version", 1)
    if any(item.get("version") == version for item in changes):
        refusal["reason"] = "zavrnjeno: v tej različici je že ena sprememba"
        return refusal
    refusal["reason"] = "prag je dosežen, samodejne spremembe pa so izklopljene"
    return refusal


def new_state(now: datetime) -> dict:
    return {
        "version": 1,
        "note": "PAPER",
        "cash_eur": START_EUR,
        "day_key": utc_day(now),
        "day_start_equity": START_EUR,
        "week_key": lj_week(now),
        "week_start_equity": START_EUR,
        "peak_equity": START_EUR,
        "kill": False,
        "alerts": [],
        "champion_id": None,
        "champion_since": None,
        "challenger_id": None,
        "challenger_since": None,
        "positions": [],
        "closed": [],
        "equity_history": [{"t": iso(now), "equity": START_EUR}],
        "incidents": [],
        "event_cuts": [],
        "learning": {"version": 1, "changes": []},
        "created": iso(now),
    }


def _incident(state: dict, now: datetime, kind: str, detail: str) -> None:
    state.setdefault("incidents", []).append({"t": iso(now), "kind": kind, "detail": detail})
    state["incidents"] = state["incidents"][-80:]


def _close_position(state: dict, pos: dict, exit_px: float, now: datetime, reason: str, slip: float) -> None:
    exit_eur = pos["qty"] * exit_px / pos["fx"]
    fee = exit_eur * FEE
    slip_eur = exit_eur * slip
    pnl = exit_eur - pos["cost_eur"] - fee - slip_eur + pos.get("realized_eur", 0)
    state["cash_eur"] = round(state["cash_eur"] + exit_eur - fee - slip_eur, 4)
    risk = pos.get("risk_eur") or 0
    result = None if risk <= 0 else pnl / risk
    fees = round(pos.get("costs_eur", 0) + fee + slip_eur, 4)
    expected = (state.get("research") or {}).get("expected_r", {}).get(pos["strategy"])
    state["closed"].append(
        {
            "trade_id": pos["trade_id"],
            "strategy": pos["strategy"],
            "mode": "paper",
            "market": pos["market"],
            "side": "long",
            "entry_time": pos["entry_time"],
            "entry_price": pos["entry"],
            "stop": pos["stop"],
            "target": pos.get("target"),
            "exit_time": iso(now),
            "exit_price": round(exit_px, 8),
            "fees": fees,
            "funding": pos.get("funding") if pos.get("funding") is not None else "",
            "slippage": round(slip, 5),
            "pnl": round(pnl, 4),
            "r_multiple": None if result is None else round(result, 4),
            "reason_in": pos.get("reason_in") or "",
            "reason_out": reason,
            "regime": pos.get("regime", ""),
            "backtest_expected_r": "" if expected is None else expected,
            "notes": pos.get("notes", "PAPER"),
        }
    )
    state["closed"] = state["closed"][-300:]


def _mark(pos: dict, price: float) -> float:
    if pos["fx"] <= 0:
        return 0.0
    return pos["qty"] * price / pos["fx"]


def _equity(state: dict, prices: dict[str, float]) -> float:
    marked = state["cash_eur"]
    for pos in state["positions"]:
        price = prices.get(pos["market"])
        if price:
            marked += _mark(pos, price)
        else:
            marked += pos["cost_eur"]
    return marked


def _manage(state: dict, books: dict[str, list[Bar]], now: datetime, prices: dict[str, float]) -> None:
    still = []
    for pos in state["positions"]:
        bars = books.get(pos["market"]) or []
        if not bars:
            still.append(pos)
            continue
        bar = bars[-1]
        prices[pos["market"]] = bar.c
        risk_px = abs(pos["entry"] - pos["stop_init"])
        if risk_px > 0:
            pos["mfe_r"] = round(max(pos.get("mfe_r", 0), (bar.h - pos["entry"]) / risk_px), 4)
            pos["mae_r"] = round(min(pos.get("mae_r", 0), (bar.l - pos["entry"]) / risk_px), 4)
        exit_px = None
        reason = ""
        if bar.o <= pos["stop"]:
            exit_px, reason = bar.o, "stop"
        elif bar.l <= pos["stop"]:
            exit_px, reason = pos["stop"], "stop"
        else:
            target = pos["entry"] + 2 * risk_px
            just_partial = False
            if not pos.get("partial") and risk_px > 0 and bar.h >= target:
                third = pos["qty"] / 3
                exit_eur = third * target / pos["fx"]
                fee = exit_eur * (FEE + CEX_SLIPPAGE)
                cost_third = pos["cost_eur"] / 3
                state["cash_eur"] = round(state["cash_eur"] + exit_eur - fee, 4)
                pos["qty"] -= third
                pos["cost_eur"] = round(pos["cost_eur"] - cost_third, 4)
                pos["costs_eur"] = round(pos.get("costs_eur", 0) + fee, 4)
                pos["partial"] = True
                just_partial = True
                pos["stop"] = pos["entry"]
                pos["realized_eur"] = round(pos.get("realized_eur", 0) + exit_eur - fee - cost_third, 4)
                pos["notes"] = (pos.get("notes") or "") + " delni izstop na 2R."
            if pos.get("partial") and not just_partial:
                a = atr(bars) or 0
                if a:
                    pos["stop"] = max(pos["stop"], bar.c - 1.5 * a)
                if exit_px is None and bar.l <= pos["stop"]:
                    exit_px, reason = pos["stop"], "sled"
            opened = datetime.fromisoformat(pos["entry_time"].replace("Z", "+00:00"))
            if exit_px is None and (now - opened) >= timedelta(hours=48) and pos.get("mfe_r", 0) < 0.5:
                exit_px, reason = bar.c, "časovni izstop"
        if exit_px is None:
            still.append(pos)
        else:
            _close_position(state, pos, exit_px, now, reason, CEX_SLIPPAGE)
    state["positions"] = still


def _flatten(state: dict, prices: dict[str, float], now: datetime, reason: str) -> None:
    for pos in list(state["positions"]):
        price = prices.get(pos["market"])
        if not price:
            continue
        _close_position(state, pos, price, now, reason, CEX_SLIPPAGE)
        state["positions"] = [p for p in state["positions"] if p is not pos]


def _cut_half(state: dict, prices: dict[str, float], now: datetime, event_id: str) -> None:
    done = set(state.get("event_cuts") or [])
    if event_id in done:
        return
    for pos in list(state["positions"]):
        price = prices.get(pos["market"])
        if not price or pos["qty"] <= 0:
            continue
        half = dict(pos)
        half["qty"] = pos["qty"] / 2
        half["cost_eur"] = pos["cost_eur"] / 2
        half["size_eur"] = pos["size_eur"] / 2
        half["risk_eur"] = pos["risk_eur"] / 2
        _close_position(state, half, price, now, "rez dogodka", CEX_SLIPPAGE)
        pos["qty"] /= 2
        pos["cost_eur"] = round(pos["cost_eur"] / 2, 4)
        pos["size_eur"] = round(pos["size_eur"] / 2, 2)
        pos["risk_eur"] = round(pos["risk_eur"] / 2, 4)
    state.setdefault("event_cuts", []).append(event_id)


def risk_today(state: dict, now: datetime) -> float:
    """Half size for the rest of the UTC day after an event window has closed."""
    if state.get("half_day") == utc_day(now):
        return RISK_PCT / 2
    return RISK_PCT


def _open(state: dict, signal: Signal, now: datetime, fx: float, regime: str, funding: Optional[float], equity: float) -> Optional[str]:
    if any(p["market"] == signal.symbol for p in state["positions"]):
        return "trg je že odprt"
    if not stop_ok(signal.entry, signal.stop):
        return "stop ni veljaven"
    slip = CEX_SLIPPAGE
    if slip > SLIPPAGE_LIMIT:
        alert(state, now, "failed_order", f"{signal.symbol}: zdrs {slip:.2%} čez mejo")
        return "zdrs čez mejo"
    if signal.kind == "meme" and tx_fails(f"{signal.symbol}|{iso(now)[:13]}"):
        alert(state, now, "failed_order", f"{signal.symbol} papirna transakcija pade v 8% koš")
        return "transakcija pade"
    fill = signal.entry * (1 + FEE + slip)
    size = position_eur(equity, fill, signal.stop, risk_today(state, now))
    if size <= 0 or state["cash_eur"] < size:
        return "premalo gotovine"
    qty = (size * fx) / fill
    risk = size * abs(fill - signal.stop) / fill
    if equity > 0 and risk / equity > RISK_PCT + 1e-6:
        return "tveganje bi preseglo 1%"
    target = fill + 2 * (fill - signal.stop)
    state["cash_eur"] = round(state["cash_eur"] - size, 4)
    state["positions"].append(
        {
            "trade_id": f"T{int(now.timestamp())}-{signal.symbol[:6]}",
            "market": signal.symbol,
            "strategy": signal.strategy,
            "side": "long",
            "entry": round(fill, 8),
            "stop": round(signal.stop, 8),
            "stop_init": round(signal.stop, 8),
            "target": round(target, 8),
            "qty": qty,
            "size_eur": size,
            "cost_eur": size,
            "risk_eur": round(risk, 4),
            "fx": fx,
            "entry_time": iso(now),
            "regime": regime,
            "funding": funding if funding is not None else "",
            "costs_eur": round(size * FEE, 4),
            "partial": False,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "reason_in": signal.reason,
            "notes": "PAPER. " + signal.reason,
            "kind": signal.kind,
        }
    )
    return None


def alert(state: dict, now: datetime, kind: str, detail: str) -> None:
    _incident(state, now, kind, detail)
    state.setdefault("alerts", []).append({"t": iso(now), "kind": kind, "detail": detail})
    state["alerts"] = state["alerts"][-40:]
    if kind in ("kill", "missing_stop", "failed_order", "exchange"):
        send_telegram(f"SKLAD PAPER\n{kind}\n{detail}")


def send_telegram(text: str) -> bool:
    """Optional. Sends only when both secrets exist. Never used for anything but alerts and the daily note."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    body = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body),
            timeout=15,
        )
        return True
    except Exception:
        return False


def gap_risk_eur(state: dict, prices: dict[str, float]) -> float:
    total = 0.0
    for pos in state["positions"]:
        price = prices.get(pos["market"]) or pos["entry"]
        if price <= pos["stop"] or pos["fx"] <= 0:
            total += pos.get("risk_eur", 0)
            continue
        total += pos["qty"] * max(0.0, price - pos["stop"]) / pos["fx"]
    return total


def _trim_gap(state: dict, prices: dict[str, float], now: datetime) -> None:
    """Cut size when a gap through the stops would break the daily loss limit."""
    day_start = state.get("day_start_equity") or 0
    if day_start <= 0:
        return
    floor = day_start * (1 - DAILY_LOSS)
    equity = _equity(state, prices)
    if equity - gap_risk_eur(state, prices) >= floor:
        return
    ordered = sorted(state["positions"], key=lambda pos: pos.get("risk_eur", 0), reverse=True)
    for pos in ordered:
        price = prices.get(pos["market"])
        if not price:
            continue
        equity = _equity(state, prices)
        excess = floor - (equity - gap_risk_eur(state, prices))
        if excess <= 0:
            break
        part = dict(pos)
        frac = min(1.0, max(0.25, excess / max(pos.get("risk_eur", excess), 1e-9)))
        frac = min(1.0, frac)
        part["qty"] = pos["qty"] * frac
        part["cost_eur"] = pos["cost_eur"] * frac
        part["size_eur"] = pos["size_eur"] * frac
        part["risk_eur"] = pos["risk_eur"] * frac
        _close_position(state, part, price, now, "rez pred dogodkom", CEX_SLIPPAGE)
        if frac >= 0.999:
            state["positions"] = [p for p in state["positions"] if p is not pos]
        else:
            pos["qty"] *= 1 - frac
            pos["cost_eur"] = round(pos["cost_eur"] * (1 - frac), 4)
            pos["size_eur"] = round(pos["size_eur"] * (1 - frac), 2)
            pos["risk_eur"] = round(pos["risk_eur"] * (1 - frac), 4)


def paper_pnl(state: dict, strategy: str, since: datetime) -> float:
    total = 0.0
    for trade in state.get("closed") or []:
        if trade.get("strategy") != strategy:
            continue
        try:
            exited = datetime.fromisoformat(str(trade.get("exit_time")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if exited >= since:
            total += float(trade.get("pnl") or 0)
    return total


def active_champion(state: dict, now: datetime) -> tuple[Optional[str], str]:
    research = state.get("research") or {}
    passed = [name for name in (research.get("passed") or []) if name in STRATEGIES]
    champ = state.get("champion_id")
    if champ not in passed:
        if not passed:
            return None, "ni strategije, ki bi prestala backtest s polnimi stroški"
        state["champion_id"] = passed[0]
        state["champion_since"] = iso(now)
        state["challenger_id"] = passed[1] if len(passed) > 1 else None
        state["challenger_since"] = iso(now) if len(passed) > 1 else None
        return passed[0], "prvi prvak po backtestu"
    challenger = next((name for name in passed if name != champ), None)
    if challenger and state.get("challenger_id") != challenger:
        state["challenger_id"] = challenger
        state["challenger_since"] = iso(now)
    since_raw = state.get("challenger_since")
    if challenger and since_raw:
        since = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
        if now - since >= timedelta(days=14):
            if paper_pnl(state, challenger, since) > paper_pnl(state, champ, since):
                state["champion_id"] = challenger
                state["champion_since"] = iso(now)
                state["challenger_id"] = champ
                state["challenger_since"] = iso(now)
                return challenger, "izzivalec je na papirju boljši dva tedna"
    return champ, "prvak ostane, dokler izzivalec dva tedna ne zmaga na papirju"


def _daily_report(state: dict, now: datetime, equity: float, daily: float, blocked: str) -> dict:
    lesson = "danes ni zaprtega posla"
    todays = [trade for trade in state.get("closed") or [] if str(trade.get("exit_time", "")).startswith(utc_day(now))]
    if todays:
        worst = min(todays, key=lambda trade: float(trade.get("pnl") or 0))
        lesson = f"najslabši {worst.get('market')} izstop {worst.get('reason_out')} P&L {worst.get('pnl')} EUR"
    local = now.astimezone(LJ)
    report = {
        "date": local.strftime("%Y-%m-%d"),
        "pnl_eur": round(equity - START_EUR, 2),
        "day_pnl": round(daily, 4),
        "trades": len(todays),
        "alerts": [item["detail"] for item in (state.get("alerts") or [])[-5:]],
        "lesson": lesson,
        "tonight": "nočni backtest ob 23:00" if local.hour >= 20 else "večerni cikel še ni na vrsti",
        "blocked": blocked,
    }
    if os.environ.get("FUND_RUNTIME") and local.hour >= 20 and state.get("report_day") != report["date"]:
        state["report_day"] = report["date"]
        send_telegram(
            "SKLAD PAPER dnevno\n"
            f"P&L {report['pnl_eur']} EUR, dan {report['day_pnl']}, posli {report['trades']}\n{lesson}"
        )
    return report


def phase_name(now: datetime) -> str:
    hour = now.astimezone(LJ).hour
    if 7 <= hour < 9:
        return "jutranji brief"
    if 9 <= hour < 20:
        return "srčni utrip"
    if 20 <= hour < 23:
        return "večerni raport"
    return "nočna raziskava"


def kickoff(status: dict) -> list[dict]:
    research = status.get("research") or {}
    files = research.get("files") or {}
    return [
        {"day": 1, "item": "Mandat in mape so napisane. Borzni ključi in Telegram še niso, papir teče brez njiju.", "done": False},
        {"day": 2, "item": "4 leta sveč, preverjanje, sources.md, calendar.md", "done": bool(research.get("history_ok")) and bool(files.get("calendar"))},
        {"day": 3, "item": "20+ idej in prvi hitri mrežni testi", "done": bool(research.get("ideas_ok")) and bool(research.get("fast_ran"))},
        {"day": 4, "item": "Polni backtest, walk-forward, kartice strategij", "done": bool(research.get("walk_forward")) and bool(files.get("cards"))},
        {"day": 5, "item": "Papirni bot in preizkus izklopa", "done": status.get("note") == "PAPER" and bool(research.get("kill_drill"))},
        {"day": 6, "item": "Papirni posel in nočni cikel", "done": bool(status.get("closed")) and bool(research.get("generated"))},
        {"day": 7, "item": "Prvi tedenski pregled", "done": bool(research.get("weekly_review"))},
    ]


def build_status(state: dict, now: datetime, extra: dict) -> dict:
    prices = extra["prices"]
    equity = _equity(state, prices)
    curve = [point["equity"] for point in state["equity_history"]] + [equity]
    day_start = state["day_start_equity"] or equity
    week_start = state["week_start_equity"] or equity
    daily = equity / day_start - 1 if day_start else 0
    weekly = equity / week_start - 1 if week_start else 0
    peak = max(float(state.get("peak_equity") or equity), equity)
    from_peak = equity / peak - 1 if peak else 0
    name = state.get("champion_id")
    why = extra.get("why") or ""
    grouped: dict[str, list[float]] = {}
    for trade in state["closed"]:
        if trade.get("r_multiple") not in (None, ""):
            grouped.setdefault(trade["strategy"], []).append(float(trade["r_multiple"]))
    arena = []
    for strategy in STRATEGIES:
        vals = grouped.get(strategy, [])
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v <= 0]
        pf = None
        if losses and wins:
            pf = sum(wins) / abs(sum(losses))
        arena.append(
            {
                "id": strategy,
                "role": "prvak" if strategy == name else "izzivalec",
                "n": len(vals),
                "expectancy_r": None if not vals else round(sum(vals) / len(vals), 4),
                "profit_factor": None if pf is None else round(pf, 3),
            }
        )
    open_risk = sum(p.get("risk_eur", 0) for p in state["positions"])
    status = {
        "note": "PAPER",
        "banner": "PAPER · VIRTUALNI DENAR",
        "capital_eur": START_EUR,
        "equity_eur": round(equity, 2),
        "cash_eur": round(state["cash_eur"], 2),
        "pnl_eur": round(equity - START_EUR, 2),
        "pnl_pct": round(equity / START_EUR - 1, 4) if START_EUR else 0,
        "max_drawdown": round(max_drawdown(curve), 4),
        "daily_pnl": round(daily, 4),
        "weekly_pnl": round(weekly, 4),
        "drawdown_from_peak": round(from_peak, 4),
        "peak_equity": round(peak, 2),
        "risk_pct": RISK_PCT,
        "daily_brake": daily <= -DAILY_LOSS,
        "kill": bool(state.get("kill")) or from_peak <= -KILL_DD,
        "half_size": state.get("half_day") == utc_day(now),
        "open_risk_eur": round(open_risk, 2),
        "positions": [
            {
                "market": p["market"],
                "strategy": p["strategy"],
                "entry": p["entry"],
                "stop": p["stop"],
                "size_eur": p["size_eur"],
                "entry_time": p["entry_time"],
                "mfe_r": p.get("mfe_r"),
                "mae_r": p.get("mae_r"),
            }
            for p in state["positions"]
        ],
        "closed": list(reversed(state["closed"][-40:])),
        "arena": arena,
        "champion": {"id": name, "why": why},
        "ideas": extra.get("ideas") or [],
        "events": extra.get("events") or [],
        "event_window": extra.get("event_window") or False,
        "calendar_ok": extra.get("calendar_ok") or False,
        "fresh": extra.get("fresh") or False,
        "phase": phase_name(now),
        "regime": extra.get("regime") or "ni podatka",
        "sources": extra.get("sources") or [],
        "incidents": list(reversed(state["incidents"][-20:])),
        "learning": {
            "version": state["learning"]["version"],
            "changes": state["learning"]["changes"],
            "closed": len(state["closed"]),
            "guard": "Sodba šele po 30 poslih. Ena sprememba na cikel. Tveganje, izklop in vetoji se ne spreminjajo.",
        },
        "research": state.get("research") or {},
        "report": extra.get("report") or {},
        "alerts": list(reversed((state.get("alerts") or [])[-12:])),
        "reconcile": {
            "ok": all(p.get("stop") and p["stop"] <= p["entry"] for p in state["positions"]) and state["cash_eur"] >= -0.01,
            "note": "Papir nima borznega računa. Ujemata se denar in stop vsakega posla.",
        },
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip()),
        "equity_history": state["equity_history"][-240:],
        "agents": extra.get("agents") or [],
        "brief": extra.get("brief") or {},
        "cards": STRATEGY_CARDS,
        "golden": GOLDEN,
        "heartbeat": {
            "t": iso(now),
            "ok": bool(extra.get("fresh")) and all(p.get("stop") and p["stop"] <= p["entry"] for p in state["positions"]),
            "feed_minutes": 2,
        },
        "updated": iso(now),
        "fx_eurusdt": extra.get("fx"),
        "entries_blocked": extra.get("blocked") or "",
        "meme_checked": extra.get("meme_checked") or [],
    }
    status["kickoff"] = kickoff(status)
    return status


def run_once(state: dict, now: datetime, feed: "Feed") -> dict:
    sources = []
    fx, fx_status = feed.eur()
    sources.append({"name": "EURUSDT", "status": fx_status, "rows": 1 if fx else 0, "ts": iso(now)})
    books: dict[str, list[Bar]] = {}
    funding, fund_status = feed.funding()
    sources.append({"name": "hyperliquid", "status": fund_status, "rows": len(funding), "ts": iso(now)})
    fresh_symbols = 0
    for symbol in UNIVERSE:
        bars, code = feed.klines(symbol)
        books[symbol] = bars
        sources.append({"name": symbol, "status": code, "rows": len(bars), "ts": iso(now)})
        if bars_fresh(bars, now):
            fresh_symbols += 1
    memes, meme_sources = feed.memes(state)
    sources.extend(meme_sources)
    events, cal_status = feed.calendar(now)
    sources.append({"name": "calendar", "status": cal_status, "rows": len(events), "ts": iso(now)})
    calendar_ok = cal_status == 200
    prices = {symbol: bars[-1].c for symbol, bars in books.items() if bars}
    for meme in memes:
        if meme.get("price"):
            prices[meme["symbol"]] = meme["price"]

    if state.get("day_key") != utc_day(now):
        state["day_key"] = utc_day(now)
        state["day_start_equity"] = _equity(state, prices)
    if state.get("week_key") != lj_week(now):
        state["week_key"] = lj_week(now)
        state["week_start_equity"] = _equity(state, prices)

    equity = _equity(state, prices)
    peak = max(float(state.get("peak_equity") or equity), equity)
    state["peak_equity"] = round(peak, 4)
    from_peak = equity / peak - 1 if peak else 0
    daily = equity / state["day_start_equity"] - 1 if state["day_start_equity"] else 0
    if from_peak <= -KILL_DD and not state.get("kill"):
        state["kill"] = True
        alert(state, now, "kill", "izklop: −15% od vrha. Stopi ostanejo. Novih vstopov ni.")
    elif from_peak <= -KILL_DD:
        state["kill"] = True
    btc = books.get("BTCUSDT") or []
    regime = "ni podatka"
    baseline = None
    if len(btc) >= 2 and btc[0].c > 0:
        baseline = btc[-1].c / btc[0].c - 1
    if len(btc) >= 60:
        closes = [b.c for b in btc]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        regime = "trend" if e20[-1] > e50[-1] else "razpon"

    _manage(state, books, now, prices)
    window = in_event_window(now, events) if calendar_ok else []
    if window:
        seen = set(state.get("event_cuts") or [])
        _trim_gap(state, prices, now)
        state.setdefault("event_cuts", []).append(window[0]["id"])
        state["half_day"] = utc_day(now)
        if window[0]["id"] not in seen:
            _incident(state, now, "event", f"okno preživetja: {window[0]['title']}")
    elif any(event["at"].date() == now.date() and now > event["at"] + timedelta(minutes=30) for event in events):
        state["half_day"] = utc_day(now)

    ideas = []
    signals: list[Signal] = []
    for symbol, bars in books.items():
        if not bars_fresh(bars, now):
            continue
        fund_px = funding.get(symbol.replace("USDT", ""))
        for builder in (
            lambda s=symbol, b=bars, f=fund_px: trend_signal(s, b, f),
            lambda s=symbol, b=bars: mean_signal(s, b),
            lambda s=symbol, b=bars: breakout_signal(s, b),
            lambda s=symbol, b=bars: relative_signal(s, b, btc),
            lambda s=symbol, b=bars, f=fund_px: funding_signal(s, b, f),
        ):
            signal = builder()
            if signal:
                signals.append(signal)
                ideas.append({"market": signal.symbol, "strategy": signal.strategy, "reason": signal.reason})
    cross = xsection_signal({s: b for s, b in books.items() if bars_fresh(b, now)})
    if cross:
        signals.append(cross)
        ideas.append({"market": cross.symbol, "strategy": cross.strategy, "reason": cross.reason})

    name, why = active_champion(state, now)
    missing_stop = [pos["market"] for pos in state["positions"] if not pos.get("stop") or pos["stop"] > pos["entry"]]
    if missing_stop:
        alert(state, now, "missing_stop", "manjka stop: " + ", ".join(missing_stop))
    exchange_down = fresh_symbols == 0 and any(src["name"] in MAJORS and src["status"] != 200 for src in sources)
    if exchange_down:
        alert(state, now, "exchange", "sveče večjih kovancev niso prišle")
    blocked = ""
    if not fx or fx <= 0:
        blocked = "ni tečaja EUR"
    elif fresh_symbols < len(MAJORS):
        blocked = "sveče niso sveže"
    elif not calendar_ok:
        blocked = "koledar ni dosegljiv"
    elif window:
        blocked = "okno CPI/FOMC"
    elif state.get("kill"):
        blocked = "izklop −15% od vrha"
    elif daily <= -DAILY_LOSS:
        blocked = "dnevna zavora 3%"
    elif missing_stop:
        blocked = "manjka stop"
    elif not name:
        blocked = "ni backtesta s polnimi stroški"
    else:
        chosen = [s for s in signals if s.strategy == name]
        opened = 0
        for signal in chosen:
            if opened:
                break
            equity = _equity(state, prices)
            err = _open(state, signal, now, fx, regime, funding.get(signal.symbol.replace("USDT", "")), equity)
            if err:
                _incident(state, now, "skip", f"{signal.symbol}: {err}")
            else:
                opened += 1

    meme_checked = []
    if not blocked:
        for meme in memes:
            ok, reason = meme_verdict(meme.get("security"))
            meme_checked.append({"market": meme["symbol"], "ok": ok, "reason": reason})
            if not ok:
                _incident(state, now, "veto", f"{meme['symbol']}: {reason}")
                continue
            _incident(state, now, "veto", f"{meme['symbol']}: ni sveč za strukturni stop")

    equity = _equity(state, prices)
    state["equity_history"].append({"t": iso(now), "equity": round(equity, 2)})
    state["equity_history"] = state["equity_history"][-500:]
    fresh = fresh_symbols >= len(MAJORS) and bool(fx)
    weekly = equity / state["week_start_equity"] - 1 if state.get("week_start_equity") else 0
    agents = _agents(equity, ideas, fresh, daily, weekly, why, blocked, name or "")
    report = _daily_report(state, now, equity, daily, blocked)
    brief = {
        "phase": phase_name(now),
        "regime": regime,
        "open_risk_eur": round(sum(p.get("risk_eur", 0) for p in state["positions"]), 2),
        "blocked": blocked,
        "note": "PAPER. Številke so iz tega kroga, ne iz vzorca.",
        "baseline_btc_120h": None if baseline is None else round(baseline, 4),
    }
    public_events = []
    nearest = sorted(events, key=lambda item: (item["at"] < now, abs((item["at"] - now).total_seconds())))
    for event in nearest[:4]:
        public_events.append(
            {
                "title": event["title"],
                "at": iso(event["at"]),
                "active": any(hit["id"] == event["id"] for hit in window),
            }
        )
    return build_status(
        state,
        now,
        {
            "prices": prices,
            "ideas": ideas[:12],
            "events": public_events[:6],
            "event_window": bool(window),
            "calendar_ok": calendar_ok,
            "fresh": fresh,
            "regime": regime,
            "sources": sources,
            "agents": agents,
            "brief": brief,
            "why": why,
            "report": report,
            "fx": fx,
            "blocked": blocked,
            "meme_checked": meme_checked[:8],
        },
    )


def _sl_eur(value: float) -> str:
    whole, cents = f"{value:.2f}".split(".")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return ".".join(reversed(groups)) + "," + cents + " €"


def _sl_pct(value: float) -> str:
    return f"{value * 100:.1f}".replace(".", ",") + " %"


def _agents(equity: float, ideas, fresh: bool, daily: float, weekly: float, why: str, blocked: str, champion: str) -> list[dict]:
    labels = {card["id"]: card["name"] for card in STRATEGY_CARDS}
    own = [idea for idea in ideas if idea["strategy"] == champion]
    if own:
        idea = own[0]["reason"]
    elif ideas:
        idea = f"Izzivalec {labels.get(ideas[0]['strategy'], ideas[0]['strategy'])}: {ideas[0]['reason']}. Prvak nima vstopa."
    else:
        idea = "v tem krogu ni signala, ki bi šel skozi pravila"
    return [
        {"id": "uprava", "name": "Uprava", "tldr": f"Knjiga {_sl_eur(equity)}. {why}."},
        {"id": "raziskava", "name": "Raziskava", "tldr": idea},
        {
            "id": "tveganje",
            "name": "Tveganje",
            "tldr": f"Dan {_sl_pct(daily)}, teden {_sl_pct(weekly)}. " + (blocked or "nove vstope pravila spustijo"),
        },
        {
            "id": "izvrsitev",
            "name": "Izvršitev",
            "tldr": "Samo papir. " + ("vir je svež." if fresh else "vir ni svež, posla ni."),
        },
    ]


class Feed:
    def eur(self) -> tuple[Optional[float], int]:
        raise NotImplementedError

    def klines(self, symbol: str) -> tuple[list[Bar], int]:
        raise NotImplementedError

    def funding(self) -> tuple[dict[str, float], int]:
        raise NotImplementedError

    def memes(self, state: dict) -> tuple[list[dict], list[dict]]:
        raise NotImplementedError

    def calendar(self, now: datetime) -> tuple[list[dict], int]:
        raise NotImplementedError


def http_json(url: str, body: Optional[bytes] = None, headers: Optional[dict] = None) -> tuple[int, Optional[object]]:
    last = 0
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            last = exc.code
            if exc.code not in (429, 500, 502, 503, 504):
                return exc.code, None
        except Exception:
            last = 0
        time.sleep(0.35 * (attempt + 1))
    return last, None


def clean_bars(raw) -> list[Bar]:
    found: dict[int, Bar] = {}
    if not isinstance(raw, list):
        return []
    for row in raw:
        try:
            t = int(row[0])
            found[t] = Bar(t, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
        except (TypeError, ValueError, IndexError):
            continue
    return [found[k] for k in sorted(found)]


class NetworkFeed(Feed):
    def eur(self) -> tuple[Optional[float], int]:
        for base in (VISION, BINANCE):
            code, payload = http_json(f"{base}/api/v3/ticker/price?symbol=EURUSDT")
            if code == 200 and isinstance(payload, dict):
                try:
                    price = float(payload["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if price > 0:
                    return price, 200
        return None, code if "code" in dir() else 0

    def klines(self, symbol: str) -> tuple[list[Bar], int]:
        last = 0
        for base in (VISION, BINANCE):
            code, payload = http_json(f"{base}/api/v3/klines?symbol={symbol}&interval=1h&limit=120")
            last = code
            bars = clean_bars(payload)
            if code == 200 and bars:
                return bars, 200
        return [], last

    def funding(self) -> tuple[dict[str, float], int]:
        code, payload = http_json(
            "https://api.hyperliquid.xyz/info",
            body=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        out: dict[str, float] = {}
        if code == 200 and isinstance(payload, list) and len(payload) >= 2:
            names = [item.get("name") for item in payload[0].get("universe") or []]
            for name, ctx in zip(names, payload[1]):
                try:
                    out[str(name)] = float(ctx.get("funding"))
                except (TypeError, ValueError, AttributeError):
                    continue
        return out, code

    def memes(self, state: dict) -> tuple[list[dict], list[dict]]:
        found = []
        sources = []
        for chain, network in (("sol", "solana"), ("bsc", "bsc")):
            code, payload = http_json(
                f"https://api.geckoterminal.com/api/v2/networks/{network}/trending_pools?page=1&include=base_token"
            )
            rows = (payload or {}).get("data") if isinstance(payload, dict) else None
            sources.append({"name": f"gecko-{network}", "status": code, "rows": len(rows or []), "ts": iso(datetime.now(timezone.utc))})
            included = {(item.get("id")): item for item in (payload or {}).get("included") or []} if isinstance(payload, dict) else {}
            for pool in rows or []:
                attr = pool.get("attributes") or {}
                rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
                mint = str(rel.get("id") or "").split("_")[-1]
                if not mint or mint.lower().endswith("pump"):
                    continue
                created = attr.get("pool_created_at") or ""
                try:
                    opened = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age_h = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
                except ValueError:
                    age_h = 0
                try:
                    vol = float((attr.get("volume_usd") or {}).get("h24") or 0)
                    mc = float(attr.get("market_cap_usd") or attr.get("fdv_usd") or 0)
                    reserve = float(attr.get("reserve_in_usd") or 0)
                    price = float(attr.get("base_token_price_usd") or 0)
                except (TypeError, ValueError):
                    continue
                if age_h < MIN_AGE_H or mc < MIN_MC or vol < MIN_VOL or price <= 0:
                    continue
                token = included.get(rel.get("id")) or {}
                name = ((token.get("attributes") or {}).get("symbol")) or attr.get("name") or mint[:6]
                found.append(
                    {
                        "symbol": f"{chain}:{mint}",
                        "name": str(name).replace("&amp;", "&"),
                        "price": price,
                        "chain": chain,
                        "mint": mint,
                        "reserve": reserve,
                    }
                )
        cursor = int(state.get("meme_cursor") or 0)
        picked = found[cursor : cursor + 4] or found[:4]
        state["meme_cursor"] = cursor + 4
        checked = []
        for meme in picked:
            meme["security"] = _goplus(meme)
            checked.append(meme)
        return checked, sources

    def calendar(self, now: datetime) -> tuple[list[dict], int]:
        code, payload = http_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
        events = []
        if code == 200 and isinstance(payload, list):
            for item in payload:
                title = str(item.get("title") or "")
                if item.get("country") != "USD" or not is_macro_event(title):
                    continue
                try:
                    at = datetime.fromisoformat(str(item.get("date")))
                except ValueError:
                    continue
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                if abs((at - now).total_seconds()) > 7 * 86400:
                    continue
                events.append({"id": f"{title}|{at.isoformat()}", "title": title, "at": at})
        events.sort(key=lambda item: item["at"])
        return events, code


def _flag(info: dict, key: str) -> Optional[str]:
    value = info.get(key)
    if isinstance(value, dict):
        value = value.get("status")
    if value in (None, ""):
        return None
    return str(value).lower()


def _top10(holders) -> Optional[float]:
    if not isinstance(holders, list) or not holders:
        return None
    total = 0.0
    for holder in holders[:10]:
        try:
            total += float(holder.get("percent"))
        except (TypeError, ValueError, AttributeError):
            return None
    return total


def _goplus(meme: dict) -> dict:
    mint = meme["mint"]
    if meme["chain"] == "sol":
        url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={mint}"
    else:
        url = f"https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={mint}"
    code, payload = http_json(url)
    info = {}
    if code == 200 and isinstance(payload, dict):
        result = payload.get("result") or {}
        if isinstance(result, dict) and result:
            info = next(iter(result.values()))
            if not isinstance(info, dict):
                info = {}
    honeypot = _flag(info, "is_honeypot")
    if honeypot is None and meme["chain"] == "sol":
        honeypot = "0" if info else None
        if info and _flag(info, "non_transferable") in ("1", "true"):
            honeypot = "1"
    return {
        "honeypot": honeypot,
        "mintable": _flag(info, "mintable") if "mintable" in info else _flag(info, "is_mintable"),
        "freezable": _flag(info, "freezable") if "freezable" in info else _flag(info, "transfer_pausable"),
        "top10": _top10(info.get("holders")),
        "bundle": info.get("bundle_pct", None),
        "reserve_usd": meme.get("reserve") or None,
        "source": code,
    }


def load_state(path: Path, now: datetime) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return new_state(now)


def save_outputs(state: dict, status: dict, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps(state, indent=2))
    (folder / "status.json").write_text(json.dumps(status, indent=2))
    incidents = folder / "incidents.jsonl"
    incidents.write_text("".join(json.dumps(item) + "\n" for item in state.get("incidents") or []))
    with (folder / "trades.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(state.get("closed") or [])


def load_research(state: dict) -> None:
    path = ROOT / "backtests" / "latest.json"
    if not path.exists():
        return
    try:
        blob = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    state["research"] = {
        "passed": blob.get("passed") or [],
        "expected_r": blob.get("expected_r") or {},
        "generated": blob.get("generated"),
        "history_ok": blob.get("history_ok"),
        "ideas_ok": blob.get("ideas_ok"),
        "fast_ran": blob.get("fast_ran"),
        "walk_forward": blob.get("walk_forward"),
        "kill_drill": blob.get("kill_drill"),
        "weekly_review": blob.get("weekly_review"),
        "files": blob.get("files") or {},
        "summary": blob.get("summary") or [],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=0)
    parser.add_argument("--sleep", type=float, default=120)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    os.environ["FUND_RUNTIME"] = "1"
    folder = HERE
    deadline = time.time() + max(args.minutes, 0) * 60
    while True:
        now = datetime.now(timezone.utc)
        state = load_state(folder / "state.json", now)
        load_research(state)
        try:
            status = run_once(state, now, NetworkFeed())
        except Exception as exc:
            _incident(state, now, "engine", str(exc)[:240])
            status = build_status(
                state,
                now,
                {"prices": {}, "blocked": "krog je padel", "fresh": False, "calendar_ok": False},
            )
        save_outputs(state, status, folder)
        print(f"{status['updated']} equity={status['equity_eur']} blocked={status['entries_blocked']!r} positions={len(status['positions'])}")
        if args.once or args.minutes <= 0 or time.time() >= deadline:
            break
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
