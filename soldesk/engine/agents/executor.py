"""Executor — paper entries only. Sizes at most 8% of current equity. Never more."""

from __future__ import annotations

from typing import Optional

import config
from models import AnalystResult, Candidate, Event, Position, iso


class Executor:
    def __init__(self) -> None:
        assert config.PAPER_MODE, "Executor refuses to start unless PAPER_MODE is True"
        assert config.MAX_POSITION_PCT <= 0.08 + 1e-12, "max size cannot exceed 8%"

    def size_usd(self, equity: float, cash: float) -> float:
        cap_equity = equity * config.MAX_POSITION_PCT
        cap_cash = cash * config.MAX_POSITION_PCT
        size = min(cap_equity, cap_cash, cash)
        # Hard guarantee the brief asked for
        assert size <= equity * config.MAX_POSITION_PCT + 1e-9, "size exceeds 8% of current balance"
        assert size <= cash + 1e-9, "size exceeds available cash"
        return size

    def try_entry(
        self,
        cand: Candidate,
        analysis: AnalystResult,
        equity: float,
        cash: float,
        open_positions: list[Position],
        mid_price: Optional[float] = None,
    ) -> tuple[Optional[Position], Event]:
        assert config.PAPER_MODE
        if not analysis.passed or analysis.total < config.MIN_ANALYST_SCORE:
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=f"{cand.symbol}  refused — analyst {analysis.total:.2f} < {config.MIN_ANALYST_SCORE:.2f}",
                mint=cand.mint,
                symbol=cand.symbol,
            )
            return None, ev

        if any(p.mint == cand.mint for p in open_positions):
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=f"{cand.symbol}  refused — already in a position (no averaging down)",
                mint=cand.mint,
                symbol=cand.symbol,
            )
            return None, ev

        if len(open_positions) >= config.MAX_CONCURRENT_POSITIONS:
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=f"{cand.symbol}  refused — max {config.MAX_CONCURRENT_POSITIONS} concurrent positions",
                mint=cand.mint,
                symbol=cand.symbol,
            )
            return None, ev

        mid = mid_price if mid_price and mid_price > 0 else cand.price_usd
        if mid <= 0:
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=f"{cand.symbol}  refused — ni podatka (price)",
                mint=cand.mint,
                symbol=cand.symbol,
            )
            return None, ev

        size = self.size_usd(equity, cash)
        if size < 1.0:
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=f"{cand.symbol}  refused — size ${size:.2f} too small",
                mint=cand.mint,
                symbol=cand.symbol,
            )
            return None, ev

        if cand.liquidity_usd > 0 and size > cand.liquidity_usd * config.MAX_TRADE_VS_LIQUIDITY:
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=(
                    f"{cand.symbol}  refused — ${size:.2f} is > "
                    f"{config.MAX_TRADE_VS_LIQUIDITY:.0%} of liquidity ${cand.liquidity_usd:,.0f}"
                ),
                mint=cand.mint,
                symbol=cand.symbol,
            )
            return None, ev

        fill = mid * (1.0 + config.ENTRY_SLIPPAGE_PCT) * (1.0 + config.ENTRY_FEE_PCT)
        qty = size / fill
        hard_stop = fill * (1.0 + config.HARD_STOP_PCT)
        pct_used = (size / equity) * 100 if equity else 0.0
        assert pct_used <= config.MAX_POSITION_PCT * 100 + 1e-6

        pos = Position(
            mint=cand.mint,
            symbol=cand.symbol,
            pair_address=cand.pair_address,
            dex=cand.dex,
            qty=qty,
            fill_price=fill,
            size_usd=size,
            opened_at=iso(),
            peak_price=fill,
            last_price=mid,
            trail_armed=False,
            trail_stop_price=None,
            hard_stop_price=hard_stop,
            source=cand.source,
            analyst_score=analysis.total,
            original_qty=qty,
            original_size_usd=size,
            setup=analysis.structure_label,
            entry_reasons=[
                f"paper BUY {cand.symbol}",
                f"score {analysis.total:.2f}",
                f"size ${size:.2f} ({pct_used:.2f}% of equity ${equity:.2f})",
                f"mid ${mid:.8g} → fill ${fill:.8g} (slip {config.ENTRY_SLIPPAGE_PCT:.2%}+fee {config.ENTRY_FEE_PCT:.2%})",
                f"hard stop ${hard_stop:.8g} ({config.HARD_STOP_PCT:.0%})",
                analysis.structure_label,
            ],
        )
        ev = Event(
            ts=iso(),
            agent="EXECUTOR",
            level="trade",
            message=(
                f"PAPER BUY {cand.symbol}  ${size:.2f} ({pct_used:.2f}% of ${equity:.2f})  "
                f"fill ${fill:.8g}  qty {qty:.6g}  stop {config.HARD_STOP_PCT:.0%}"
            ),
            mint=cand.mint,
            symbol=cand.symbol,
            extra={"size_usd": size, "fill": fill, "pct_of_equity": pct_used, "paper": True},
        )
        return pos, ev
