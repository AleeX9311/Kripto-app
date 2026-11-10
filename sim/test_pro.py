"""Regime, scam, metrics, partial exit, and the daily brake. No network."""

from __future__ import annotations

import trader
from metrics import compute_metrics
from regime import classify_chain, classify_closes, size_multiplier
from report import build_report, render_md, report_due
from scam import lp_locked_fraction, scam_reason, wash_reasons
from datetime import datetime, timezone, timedelta


def _uptrend(n=80, start=100.0):
    return [start * (1.004 ** i) for i in range(n)]


def _downtrend(n=80, start=100.0):
    return [start * (0.996 ** i) for i in range(n)]


def test_regime_labels():
    assert classify_closes(_uptrend()) == "BIK"
    assert classify_closes(_downtrend()) == "MEDVED"
    assert classify_closes([100.0] * 10) == "NEVTRALNO"
    choppy = []
    px = 100.0
    for i in range(80):
        px *= 1.03 if i % 2 == 0 else 0.97
        choppy.append(px)
    # Wild hourly swings are a high-vol regime, not a trend.
    assert classify_closes(choppy) == "VISOKA VOLATILNOST"
    assert classify_chain(_uptrend(), _downtrend()) == "NEVTRALNO"
    assert size_multiplier("MEDVED", "MEME") is None
    assert size_multiplier("MEDVED", "ALT") == 0.5
    assert size_multiplier("VISOKA VOLATILNOST", "MAJOR") == 0.5
    assert size_multiplier("BIK", "MEME") == 1.0


def test_scam_rules():
    assert lp_locked_fraction({"lp_holders": [
        {"address": "0x000000000000000000000000000000000000dead", "percent": "0.9", "is_locked": "1"},
        {"address": "0xabc", "percent": "0.1"},
    ]}) >= 0.8
    assert lp_locked_fraction({"lp_holders": [{"address": "0xabc", "percent": "1"}]}) < 0.8
    assert lp_locked_fraction({}) is None
    pair = {
        "liquidity": {"usd": 10000},
        "volume": {"h24": 400000},
        "txns": {"h1": {"buys": 90, "sells": 2}},
        "pairCreatedAt": 1,
    }
    reasons = wash_reasons(pair, {"buyers": 2, "sellers": 1, "txns": 80}, [{"volume_usd": 5}] * 10)
    assert any("30x" in r for r in reasons)
    assert any("neravnovesje" in r for r in reasons)
    assert any("edinstvenih" in r for r in reasons)
    assert any("enaki" in r for r in reasons)
    veto, text = scam_reason(pair, 0.2)
    assert veto and "prevara" in text and "80%" in text


def test_partial_moves_stop_to_breakeven():
    pos = {
        "symbol": "X", "name": "X", "token": "t", "pair": "p",
        "entry_price": 100.0, "eff_entry": 101.0, "tokens": 2.0,
        "cost_usd": 202.0, "mark_usd": 230.0, "peak_price": 115.0,
        "last_price": 115.0, "opened_ts": trader.now_ts(), "opened_at": "t",
        "url": "", "chain": "bsc", "category": "ALT",
        "cost_rate": 0.01, "stop_frac": -0.10, "stop_price": 90.0,
    }
    st = {"open": [pos], "cash_usd": 50.0, "closed": [], "stats": {}, "cooldowns": {}}
    trader.maybe_exit(st)
    assert pos["partial_done"] is True
    assert abs(pos["tokens"] - 1.0) < 1e-9
    assert pos["stop_price"] > 100
    assert st["closed"][-1]["reason"] == "delno_10"


def test_brake_blocks_and_resets_fields():
    st = {
        "cash_usd": 96.0, "open": [], "closed": [], "stats": {}, "cooldowns": {}, "activity": [],
        "day": {"date": trader._day_key(), "start_equity": 100.0, "brake": False, "brake_events": []},
    }
    assert trader.update_brake(st) is True
    assert st["day"]["brake"] is True
    st["cash_usd"] = 100.0
    st["day"]["date"] = "1999-01-01"
    assert trader.update_brake(st) is False


def test_metrics_and_report():
    closed = [
        {"pnl_usd": 2, "pnl_pct": 10, "held_sec": 100, "chain": "bsc", "category": "MEME"},
        {"pnl_usd": -1, "pnl_pct": -5, "held_sec": 50, "chain": "solana", "category": "ALT"},
    ]
    stats = compute_metrics(closed, [{"t": "2026-11-09T10:00:00", "usd": 100}, {"t": "2026-11-10T10:00:00", "usd": 101}], [], 101)
    assert stats["n"] == 2
    assert stats["win_rate"] == 0.5
    assert stats["sharpe_trade"] is None
    assert "bsc|MEME" in stats["breakdown"]
    now = datetime(2026, 11, 10, 21, 5, tzinfo=timezone(timedelta(hours=1)))
    assert report_due(now, [])
    assert not report_due(now, ["2026-11-10"])
    report = build_report("2026-11-10", {"closed": [{"symbol": "A", "pnl_usd": 1, "closed_at": "2026-11-10T12:00:00", "reason": "x"}], "day": {"brake_events": ["t"], "scam_rejects": 2}}, {"lessons": [{"text": "Lekcija KIT: test"}]}, {"bsc": "BIK"})
    text = render_md(report)
    assert "Poročilo" in text and "Lekcija KIT" in text


if __name__ == "__main__":
    test_regime_labels()
    test_scam_rules()
    test_partial_moves_stop_to_breakeven()
    test_brake_blocks_and_resets_fields()
    test_metrics_and_report()
    print("pro tests ok")
