"""Support-stop tests. No network."""

from support import find_support, plan_stop, size_for_stop, swing_lows


def _candle(i, low, high=None, close=None, volume=10):
    high = high if high is not None else low + 2
    close = close if close is not None else (low + high) / 2
    return [float(i), close, high, low, close, float(volume)]


def _series():
    """Two swing lows at 92, then price back near 100. Entry will be 100."""
    rows = []
    for i in range(30):
        low = 100.4
        if i in (8, 18):
            low = 92
        rows.append(_candle(i, low, low + 1.2, low + 0.8, volume=50 if i in (8, 18) else 5))
    return rows


def test_swing_and_nearest_support():
    candles = _series()
    lows = swing_lows(candles)
    assert any(abs(price - 92) < 1e-6 for price, _v in lows)
    zone = find_support(100, candles, [])
    assert zone is not None
    assert zone["low"] == 92
    assert zone["touches"] >= 2


def test_stop_buffer_and_clamp():
    plan = plan_stop(100, _series(), [])
    assert plan["ok"], plan
    # Zone low 92, buffer at least 1% of 100 = 1, so stop <= 91, distance about -9%.
    assert -15 < plan["stop_pct"] < -5
    assert plan["stop_price"] < 92
    assert plan["reward_risk"] >= 1.5


def test_no_support_uses_ten_percent():
    flat = [_candle(i, 100, 102, 101) for i in range(10)]
    plan = plan_stop(100, flat, [])
    assert plan["ok"]
    assert abs(plan["stop_pct"] + 10) < 1e-6
    assert plan["reason"] == "ni podpore"


def test_far_support_skips():
    rows = [_candle(i, 101, 103, 102) for i in range(20)]
    rows[8] = _candle(8, 70, 74, 73, 80)
    rows[14] = _candle(14, 70, 74, 73, 80)
    plan = plan_stop(100, rows, [])
    assert not plan["ok"]
    assert plan["reason"] == "podpora predaleč"


def test_tight_support_clamps_to_five_and_sizes():
    rows = [_candle(i, 100.5, 102, 101) for i in range(20)]
    rows[8] = _candle(8, 98, 100.2, 99.5, 40)
    rows[14] = _candle(14, 98, 100.2, 99.5, 40)
    plan = plan_stop(100, rows, [])
    # Support is very close, so the stop is clamped to -5% and reward is 4.
    assert plan["ok"], plan
    assert abs(plan["stop_frac"] + 0.05) < 1e-9
    size = size_for_stop(100, plan["stop_frac"])
    # 1.2% / 5% = 24% of equity, capped at 10%.
    assert abs(size - 10) < 1e-9
    wide = size_for_stop(100, -0.12)
    # 1.2 / 0.12 = 10, so the 10% cap and the risk cap meet.
    assert abs(wide - 10) < 1e-6
    deeper = size_for_stop(100, -0.15)
    assert abs(deeper - 8) < 1e-6


if __name__ == "__main__":
    test_swing_and_nearest_support()
    test_stop_buffer_and_clamp()
    test_no_support_uses_ten_percent()
    test_far_support_skips()
    test_tight_support_clamps_to_five_and_sizes()
    print("support tests ok")
