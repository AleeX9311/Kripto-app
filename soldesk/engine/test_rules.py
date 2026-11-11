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


def test_time_stop_when_flat():
    risk = Risk(day_start_balance=1000, day_start_date="2026-11-11")
    opened = (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat()
    pos = _pos(1.0, opened=opened)
    reason = risk.check_exit(pos, 1.0)
    assert reason and reason.startswith("ČASOVNI")


if __name__ == "__main__":
    test_top10_cap_is_75_and_excludes_curve()
    test_launch_stop_and_half()
    test_hard_stop_uses_net_pnl()
    test_partial_then_remainder()
    test_learning_only_tightens()
    test_time_stop_when_flat()
    print("soldesk rules ok")
