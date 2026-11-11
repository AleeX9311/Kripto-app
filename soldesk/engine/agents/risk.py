"""Risk — hard stop, trailing stop after +12%, no averaging down, no revenge trades, daily loss brake.

Exits are simulated on each price poll. No real orders are sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config
from models import ClosedTrade, Event, Position, iso, utcnow


TZ = ZoneInfo(config.TIMEZONE)


class Risk:
    def __init__(
        self,
        banned_tokens: Optional[set[str]] = None,
        consecutive_losses: int = 0,
        cooldown_until: Optional[str] = None,
        day_start_balance: float = 0.0,
        day_start_date: str = "",
    ) -> None:
        self.banned_tokens = banned_tokens if banned_tokens is not None else set()
        self.consecutive_losses = consecutive_losses
        self.cooldown_until = cooldown_until
        self.day_start_balance = day_start_balance
        self.day_start_date = day_start_date

    def local_day(self, now: Optional[datetime] = None) -> str:
        return (now or utcnow()).astimezone(TZ).date().isoformat()

    def rollover_day(self, equity: float, now: Optional[datetime] = None) -> None:
        day = self.local_day(now)
        if day != self.day_start_date:
            self.day_start_date = day
            self.day_start_balance = equity

    def daily_brake_active(self, equity: float) -> bool:
        if self.day_start_balance <= 0:
            return False
        pnl = (equity - self.day_start_balance) / self.day_start_balance
        return pnl <= config.DAILY_LOSS_BRAKE_PCT

    def daily_pnl_pct(self, equity: float) -> float:
        if self.day_start_balance <= 0:
            return 0.0
        return (equity - self.day_start_balance) / self.day_start_balance

    def cooldown_active(self, now: Optional[datetime] = None) -> bool:
        if not self.cooldown_until:
            return False
        try:
            until = datetime.fromisoformat(self.cooldown_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return (now or utcnow()) < until

    def allow_new_entry(
        self, mint: str, equity: float, has_open: bool
    ) -> tuple[bool, str]:
        if has_open:
            return False, "no averaging down — already in this token"
        if mint in self.banned_tokens:
            return False, "no re-entry — token was stopped out (no revenge trades)"
        if self.cooldown_active():
            return False, f"global cooldown after {config.CONSECUTIVE_LOSS_LIMIT} consecutive losses until {self.cooldown_until}"
        if self.daily_brake_active(equity):
            return False, (
                f"daily loss brake  {self.daily_pnl_pct(equity):.2%} ≤ "
                f"{config.DAILY_LOSS_BRAKE_PCT:.0%} of start-of-day ${self.day_start_balance:.2f}"
            )
        return True, "ok"

    def exit_factor(self) -> float:
        return (1.0 - config.EXIT_SLIPPAGE_PCT) * (1.0 - config.EXIT_FEE_PCT)

    def net_pnl_pct(self, pos: Position, mid: float) -> float:
        """Realized percent on the remaining cost if we sold at this mid, after slip and fee."""
        if mid <= 0 or pos.size_usd <= 0 or pos.qty <= 0:
            return 0.0
        proceeds = pos.qty * mid * self.exit_factor()
        return (proceeds - pos.size_usd) / pos.size_usd

    def mark(self, pos: Position, mid: float) -> None:
        if mid <= 0:
            return
        pos.last_price = mid
        if mid > pos.peak_price:
            pos.peak_price = mid
        net = self.net_pnl_pct(pos, mid)
        if net >= config.BREAKEVEN_ARM_PCT:
            pos.breakeven = True
        if net >= config.TRAIL_ACTIVATE_PCT:
            pos.trail_armed = True
        if pos.trail_armed:
            pos.trail_stop_price = pos.peak_price * (1.0 - config.TRAIL_PCT)

    def check_exit(self, pos: Position, mid: float) -> Optional[str]:
        """Return an exit or partial reason. The hard stop uses net PnL, not the mid print."""
        if mid <= 0:
            return None
        self.mark(pos, mid)
        net = self.net_pnl_pct(pos, mid)
        if net <= config.HARD_STOP_PCT:
            return (
                f"HARD STOP  neto {net:.2%} ≤ {config.HARD_STOP_PCT:.0%} "
                f"(vključuje zdrs {config.EXIT_SLIPPAGE_PCT:.2%} in provizijo {config.EXIT_FEE_PCT:.2%})"
            )
        if not pos.tp1_done and net >= config.PARTIAL_TP[0]:
            return "PARTIAL_15"
        if pos.tp1_done and not pos.tp2_done and net >= config.PARTIAL_TP[1]:
            return "PARTIAL_30"
        if pos.breakeven and net <= 0:
            return f"BREAKEVEN  neto {net:.2%} po +{config.BREAKEVEN_ARM_PCT:.0%}"
        if pos.trail_armed and pos.trail_stop_price is not None and mid <= pos.trail_stop_price:
            return (
                f"TRAIL STOP  mid ${mid:.8g} ≤ trail ${pos.trail_stop_price:.8g} "
                f"(oborožen pri neto +{config.TRAIL_ACTIVATE_PCT:.0%})"
            )
        try:
            opened = datetime.fromisoformat(pos.opened_at)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            held = (utcnow() - opened).total_seconds()
        except (TypeError, ValueError):
            held = 0
        if held >= config.TIME_STOP_SEC and abs(net) < config.TIME_STOP_FLAT_PCT:
            return f"ČASOVNI IZHOD  {held / 60:.0f} min, ploščat neto {net:.2%}"
        return None

    def take_partial(self, pos: Position, mid: float, which: str) -> tuple[ClosedTrade, Event]:
        """Sell one third of the original size. The rest stays open."""
        original_qty = pos.original_qty or pos.qty
        sell_qty = min(pos.qty, original_qty * config.PARTIAL_FRACTION)
        if sell_qty <= 0 or pos.qty <= 0:
            sell_qty = pos.qty / 3.0
        portion = sell_qty / pos.qty
        cost_slice = pos.size_usd * portion
        fill_exit = mid * self.exit_factor()
        proceeds = sell_qty * fill_exit
        pnl_usd = proceeds - cost_slice
        pnl_pct = (pnl_usd / cost_slice) if cost_slice else 0.0
        pos.qty -= sell_qty
        pos.size_usd -= cost_slice
        pos.banked_pnl_usd = float(pos.banked_pnl_usd or 0) + pnl_usd
        if which == "PARTIAL_15":
            pos.tp1_done = True
        else:
            pos.tp2_done = True
        label = "+15 %" if which == "PARTIAL_15" else "+30 %"
        trade = ClosedTrade(
            mint=pos.mint,
            symbol=pos.symbol,
            side="SELL",
            qty=sell_qty,
            entry_price=pos.fill_price,
            exit_price=mid,
            fill_exit_price=fill_exit,
            size_usd=cost_slice,
            proceeds_usd=proceeds,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            reason=f"DELNI IZHOD {label}",
            opened_at=pos.opened_at,
            closed_at=iso(),
            source=pos.source,
            analyst_score=pos.analyst_score,
        )
        ev = Event(
            ts=iso(),
            agent="RISK",
            level="trade",
            message=f"PAPER DELNO {pos.symbol}  {label}  ${pnl_usd:+.2f} ({pnl_pct:+.2%})",
            mint=pos.mint,
            symbol=pos.symbol,
            extra={"partial": True, "pnl_usd": pnl_usd, "paper": True},
        )
        return trade, ev

    def close(
        self, pos: Position, mid: float, reason: str
    ) -> tuple[ClosedTrade, Event]:
        fill_exit = mid * self.exit_factor()
        proceeds = pos.qty * fill_exit
        remaining_pnl = proceeds - pos.size_usd
        pnl_usd = remaining_pnl + float(pos.banked_pnl_usd or 0)
        base = pos.original_size_usd or (pos.size_usd + float(pos.banked_pnl_usd or 0))
        # Percent is on the original cash spent, including earlier partials.
        pnl_pct = (pnl_usd / base) if base else 0.0
        trade = ClosedTrade(
            mint=pos.mint,
            symbol=pos.symbol,
            side="SELL",
            qty=pos.qty,
            entry_price=pos.fill_price,
            exit_price=mid,
            fill_exit_price=fill_exit,
            size_usd=base,
            proceeds_usd=proceeds,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            reason=reason,
            opened_at=pos.opened_at,
            closed_at=iso(),
            source=pos.source,
            analyst_score=pos.analyst_score,
        )
        # Stops (hard or trail) ban the token. No revenge re-entry.
        if "STOP" in reason.upper():
            self.banned_tokens.add(pos.mint)
        if pnl_usd < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= config.CONSECUTIVE_LOSS_LIMIT:
                until = utcnow()
                from datetime import timedelta

                until = until + timedelta(seconds=config.CONSECUTIVE_LOSS_COOLDOWN_SEC)
                self.cooldown_until = until.isoformat()
        else:
            self.consecutive_losses = 0
        ev = Event(
            ts=iso(),
            agent="RISK",
            level="trade",
            message=(
                f"PAPER SELL {pos.symbol}  {reason}  "
                f"realized ${pnl_usd:+.2f} ({pnl_pct:+.2%})  mid ${mid:.8g} → fill ${fill_exit:.8g}"
            ),
            mint=pos.mint,
            symbol=pos.symbol,
            extra={"pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "paper": True},
        )
        return trade, ev
