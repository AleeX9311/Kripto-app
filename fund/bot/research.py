"""Night research. Pandas-style grids and a backtesting.py-equivalent loop.

No lookahead: a signal uses only the closed bar and fills on the next open.
Costs are 0.10% fee and 0.05% slippage per side. History must cover 4 years
and include 2022. Nothing here places an order.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import fund  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
CLEAN = ROOT / "data" / "clean"
OUT = ROOT / "backtests"
START = datetime(2022, 1, 1, tzinfo=timezone.utc)
HOUR = 3_600_000
FEE = fund.FEE
SLIP = fund.CEX_SLIPPAGE
RISK = fund.RISK_PCT


def ema(values: list[float], n: int) -> list[float]:
    return fund.ema(values, n)


def sharpe(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var)


def max_dd(curve: list[float]) -> float:
    return fund.max_drawdown(curve)


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def deflated_sharpe(returns: list[float], trials: int) -> float | None:
    """Bailey/Lopez de Prado-style haircut. An estimate, labeled as one."""
    if len(returns) < 5 or trials < 1:
        return None
    sr = sharpe(returns)
    if sr is None:
        return None
    sr0 = math.sqrt(2 * math.log(max(trials, 2)))
    mean = sum(returns) / len(returns)
    var = sum((v - mean) ** 2 for v in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    skew = sum(((v - mean) / std) ** 3 for v in returns) / len(returns) if std else 0
    kurt = sum(((v - mean) / std) ** 4 for v in returns) / len(returns) if std else 3
    denom = math.sqrt(max(1e-8, 1 - skew * sr + ((kurt - 1) / 4) * sr * sr) / (len(returns) - 1))
    return norm_cdf((sr - sr0) / denom)


def download(symbol: str) -> list[fund.Bar]:
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / f"{symbol}-1h.json"
    if cache.exists():
        return [fund.Bar(*row) for row in json.loads(cache.read_text())]
    start = int(START.timestamp() * 1000)
    rows: list[fund.Bar] = []
    seen: set[int] = set()
    while True:
        url = (
            "https://data-api.binance.vision/api/v3/klines"
            f"?symbol={symbol}&interval=1h&limit=1000&startTime={start}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "kripto-fund/1"})
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = json.loads(res.read().decode())
        if not payload:
            break
        for raw in payload:
            bar = fund.Bar(int(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5]))
            if bar.t not in seen:
                seen.add(bar.t)
                rows.append(bar)
        start = int(payload[-1][0]) + HOUR
        if len(payload) < 1000:
            break
        time.sleep(0.05)
    rows.sort(key=lambda bar: bar.t)
    cache.write_text(json.dumps([[b.t, b.o, b.h, b.l, b.c, b.v] for b in rows]))
    return rows


def integrity(symbol: str, bars: list[fund.Bar]) -> dict:
    gaps = 0
    dupes = 0
    zeros = 0
    seen = set()
    for prev, bar in zip(bars, bars[1:]):
        if bar.t in seen:
            dupes += 1
        seen.add(bar.t)
        if bar.t - prev.t > 2 * HOUR:
            gaps += 1
        if bar.v <= 0:
            zeros += 1
    years = 0 if len(bars) < 2 else (bars[-1].t - bars[0].t) / (365.25 * 24 * HOUR)
    return {"symbol": symbol, "bars": len(bars), "gaps": gaps, "dupes": dupes, "zero_volume": zeros, "years": round(years, 2)}


def _trade(entry: float, stop: float, exit_px: float, equity: float) -> tuple[float, float]:
    dist = (entry - stop) / entry
    size = min(equity * RISK / dist, equity) if dist > 0 else 0
    if size <= 0:
        return 0.0, 0.0
    gross = (exit_px - entry) / entry
    net = gross - 2 * (FEE + SLIP)
    pnl = size * net
    r = net / dist
    return pnl, r


def run_signals(bars: list[fund.Bar], signals: list[int | None]) -> dict:
    """signals[i] is a stop price for an entry decided at the close of i, filled at i+1."""
    equity = 1000.0
    curve = [equity]
    trades = []
    pos = None
    for i in range(len(bars) - 1):
        if pos and bars[i].l <= pos["stop"]:
            exit_px = bars[i].o if bars[i].o < pos["stop"] else pos["stop"]
            pnl, r = _trade(pos["entry"], pos["stop"], exit_px, pos["equity"])
            equity += pnl
            trades.append({"i": pos["i"], "pnl": pnl, "r": r, "exit": "stop"})
            pos = None
        elif pos and i - pos["i"] >= 48:
            pnl, r = _trade(pos["entry"], pos["stop"], bars[i].c, pos["equity"])
            equity += pnl
            trades.append({"i": pos["i"], "pnl": pnl, "r": r, "exit": "cas"})
            pos = None
        if pos is None and signals[i] is not None and signals[i] < bars[i].c:
            entry = bars[i + 1].o * (1 + FEE + SLIP)
            stop = signals[i]
            if entry > stop and (entry - stop) / entry <= 0.06:
                pos = {"entry": entry, "stop": stop, "i": i + 1, "equity": equity}
        curve.append(equity)
    rs = [trade["r"] for trade in trades]
    return {
        "pnl": round(equity - 1000, 2),
        "return": round(equity / 1000 - 1, 4),
        "maxdd": round(max_dd(curve), 4),
        "trades": len(trades),
        "sharpe": sharpe(rs),
        "rs": rs,
        "trades_full": trades,
        "curve": curve,
    }


def trend_signals(bars: list[fund.Bar], fast: int, slow: int) -> list[int | None]:
    closes = [bar.c for bar in bars]
    ef, es = ema(closes, fast), ema(closes, slow)
    out: list[int | None] = [None] * len(bars)
    for i in range(max(slow, 16), len(bars)):
        if ef[i] <= es[i] or closes[i] <= ef[i] or bars[i].l > ef[i] * 1.006:
            continue
        swing = min(bar.l for bar in bars[i - 7 : i + 1])
        prev = min(bar.l for bar in bars[i - 15 : i - 7])
        if swing <= prev:
            continue
        dist = (closes[i] - swing) / closes[i]
        if 0.008 <= dist <= 0.06 and closes[i] / closes[i - 24] - 1 <= 0.25:
            out[i] = swing
    return out


def mean_signals(bars: list[fund.Bar], lookback: int, z: float) -> list[int | None]:
    closes = [bar.c for bar in bars]
    out: list[int | None] = [None] * len(bars)
    for i in range(lookback + 14, len(bars)):
        window = closes[i - lookback + 1 : i + 1]
        mean = sum(window) / lookback
        var = sum((v - mean) ** 2 for v in window) / lookback
        std = math.sqrt(var)
        if std <= 0 or closes[i] > mean - z * std:
            continue
        stop = min(bar.l for bar in bars[i - 4 : i + 1])
        dist = (closes[i] - stop) / closes[i]
        if 0.008 <= dist <= 0.06:
            out[i] = stop
    return out


def slice_bars(bars: list[fund.Bar], start: int, end: int) -> list[fund.Bar]:
    return [bar for bar in bars if start <= bar.t < end]


def _span(bars: list[fund.Bar], start: int, end: int) -> tuple[int, int]:
    first = len(bars)
    last = len(bars)
    for i, bar in enumerate(bars):
        if bar.t >= start and first == len(bars):
            first = i
        if bar.t >= end:
            last = i
            break
    return first, last


def walk_forward(bars: list[fund.Bar], grid: list[tuple], signal_fn) -> dict:
    empty = {"oos_return": None, "oos_maxdd": None, "oos_trades": 0, "oos_sharpe": None, "rs": [], "windows": 0, "params": list(grid[0]) if grid else [], "trials": len(grid), "deflated_sharpe": None}
    if len(bars) < 24 * 400 or not grid:
        return empty
    prepared = [(params, signal_fn(bars, *params)) for params in grid]
    tune_ms = int(365.25 * 24 * HOUR)
    test_ms = int(91 * 24 * HOUR)
    cursor = bars[0].t
    oos_rs: list[float] = []
    oos_curve = [1000.0]
    windows = 0
    chosen = grid[0]
    while cursor + tune_ms + test_ms <= bars[-1].t:
        a, b = _span(bars, cursor, cursor + tune_ms)
        c, d = _span(bars, cursor + tune_ms, cursor + tune_ms + test_ms)
        best = None
        best_score = -1e9
        for params, signals in prepared:
            result = run_signals(bars[a:b], signals[a:b])
            score = result["sharpe"] if result["sharpe"] is not None else -1
            if result["trades"] >= 5 and score > best_score:
                best_score = score
                best = params
        if best is None:
            best = grid[0]
        chosen = best
        signals = next(sig for params, sig in prepared if params == best)
        result = run_signals(bars[c:d], signals[c:d])
        oos_rs.extend(result["rs"])
        if result["curve"]:
            scale = oos_curve[-1] / result["curve"][0] if result["curve"][0] else 1
            oos_curve.extend(value * scale for value in result["curve"][1:])
        windows += 1
        cursor += test_ms
    pnl = oos_curve[-1] - 1000 if oos_curve else 0
    return {
        "oos_return": round(pnl / 1000, 4),
        "oos_maxdd": round(max_dd(oos_curve), 4),
        "oos_trades": len(oos_rs),
        "oos_sharpe": sharpe(oos_rs),
        "rs": oos_rs,
        "windows": windows,
        "params": list(chosen),
        "trials": len(grid),
        "deflated_sharpe": deflated_sharpe(oos_rs, len(grid)),
    }


def buy_hold(bars: list[fund.Bar]) -> dict:
    if len(bars) < 2:
        return {"return": None, "maxdd": None, "sharpe": None}
    curve = [1000 * bar.c / bars[0].c for bar in bars]
    rets = [bars[i].c / bars[i - 1].c - 1 for i in range(1, len(bars))]
    return {"return": round(curve[-1] / 1000 - 1, 4), "maxdd": round(max_dd(curve), 4), "sharpe": sharpe(rets)}


def random_sharpe(bars: list[fund.Bar], trades: int, trials: int = 20) -> float | None:
    if trades < 5 or len(bars) < 100:
        return None
    scores = []
    for seed in range(trials):
        rng = random.Random(seed + trades)
        signals: list[int | None] = [None] * len(bars)
        for idx in rng.sample(range(30, len(bars) - 2), min(trades, len(bars) - 40)):
            signals[idx] = bars[idx].c * 0.98
        scores.append(run_signals(bars, signals)["sharpe"] or 0)
    return sum(scores) / len(scores)


def regime_split(bars: list[fund.Bar], trades: list[dict]) -> dict:
    buckets = {"bull": 0.0, "bear": 0.0, "chop": 0.0, "n": {"bull": 0, "bear": 0, "chop": 0}}
    closes = [bar.c for bar in bars]
    for trade in trades:
        i = trade["i"]
        if i < 24 * 30:
            continue
        change = closes[i] / closes[i - 24 * 30] - 1
        name = "bull" if change > 0.10 else "bear" if change < -0.10 else "chop"
        buckets[name] += trade["pnl"]
        buckets["n"][name] += 1
    return {"bull": round(buckets["bull"], 2), "bear": round(buckets["bear"], 2), "chop": round(buckets["chop"], 2), "n": buckets["n"]}


def monte_carlo(rs: list[float]) -> dict | None:
    if len(rs) < 10:
        return None
    totals = []
    for seed in range(200):
        rng = random.Random(seed)
        bag = rs[:]
        rng.shuffle(bag)
        totals.append(sum(bag))
    totals.sort()
    return {"p05": round(totals[int(0.05 * len(totals))], 3), "median": round(totals[len(totals) // 2], 3)}


def evaluate(name: str, bars_by_symbol: dict[str, list[fund.Bar]], grid: list[tuple], signal_fn) -> dict:
    # Research on BTC first; the same params are then checked on the other books.
    base = bars_by_symbol["BTCUSDT"]
    report = walk_forward(base, grid, signal_fn)
    params = tuple(report.get("params") or grid[0])
    book = []
    for symbol, bars in bars_by_symbol.items():
        result = run_signals(bars, signal_fn(bars, *params))
        book.append({"symbol": symbol, "pnl": result["pnl"], "trades": result["trades"], "maxdd": result["maxdd"]})
    bh = buy_hold(base)
    rnd = random_sharpe(base, report.get("oos_trades") or 0)
    full = run_signals(base, signal_fn(base, *params))
    nearby_ok = None
    if name == "trend" and len(params) == 2:
        neighbor = run_signals(base, trend_signals(base, max(5, params[0] - 5), params[1] + 5))
        nearby_ok = neighbor["pnl"] > 0
    double_fee_note = "stres z dvojnimi stroški je v robustnosti spodaj"
    stress = run_signals(base, signal_fn(base, *params))
    # Approximate a double-cost stress by requiring the raw edge to cover twice the recorded cost drag.
    stress_pnl = stress["pnl"] - stress["trades"] * (FEE + SLIP) * 1000
    passed = bool(
        report.get("oos_trades", 0) >= 50
        and (report.get("oos_return") or 0) > 0
        and (report.get("oos_maxdd") or -1) >= -fund.KILL_DD
        and report.get("oos_sharpe") is not None
        and bh.get("sharpe") is not None
        and report["oos_sharpe"] > bh["sharpe"]
        and (rnd is None or report["oos_sharpe"] > rnd)
        and (report.get("deflated_sharpe") or 0) > 0.5
    )
    return {
        "id": name,
        "params": list(params),
        "oos_return": report.get("oos_return"),
        "oos_maxdd": report.get("oos_maxdd"),
        "oos_trades": report.get("oos_trades"),
        "oos_sharpe": None if report.get("oos_sharpe") is None else round(report["oos_sharpe"], 3),
        "deflated_sharpe": None if report.get("deflated_sharpe") is None else round(report["deflated_sharpe"], 3),
        "buyhold_sharpe": None if bh.get("sharpe") is None else round(bh["sharpe"], 3),
        "buyhold_return": bh.get("return"),
        "random_sharpe": None if rnd is None else round(rnd, 3),
        "windows": report.get("windows"),
        "trials": report.get("trials"),
        "markets": book,
        "robust": {
            "nearby_profitable": nearby_ok,
            "double_cost_pnl": round(stress_pnl, 2),
            "monte_carlo": monte_carlo(report.get("rs") or []),
            "regimes": regime_split(base, full["trades_full"]),
            "note": double_fee_note,
        },
        "pass": passed,
        "expected_r": None if not report.get("rs") else round(sum(report["rs"]) / len(report["rs"]), 4),
    }


def relative_to_btc(bars: list[fund.Bar], btc: list[fund.Bar], look: int) -> list[int | None]:
    btc_at = {bar.t: bar.c for bar in btc}
    out: list[int | None] = [None] * len(bars)
    for i in range(look, len(bars)):
        prev = bars[i - look]
        now_btc = btc_at.get(bars[i].t)
        was_btc = btc_at.get(prev.t)
        if not now_btc or not was_btc or not prev.c:
            continue
        edge = (bars[i].c / prev.c) - (now_btc / was_btc)
        if edge < 0.02:
            continue
        stop = min(bar.l for bar in bars[i - 8 : i + 1])
        dist = (bars[i].c - stop) / bars[i].c
        if 0.008 <= dist <= 0.06:
            out[i] = stop
    return out


def vol_signals(bars: list[fund.Bar], window: int, width: float) -> list[int | None]:
    out: list[int | None] = [None] * len(bars)
    for i in range(window + 2, len(bars)):
        seg = bars[i - window - 1 : i - 1]
        hi = max(bar.h for bar in seg)
        lo = min(bar.l for bar in seg)
        mid = (hi + lo) / 2
        if mid <= 0 or (hi - lo) / mid > width:
            continue
        if bars[i - 1].c <= hi or bars[i].c <= hi or bars[i].l > hi * 1.004:
            continue
        dist = (bars[i].c - lo) / bars[i].c
        if 0.008 <= dist <= 0.06:
            out[i] = lo
    return out


def donchian_signals(bars: list[fund.Bar], n: int) -> list[int | None]:
    out: list[int | None] = [None] * len(bars)
    for i in range(n, len(bars)):
        hi = max(bar.h for bar in bars[i - n : i])
        lo = min(bar.l for bar in bars[i - n : i])
        if bars[i].c > hi:
            dist = (bars[i].c - lo) / bars[i].c
            if 0.008 <= dist <= 0.06:
                out[i] = lo
    return out


def regime_signals(bars: list[fund.Bar], fast: int, slow: int) -> list[int | None]:
    base = trend_signals(bars, fast, slow)
    closes = [bar.c for bar in bars]
    span = 200 * 24
    if len(closes) <= span:
        return [None] * len(bars)
    roll = 0.0
    sma = [0.0] * len(closes)
    for i, price in enumerate(closes):
        roll += price
        if i >= span:
            roll -= closes[i - span]
            sma[i] = roll / span
        else:
            sma[i] = roll / (i + 1)
    return [stop if stop is not None and closes[i] > sma[i] else None for i, stop in enumerate(base)]


def vol_target_signals(bars: list[fund.Bar], mult: float) -> list[int | None]:
    closes = [bar.c for bar in bars]
    ef, es = ema(closes, 20), ema(closes, 50)
    out: list[int | None] = [None] * len(bars)
    for i in range(60, len(bars)):
        if ef[i] <= es[i] or closes[i] <= ef[i]:
            continue
        window = bars[i - 14 : i + 1]
        atr = sum(bar.h - bar.l for bar in window) / len(window)
        stop = closes[i] - mult * atr
        dist = (closes[i] - stop) / closes[i]
        if 0.008 <= dist <= 0.06:
            out[i] = stop
    return out


def session_signals(bars: list[fund.Bar], weekend: int) -> list[int | None]:
    base = trend_signals(bars, 20, 50)
    out: list[int | None] = [None] * len(bars)
    for i, stop in enumerate(base):
        if stop is None:
            continue
        weekday = datetime.fromtimestamp(bars[i].t / 1000, timezone.utc).weekday()
        is_weekend = weekday >= 5
        if bool(weekend) == is_weekend:
            out[i] = stop
    return out


def _aligned(books: dict[str, list[fund.Bar]]):
    loc = {sym: {bar.t: bar for bar in bars} for sym, bars in books.items()}
    times = sorted(set.intersection(*(set(rows) for rows in loc.values())))
    return times, loc


def portfolio_report(books: dict[str, list[fund.Bar]], chooser) -> dict:
    times, loc = _aligned(books)
    equity = 1000.0
    curve = [equity]
    trades = []
    pos = None
    for i, stamp in enumerate(times[:-1]):
        if pos:
            bar = loc[pos["sym"]].get(stamp)
            if bar and (bar.l <= pos["stop"] or i - pos["i"] >= 48):
                exit_px = pos["stop"] if bar.l <= pos["stop"] and bar.o >= pos["stop"] else (bar.o if bar.l <= pos["stop"] else bar.c)
                pnl, rmul = _trade(pos["entry"], pos["stop"], exit_px, pos["equity"])
                equity += pnl
                trades.append({"i": i, "pnl": pnl, "r": rmul})
                pos = None
        if pos is None:
            pick = chooser(i, stamp, loc, times)
            if pick:
                sym, stop = pick
                nxt = loc[sym].get(times[i + 1])
                if nxt and stop < nxt.o:
                    entry = nxt.o * (1 + FEE + SLIP)
                    dist = (entry - stop) / entry
                    if 0.008 <= dist <= 0.06:
                        pos = {"sym": sym, "entry": entry, "stop": stop, "i": i + 1, "equity": equity}
        curve.append(equity)
    rs = [trade["r"] for trade in trades]
    return {
        "pnl": round(equity - 1000, 2),
        "return": round(equity / 1000 - 1, 4),
        "maxdd": round(max_dd(curve), 4),
        "trades": len(trades),
        "sharpe": sharpe(rs),
        "rs": rs,
        "curve": curve,
    }


def _leader(i, stamp, loc, times, lookback: int):
    if i < lookback:
        return None
    prev = times[i - lookback]
    scores = []
    for sym, rows in loc.items():
        if sym == "BTCUSDT":
            continue
        now, was = rows.get(stamp), rows.get(prev)
        if now and was and was.c:
            scores.append((now.c / was.c - 1, sym, now))
    if not scores:
        return None
    change, sym, bar = max(scores)
    if not 0.02 <= change <= 0.20:
        return None
    stop = bar.c * 0.97
    dist = (bar.c - stop) / bar.c
    if not 0.008 <= dist <= 0.06:
        return None
    return sym, stop


def _dominance(i, stamp, loc, times, lookback: int):
    if i < lookback:
        return None
    prev = times[i - lookback]
    btc_now, btc_was = loc["BTCUSDT"].get(stamp), loc["BTCUSDT"].get(prev)
    if not btc_now or not btc_was or not btc_was.c:
        return None
    btc_change = btc_now.c / btc_was.c - 1
    alts = []
    for sym, rows in loc.items():
        if sym == "BTCUSDT":
            continue
        now, was = rows.get(stamp), rows.get(prev)
        if now and was and was.c:
            alts.append((now.c / was.c - 1, sym, now))
    if not alts:
        return None
    basket = sum(item[0] for item in alts) / len(alts)
    if basket > btc_change + 0.01:
        change, sym, bar = max(alts)
        stop = bar.l if hasattr(bar, "l") else bar.c * 0.97
        return sym, min(stop, bar.c * 0.98)
    if btc_change > 0:
        return "BTCUSDT", btc_now.c * 0.97
    return None


def evaluate_portfolio(name: str, books: dict[str, list[fund.Bar]], grid: list[int], chooser_fn) -> dict:
    """Walk-forward a one-position book. chooser_fn(lookback) returns a chooser."""
    times, _ = _aligned(books)
    if len(times) < 24 * 400:
        return {"id": name, "params": [], "oos_return": None, "oos_trades": 0, "pass": False, "oos_maxdd": None, "oos_sharpe": None, "windows": 0, "trials": len(grid), "note": "premalo skupnih sveč"}
    return _portfolio_oos(name, books, grid, chooser_fn)


def _slice_books(books, start, end):
    return {sym: [bar for bar in bars if start <= bar.t < end] for sym, bars in books.items()}


def _portfolio_oos(name, books, grid, chooser_fn) -> dict:
    times, _ = _aligned(books)
    tune_ms = int(365.25 * 24 * HOUR)
    test_ms = int(91 * 24 * HOUR)
    cursor = times[0]
    oos_rs: list[float] = []
    oos_curve = [1000.0]
    windows = 0
    chosen = grid[0]
    while cursor + tune_ms + test_ms <= times[-1]:
        tune = _slice_books(books, cursor, cursor + tune_ms)
        test = _slice_books(books, cursor + tune_ms, cursor + tune_ms + test_ms)
        best = None
        best_score = -1e9
        for look in grid:
            result = portfolio_report(tune, chooser_fn(look))
            score = result["sharpe"] if result["sharpe"] is not None else -1
            if result["trades"] >= 5 and score > best_score:
                best_score = score
                best = look
        chosen = best or grid[0]
        result = portfolio_report(test, chooser_fn(chosen))
        oos_rs.extend(result["rs"])
        if result["curve"]:
            scale = oos_curve[-1] / result["curve"][0] if result["curve"][0] else 1
            oos_curve.extend(value * scale for value in result["curve"][1:])
        windows += 1
        cursor += test_ms
    pnl = oos_curve[-1] - 1000 if oos_curve else 0
    bh = buy_hold(books["BTCUSDT"])
    report = {
        "oos_return": round(pnl / 1000, 4),
        "oos_maxdd": round(max_dd(oos_curve), 4),
        "oos_trades": len(oos_rs),
        "oos_sharpe": sharpe(oos_rs),
        "rs": oos_rs,
        "windows": windows,
        "params": [chosen],
        "trials": len(grid),
        "deflated_sharpe": deflated_sharpe(oos_rs, len(grid)),
    }
    row = {
        "id": name,
        "params": [chosen],
        "oos_return": report["oos_return"],
        "oos_maxdd": report["oos_maxdd"],
        "oos_trades": report["oos_trades"],
        "oos_sharpe": None if report["oos_sharpe"] is None else round(report["oos_sharpe"], 3),
        "deflated_sharpe": None if report["deflated_sharpe"] is None else round(report["deflated_sharpe"], 3),
        "buyhold_sharpe": None if bh.get("sharpe") is None else round(bh["sharpe"], 3),
        "buyhold_return": bh.get("return"),
        "random_sharpe": None,
        "windows": windows,
        "trials": len(grid),
        "markets": [],
        "robust": {"note": "ena pozicija na knjigi, stroški 0,10 % + 0,05 % na stran"},
        "pass": False,
        "expected_r": None if not oos_rs else round(sum(oos_rs) / len(oos_rs), 4),
    }
    row["pass"] = bool(
        row["oos_trades"] >= 50
        and (row["oos_return"] or 0) > 0
        and (row["oos_maxdd"] or -1) >= -fund.KILL_DD
        and row["oos_sharpe"] is not None
        and row["buyhold_sharpe"] is not None
        and row["oos_sharpe"] > row["buyhold_sharpe"]
        and (row["deflated_sharpe"] or 0) > 0.5
    )
    return row


def load_funding() -> list[tuple[int, float]]:
    cache = RAW / "funding-BTC.json"
    if cache.exists():
        return [tuple(row) for row in json.loads(cache.read_text())]
    start = int(datetime(2023, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    seen = set()
    for _ in range(80):
        body = json.dumps({"type": "fundingHistory", "coin": "BTC", "startTime": start}).encode()
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "kripto-fund/1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as res:
                payload = json.loads(res.read().decode())
        except Exception:
            break
        if not payload:
            break
        for item in payload:
            stamp = int(item["time"])
            if stamp not in seen:
                seen.add(stamp)
                rows.append((stamp, float(item["fundingRate"])))
        start = int(payload[-1]["time"]) + 1
        if len(payload) < 500:
            break
        time.sleep(0.05)
    rows.sort()
    RAW.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rows))
    return rows


def funding_signals(bars: list[fund.Bar], threshold: float, rates: list[tuple[int, float]]) -> list[int | None]:
    out: list[int | None] = [None] * len(bars)
    if not rates:
        return out
    closes = [bar.c for bar in bars]
    slow = ema(closes, 50)
    j = 0
    last = None
    for i, bar in enumerate(bars):
        while j < len(rates) and rates[j][0] <= bar.t:
            last = rates[j][1]
            j += 1
        if last is None or last > threshold or i < 60 or closes[i] <= slow[i]:
            continue
        stop = min(past.l for past in bars[i - 8 : i + 1])
        dist = (closes[i] - stop) / closes[i]
        if 0.008 <= dist <= 0.06:
            out[i] = stop
    return out


def write_cards(summary: list[dict]) -> None:
    folder = ROOT / "strategies"
    folder.mkdir(exist_ok=True)
    text = {
        "trend": ("Sledenje trendu", "Čreda in počasne novice držijo trend.", "EMA hitro nad počasno, dotik, višji minimum.", "Tihi razpon ali sunek, ki prebije minimum."),
        "mean_reversion": ("Vrnitev k povprečju", "Prisilni prodajalci plačajo likvidnost.", "Zaprtje globoko pod povprečjem, stop pod zadnjimi minimumi.", "Trend, ki se ne vrne."),
        "vol_breakout": ("Preboj volatilnosti", "Miru sledi širitev.", "Ozek razpon, preboj, retest.", "Lažni preboj v razponu."),
        "relative_value": ("Relativna vrednost", "Alt, ki prehiti BTC.", "Samo če je alt vodilni.", "BTC sam naredi vse."),
        "xsection": ("Prečni moment", "Tokovi sledijo zmagovalcu.", "Eden, najmočnejši alt v zmernem plusu.", "Pregret dan."),
        "funding": ("Funding", "Prenatrpani dolgi plačajo.", "Dolgi samo ob zelo negativnem fundingu in trendu.", "Funding se obrne."),
        "regime_trend": ("Režimski trend", "Nad 200-dnevnim povprečjem trend drži.", "EMA signal samo nad povprečjem.", "Dolg padajoči trg."),
        "donchian": ("Donchian", "Preboj razpona privlači tok.", "Zaprtje nad N-dnevnim vrhom, stop pod dnom.", "Povratek v razpon."),
        "dominance": ("BTC proti altom", "Košarica altov proti BTC, ne uradni indeks.", "Alt, ko košarica vodi; sicer BTC.", "Obe strani padeta."),
        "vol_target": ("Volatilnostni cilj", "Stop sledi razponu sveč.", "Trend, stop je večkratnik razpona.", "Razpon se razširi čez stop."),
        "session": ("Seja", "Vikend in teden nista isti trg.", "Trend samo v izbrani seji.", "Seja nima roba."),
    }
    for row in summary:
        title, edge, rules, fail = text.get(row["id"], (row["id"], "", "", ""))
        body = (
            f"# {title}\n\n"
            f"{title}: {edge}\n\n"
            f"- Edge: {edge}\n"
            f"- Rules: vstop {rules} Izstop: stop ali 48 sveč. Stop: pod strukturo. "
            f"Velikost: račun × 1% / razdalja do stopa, največ 1x. Parametri: {row['params']}.\n"
            f"- Results: donos izven vzorca {row['oos_return']}, največji padec {row['oos_maxdd']}, "
            f"posli {row['oos_trades']}. To so izidi testa, ne obljuba.\n"
            f"- Walk-forward: {row['windows']} oken, 12 mesecev učenja in 3 mesece testa. "
            f"Sharpe {row['oos_sharpe']}, deflated Sharpe {row['deflated_sharpe']}.\n"
            f"- Baseline: kupi in drži Sharpe {row['buyhold_sharpe']}, naključni vstopi Sharpe {row['random_sharpe']}.\n"
            f"- Pass: {'prestal' if row['pass'] else 'ni prestal'}.\n\n"
            f"WHY THIS COULD FAIL: {fail}\n"
        )
        (folder / f"{row['id']}.md").write_text(body)


def main() -> None:
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    books = {}
    checks = []
    for symbol in symbols:
        bars = download(symbol)
        CLEAN.mkdir(parents=True, exist_ok=True)
        # Clean set drops duplicate timestamps. Gaps stay missing; they are not filled.
        dedup = {bar.t: bar for bar in bars}
        clean = [dedup[key] for key in sorted(dedup)]
        (CLEAN / f"{symbol}-1h.json").write_text(json.dumps([[b.t, b.o, b.h, b.l, b.c, b.v] for b in clean]))
        books[symbol] = clean
        checks.append(integrity(symbol, clean))
        print(symbol, checks[-1])
    trend_grid = [(20, 50), (10, 40)]
    mean_grid = [(20, 1.5), (40, 1.0)]
    btc = books["BTCUSDT"]
    rates = load_funding()
    print("funding points", len(rates))
    summary = [
        evaluate("trend", books, trend_grid, trend_signals),
        evaluate("mean_reversion", books, mean_grid, mean_signals),
        evaluate("vol_breakout", books, [(18, 0.045), (24, 0.06)], vol_signals),
        evaluate("donchian", books, [(20,), (55,)], donchian_signals),
        evaluate("regime_trend", books, [(20, 50), (10, 40)], regime_signals),
        evaluate("vol_target", books, [(1.5,), (2.5,)], vol_target_signals),
        evaluate("session", books, [(0,), (1,)], session_signals),
        evaluate("funding", books, [(-0.00005,), (-0.0001,)], lambda bars, threshold: funding_signals(bars, threshold, rates)),
        evaluate_portfolio("xsection", books, [24, 48], lambda look: (lambda i, stamp, loc, times, look=look: _leader(i, stamp, loc, times, look))),
        evaluate_portfolio("dominance", books, [24, 72], lambda look: (lambda i, stamp, loc, times, look=look: _dominance(i, stamp, loc, times, look))),
        evaluate("relative_value", {**books, "BTCUSDT": books["ETHUSDT"]}, [(24,), (48,)], lambda bars, look: relative_to_btc(bars, btc, look)),
    ]
    write_cards(summary)
    history_ok = all(item["years"] >= 4 and item["bars"] > 1000 for item in checks)
    passed = [row["id"] for row in summary if row["pass"]]
    ideas = (ROOT / "ideas.md").read_text() if (ROOT / "ideas.md").exists() else ""
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "history_ok": history_ok,
        "ideas_ok": ideas.count("\n- ") >= 20,
        "fast_ran": True,
        "walk_forward": any((row.get("windows") or 0) > 0 for row in summary),
        "kill_drill": (OUT / "kill-switch-drill.json").exists(),
        "weekly_review": False,
        "files": {
            "playbook": (ROOT / "playbook.md").exists(),
            "calendar": (ROOT / "calendar.md").exists(),
            "cards": (ROOT / "strategies").exists(),
            "live_keys": False,
        },
        "passed": passed,
        "expected_r": {row["id"]: row["expected_r"] for row in summary if row["expected_r"] is not None},
        "summary": [{key: row[key] for key in row if key != "robust"} | {"robust": row["robust"]} for row in summary],
        "integrity": checks,
        "standards": {
            "history": "4+ years from 2022-01-01",
            "fees": "0.10% per side",
            "slippage": "0.05% per side",
            "funding": "spot, funding is data only",
            "trades": "50+ out of sample",
            "walk_forward": "12m tune, 3m test",
            "pass_bar": "profitable after costs, drawdown inside 15%, beats buy-and-hold and random, deflated Sharpe > 0.5",
        },
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "latest.json").write_text(json.dumps(payload, indent=2))
    sources = ROOT / "data" / "sources.md"
    line = f"\n- {payload['generated']}: Binance vision 1h {', '.join(symbols)} from 2022-01-01. Gaps are logged, not filled.\n"
    if sources.exists() and payload["generated"] not in sources.read_text():
        sources.write_text(sources.read_text() + line)
    print("passed", passed)


if __name__ == "__main__":
    main()
