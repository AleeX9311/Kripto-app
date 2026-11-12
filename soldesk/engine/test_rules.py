"""Paper-rule tests. No network."""

from datetime import datetime, timedelta, timezone

import config
from agents.risk import Risk
from learning import note_close, threshold
from models import Position, iso


def _pos(fill: float, size: float = 100.0, opened: str | None = None) -> Position:
    qty = size / fill
    return Position(
        mint="m",
        symbol="T",
        pair_address="p",
        dex="raydium",
        qty=qty,
        fill_price=fill,
        size_usd=size,
        opened_at=opened or iso(),
        peak_price=fill,
        last_price=fill,
        hard_stop_price=fill * (1 + config.HARD_STOP_PCT),
        original_qty=qty,
        original_size_usd=size,
    )


def _mid_for_net(risk: Risk, pos: Position, net: float) -> float:
    factor = risk.exit_factor()
    return pos.fill_price * (1.0 + net) / factor


def test_top10_cap_is_75_and_excludes_curve():
    from agents.filter import Filter, _excluded_holder
    assert config.MAX_TOP10_HOLDER_PCT == 75.0
    assert config.LAUNCH_MAX_POSITION_PCT <= config.MAX_POSITION_PCT
    assert config.PAPER_MODE is True
    assert _excluded_holder("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", {}, set(), "mint")
    pct = Filter(None)._sum_top10([
        {"owner": "walletA", "pct": 30},
        {"owner": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "pct": 40},
        {"owner": "curve", "pct": 20, "label": "bonding curve"},
        {"owner": "walletB", "pct": 10},
    ], set(), "mint")
    assert pct == 40.0


def test_hard_stop_uses_net_pnl():
    risk = Risk(day_start_balance=1000, day_start_date="2026-11-11")
    pos = _pos(1.012525)
    mild = _mid_for_net(risk, pos, -0.04)
    assert risk.net_pnl_pct(pos, mild) > config.HARD_STOP_PCT
    assert risk.check_exit(pos, mild) is None
    pos2 = _pos(1.012525)
    hard = _mid_for_net(risk, pos2, -0.061)
    reason = risk.check_exit(pos2, hard)
    assert reason and reason.startswith("HARD STOP")
    assert risk.net_pnl_pct(pos2, hard) <= config.HARD_STOP_PCT


def test_partial_then_remainder():
    risk = Risk(day_start_balance=1000, day_start_date="2026-11-11")
    pos = _pos(1.0)
    mid = _mid_for_net(risk, pos, 0.16)
    reason = risk.check_exit(pos, mid)
    assert reason == "PARTIAL_15"
    before = pos.qty
    trade, _ev = risk.take_partial(pos, mid, reason)
    assert trade.pnl_usd > 0
    assert pos.tp1_done
    assert pos.qty < before
    assert risk.check_exit(pos, mid) is None


def test_learning_only_tightens():
    book = {"buckets": {}}
    for _ in range(6):
        note_close(book, 0.62, "umik_visji_min", False, -4)
    need, why = threshold(book, 0.62, "umik_visji_min")
    assert need > config.MIN_ANALYST_SCORE
    assert "prag" in why
    book = {"buckets": {}}
    for _ in range(6):
        note_close(book, 0.7, "umik_visji_min", True, 5)
    need, why = threshold(book, 0.7, "umik_visji_min")
    assert need == config.MIN_ANALYST_SCORE
    assert why == ""


def test_launch_stop_and_half():
    risk = Risk(day_start_balance=1000, day_start_date="2026-11-11")
    pos = _pos(1.0)
    pos.lane = "novi_lans"
    mild = _mid_for_net(risk, pos, -0.08)
    assert risk.check_exit(pos, mild) is None
    hard = _pos(1.0)
    hard.lane = "novi_lans"
    reason = risk.check_exit(hard, _mid_for_net(risk, hard, -0.11))
    assert reason and reason.startswith("HARD STOP")
    part = _pos(1.0)
    part.lane = "novi_lans"
    mid = _mid_for_net(risk, part, 0.31)
    assert risk.check_exit(part, mid) == "PARTIAL_30"
    before = part.qty
    risk.take_partial(part, mid, "PARTIAL_30")
    assert part.qty < before * 0.6


