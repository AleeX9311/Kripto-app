"""Compact status.json for the paper desk. No wallets, no keys."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import config
from learning import public_rows
from models import to_plain


def _stats(closed: list[dict], equity_history: list) -> dict[str, Any]:
    pnls = [float(t.get("pnl_usd") or 0) for t in closed]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # 1R is the configured hard-stop loss on the original size.
    rs = []
    for trade in closed:
        size = float(trade.get("original_size_usd") or trade.get("size_usd") or 0)
        risk = abs(size * config.HARD_STOP_PCT)
        if risk > 0:
            rs.append(float(trade.get("pnl_usd") or 0) / risk)
    peak = None
    max_dd = 0.0
    for row in equity_history:
        eq = float(row[1]) if isinstance(row, (list, tuple)) and len(row) > 1 else None
        if eq is None:
            continue
        peak = eq if peak is None else max(peak, eq)
        if peak:
            max_dd = max(max_dd, (peak - eq) / peak)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "expectancy": round(sum(pnls) / n, 4) if n else None,
        "max_drawdown": round(max_dd, 4) if equity_history else 0.0,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
    }


def build_status(state: dict, events: list, rejections: list, positions: list, closed: list, market: dict) -> dict:
    equity = float(state.get("last_equity") or state.get("balance") or 0)
    start = float(state.get("starting_balance") or config.STARTING_BALANCE_USD)
    pnl_pct = (equity / start - 1.0) if start else 0.0
    plain = [to_plain(t) for t in closed]
    hist = list(state.get("equity_history") or [])
    return {
        "note": "PAPER",
        "banner": "PAPER · VIRTUALNI DENAR",
        "paper": True,
        "last_update": state.get("last_update"),
        "started": state.get("started_at"),
        "data_source": state.get("data_source") or "LIVE",
        "balance": round(float(state.get("balance") or 0), 2),
        "equity": round(equity, 2),
        "starting_balance": round(start, 2),
        "pnl_pct": round(pnl_pct, 4),
        "open_n": len(positions),
        "closed_n": len(closed),
        "nonstop": True,
        "stats": _stats(plain, hist),
        "stats_glavni": _stats([t for t in plain if (t.get("lane") or "glavni") != "novi_lans"], hist),
        "stats_lans": _stats([t for t in plain if t.get("lane") == "novi_lans"], hist),
        "open": [to_plain(p) for p in positions],
        "closed": [to_plain(t) for t in list(closed)[-40:]],
        "rejections": list(rejections)[-40:],
        "events": [to_plain(e) for e in list(events)[-80:]],
        "equity_history": list(state.get("equity_history") or [])[-240:],
        "market": market,
        "learning": public_rows(state.get("learning") or {}),
        "rules": {
            "top10_max": config.MAX_TOP10_HOLDER_PCT,
            "launch_top10_max": config.MAX_TOP10_HOLDER_PCT,
            "launch_size": config.LAUNCH_MAX_POSITION_PCT,
            "launch_stop": config.LAUNCH_HARD_STOP_PCT,
            "hard_stop": config.HARD_STOP_PCT,
            "partials": list(config.PARTIAL_TP),
            "trail_arm": config.TRAIL_ACTIVATE_PCT,
            "min_age_min": config.MIN_TOKEN_AGE_MIN,
            "analyst_floor": config.MIN_ANALYST_SCORE,
        },
    }


def write_status(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
