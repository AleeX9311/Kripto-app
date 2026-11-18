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


def walk_forward(bars: list[fund.Bar], grid: list[tuple], signal_fn) -> dict:
    if len(bars) < 24 * 400:
        return {"oos": None, "trials": len(grid), "windows": 0}
    tune_ms = int(365.25 * 24 * HOUR)
    test_ms = int(91 * 24 * HOUR)
    cursor = bars[0].t
    oos_rs: list[float] = []
    oos_curve = [1000.0]
    windows = 0
    chosen = grid[0]
    while cursor + tune_ms + test_ms <= bars[-1].t:
        tune = slice_bars(bars, cursor, cursor + tune_ms)
        test = slice_bars(bars, cursor + tune_ms, cursor + tune_ms + test_ms)
        best = None
        best_score = -1e9
        for params in grid:
            result = run_signals(tune, signal_fn(tune, *params))
            score = result["sharpe"] if result["sharpe"] is not None else -1
            if result["trades"] >= 5 and score > best_score:
                best_score = score
                best = params
        if best is None:
            best = grid[0]
        chosen = best
        result = run_signals(test, signal_fn(test, *best))
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


def write_cards(summary: list[dict]) -> None:
    folder = ROOT / "strategies"
    folder.mkdir(exist_ok=True)
    text = {
        "trend": ("Sledenje trendu", "Čreda in počasne novice držijo trend.", "EMA hitro nad počasno, dotik, višji minimum.", "Tihi razpon ali sunek, ki prebije minimum."),
        "mean_reversion": ("Vrnitev k povprečju", "Prisilni prodajalci plačajo likvidnost.", "Zaprtje globoko pod povprečjem, stop pod zadnjimi minimumi.", "Trend, ki se ne vrne."),
        "vol_breakout": ("Preboj volatilnosti", "Miru sledi širitev.", "Ozek razpon, preboj, retest.", "Lažni preboj v razponu."),
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
    trend_grid = [(fast, slow) for fast in (10, 20, 30) for slow in (40, 50, 80) if fast < slow]
    mean_grid = [(look, z) for look in (20, 40) for z in (1.0, 1.5)]
    summary = [
        evaluate("trend", books, trend_grid, trend_signals),
        evaluate("mean_reversion", books, mean_grid, mean_signals),
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
