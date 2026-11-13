"""Chart and week-cap checks. No network."""

from chart import read_chart
from scan import week_id, can_enter, consider, watch_reason


def _bar(i, price):
    return [i, price, price * 1.04, price * 0.96, price, 1000]


def test_higher_lows_and_zones():
    prices = [1, 1.2, 1.5, 1.3, 1.8, 1.45, 2.2, 1.5, 1.55]
    # stretch so swings exist
    series = []
    i = 0
    for p in (1, 1.1, 1.4, 1.15, 1.7, 1.35, 2.1, 1.5, 1.55, 2.4, 1.7, 1.75):
        series.append(_bar(i, p))
        i += 1
        series.append(_bar(i, p * 0.98))
        i += 1
    chart = read_chart(series, "1h")
    assert chart["ok"]
    assert chart["ath"] is not None
    assert chart["zones"]["no_entry"]["name"].startswith("No-entry")
    assert chart["zones"]["buy_a"]["stop"] < chart["zones"]["buy_a"]["lo"]
    assert chart["zones"]["invalidation"] > 0


def test_universe_and_watch_reason():
    young = {
        "chainId": "solana", "dexId": "raydium", "pairCreatedAt": "2026-11-13T12:00:00+00:00",
        "marketCap": 5_000_000, "volume": {"h24": 3_000_000},
    }
    assert consider(young) is False
    old = dict(young)
    old["pairCreatedAt"] = "2026-01-01T00:00:00+00:00"
    assert consider(old) is True
    assert watch_reason({"chart_1h": {"ok": True, "structure": True, "in_buy_zone": False}}) == "čaka pullback v cono A"


def test_week_cap_blocks_a_sixth_entry():
    assert can_enter({"week_id": week_id(), "week_entries": 5, "positions": []}, "mint") is False
    assert can_enter({"week_id": "1999-W01", "week_entries": 5, "positions": []}, "mint") is True


if __name__ == "__main__":
    test_higher_lows_and_zones()
    test_universe_and_watch_reason()
    test_week_cap_blocks_a_sixth_entry()
    print("runner chart ok")
