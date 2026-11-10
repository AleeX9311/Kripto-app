"""Chart-reading tests. No network. The module never adds a vote."""

from chart_read import ema_series, read_chart, rsi_series


def bar(i, o, h, l, c, v=1000):
    return [float(i), float(o), float(h), float(l), float(c), float(v)]


def test_rsi_and_ema_warmup():
    closes = [float(i) for i in range(1, 40)]
    ema = ema_series(closes, 20)
    assert ema[18] is None and ema[19] is not None
    rsi = rsi_series(closes, 14)
    assert rsi[14] is None or rsi[14] > 50
    assert rsi[-1] > 70


def test_freefall_is_refused():
    rows = []
    px = 100.0
    for i in range(48):
        o = px
        c = px * 0.99
        rows.append(bar(i, o, o * 1.002, c * 0.998, c, 900))
        px = c
    out = read_chart(rows, [], rows[-1][4], support_low=rows[-1][3])
    assert out["action"] == "reject", out
    assert out["kind"] == "prosti_pad"
    assert "pada, ni dna" in out["label"]
    assert out["note"] == "ni napoved"
    assert "vote" not in out


def test_extended_top_is_refused():
    rows = []
    px = 10.0
    for i in range(48):
        o = px
        c = px * 1.02
        rows.append(bar(i, o, c * 1.001, o * 0.999, c, 3000))
        px = c
    out = read_chart(rows, [], rows[-1][4], support_low=rows[0][3])
    assert out["action"] == "reject", out
    assert out["kind"] == "predrago"
    assert "predrago, ne lovim vrha" in out["label"]


def _bottom():
    """Drop 30% off 100, base at 70, higher low at 74, then reclaim and break."""
    path = (
        [100] * 8
        + [96, 92, 88, 84, 80, 76, 73, 70]
        + [71, 69.5, 70.5, 73, 77, 80]
        + [78, 75, 74.2, 76, 79]
        + [74.5, 75, 76, 78, 81, 84, 86]
    )
    rows = []
    prev = path[0]
    for i, c in enumerate(path):
        o = prev
        lo = min(o, c) * 0.995
        hi = max(o, c) * 1.004
        if i in (15, 16):
            lo = 68.8
        if i == 22:
            lo = 73.4
        green = c >= o
        vol = 2400 if green and i > 24 else 700
        rows.append(bar(i, o, hi, lo, c, vol))
        prev = c
    return rows


def test_confirmed_bottom_can_pass():
    rows = _bottom()
    price = rows[-1][4]
    out = read_chart(rows, [], price, support_low=69.0, support_high=71.0)
    assert out["action"] == "buy", out
    assert out["kind"] == "potrjeno_dno"
    assert out["score"] >= 60
    assert out["swing_low"] < 72
    assert out["ema20"]
    assert out["confirm_price"] == price
    text = " ".join(out["reasons"])
    assert "višje dno" in text or "povratek nad EMA20" in text


def test_forming_bottom_waits():
    rows = _bottom()[:24]
    # Pad the tail so the last lows are still the low and EMA is not reclaimed.
    price = rows[-1][4]
    out = read_chart(rows, [], price, support_low=69.0, support_high=71.0)
    assert out["action"] != "buy", out
    assert out["kind"] != "potrjeno_dno"


def test_short_history_waits():
    rows = [bar(i, 1, 1.1, 0.9, 1, 10) for i in range(10)]
    out = read_chart(rows, [], 1.0, None, None)
    assert out["action"] == "watch"
    assert out["label"] == "Čaka na potrditev"


