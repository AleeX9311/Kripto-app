"""Logger — every trade and every rejection, plus equity, persisted to disk."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Optional

from models import ClosedTrade, Decision, Event, iso, to_plain


class Logger:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_path = self.log_dir / "decisions.jsonl"
        self.trades_path = self.log_dir / "trades.csv"
        self.equity_path = self.log_dir / "equity.csv"
        self._ensure_csv(
            self.trades_path,
            [
                "closed_at",
                "symbol",
                "mint",
                "side",
                "qty",
                "entry_price",
                "exit_price",
                "fill_exit_price",
                "size_usd",
                "proceeds_usd",
                "pnl_usd",
                "pnl_pct",
                "reason",
                "opened_at",
                "source",
                "analyst_score",
            ],
        )
        self._ensure_csv(self.equity_path, ["ts", "cash", "equity", "open", "closed", "source"])

    def _ensure_csv(self, path: Path, headers: list[str]) -> None:
        if path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(headers)

    def decision(self, kind: str, mint: str, symbol: str, reasons: list[str],
                 score: Optional[float] = None, details: Optional[dict] = None) -> Decision:
        d = Decision(
            ts=iso(),
            kind=kind,
            mint=mint,
            symbol=symbol,
            reasons=reasons,
            score=score,
            details=details or {},
        )
        with self.decisions_path.open("a") as f:
            f.write(json.dumps(to_plain(d), ensure_ascii=False) + "\n")
        return d

    def trade(self, trade: ClosedTrade) -> None:
        with self.trades_path.open("a", newline="") as f:
            csv.writer(f).writerow(
                [
                    trade.closed_at,
                    trade.symbol,
                    trade.mint,
                    trade.side,
                    f"{trade.qty:.10g}",
                    f"{trade.entry_price:.10g}",
                    f"{trade.exit_price:.10g}",
                    f"{trade.fill_exit_price:.10g}",
                    f"{trade.size_usd:.4f}",
                    f"{trade.proceeds_usd:.4f}",
                    f"{trade.pnl_usd:.4f}",
                    f"{trade.pnl_pct:.6f}",
                    trade.reason,
                    trade.opened_at,
                    trade.source,
                    f"{trade.analyst_score:.4f}",
                ]
            )
        self.decision(
            "exit",
            trade.mint,
            trade.symbol,
            [trade.reason],
            score=trade.analyst_score,
            details={
                "pnl_usd": trade.pnl_usd,
                "pnl_pct": trade.pnl_pct,
                "proceeds": trade.proceeds_usd,
            },
        )

    def equity(self, cash: float, equity: float, open_n: int, closed_n: int, source: str) -> None:
        with self.equity_path.open("a", newline="") as f:
            csv.writer(f).writerow(
                [iso(), f"{cash:.4f}", f"{equity:.4f}", open_n, closed_n, source]
            )

    def event_line(self, ev: Event) -> str:
        return f"[{ev.ts}] {ev.agent:<8} {ev.message}"
