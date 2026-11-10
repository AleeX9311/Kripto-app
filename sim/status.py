#!/usr/bin/env python3
"""Build the compact status.json snapshot and print a short summary.

Simulation only — no wallets, no keys, no real orders.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import config
from agents import AGENT_IDS, AGENT_LABELS, ensure_books

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / config.STATE_FILE
STATUS_PATH = ROOT / config.STATUS_FILE


def dex_url(obj: dict) -> str | None:
    url = obj.get("url")
    if isinstance(url, str) and url.startswith("https://dexscreener.com/"):
        return url
    chain = str(obj.get("chain") or "bsc")
    slug = "solana" if chain in ("solana", "SOL") else "bsc"
    pair = obj.get("pair")
    if isinstance(pair, str) and pair:
        return f"https://dexscreener.com/{slug}/{pair}"
    token = obj.get("token")
    if isinstance(token, str) and token:
        return f"https://dexscreener.com/{slug}/{token}"
    return None


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def public_trade(t: dict) -> dict:
    return {
        "symbol": t.get("symbol") or "?",
        "name": t.get("name") or t.get("symbol") or "?",
        "chain": t.get("chain") or "bsc",
        "category": t.get("category") or "",
        "url": dex_url(t),
        "entry_price": _num(t.get("entry_price")),
        "exit_price": _num(t.get("exit_price")),
        "time": t.get("closed_at") or t.get("opened_at"),
        "opened_at": t.get("opened_at"),
        "closed_at": t.get("closed_at"),
        "reason": t.get("reason"),
        "pnl_usd": _num(t.get("pnl_usd")),
        "pnl_pct": _num(t.get("pnl_pct")),
    }


def public_open(p: dict) -> dict:
    cost = float(p.get("cost_usd") or 0)
    mark = float(p.get("mark_usd", cost) or 0)
    pnl_usd = mark - cost
    if p.get("pnl_pct_gross") is not None:
        pnl_pct = float(p["pnl_pct_gross"])
    else:
        entry = float(p.get("entry_price") or 0)
        last = float(p.get("last_price") or entry)
        pnl_pct = (last / entry - 1.0) * 100.0 if entry else 0.0
    return {
        "symbol": p.get("symbol") or "?",
        "name": p.get("name") or p.get("symbol") or "?",
        "chain": p.get("chain") or "bsc",
        "category": p.get("category") or "",
        "url": dex_url(p),
        "image": p.get("image") or "",
        "entry_price": _num(p.get("entry_price")),
        "last_price": _num(p.get("last_price") or p.get("entry_price")),
        "liq_usd": _num(p.get("liq_usd")),
        "time": p.get("opened_at"),
        "opened_at": p.get("opened_at"),
        "pnl_usd": round(pnl_usd, 4),
        "pnl_pct": round(pnl_pct, 4),
        "votes": p.get("votes") if isinstance(p.get("votes"), dict) else None,
        "support_low": _num(p.get("support_low")),
        "support_high": _num(p.get("support_high")),
        "stop_price": _num(p.get("stop_price")),
        "stop_pct": _num(p.get("stop_pct")),
        "buffer": _num(p.get("buffer")),
    }


def extreme(trades: list[dict], pick) -> dict | None:
    if not trades:
        return None
    chosen = pick(trades, key=lambda t: float(t.get("pnl_usd") or 0))
    pub = public_trade(chosen)
    return {
        "symbol": pub["symbol"],
        "name": pub["name"],
        "url": pub["url"],
        "pnl_usd": pub["pnl_usd"],
        "pnl_pct": pub["pnl_pct"],
        "reason": pub["reason"],
        "time": pub["time"],
    }


def build_status(st: dict) -> dict:
    eur_usd = float(st.get("eur_usd") or 1.08)
    if eur_usd <= 0:
        eur_usd = 1.08
    cash = float(st.get("cash_usd") or 0)
    open_pos = list(st.get("open") or [])
    closed = list(st.get("closed") or [])
    mark = sum(float(p.get("mark_usd", p.get("cost_usd", 0)) or 0) for p in open_pos)
    equity_usd = cash + mark
    equity_eur = equity_usd / eur_usd
    start_eur = float(st.get("start_eur") or config.START_EUR)
    # Percent uses the same rounded euros the page shows, versus start_eur.
    equity_eur_r = round(equity_eur, 2)
    pct = (equity_eur_r / start_eur - 1.0) * 100.0 if start_eur else 0.0
    wins = sum(1 for t in closed if float(t.get("pnl_usd") or 0) > 0)
    losses = sum(1 for t in closed if float(t.get("pnl_usd") or 0) <= 0)
    newest_first = list(reversed(closed))
    ensure_books(st)
    agents_out = {}
    for agent_id in AGENT_IDS:
        bucket = dict(st["agents"].get(agent_id) or {})
        bucket["label"] = AGENT_LABELS[agent_id]
        agents_out[agent_id] = bucket
    candidates = list(reversed(st.get("candidates") or []))[:30]
    activity = list(reversed(st.get("activity") or []))[:40]
    return {
        "note": "SIMULACIJA",
        "started": st.get("started_at"),
        "ends": config.ENDS_AT_ISO,
        "last_update": st.get("last_loop_at"),
        "equity_eur": equity_eur_r,
        "equity_usd": round(equity_usd, 2),
        "pct_vs_eur50": round(pct, 4),
        "trades": len(closed),
        "scanned": int(st.get("last_scanned") or 0),
        "scanned_bsc": int(st.get("last_scanned_bsc") or 0),
        "scanned_sol": int(st.get("last_scanned_sol") or 0),
        "wins": wins,
        "losses": losses,
        "best": extreme(closed, max),
        "worst": extreme(closed, min),
        "open": [public_open(p) for p in open_pos],
        "closed": [public_trade(t) for t in newest_first],
        "agents": agents_out,
        "candidates": candidates,
        "activity": activity,
        "equity_history": list(st.get("equity_history") or []),
        "learning": st.get("learning_public") or None,
        "regime": st.get("regime") or {},
        "brake": bool((st.get("day") or {}).get("brake")),
        "metrics": st.get("metrics") or None,
        "latestReport": st.get("latest_report"),
    }


def write_status(st: dict, path: Path | None = None) -> dict:
    payload = build_status(st)
    dest = path or STATUS_PATH
    tmp = dest.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(dest)
    return payload


def print_summary(st: dict) -> None:
    payload = build_status(st)
    print("=== Paper trader (BSC, SIMULACIJA) ===")
    print(f"Started: {payload['started']}  Ends: {payload['ends']}")
    print(f"Last update: {payload['last_update']}")
    print(
        f"Equity: €{payload['equity_eur']:.2f} / ${payload['equity_usd']:.2f}  "
        f"({payload['pct_vs_eur50']:+.2f}% vs €{config.START_EUR:.0f})"
    )
    print(f"Wins: {payload['wins']}  Losses: {payload['losses']}")
    if payload["best"]:
        b = payload["best"]
        print(f"Best:  {b['symbol']} {b['pnl_usd']:+.3f}$ ({b['pnl_pct']:+.2f}%) [{b['reason']}]")
    if payload["worst"] and payload["worst"] != payload["best"]:
        w = payload["worst"]
        print(f"Worst: {w['symbol']} {w['pnl_usd']:+.3f}$ ({w['pnl_pct']:+.2f}%) [{w['reason']}]")
    if payload["open"]:
        print(f"Open positions ({len(payload['open'])}):")
        for p in payload["open"]:
            print(
                f"  - {p['symbol']} pnl=${p['pnl_usd']:+.3f} ({p['pnl_pct']:+.2f}%) {p.get('url') or ''}"
            )
    else:
        print("Open positions: none")
    print(f"Closed trades: {len(payload['closed'])}")


def main() -> int:
    if not STATE_PATH.exists():
        print("No state.json yet — simulation has not started.")
        return 1
    st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    write_status(st)
    print_summary(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