def test_setup_trust_learns_and_only_refuses():
    from learning import fresh_book, on_close, public_summary, setup_refused
    book = fresh_book()
    features = {"setup": "potrjeno_dno", "setup_label": "potrjeno dno", "votes": {}}
    assert setup_refused(book, features) is None
    for _ in range(8):
        on_close(book, features, {"pnl_pct": -4}, "t")
    assert book["setup_trust"]["potrjeno_dno"]["n"] == 8
    assert "potrjeno dno" in setup_refused(book, features)
    assert any(row["kind"] == "potrjeno_dno" for row in public_summary(book)["setup_trust"])
    journal = book["journal"][-1]
    assert journal["setup"] == "potrjeno_dno"
    assert journal["setup_label"] == "potrjeno dno"


def _state():
    return {
        "cash_usd": 100.0,
        "open": [],
        "closed": [],
        "stats": {},
        "cooldowns": {},
        "activity": [],
        "candidates": [],
        "agents": {},
        "regime": {"bsc": "NEVTRALNO", "solana": "NEVTRALNO"},
        "day": {},
        "watchlist": [],
    }


def _pair():
    return {
        "chainId": "bsc",
        "dexId": "pancake",
        "pairAddress": "0xpool",
        "url": "https://dexscreener.com/bsc/0xpool",
        "priceUsd": "79",
        "marketCap": 2_000_000,
        "pairCreatedAt": 1_700_000_000_000,
        "baseToken": {"address": "0xtoken", "symbol": "DNO", "name": "Dno"},
        "liquidity": {"usd": 80_000},
        "volume": {"h24": 50_000},
        "priceChange": {"m5": 1, "h1": 2, "h24": -10},
        "txns": {"m5": {"buys": 20, "sells": 10}, "h1": {"buys": 40, "sells": 20}},
        "info": {},
    }


def _votes():
    return {agent: {"vote": "YES", "reason": "test", "numbers": {}} for agent in ("kit", "trend", "varnost", "bik_medved", "izvajanje")}


def test_watch_does_not_buy_and_confirmed_does():
    import trader
    st = _state()
    pair = _pair()
    trader.next_batch = lambda _st: [{"address": "0xtoken", "chain": "bsc", "symbol": "DNO"}]
    trader.fetch_token_pairs = lambda _addrs, _chain: [pair]
    trader.evaluate_agents = lambda _pair, _ctx: (True, _votes())
    trader.retune_votes = lambda _book, votes, _features: votes
    trader.decide = lambda _book, _votes, _features: (True, "4/5 in VARNOST")
    falling = []
    px = 100.0
    for i in range(48):
        o = px
        c = px * 0.99
        falling.append(bar(i, o, o * 1.002, c * 0.998, c, 900))
        px = c
    trader.fetch_ohlcv = lambda *_args, **_kwargs: falling
    trader.maybe_enter(st)
    assert st["open"] == []
    assert any(ev.get("type") in ("skip", "watch") for ev in st["activity"])
    bottom = _bottom()
    trader.fetch_ohlcv = lambda *_args, **_kwargs: bottom
    trader.plan_stop = lambda *_args, **_kwargs: {
        "ok": True,
        "reason": "podpora",
        "support_low": 69.0,
        "support_high": 71.0,
        "touches": 2,
        "buffer": 1.0,
        "stop_price": 68.0,
        "stop_pct": -10.0,
        "stop_frac": -0.10,
        "reward_risk": 2.0,
    }
    pair["priceUsd"] = str(bottom[-1][4])
    trader.maybe_enter(st)
    assert len(st["open"]) == 1, [ev.get("text") for ev in st["activity"][-4:]]
    pos = st["open"][0]
    assert pos["setup"] == "potrjeno_dno"
    assert pos["setup_score"] >= 60
    assert pos["swing_low"]
    assert "potrjeno dno" in (pos.get("learn") or {}).get("setup_label", "")


if __name__ == "__main__":
    test_rsi_and_ema_warmup()
    test_freefall_is_refused()
    test_extended_top_is_refused()
    test_confirmed_bottom_can_pass()
    test_forming_bottom_waits()
    test_short_history_waits()
    test_setup_trust_learns_and_only_refuses()
    test_watch_does_not_buy_and_confirmed_does()
    print("chart tests ok")
