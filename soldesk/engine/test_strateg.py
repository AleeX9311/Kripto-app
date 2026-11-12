"""Shadow lab tests. No network and no capital."""

import config
from strateg import ensure, live_view, maybe_promote, note_live_result, observe, public, resolve, would_enter


def _feat(**kw):
    base = {
        "mint": "m", "symbol": "T", "chain": "solana", "lane": "glavni",
        "score": 0.72, "liquidity": 100_000, "buyers": 90, "age_min": 80,
        "buy_ratio": 0.6, "pullback": 0.1, "higher_low": True, "vwap_ok": True, "ema_ok": True,
        "hard_kill": False, "price": 1.0,
        "volume_1h": 80_000,
    }
    base.update(kw)
    return base


def test_hard_kill_never_enters():
    feat = _feat(hard_kill=True, score=0.9)
    from strateg import VARIANTS
    assert all(not would_enter(feat, row["params"]) for row in VARIANTS)
    state = {}
    observe(state, feat, 0)
    assert state["strategy"]["pending"][0]["taken"] == []


def test_score_floor_holds():
    from strateg import VARIANTS
    low = _feat(score=0.50)
    assert all(not would_enter(low, row["params"]) for row in VARIANTS)


def test_promote_only_when_sample_and_better_and_not_looser_stop():
    state = {}
    book = ensure(state)
    for _ in range(14):
        book["variants"]["zdaj"]["wins"] += 1
        book["variants"]["zdaj"]["pnl"] += 0.01
    for _ in range(6):
        book["variants"]["zdaj"]["losses"] += 1
        book["variants"]["zdaj"]["pnl"] -= 0.01
    for _ in range(18):
        book["variants"]["prag_070"]["wins"] += 1
        book["variants"]["prag_070"]["pnl"] += 0.04
    book["variants"]["prag_070"]["losses"] += 2
    book["variants"]["prag_070"]["pnl"] -= 0.01
    why = maybe_promote(book)
    assert why and "promocija" in why
    assert book["live"]["min_score"] >= config.MIN_ANALYST_SCORE
    assert "stop" not in book["live"]
    assert "size" not in book["live"]
    assert book["live"]["min_score"] == 0.70


def test_small_sample_does_not_promote():
    state = {}
    book = ensure(state)
    book["variants"]["zdaj"]["wins"] = 5
    book["variants"]["zdaj"]["pnl"] = 0.05
    book["variants"]["prag_070"]["wins"] = 5
    book["variants"]["prag_070"]["pnl"] = 0.2
    assert maybe_promote(book) is None
    assert public(book)["variants"][0]["note"] or True
    assert any(row["note"] == "premalo podatkov" for row in public(book)["variants"])


def test_promote_ignores_looser_stop_and_size():
    state = {}
    book = ensure(state)
    book["variants"]["prag_070"]["params"]["stop"] = -0.20
    book["variants"]["prag_070"]["params"]["size"] = 0.50
    for _ in range(14):
        book["variants"]["zdaj"]["wins"] += 1
        book["variants"]["zdaj"]["pnl"] += 0.01
    for _ in range(6):
        book["variants"]["zdaj"]["losses"] += 1
        book["variants"]["zdaj"]["pnl"] -= 0.01
    for _ in range(18):
        book["variants"]["prag_070"]["wins"] += 1
        book["variants"]["prag_070"]["pnl"] += 0.04
    book["variants"]["prag_070"]["losses"] += 2
    book["variants"]["prag_070"]["pnl"] -= 0.01
    maybe_promote(book)
    assert book["live"]["min_score"] == 0.70
    assert "stop" not in book["live"]
    assert "size" not in book["live"]
    view = live_view(state)
    assert view["min_score"] == 0.70
    assert view["trail"] <= config.TRAIL_PCT
    assert view["time_min"] <= 40


def test_poisoned_overlay_stays_inside_floors():
    state = {
        "strategy": {
            "version": 3,
            "live": {
                "min_score": 0.2, "stop": -0.20, "size": 0.9, "trail": 0.20,
                "time_min": 90, "min_age_min": 5, "min_buyers": 1, "tp": 0.40,
            },
            "variants": {}, "history": [], "pending": [], "lessons": [],
        }
    }
    view = live_view(state)
    assert view["min_score"] >= config.MIN_ANALYST_SCORE
    assert view["min_age_min"] >= config.MIN_TOKEN_AGE_MIN
    assert view["min_buyers"] >= config.LAUNCH_MIN_BUYERS
    assert view["trail"] <= config.TRAIL_PCT
    assert view["time_min"] <= 40
    assert view["tp"] <= 0.15
    assert "stop" not in view and "size" not in view


def test_rollback_after_eight_worse_live_trades():
    book = ensure({})
    book["version"] = 2
    book["live"] = {"min_score": 0.70}
    book["history"] = [
        {"version": 1, "why": "prej", "expectancy": 0.04, "live": {}},
        {"version": 2, "why": "novo", "live": {"min_score": 0.70}},
    ]
    for _ in range(7):
        assert note_live_result(book, -0.03) is None
    why = note_live_result(book, -0.03)
    assert why and why.startswith("povratek")
    assert book["live"] == {}
    assert book["version"] == 3


def test_forward_resolve_does_not_touch_cash():
    state = {"balance": 1000.0}
    observe(state, _feat(), now_ts=0)
    def price_of(_chain, _mint):
        return 1.2
    assert resolve(state, 15 * 60, price_of) == 0
    assert resolve(state, 30 * 60, price_of) == 0
    assert resolve(state, 60 * 60, price_of) == 1
    book = state["strategy"]
    assert book["variants"]["zdaj"]["wins"] >= 1
    assert state["balance"] == 1000.0
    assert any(row["note"] == "premalo podatkov" for row in public(book)["variants"])


if __name__ == "__main__":
    test_hard_kill_never_enters()
    test_score_floor_holds()
    test_promote_only_when_sample_and_better_and_not_looser_stop()
    test_promote_ignores_looser_stop_and_size()
    test_poisoned_overlay_stays_inside_floors()
    test_small_sample_does_not_promote()
    test_rollback_after_eight_worse_live_trades()
    test_forward_resolve_does_not_touch_cash()
    print("strateg tests ok")
