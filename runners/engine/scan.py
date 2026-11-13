#!/usr/bin/env python3
"""Paper runner scanner. No wallet, no orders. Skips trenches and new pairs.

Universe: established coins, about $1M+ market cap and multi-million 24h volume,
that have already run. Public DexScreener and GeckoTerminal only, unless the
repo secret GMGN_API_KEY is present.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from chart import read_chart

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
STATUS_PATH = ROOT / "status.json"
DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
START = 1000.0
WEEK_CAP = 5
SIZE_PCT = 0.10
MIN_MC = 1_000_000.0
MIN_VOL = 2_000_000.0
MIN_AGE_H = 48.0
UA = "RunnerDeskPaper/1.0 (paper-only; no-wallet)"


def iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def week_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def can_enter(state: dict, mint: str) -> bool:
    if any(p.get("mint") == mint for p in state.get("positions") or []):
        return False
    if state.get("week_id") != week_id():
        return True
    return int(state.get("week_entries") or 0) < WEEK_CAP


def fresh_state() -> dict:
    return {
        "balance": START,
        "starting_balance": START,
        "positions": [],
        "closed": [],
        "watchlist": [],
        "week_id": week_id(),
        "week_entries": 0,
        "events": [],
        "paper": True,
    }


def load_state() -> dict:
    if not STATE_PATH.exists():
        return fresh_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fresh_state()
    data.setdefault("positions", [])
    data.setdefault("closed", [])
    data.setdefault("watchlist", [])
    data.setdefault("events", [])
    data["paper"] = True
    if data.get("week_id") != week_id():
        data["week_id"] = week_id()
        data["week_entries"] = 0
    return data


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _get(url: str, params=None, headers=None, timeout=12) -> tuple[Optional[object], Optional[int]]:
    try:
        res = requests.get(url, params=params, headers=headers or {"User-Agent": UA}, timeout=timeout)
    except requests.RequestException:
        return None, None
    if res.status_code == 429:
        return None, 429
    if res.status_code >= 400:
        return None, res.status_code
    try:
        return res.json(), res.status_code
    except ValueError:
        return None, res.status_code


def _f(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class Gmgn:
    def __init__(self) -> None:
        self.key = os.environ.get("GMGN_API_KEY") or ""
        self.note = "ni ključa GMGN_API_KEY — polja ostanejo n/a" if not self.key else "ključ je nastavljen"
        self.limited = False

    def token(self, chain: str, mint: str) -> dict:
        if not self.key or self.limited or not mint:
            return {}
        slug = "sol" if chain == "solana" else "bsc"
        headers = {"User-Agent": UA, "Authorization": f"Bearer {self.key}", "X-API-KEY": self.key}
        data, code = _get(f"https://gmgn.ai/defi/quotation/v1/tokens/{slug}/{mint}", headers=headers)
        if code == 429:
            time.sleep(2.5)
            data, code = _get(f"https://gmgn.ai/defi/quotation/v1/tokens/{slug}/{mint}", headers=headers)
            if code == 429:
                self.limited = True
                self.note = "rate limit"
                return {}
        if not isinstance(data, dict):
            return {}
        return data.get("data") or data.get("token") or data


def _pick(blob: dict, *keys) -> Optional[float]:
    if not isinstance(blob, dict):
        return None
    for key in keys:
        if key in blob and blob[key] not in (None, ""):
            num = _f(blob[key])
            if num is not None:
                return num
    return None


def _text(blob: dict, *keys) -> Optional[str]:
    if not isinstance(blob, dict):
        return None
    for key in keys:
        if blob.get(key) not in (None, ""):
            return str(blob[key])
    return None


def risk_row(pair: dict, gmgn_blob: dict) -> dict:
    liq = _f(((pair.get("liquidity") or {}).get("usd")))
    vol = _f(((pair.get("volume") or {}).get("h24")))
    mc = _f(pair.get("marketCap")) or _f(pair.get("fdv"))
    price = _f(pair.get("priceUsd"))
    rug = (liq is not None and liq < 1000) or (mc is not None and mc < 5000 and (liq or 0) < 100)
    top10 = _pick(gmgn_blob, "top_10_holder_rate", "top10_holder_rate", "top_10_holder_percent")
    insiders = _pick(gmgn_blob, "insider_rate", "insider_percent", "rat_rate")
    dev_holds = _pick(gmgn_blob, "creator_hold_rate", "dev_hold_rate", "creator_balance_rate")
    bundlers = _pick(gmgn_blob, "bundler_rate", "bundler_percent", "bundle_rate")
    entrap = _pick(gmgn_blob, "entrapment_rate", "entrapment_wallet_rate", "phishing_rate")
    # Rates from GMGN are often 0-1. Show percents only when the key existed.
    def pct(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        return round(value * 100, 2) if value <= 1 else round(value, 2)

    return {
        "reused_dev": _text(gmgn_blob, "creator_token_status", "dev_status", "creator_flag"),
        "x_created": _text(gmgn_blob, "twitter_created_at", "x_created_at", "twitter_create_time"),
        "dev_coins_before": _text(gmgn_blob, "creator_created_count", "dev_created_count", "creator_token_count"),
        "dev_best": _text(gmgn_blob, "creator_best_token", "dev_best_token", "creator_ath_token"),
        "dev_best_mcap": _pick(gmgn_blob, "creator_ath_mcap", "dev_best_ath_mcap"),
        "creator_fee": _text(gmgn_blob, "creator_fee", "fee_rate"),
        "holder_rewards": _text(gmgn_blob, "holder_rewards", "reward_rate"),
        "top10_pct": pct(top10),
        "insiders_pct": pct(insiders),
        "dev_holds_pct": pct(dev_holds),
        "bundlers_pct": pct(bundlers),
        "entrapment_pct": pct(entrap),
        "liquidity_usd": liq,
        "volume_24h": vol,
        "social": "n/a",
        "main_risk": "likvidnost ~0, sveča v nič" if rug else ("n/a" if not gmgn_blob else _text(gmgn_blob, "risk_summary", "main_risk") or "n/a"),
        "rug": rug,
        "market_cap": mc,
        "price": price,
    }


def gecko_ohlcv(pool: str, network: str, aggregate: int) -> list[list]:
    data, code = _get(
        f"{GECKO}/networks/{network}/pools/{pool}/ohlcv/hour",
        params={"aggregate": aggregate, "limit": 48, "currency": "usd"},
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    if code == 429:
        time.sleep(2)
        data, _ = _get(
            f"{GECKO}/networks/{network}/pools/{pool}/ohlcv/hour",
            params={"aggregate": aggregate, "limit": 48, "currency": "usd"},
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
    if not isinstance(data, dict):
        return []
    series = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return series if isinstance(series, list) else []


def _age_hours(pair: dict) -> Optional[float]:
    created = pair.get("pairCreatedAt")
    if isinstance(created, str) and "T" in created:
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds()) / 3600.0
    ms = _f(created)
    if ms is None:
        return None
    if ms > 10_000_000_000:
        ms = ms / 1000.0
    return max(0.0, time.time() - ms) / 3600.0


def consider(pair: dict) -> bool:
    """Established runner. New pairs and trenches never qualify."""
    chain = (pair.get("chainId") or "").lower()
    if chain not in {"solana", "bsc"}:
        return False
    dex = (pair.get("dexId") or "").lower()
    if dex in {"pumpfun", "pump.fun"} or "launchlab" in dex:
        return False
    labels = " ".join(str(x).lower() for x in (pair.get("labels") or []))
    if "trench" in labels or "new" in labels:
        return False
    mc = _f(pair.get("marketCap")) or _f(pair.get("fdv"))
    vol = _f(((pair.get("volume") or {}).get("h24")))
    age = _age_hours(pair)
    if mc is None or mc < MIN_MC or vol is None or vol < MIN_VOL:
        return False
    if age is None or age < MIN_AGE_H:
        return False
    return True


def _pair_from_gecko(network: str, pool: dict, included: dict) -> Optional[dict]:
    attrs = pool.get("attributes") or {}
    rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    token = included.get(rel.get("id")) or {}
    token_attrs = token.get("attributes") or {}
    mint = token_attrs.get("address") or str(rel.get("id") or "").split("_", 1)[-1]
    if not mint or not attrs.get("address"):
        return None
    vol = (attrs.get("volume_usd") or {}).get("h24")
    return {
        "chainId": network,
        "dexId": "gecko-trending",
        "pairAddress": attrs.get("address"),
        "baseToken": {
            "address": mint,
            "symbol": token_attrs.get("symbol") or (attrs.get("name") or "?").split(" / ")[0],
            "name": token_attrs.get("name") or attrs.get("name") or "",
        },
        "priceUsd": attrs.get("base_token_price_usd"),
        "liquidity": {"usd": attrs.get("reserve_in_usd")},
        "volume": {"h24": vol},
        "marketCap": attrs.get("market_cap_usd") or attrs.get("fdv_usd"),
        "fdv": attrs.get("fdv_usd"),
        "pairCreatedAt": attrs.get("pool_created_at"),
        "url": f"https://www.geckoterminal.com/{network}/pools/{attrs.get('address')}",
    }


def discover() -> list[dict]:
    """Trending established pools only. New-pool and trench feeds are never called."""
    found: dict[str, dict] = {}
    for network in ("solana", "bsc"):
        data, code = _get(
            f"{GECKO}/networks/{network}/trending_pools",
            params={"page": 1, "include": "base_token"},
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        if code == 429:
            time.sleep(2.5)
            data, _ = _get(
                f"{GECKO}/networks/{network}/trending_pools",
                params={"page": 1, "include": "base_token"},
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
        if not isinstance(data, dict):
            continue
        included = {item.get("id"): item for item in (data.get("included") or [])}
        for pool in data.get("data") or []:
            pair = _pair_from_gecko(network, pool, included)
            if not pair or not consider(pair):
                continue
            key = f"{network}:{pair['baseToken']['address']}"
            prev = found.get(key)
            vol = _f(pair["volume"]["h24"]) or 0
            if prev is None or vol > (_f(prev["volume"]["h24"]) or 0):
                found[key] = pair
            if len(found) >= 6:
                return list(found.values())
        time.sleep(0.4)
    return list(found.values())


def _mark(state: dict, message: str, agent: str) -> None:
    state.setdefault("events", []).append({"ts": iso(), "agent": agent, "message": message})
    state["events"] = state["events"][-80:]


def _equity(state: dict, prices: dict[str, float]) -> float:
    cash = float(state.get("balance") or 0)
    marked = 0.0
    for pos in state.get("positions") or []:
        px = prices.get(pos["mint"]) or _f(pos.get("last")) or 0
        marked += float(pos.get("qty") or 0) * px
    return round(cash + marked, 2)


def manage_positions(state: dict, rows: list[dict]) -> None:
    prices = {row["mint"]: row["price"] for row in rows if row.get("price")}
    still = []
    for pos in state.get("positions") or []:
        px = prices.get(pos["mint"])
        if not px:
            still.append(pos)
            continue
        pos["last"] = px
        entry = float(pos["fill"])
        chart = pos.get("chart") or {}
        zones = (chart.get("zones") or {})
        invalid = zones.get("invalidation")
        stop = float(pos.get("stop") or 0)
        reason = None
        if stop and px <= stop:
            reason = "stop pod cono"
        elif invalid and px < float(invalid):
            reason = "invalidacija, višji minimum se je zlomil"
        elif px >= entry * 3 or (chart.get("ath") and px >= float(chart["ath"]) * 0.98):
            reason = "cilj proti 3x–5x ali nazaj proti ATH"
        if not reason:
            still.append(pos)
            continue
        qty = float(pos["qty"])
        proceeds = qty * px * 0.99
        pnl = proceeds - float(pos["size"])
        state["balance"] = round(float(state["balance"]) + proceeds, 2)
        state.setdefault("closed", []).append({**pos, "exit": px, "pnl": round(pnl, 2), "reason": reason, "closed_at": iso()})
        _mark(state, f"{pos['symbol']} zaprt, {reason}. TLDR: papirni izstop, brez naročila.", "ARCH")
    state["positions"] = still
    state["closed"] = (state.get("closed") or [])[-40:]


def maybe_enter(state: dict, row: dict) -> None:
    chart = row.get("chart_1h") or {}
    if not chart.get("in_buy_zone"):
        if row.get("market_cap") and row["market_cap"] >= MIN_MC:
            watch = state.setdefault("watchlist", [])
            if not any(w.get("mint") == row["mint"] for w in watch):
                watch.append({
                    "mint": row["mint"], "symbol": row["symbol"], "chain": row["chain"],
                    "note": "Shrani in javi, če bo spet tekel", "ts": iso(),
                })
                _mark(state, f"Watchlist {row['symbol']}. Javi, če trendline poči ali višji minimum drži.", "SCOUT")
        return
    if not can_enter(state, row["mint"]):
        _mark(state, f"{row['symbol']} je v coni, toda tedenski limit {WEEK_CAP} je poln. Ni posla.", "ARCH")
        return
    equity = _equity(state, {row["mint"]: row["price"]})
    size = min(equity * SIZE_PCT, float(state["balance"]))
    if size < 10 or not row.get("price"):
        return
    fill = float(row["price"]) * 1.01
    stop = (chart.get("zones") or {}).get("buy_a", {}).get("stop")
    pos = {
        "mint": row["mint"], "symbol": row["symbol"], "chain": row["chain"],
        "size": round(size, 2), "fill": fill, "qty": size / fill, "stop": stop,
        "opened_at": iso(), "last": row["price"], "chart": chart, "lane": "runner",
    }
    state["balance"] = round(float(state["balance"]) - size, 2)
    state["positions"].append(pos)
    state["week_entries"] = int(state.get("week_entries") or 0) + 1
    _mark(state, f"PAPER BUY {row['symbol']} ${size:.0f} (10 %). Stop pod cono. Cilj 3x–5x / ATH.", "ARCH")


def check_watch(state: dict, rows: list[dict]) -> None:
    by_mint = {row["mint"]: row for row in rows}
    for item in state.get("watchlist") or []:
        row = by_mint.get(item.get("mint"))
        if not row:
            continue
        chart = row.get("chart_1h") or {}
        if chart.get("trendline_break") or chart.get("higher_lows"):
            _mark(state, f"Opozorilo {row['symbol']}: preboj ali višji minimum. Še vedno papir, brez naročila.", "VIEW")


def build_candidate(pair: dict, gmgn: Gmgn) -> dict:
    chain = (pair.get("chainId") or "").lower()
    base = pair.get("baseToken") or {}
    mint = base.get("address") or ""
    blob = gmgn.token(chain, mint)
    risk = risk_row(pair, blob if isinstance(blob, dict) else {})
    pool = pair.get("pairAddress") or ""
    c1 = gecko_ohlcv(pool, chain, 1) if pool else []
    time.sleep(0.4)
    c4 = gecko_ohlcv(pool, chain, 4) if pool else []
    chart1 = read_chart(c1, "1h")
    chart4 = read_chart(c4, "4h")
    setup = bool(chart1.get("in_buy_zone") and chart1.get("structure") and not risk["rug"])
    return {
        "symbol": base.get("symbol") or "?",
        "name": base.get("name") or "",
        "mint": mint,
        "chain": chain,
        "pair": pool,
        "dex": pair.get("dexId") or "",
        "price": risk["price"],
        "market_cap": risk["market_cap"],
        "volume_24h": risk["volume_24h"],
        "liquidity_usd": risk["liquidity_usd"],
        "age_h": None if _age_hours(pair) is None else round(_age_hours(pair), 1),
        "url": pair.get("url") or "",
        "risk": risk,
        "chart_1h": {k: v for k, v in chart1.items()},
        "chart_4h": {k: v for k, v in chart4.items() if k != "zones" or True},
        "candles_1h": [[r[0], r[2], r[3], r[4]] for r in sorted(c1, key=lambda x: x[0])[-48:]],
        "setup": setup,
        "tldr": (
            f"{base.get('symbol')} je v nakupni coni na 1h, struktura višjih dni drži. Papirni vstop samo tam."
            if setup else
            f"{base.get('symbol')} je tekač, a ni globokega dipa v coni. Počakaj. Ni napoved."
        ),
    }


def pass_once(state: dict) -> dict:
    gmgn = Gmgn()
    _mark(state, "Scout: preskočim trenches in nove pare. Iščem samo tekače z več milijoni prometa.", "SCOUT")
    pairs = discover()
    rows = []
    for pair in pairs:
        try:
            rows.append(build_candidate(pair, gmgn))
        except Exception as exc:
            _mark(state, f"Scout preskok: {type(exc).__name__}. Podatek manjka, nič ne izmišljujem.", "SCOUT")
        time.sleep(0.3)
    setups = [r for r in rows if r.get("setup")]
    manage_positions(state, rows)
    check_watch(state, rows)
    for row in setups:
        maybe_enter(state, row)
    if not setups:
        _mark(state, "Ni setupov — to je dobro znak. Večina kovancev ni pravi posel.", "ARCH")
    else:
        names = ", ".join(r["symbol"] for r in setups[:4])
        _mark(state, f"TLDR: {len(setups)} papirnih setupov ({names}). Ostalo pusti.", "ARCH")
    _mark(state, "Meta Analyst vpraša Scout: kateri tekač je danes sploh v coni?", "META")
    answer = names if setups else "nihče"
    _mark(state, f"Scout odgovori: {answer}. Brez esej. Ni napoved.", "SCOUT")
    themes = ", ".join(r["symbol"] for r in rows[:4]) or "ni dovolj vzorca"
    meta = (
        f"Jutranji TLDR: vidim {len(rows)} ustaljenih tekačev ({themes}). "
        f"Setupov v coni: {len(setups)}. Trg je tanek. Novih parov ne gledam."
    )
    _mark(state, meta, "META")
    prices = {r["mint"]: r["price"] for r in rows if r.get("price")}
    for pos in state.get("positions") or []:
        if pos["mint"] in prices:
            pos["last"] = prices[pos["mint"]]
    equity = _equity(state, prices)
    status = {
        "note": "PAPER",
        "banner": "PAPER · VIRTUALNI DENAR",
        "paper": True,
        "last_update": iso(),
        "gmgn": gmgn.note,
        "balance": round(float(state.get("balance") or 0), 2),
        "equity": equity,
        "starting_balance": START,
        "week_id": state.get("week_id"),
        "week_entries": int(state.get("week_entries") or 0),
        "week_cap": WEEK_CAP,
        "size_pct": SIZE_PCT,
        "empty_ok": not setups,
        "candidates": rows,
        "positions": state.get("positions") or [],
        "closed": (state.get("closed") or [])[-20:],
        "watchlist": state.get("watchlist") or [],
        "events": (state.get("events") or [])[-40:],
        "meta_tldr": meta,
        "scanned": len(rows),
        "setups": len(setups),
    }
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False) + "\n", encoding="utf-8")
    save_state(state)
    print(f"{status['last_update']} runners={len(rows)} setups={len(setups)} equity={equity} gmgn={gmgn.note}", flush=True)
    return status


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=0)
    p.add_argument("--sleep", type=float, default=90)
    args = p.parse_args()
    state = load_state()
    deadline = time.time() + args.minutes * 60 if args.minutes else None
    pass_once(state)
    if deadline is None:
        return 0
    while time.time() < deadline:
        time.sleep(args.sleep)
        state = load_state()
        pass_once(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
