"""Logic tests for the five entry agents. No network."""

from __future__ import annotations

import time

from agents import (
    AGENT_IDS,
    evaluate_agents,
    first_veto,
    note_votes,
    push_activity,
    push_candidate,
    push_equity,
)


def _pair(**overrides):
    now_ms = time.time() * 1000
    pair = {
        "dexId": "pancakeswap",
        "priceUsd": "0.0012",
        "pairAddress": "0x" + "ab" * 20,
        "url": "https://dexscreener.com/bsc/" + "ab" * 20,
        "baseToken": {"address": "0x" + "11" * 20, "symbol": "TEST", "name": "Test"},
        "priceChange": {"m5": 2.4, "h1": 9.0},
        "volume": {"m5": 9000, "h1": 70000, "h24": 220000},
        "txns": {
            "m5": {"buys": 42, "sells": 16},
            "h1": {"buys": 210, "sells": 90},
        },
        "liquidity": {"usd": 90000},
        "fdv": 800000,
        "marketCap": 600000,
        "pairCreatedAt": now_ms - 5 * 3600 * 1000,
    }
    pair.update(overrides)
    return pair


def _ctx(**extra):
    ctx = {
        "boosted": True,
        "size_usd": 8.0,
        "run_goplus": True,
        "goplus": (True, "ok buy_tax=0.01 sell_tax=0.01"),
    }
    ctx.update(extra)
    return ctx


def test_all_yes_buys():
    ok, votes = evaluate_agents(_pair(), _ctx())
    assert ok, {k: v["reason"] for k, v in votes.items()}
    assert set(votes) == set(AGENT_IDS)
    assert all(v["vote"] == "YES" for v in votes.values())
    assert first_veto(votes) is None
    assert votes["kit"]["numbers"]["buys_5m"] == 42
    assert votes["izvajanje"]["numbers"]["dex"] == "pancakeswap"


def test_each_agent_can_veto():
    cases = {
        "kit": _pair(txns={"m5": {"buys": 4, "sells": 4}, "h1": {"buys": 10, "sells": 10}}),
        "trend": _pair(priceChange={"m5": -1.2, "h1": 4.0}),
        "varnost": _pair(liquidity={"usd": 1000}),
        "bik_medved": _pair(priceChange={"m5": -0.4, "h1": 6.0}),
        "izvajanje": _pair(dexId="biswap"),
    }
    for agent_id, pair in cases.items():
        ok, votes = evaluate_agents(pair, _ctx())
        assert not ok, agent_id
        assert votes[agent_id]["vote"] == "NO", (agent_id, votes[agent_id])
        assert first_veto(votes) in AGENT_IDS
        # Every agent still casts a vote.
        assert len(votes) == 5


def test_goplus_honeypot_blocks_even_if_others_pass():
    ok, votes = evaluate_agents(_pair(), _ctx(goplus=(False, "honeypot")))
    assert not ok
    assert votes["varnost"]["vote"] == "NO"
    assert "honeypot" in votes["varnost"]["reason"]
    assert first_veto(votes) == "varnost" or votes["varnost"]["vote"] == "NO"


def test_books_keep_latest_window():
    st = {}
    ok, votes = evaluate_agents(_pair(), _ctx())
    for i in range(40):
        note_votes(st, f"T{i}", votes, f"2026-11-09T21:{i:02d}:00+01:00")
        push_candidate(st, {"symbol": f"T{i}", "votes": votes})
        push_activity(st, {"type": "veto", "n": i})
    assert len(st["candidates"]) == 30
    assert st["candidates"][-1]["symbol"] == "T39"
    assert st["candidates"][0]["symbol"] == "T10"
    assert len(st["activity"]) == 40
    assert st["agents"]["kit"]["evaluated"] == 40
    assert st["agents"]["kit"]["approved"] == 40
    assert st["agents"]["kit"]["latest"]["symbol"] == "T39"
    for i in range(600):
        push_equity(st, {"t": i, "usd": 50 + i})
    assert len(st["equity_history"]) == 500
    assert st["equity_history"][-1]["usd"] == 50 + 599


if __name__ == "__main__":
    test_all_yes_buys()
    test_each_agent_can_veto()
    test_goplus_honeypot_blocks_even_if_others_pass()
    test_books_keep_latest_window()
    print("agent tests ok")
