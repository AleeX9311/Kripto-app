"""Learning journal tests. No network."""

from __future__ import annotations

from learning import (
    decide,
    fresh_book,
    on_close,
    resolve_pending,
    retune_votes,
    snapshot,
    stop_loss,
    _maybe_rollback,
)
from agents import AGENT_IDS


def _votes(yes_ids):
    votes = {}
    for agent in AGENT_IDS:
        yes = agent in yes_ids
        votes[agent] = {
            "vote": "YES" if yes else "NO",
            "reason": "test",
            "numbers": {"ch5": 2.0, "ch1h": 8.0, "ratio_5m": 2.0, "slip": 0.01, "goplus": "ok"},
        }
    return votes


def _pair():
    return {
        "priceUsd": "1.0",
        "liquidity": {"usd": 90000},
        "volume": {"h24": 200000},
        "priceChange": {"m5": 2, "h1": 8, "h24": 10},
        "txns": {"m5": {"buys": 30, "sells": 10}},
        "pairCreatedAt": 1,
    }


def _features(yes_ids=("kit", "trend", "varnost", "bik_medved")):
    return snapshot(_pair(), _votes(yes_ids), "MEME", "bsc")


def test_rule_stays_four_of_five_and_varnost():
    book = fresh_book()
    ok, why = decide(book, _votes(("kit", "trend", "bik_medved", "izvajanje")), _features())
    assert not ok and "VARNOST" in why
    ok4, why4 = decide(book, _votes(("kit", "trend", "varnost", "bik_medved")), _features())
    assert ok4, why4
    book["trust"]["kit"] = {"alpha": 1, "beta": 40}
    book["trust"]["trend"] = {"alpha": 1, "beta": 40}
    book["trust"]["varnost"] = {"alpha": 1, "beta": 40}
    book["trust"]["bik_medved"] = {"alpha": 1, "beta": 40}
    book["trust"]["izvajanje"] = {"alpha": 40, "beta": 1}
    blocked, reason = decide(book, _votes(("kit", "trend", "varnost", "bik_medved")), _features())
    assert not blocked and "utež" in reason


def test_snapshot_has_required_fields():
    feat = _features()
    for key in ("chain", "category", "liq_usd", "vol24", "vol_liq", "ch5", "ch1h", "ch24", "buy_ratio", "votes", "yes_count"):
        assert key in feat
    assert feat["yes_count"] == 4
    assert feat["votes"]["kit"]["score"] >= 0


def test_threshold_moves_at_most_ten_percent_after_five_losses():
    book = fresh_book()
    feat = _features(("kit", "trend", "varnost", "bik_medved", "izvajanje"))
    old = book["params"]["kit"]["min_ratio"]
    for i in range(5):
        on_close(book, feat, {"pnl_pct": -3, "reason": "stop_loss", "held_sec": 100, "mfe_pct": 1, "mae_pct": -4}, f"t{i}")
    new = book["params"]["kit"]["min_ratio"]
    assert new > old
    assert new <= old * 1.10 + 1e-6
    assert book["version"] == 2
    assert any("Lekcija KIT" in row["text"] for row in book["lessons"])


def test_safety_cannot_loosen():
    book = fresh_book()
    book["params"]["global"]["stop_loss"] = -0.20
    book["params"]["varnost"]["min_liq"] = 1000
    from learning import _merge_params
    merged = _merge_params(book["params"])
    book["params"] = merged
    assert stop_loss(book) >= -0.15
    assert stop_loss(book) <= -0.03
    assert book["params"]["varnost"]["min_liq"] >= 30000


def test_avoid_pattern_blocks():
    book = fresh_book()
    feat = _features()
    for i in range(5):
        on_close(book, feat, {"pnl_pct": -4, "reason": "stop_loss", "held_sec": 10, "mfe_pct": 0, "mae_pct": -4}, f"a{i}")
    assert book["avoid"]
    ok, why = decide(book, _votes(("kit", "trend", "varnost", "bik_medved", "izvajanje")), feat)
    assert not ok and "izogibaj" in why


def test_reject_followup_credits_a_wrong_no():
    book = fresh_book()
    feat = _features(("varnost",))
    book["pending"].append({
        "symbol": "X",
        "pair": "p",
        "chain": "bsc",
        "price": 1.0,
        "features": feat,
        "check_after": 0,
        "time": "t",
    })
    before = book["trust"]["kit"]["beta"]
    n = resolve_pending(book, 10, lambda _p, _c: 1.2, "later")
    assert n == 1
    assert book["trust"]["kit"]["beta"] > before
    assert book["pending"][0]["missed_winner"] is True


def test_rollback_when_new_version_is_worse():
    book = fresh_book()
    book["version"] = 2
    book["history"].append({"version": 1, "why": "začetek", "params": book["params"]})
    book["history"].append({"version": 2, "why": "test", "params": book["params"]})
    for i in range(4):
        book["journal"].append({"version": 1, "outcome": {"pnl_pct": 5}, "votes": {}})
    for i in range(10):
        book["journal"].append({"version": 2, "outcome": {"pnl_pct": -3}, "votes": {}})
    _maybe_rollback(book, "now")
    assert book["version"] == 3
    assert "povratek" in book["history"][-1]["why"]


def test_retune_does_not_loosen_into_a_yes():
    book = fresh_book()
    votes = _votes(())
    out = retune_votes(book, votes, _features(()))
    assert all(row["vote"] == "NO" for row in out.values())


if __name__ == "__main__":
    test_rule_stays_four_of_five_and_varnost()
    test_snapshot_has_required_fields()
    test_threshold_moves_at_most_ten_percent_after_five_losses()
    test_safety_cannot_loosen()
    test_avoid_pattern_blocks()
    test_reject_followup_credits_a_wrong_no()
    test_rollback_when_new_version_is_worse()
    test_retune_does_not_loosen_into_a_yes()
    print("learning tests ok")
