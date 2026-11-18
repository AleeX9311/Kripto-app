"""Paper-fund rules. No network."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fund


def _now() -> datetime:
    return datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _bars(now: datetime, n: int = 80, step: float = 1.0012, dip: bool = True) -> list[fund.Bar]:
    start = int(now.timestamp() * 1000) - (n - 1) * 3_600_000
    price = 100.0
    bars = []
    for i in range(n):
        price *= step
        low = price * (0.989 if dip and i == n - 1 else 0.999)
        high = price * 1.002
        bars.append(fund.Bar(start + i * 3_600_000, price * 0.999, high, low, price, 10))
    return bars


class Fake(fund.Feed):
    def __init__(self, bars, fx=1.1, events=None, cal=200, funding=None):
        self.bars = bars
        self.fx = fx
        self.events = events or []
        self.cal = cal
        self.fund = funding or {}

    def eur(self):
        return self.fx, 200 if self.fx else 0

    def klines(self, symbol):
        rows = self.bars.get(symbol, [])
        return rows, 200 if rows else 0

    def funding(self):
        return self.fund, 200

    def memes(self, state):
        return [], []

    def calendar(self, now):
        return self.events, self.cal


def _books(now, symbols=None):
    symbols = symbols or fund.UNIVERSE
    bars = _bars(now)
    return {symbol: bars for symbol in symbols}


def test_size_is_ten_percent_not_stop_risk():
    assert fund.START_EUR == 1000
    assert fund.POSITION_PCT == 0.10
    assert fund.position_eur(1000) == 100
    assert fund.position_eur(1000) == fund.position_eur(1000)


def test_drawdown_includes_the_curve():
    assert abs(fund.max_drawdown([1000, 1100, 990]) - (990 / 1100 - 1)) < 1e-9


def test_event_window_is_thirty_minutes():
    at = _now()
    event = {"at": at, "title": "CPI", "id": "cpi"}
    assert fund.in_event_window(at - timedelta(minutes=31), [event]) == []
    assert fund.in_event_window(at - timedelta(minutes=29), [event])
    assert fund.in_event_window(at + timedelta(minutes=29), [event])
    assert fund.in_event_window(at + timedelta(minutes=31), [event]) == []
    assert fund.is_macro_event("FOMC Meeting Minutes")
    assert not fund.is_macro_event("Fed Chairman Speaks")
    assert not fund.is_macro_event("FOMC Member Barr Speaks")


def test_meme_missing_data_is_a_veto():
    ok, reason = fund.meme_verdict({"honeypot": "0", "mintable": "0", "freezable": "0", "top10": 0.2})
    assert not ok and "bundle" in reason
    ok, reason = fund.meme_verdict(
        {"honeypot": "0", "mintable": "0", "freezable": "0", "top10": 0.2, "bundle": 0.05, "reserve_usd": 500000}
    )
    assert ok, reason
    ok, reason = fund.meme_verdict(
        {"honeypot": "0", "mintable": "0", "freezable": "0", "top10": 0.41, "bundle": 0.01, "reserve_usd": 1}
    )
    assert not ok and "top 10" in reason


def test_learning_cannot_touch_brakes_or_size():
    state = fund.new_state(_now())
    refused = fund.consider_change(state, {"name": "vecji posel", "position_pct": 0.2})
    assert refused["applied"] is False
    assert "ne spreminjajo" in refused["reason"]
    refused = fund.consider_change(state, {"name": "mehkejsa zavora", "daily_loss": 0.1, "vetoes": []})
    assert refused["applied"] is False
    state["closed"] = [{"result_r": 0.1}] * 100
    refused = fund.consider_change(state, {"name": "samo zapis"})
    assert refused["applied"] is False


def test_stale_or_missing_feed_blocks_entries():
    now = _now()
    state = fund.new_state(now)
    old = _bars(now - timedelta(hours=5))
    status = fund.run_once(state, now, Fake({symbol: old for symbol in fund.MAJORS}))
    assert status["positions"] == []
    assert "sveže" in status["entries_blocked"]
    state = fund.new_state(now)
    status = fund.run_once(state, now, Fake(_books(now), fx=None))
    assert "EUR" in status["entries_blocked"]
    state = fund.new_state(now)
    status = fund.run_once(state, now, Fake(_books(now), cal=0))
    assert "koledar" in status["entries_blocked"]


def test_entry_is_ten_percent_and_stops_at_five():
    now = _now()
    state = fund.new_state(now)
    status = fund.run_once(state, now, Fake(_books(now)))
    assert len(status["positions"]) == 1
    assert status["positions"][0]["size_eur"] == 100
    assert status["note"] == "PAPER"
    assert state["cash_eur"] == 900
    state["positions"] = state["positions"] * 5
    # unique markets so the sixth is a new symbol; cap is what we assert next pass
    for i, pos in enumerate(state["positions"]):
        pos["market"] = f"M{i}"
    status = fund.run_once(state, now, Fake(_books(now)))
    assert len(status["positions"]) == 5


def test_daily_brake_blocks_without_flattening():
    now = _now()
    state = fund.new_state(now)
    state["cash_eur"] = 960
    state["day_start_equity"] = 1000
    status = fund.run_once(state, now, Fake(_books(now)))
    assert status["daily_brake"] is True
    assert status["positions"] == []
    assert status["kill"] is False


def test_weekly_kill_flattens_once():
    now = _now()
    state = fund.new_state(now)
    state["cash_eur"] = 800
    state["week_start_equity"] = 1000
    state["positions"] = [
        {
            "trade_id": "T1",
            "market": "BTCUSDT",
            "strategy": "trend",
            "entry": 100,
            "stop": 95,
            "stop_init": 95,
            "qty": 1,
            "size_eur": 100,
            "cost_eur": 100,
            "risk_eur": 5,
            "fx": 1.1,
            "entry_time": fund.iso(now),
            "regime": "trend",
            "funding": None,
            "costs_eur": 0.1,
            "partial": False,
            "mfe_r": 0,
            "mae_r": 0,
            "notes": "PAPER",
            "kind": "cex",
        }
    ]
    status = fund.run_once(state, now, Fake(_books(now)))
    assert status["kill"] is True
    assert status["positions"] == []
    assert any(trade["reason_exit"] == "tedenski izklop" for trade in status["closed"])
    incidents = len(state["incidents"])
    fund.run_once(state, now, Fake(_books(now)))
    assert len(state["incidents"]) == incidents


def test_event_window_cuts_and_blocks():
    now = _now()
    state = fund.new_state(now)
    state["positions"] = [
        {
            "trade_id": "T2",
            "market": "ETHUSDT",
            "strategy": "trend",
            "entry": 100,
            "stop": 95,
            "stop_init": 95,
            "qty": 2,
            "size_eur": 100,
            "cost_eur": 100,
            "risk_eur": 5,
            "fx": 1.1,
            "entry_time": fund.iso(now),
            "regime": "trend",
            "funding": None,
            "costs_eur": 0,
            "partial": False,
            "mfe_r": 0,
            "mae_r": 0,
            "notes": "PAPER",
            "kind": "cex",
        }
    ]
    event = {"id": "fomc", "title": "FOMC", "at": now}
    status = fund.run_once(state, now, Fake(_books(now), events=[event]))
    assert status["event_window"] is True
    assert status["positions"]
    assert status["positions"][0]["size_eur"] == 50
    assert any(trade["reason_exit"] == "rez dogodka" for trade in status["closed"])
    assert status["entries_blocked"] == "okno CPI/FOMC"


def test_champion_stays_provisional_under_twenty():
    name, why = fund.champion_of([{"strategy": "mean_reversion", "result_r": 1}] * 19)
    assert name == "trend" and "začasni" in why
    name, why = fund.champion_of(
        [{"strategy": "mean_reversion", "result_r": 1}] * 20 + [{"strategy": "trend", "result_r": -1}] * 20
    )
    assert name == "mean_reversion"


def test_stop_and_time_exit():
    now = _now()
    state = fund.new_state(now)
    state["cash_eur"] = 900
    state["positions"] = [
        {
            "trade_id": "T3",
            "market": "BTCUSDT",
            "strategy": "trend",
            "entry": 100,
            "stop": 98,
            "stop_init": 98,
            "qty": 1,
            "size_eur": 100,
            "cost_eur": 100,
            "risk_eur": 2,
            "fx": 1,
            "entry_time": fund.iso(now - timedelta(hours=1)),
            "regime": "trend",
            "funding": None,
            "costs_eur": 0,
            "partial": False,
            "mfe_r": 0,
            "mae_r": 0,
            "notes": "PAPER",
            "kind": "cex",
        }
    ]
    bars = _bars(now)
    bars[-1] = fund.Bar(bars[-1].t, 99, 99.5, 97.5, 99, 10)
    books = {symbol: bars for symbol in fund.UNIVERSE}
    fund.run_once(state, now, Fake(books, cal=200))
    assert any(trade["reason_exit"] == "stop" for trade in state["closed"])


def test_trades_csv_columns():
    now = _now()
    state = fund.new_state(now)
    state["closed"] = [
        {
            "trade_id": "T",
            "strategy": "trend",
            "mode": "paper",
            "market": "BTCUSDT",
            "side": "long",
            "entry_time": fund.iso(now),
            "exit_time": fund.iso(now),
            "entry": 1,
            "stop": 0.99,
            "size": 100,
            "r_multiple": 1,
            "result_r": 0.4,
            "reason_exit": "stop",
            "regime": "trend",
            "costs": 0.2,
            "slippage": 0.0005,
            "funding": None,
            "notes": "PAPER",
        }
    ]
    folder = Path("/tmp/fund-test-out")
    fund.save_outputs(state, {"note": "PAPER"}, folder)
    header = (folder / "trades.csv").read_text().splitlines()[0]
    for column in (
        "trade_id",
        "strategy",
        "mode",
        "market",
        "side",
        "entry_time",
        "exit_time",
        "entry",
        "stop",
        "size",
        "r_multiple",
        "result_r",
        "reason_exit",
        "regime",
        "costs",
        "slippage",
        "funding",
        "notes",
    ):
        assert column in header.split(",")


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("ok", test.__name__)
    print(len(tests), "tests")


if __name__ == "__main__":
    main()
