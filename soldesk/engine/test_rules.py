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


def test_top10_cap_is_35():
    assert config.MAX_TOP10_HOLDER_PCT == 35.0
    assert config.PAPER_MODE is True


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


def test_time_stop_when_flat():
    risk = Risk(day_start_balance=1000, day_start_date="2026-11-11")
    opened = (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat()
    pos = _pos(1.0, opened=opened)
    reason = risk.check_exit(pos, 1.0)
    assert reason and reason.startswith("ČASOVNI")


if __name__ == "__main__":
    test_top10_cap_is_35()
    test_hard_stop_uses_net_pnl()
    test_partial_then_remainder()
    test_learning_only_tightens()
    test_time_stop_when_flat()
    print("soldesk rules ok")