def test_bsc_goplus_fail_closed():
    from agents.filter import bsc_security_checks
    missing = bsc_security_checks(None, False, False)
    assert missing[0].passed is False
    clean = {
        "is_honeypot": "0", "cannot_sell_all": "0", "buy_tax": "0.01", "sell_tax": "0.02",
        "is_mintable": "0", "hidden_owner": "0", "is_proxy": "0", "is_blacklisted": "0",
        "trading_cooldown": "0", "transfer_pausable": "0", "honeypot_with_same_creator": "0",
        "lp_holders": [{"address": "0x000000000000000000000000000000000000dead", "percent": "0.95", "is_locked": "1"}],
        "holders": [
            {"address": "0x000000000000000000000000000000000000dead", "percent": "0.40", "is_locked": "1", "tag": "burn"},
            {"address": "0xabc", "percent": "0.20", "is_locked": "0"},
        ],
    }
    assert all(c.passed for c in bsc_security_checks(clean, False, False))
    dirty = dict(clean)
    dirty["is_honeypot"] = "1"
    dirty["buy_tax"] = "0.2"
    names = {c.name for c in bsc_security_checks(dirty, False, False) if not c.passed}
    assert "honeypot" in names and "tax" in names
    pre = bsc_security_checks(clean, True, True)
    assert any(c.name == "lp_lock" and c.passed and "four.meme" in c.reason for c in pre)


def test_time_stop_when_flat():
    risk = Risk(day_start_balance=1000, day_start_date="2026-11-11")
    opened = (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat()
    pos = _pos(1.0, opened=opened)
    reason = risk.check_exit(pos, 1.0)
    assert reason and reason.startswith("ČASOVNI")


def test_entry_gate_needs_structure_and_one_confirmation():
    from agents.timing import entry_gate

    assert entry_gate(False, True, True, True) is False
    assert entry_gate(True, False, False, False) is False
    assert entry_gate(True, True, False, False) is True
    assert entry_gate(True, False, True, False) is True
    assert entry_gate(True, False, False, True) is True


def test_manual_baseline_is_v2_and_keeps_stats():
    import strateg

    state = {"strategy": strateg.fresh()}
    state["strategy"]["rules_version"] = 1
    state["strategy"]["version"] = 1
    state["strategy"]["history"] = [{"version": 1, "why": "začetek"}]
    state["strategy"]["variants"]["zdaj"]["wins"] = 3
    state["strategy"]["variants"]["zdaj"]["params"]["min_score"] = 0.60
    book = strateg.ensure(state)
    assert book["rules_version"] == 2
    assert book["last_change"].startswith("v2 · ročno sproščeno")
    assert book["variants"]["zdaj"]["wins"] == 3
    assert book["variants"]["zdaj"]["params"]["min_score"] == config.MIN_ANALYST_SCORE
    assert book["variants"]["zdaj"]["params"]["min_buyers"] == config.LAUNCH_MIN_BUYERS
    again = strateg.ensure(state)
    assert sum(1 for row in again["history"] if "ročno sproščeno" in row.get("why", "")) == 1


def test_pool_stats_uses_the_http_limiter():
    from market import Market

    market = Market()
    seen = {}

    def _get(limiter, url, params=None):
        seen["limiter"] = limiter
        seen["url"] = url
        return {"data": []}

    market.http._get = _get
    assert market.token_pool_stats("mint", "solana") == {}
    assert seen["limiter"] is market.http.gecko_rl
    assert "/networks/solana/tokens/mint/pools" in seen["url"]


if __name__ == "__main__":
    test_top10_cap_is_75_and_excludes_curve()
    test_launch_stop_and_half()
    test_bsc_goplus_fail_closed()
    test_hard_stop_uses_net_pnl()
    test_partial_then_remainder()
    test_learning_only_tightens()
    test_time_stop_when_flat()
    test_entry_gate_needs_structure_and_one_confirmation()
    test_manual_baseline_is_v2_and_keeps_stats()
    test_pool_stats_uses_the_http_limiter()
    print("soldesk rules ok")
