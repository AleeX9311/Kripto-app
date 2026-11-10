"""Performance stats from the paper book. Missing samples stay empty."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def _downside(values: list[float]) -> float | None:
    neg = [min(v, 0.0) ** 2 for v in values]
    if len(values) < 2:
        return None
    return math.sqrt(sum(neg) / (len(values) - 1))


def _ratio(mean: float, dev: float | None, periods: float | None = None) -> float | None:
    if dev is None or dev <= 1e-12:
        return None
    value = mean / dev
    if periods:
        value *= math.sqrt(periods)
    return value


def _pack(trades: list[dict]) -> dict[str, Any]:
    n = len(trades)
    pnls = [_f(t.get("pnl_usd")) for t in trades]
    pcts = [_f(t.get("pnl_pct")) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_pcts = [p for p in pcts if p > 0]
    loss_pcts = [p for p in pcts if p < 0]
    holds = [_f(t.get("held_sec")) for t in trades if t.get("held_sec") is not None]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    enough = n >= 5
    return {
        "n": n,
        "win_rate": (len(wins) / n) if n else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 and enough else None,
        "expectancy": (sum(pnls) / n) if n else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "avg_hold_sec": (sum(holds) / len(holds)) if holds else None,
        "sharpe_trade": _ratio(sum(pcts) / n, _stdev(pcts)) if enough else None,
        "sortino_trade": _ratio(sum(pcts) / n, _downside(pcts)) if enough else None,
    }


def max_drawdown(equity: list[float]) -> float | None:
    if len(equity) < 2:
        return None
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def daily_returns(history: list[dict]) -> list[float]:
    by_day: dict[str, float] = {}
    for point in history:
        stamp = str(point.get("t") or "")
        day = stamp[:10]
        usd = point.get("usd")
        if len(day) == 10 and usd is not None:
            by_day[day] = _f(usd)
    days = sorted(by_day)
    out = []
    for i in range(1, len(days)):
        prev = by_day[days[i - 1]]
        if prev > 0:
            out.append(by_day[days[i]] / prev - 1.0)
    return out


def compute_metrics(closed: list[dict], equity_history: list[dict], open_positions: list[dict], equity: float) -> dict[str, Any]:
    base = _pack(closed)
    curve = [_f(p.get("usd")) for p in equity_history if p.get("usd") is not None]
    daily = daily_returns(equity_history)
    base["max_drawdown"] = max_drawdown(curve)
    base["sharpe_daily"] = _ratio(sum(daily) / len(daily), _stdev(daily), 365) if len(daily) >= 5 else None
    base["sortino_daily"] = _ratio(sum(daily) / len(daily), _downside(daily), 365) if len(daily) >= 5 else None
    exposed = sum(_f(p.get("cost_usd")) for p in open_positions)
    base["exposure"] = (exposed / equity) if equity > 0 else None
    groups: dict[str, list[dict]] = defaultdict(list)
    for trade in closed:
        chain = trade.get("chain") or "?"
        klass = trade.get("category") or "?"
        groups[f"{chain}|{klass}"].append(trade)
    base["breakdown"] = {
        key: {"n": len(rows), "pnl": round(sum(_f(t.get("pnl_usd")) for t in rows), 4),
              "win_rate": sum(1 for t in rows if _f(t.get("pnl_usd")) > 0) / len(rows)}
        for key, rows in groups.items()
    }
    return base
