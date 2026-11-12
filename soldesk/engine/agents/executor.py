"""Executor — paper entries only. Main lane stays at 8%. DNO is 10% by user request."""

from __future__ import annotations

from typing import Optional

import config
from models import AnalystResult, Candidate, Event, Position, iso


class Executor:
    def __init__(self) -> None:
        assert config.PAPER_MODE, "Executor refuses to start unless PAPER_MODE is True"
        assert config.MAX_POSITION_PCT <= 0.08 + 1e-12, "main-lane size cannot exceed 8%"
        assert config.LAUNCH_MAX_POSITION_PCT <= config.MAX_POSITION_PCT + 1e-12
        assert config.DNO_MAX_POSITION_PCT <= 0.10 + 1e-12, "DNO size cannot exceed the user 10%"

    def size_usd(self, equity: float, cash: float, pct: float | None = None, ceiling: float | None = None) -> float:
        ceiling = config.MAX_POSITION_PCT if ceiling is None else float(ceiling)
        cap_pct = ceiling if pct is None else min(float(pct), ceiling)
        cap_equity = equity * cap_pct
        cap_cash = cash * cap_pct
        size = min(cap_equity, cap_cash, cash)
        assert size <= equity * ceiling + 1e-6, "size exceeds the lane cap"
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

        lane = getattr(cand, "lane", "glavni")
        launch = lane == "novi_lans"
        dno = lane == "dno"
        launch_open = sum(1 for p in open_positions if getattr(p, "lane", "") == "novi_lans")
        dno_open = sum(1 for p in open_positions if getattr(p, "lane", "") == "dno")
        if dno and dno_open >= config.DNO_MAX_POSITIONS:
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=f"{cand.symbol}  refused — max {config.DNO_MAX_POSITIONS} DNO positions",
                mint=cand.mint,
                symbol=cand.symbol,
                extra={"lane": "dno"},
            )
            return None, ev
        if launch and launch_open >= config.LAUNCH_MAX_POSITIONS:
            ev = Event(
                ts=iso(),
                agent="EXECUTOR",
                level="kill",
                message=f"{cand.symbol}  refused — max {config.LAUNCH_MAX_POSITIONS} launch positions",
                mint=cand.mint,
                symbol=cand.symbol,
                extra={"lane": "novi_lans"},
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

        if dno:
            size_pct = config.DNO_MAX_POSITION_PCT
        elif launch:
            size_pct = config.LAUNCH_MAX_POSITION_PCT
        else:
            size_pct = config.MAX_POSITION_PCT
        size = self.size_usd(
            equity,
            cash,
            size_pct,
            ceiling=config.DNO_MAX_POSITION_PCT if dno else config.MAX_POSITION_PCT,
        )
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
        if dno:
            stop_pct = config.DNO_HARD_STOP_PCT
        elif launch:
            stop_pct = config.LAUNCH_HARD_STOP_PCT
        else:
            stop_pct = config.HARD_STOP_PCT
        hard_stop = fill * (1.0 + stop_pct)
        pct_used = (size / equity) * 100 if equity else 0.0
        lane_cap = config.DNO_MAX_POSITION_PCT if dno else config.MAX_POSITION_PCT
        assert pct_used <= lane_cap * 100 + 1e-6

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
            lane=getattr(cand, "lane", "glavni"),
            chain=getattr(cand, "chain", "solana"),
            entry_reasons=[
                f"paper BUY {cand.symbol}",
                f"score {analysis.total:.2f}",
                f"size ${size:.2f} ({pct_used:.2f}% of equity ${equity:.2f})",
                f"mid ${mid:.8g} → fill ${fill:.8g} (slip {config.ENTRY_SLIPPAGE_PCT:.2%}+fee {config.ENTRY_FEE_PCT:.2%})",
                f"hard stop ${hard_stop:.8g} ({stop_pct:.0%})",
                analysis.structure_label,
            ],
        )
        ev = Event(
            ts=iso(),
            agent="EXECUTOR",
            level="trade",
            message=(
                f"PAPER BUY {cand.symbol}  ${size:.2f} ({pct_used:.2f}% of ${equity:.2f})  "
                f"fill ${fill:.8g}  qty {qty:.6g}  stop {stop_pct:.0%}"
                + (f"  cilj {config.DNO_TAKE_PROFIT_PCT:.0%}" if dno else "")
            ),
            mint=cand.mint,
            symbol=cand.symbol,
            extra={"size_usd": size, "fill": fill, "pct_of_equity": pct_used, "paper": True},
        )
        return pos, ev
