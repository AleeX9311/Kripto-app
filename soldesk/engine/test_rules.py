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
    assert book["rules_version"] == 3
    assert book["last_change"].startswith("v3 · DNO lans")
    assert book["variants"]["zdaj"]["wins"] == 3
    assert book["variants"]["zdaj"]["params"]["min_score"] == config.MIN_ANALYST_SCORE
    assert book["variants"]["zdaj"]["params"]["min_buyers"] == config.LAUNCH_MIN_BUYERS
    again = strateg.ensure(state)
    assert sum(1 for row in again["history"] if "DNO lans" in row.get("why", "")) == 1


def _ohlc(closes: list[float], spread: float = 0.02) -> list[list]:
    rows = []
    for i, close in enumerate(closes):
        rows.append([i, close, close * (1 + spread), close * (1 - spread), close, 100 + i])
    return rows


def test_bottom_confirms_reversal_and_rejects_a_knife():
    from agents.bottom import assess

    rise = [2.0] * 6
    fall = [2.0 - i * 0.10 for i in range(1, 12)]
    base = [0.95 + i * 0.025 for i in range(10)]
    closes = rise + fall + base
    read = assess(_ohlc(closes), buys=80, sells=40)
    assert read.drawdown is not None and read.drawdown >= 0.45
    assert read.confirmed, read.reasons
    knife = closes[:-1] + [min(closes) * 0.9]
    missed = assess(_ohlc(knife), buys=80, sells=40)
    assert missed.confirmed is False
    shallow = assess(_ohlc([1 + (i % 3) * 0.01 for i in range(24)]), buys=10, sells=4)
    assert shallow.confirmed is False


def test_dno_exits_are_full_and_fixed():
    risk = Risk()
    stopped = _pos(1.0, 30.0)
    stopped.lane = "dno"
    assert risk.check_exit(stopped, _mid_for_net(risk, stopped, -0.30)).startswith("DNO STOP")
    target = _pos(1.0, 30.0)
    target.lane = "dno"
    assert risk.check_exit(target, _mid_for_net(risk, target, 0.50)).startswith("DNO CILJ")
    held = _pos(1.0, 30.0)
    held.lane = "dno"
    assert risk.check_exit(held, _mid_for_net(risk, held, 0.20)) is None
    assert held.trail_armed is False
    from agents.executor import Executor
    size = Executor().size_usd(1000, 1000, config.DNO_MAX_POSITION_PCT, ceiling=config.DNO_MAX_POSITION_PCT)
    assert config.DNO_MAX_POSITION_PCT == 0.10
    assert config.DNO_MAX_POSITIONS == 5
    assert size == 100
    assert config.DNO_HARD_STOP_PCT == -0.30
    assert config.DNO_TAKE_PROFIT_PCT == 0.50


def test_learner_cannot_move_dno_stop_or_target():
    import strateg
    poisoned = {
        "min_score": 0.55, "min_age_min": 45, "min_buyers": 25, "min_liquidity": 0,
        "pullback_min": 0.03, "trail": 0.06, "time_min": 40, "tp": 0.15,
        "dno_stop": -0.50, "dno_tp": 0.20, "dno_size": 0.20, "dno_positions": 9,
        "stop": -0.40, "size": 0.20,
        "dno_min_drawdown": 0.55,
    }
    tightened = strateg._tighten({}, poisoned)
    assert "dno_stop" not in tightened and "dno_tp" not in tightened and "stop" not in tightened
    assert "dno_size" not in tightened and "dno_positions" not in tightened and "size" not in tightened
    assert tightened["dno_min_drawdown"] == 0.55
    view = strateg.live_view({"strategy": {"version": 3, "rules_version": 3, "live": {"dno_stop": -0.50, "dno_tp": 0.10, "dno_size": 0.25, "dno_positions": 9, "dno_min_score": 0.20}, "variants": {}, "history": [], "pending": [], "lessons": []}})
    assert "dno_stop" not in view and "dno_tp" not in view and "dno_size" not in view and "dno_positions" not in view
    assert view["dno_min_score"] >= config.DNO_MIN_SCORE
    assert view["dno_min_drawdown"] >= config.DNO_MIN_DRAWDOWN


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
    test_bottom_confirms_reversal_and_rejects_a_knife()
    test_dno_exits_are_full_and_fixed()
    test_learner_cannot_move_dno_stop_or_target()
    test_pool_stats_uses_the_http_limiter()
    print("soldesk rules ok")
