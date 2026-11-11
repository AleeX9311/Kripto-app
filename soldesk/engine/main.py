#!/usr/bin/env python3
"""Paper-trading Solana memecoin desk. Never places a real transaction.

Usage:
  python main.py [--sim] [--balance 1000] [--headless --minutes N]
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import config
from agents.analyst import Analyst
from agents.executor import Executor
from agents.filter import Filter
from agents.logger import Logger
from agents.risk import Risk
from agents.scout import Scout
from market import Market
from models import Candidate, Event, Position, iso, to_plain, utcnow
from simfeed import SimFeed
import learning
import strateg
import state_store
from market import candidate_from_dex_pair
from status import build_status, write_status
from agents.timing import evaluate as timing_evaluate
from terminal_ui import DeskView, local_ts, render_snapshot_svg

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo(config.TIMEZONE)

# Re-check a rejected mint after this many seconds (volume/liquidity can change)
RECHECK_SEC = 10 * 60


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Paper-trading Solana memecoin desk (no real transactions, no wallets)."
    )
    p.add_argument("--sim", action="store_true", help="Force the clearly labelled SIMULATED data feed")
    p.add_argument("--balance", type=float, default=config.STARTING_BALANCE_USD, help="Starting virtual USD balance")
    p.add_argument("--headless", action="store_true", help="Plain log lines instead of the rich live UI")
    p.add_argument("--minutes", type=float, default=0, help="Stop after N minutes (0 = run until Ctrl-C)")
    p.add_argument("--fresh", action="store_true", help="Ignore state.json and start a new paper book")
    p.add_argument("--snapshot", type=str, default="", help="Write a rich SVG snapshot to this path and exit (after one cycle, or immediately if state exists)")
    p.add_argument("--poll", type=float, default=0, help="Override position-poll interval in seconds")
    return p.parse_args()


class Desk:
    def __init__(self, args: argparse.Namespace) -> None:
        if not config.PAPER_MODE:
            raise SystemExit("Refusing to start: PAPER_MODE is not True")
        if args.balance <= 0:
            raise SystemExit("Starting balance must be positive")

        self.args = args
        self.started_mono = time.time()
        self.started_at = iso()
        self.stop = False
        self.market = Market()
        self.sim = SimFeed()
        self.force_sim = bool(args.sim)
        self.scout = Scout(self.market, self.sim, force_sim=self.force_sim)
        source = self.scout.choose_source()
        self.fallback_note = ""
        if not self.force_sim and source == "SIM":
            self.fallback_note = "LIVE APIs failed — using SIMULATED feed"

        self.logger = Logger(ROOT / "logs")
        self.events: deque[Event] = deque(maxlen=config.EVENT_HISTORY)
        self.rejections: deque[dict] = deque(maxlen=config.REJECTION_HISTORY)
        self.positions: list[Position] = []
        self.closed: list = []
        self.seen: dict[str, float] = {}  # mint -> epoch last processed

        now_day = utcnow().astimezone(TZ).date().isoformat()
        loaded = None if args.fresh else state_store.load()
        if loaded and loaded.get("paper_mode") and not args.fresh:
            self.state = loaded
            # If the user asked for --sim, stay on SIM even if the old run was LIVE
            if args.sim:
                self.state["data_source"] = "SIM"
            self.positions = state_store.positions_from_state(self.state)
            self.closed = state_store.closed_from_state(self.state)
            seen = self.state.get("seen_mints") or {}
            # values may be iso strings from older builds or epoch floats
            for m, v in seen.items():
                try:
                    self.seen[m] = float(v)
                except (TypeError, ValueError):
                    self.seen[m] = time.time()
            self._emit(
                Event(
                    ts=iso(),
                    agent="LOGGER",
                    level="info",
                    message=f"resumed state.json  cash ${self.state['balance']:.2f}  open {len(self.positions)}",
                )
            )
        else:
            self.state = state_store.default_state(args.balance, source, self.started_at, now_day)

        # Keep the live/sim decision from this boot unless we resumed a same-mode book
        self.state["data_source"] = source if (args.sim or not loaded) else self.state.get("data_source", source)
        # If we forced sim, always mark it
        if args.sim:
            self.state["data_source"] = "SIM"

        self.filter = Filter(
            self.market,
            self.sim,
            blacklisted_creators=set(self.state.get("blacklisted_creators") or []),
            observed_liquidity=dict(self.state.get("observed_liquidity") or {}),
            data_source=self.state["data_source"],
        )
        self.analyst = Analyst(self.market, self.sim, data_source=self.state["data_source"])
        self.executor = Executor()
        self.risk = Risk(
            banned_tokens=set(self.state.get("banned_tokens") or []),
            consecutive_losses=int(self.state.get("consecutive_losses") or 0),
            cooldown_until=self.state.get("cooldown_until"),
            day_start_balance=float(self.state.get("day_start_balance") or self.cash),
            day_start_date=self.state.get("day_start_date") or now_day,
        )
        self.last_scout = 0.0
        self.last_poll = 0.0
        self.last_persist = 0.0
        self.maker_memory = dict(self.state.get("maker_memory") or {})
        self.market_note = dict(self.state.get("market") or {})
        cloud = __import__("os").environ.get("SOLDESK_CLOUD") == "1"
        self.poll_sec = args.poll or (
            8.0 if (args.sim or self.state["data_source"] == "SIM") else (20.0 if cloud else config.POSITION_POLL_SEC)
        )
        self.scout_sec = 20.0 if self.state["data_source"] == "SIM" else (20.0 if cloud else config.SCOUT_INTERVAL_SEC)

    # ----- portfolio ---------------------------------------------------
    @property
    def cash(self) -> float:
        return float(self.state["balance"])

    @cash.setter
    def cash(self, value: float) -> None:
        self.state["balance"] = float(value)

    @property
    def equity(self) -> float:
        return self.cash + sum(p.mark_value() for p in self.positions)

    def uptime(self) -> str:
        sec = int(time.time() - self.started_mono)
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def wins(self) -> int:
        return sum(1 for t in self.closed if t.pnl_usd > 0)

    # ----- events / persist -------------------------------------------
    def _emit(self, ev: Event, echo: bool = True) -> None:
        self.events.append(ev)
        if echo and self.args.headless:
            print(self.logger.event_line(ev), flush=True)

    def persist(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_persist < 3:
            return
        self.last_persist = now
        eq = self.equity
        self.state["positions"] = [to_plain(p) for p in self.positions]
        self.state["closed_trades"] = [to_plain(t) for t in self.closed]
        self.state["banned_tokens"] = sorted(self.risk.banned_tokens)
        self.state["blacklisted_creators"] = sorted(self.filter.blacklisted_creators)
        self.state["observed_liquidity"] = dict(self.filter.observed_liquidity)
        self.state["consecutive_losses"] = self.risk.consecutive_losses
        self.state["cooldown_until"] = self.risk.cooldown_until
        self.state["day_start_balance"] = self.risk.day_start_balance
        self.state["day_start_date"] = self.risk.day_start_date
        self.state["equity_peak"] = max(float(self.state.get("equity_peak") or 0), eq)
        hist = list(self.state.get("equity_history") or [])
        hist.append([iso(), eq])
        self.state["equity_history"] = hist[-config.EQUITY_HISTORY_MAX :]
        self.state["seen_mints"] = {m: t for m, t in self.seen.items()}
        self.state["maker_memory"] = self.maker_memory
        self.state["market"] = self.market_note
        self.state["last_equity"] = eq
        self.state["last_update"] = iso()
        self.state["events_tail"] = [to_plain(e) for e in self.events]
        state_store.save(self.state)
        write_status(
            ROOT / "status.json",
            build_status(self.state, list(self.events), list(self.rejections), self.positions, self.closed, self.market_note),
        )
        self.logger.equity(self.cash, eq, len(self.positions), len(self.closed), self.state["data_source"])

    def reject(self, kind: str, mint: str, symbol: str, reasons: list[str], score=None, details=None) -> None:
        d = self.logger.decision(kind, mint, symbol, reasons, score=score, details=details)
        self.rejections.append(
            {
                "ts": d.ts, "kind": kind, "mint": mint, "symbol": symbol,
                "reasons": reasons, "score": score,
                "lane": (details or {}).get("lane") or "glavni",
                "chain": (details or {}).get("chain") or "solana",
            }
        )

    # ----- cycles ------------------------------------------------------
    def scout_cycle(self) -> None:
        cands, ev = self.scout.scan()
        if self.scout.data_source != self.state["data_source"]:
            self.state["data_source"] = self.scout.data_source
            self.filter.data_source = self.scout.data_source
            self.analyst.data_source = self.scout.data_source
            if self.scout.data_source == "SIM":
                self.fallback_note = "LIVE APIs failed — using SIMULATED feed"
        self._emit(ev)

        rugs = self.filter.detect_liquidity_rugs(cands)
        if rugs and self.state["data_source"] == "LIVE":
            # Creator is often unknown until a later report; record the mint as banned at least
            for m in rugs:
                self.risk.banned_tokens.add(m)
            self._emit(
                Event(
                    ts=iso(),
                    agent="FILTER",
                    level="warn",
                    message=f"liquidity collapse >{config.RUG_LIQUIDITY_COLLAPSE_PCT:.0%} on {len(rugs)} mint(s) — banned",
                )
            )

        self._refresh_market(cands)
        now = time.time()
        processed = 0
        this_cycle = 0
        handed = 0
        launch_n = 0
        for cand in cands:
            if self.stop:
                break
            _assign_lane(cand)
            last = self.seen.get(cand.mint)
            wait = config.LAUNCH_RECHECK_SEC if cand.lane == "novi_lans" else RECHECK_SEC
            if last and (now - last) < wait:
                continue
            vol_ok = cand.volume_1h_usd is not None and cand.volume_1h_usd >= config.MIN_VOLUME_1H_USD
            if cand.lane == "novi_lans":
                if self.state["data_source"] == "LIVE" and launch_n >= config.LAUNCH_BATCH:
                    continue
            elif self.state["data_source"] == "LIVE" and vol_ok and processed >= 12:
                continue
            self.process_candidate(cand)
            self.seen[cand.mint] = time.time()
            if cand.lane == "novi_lans":
                launch_n += 1
            elif vol_ok:
                processed += 1
            this_cycle += 1
        if this_cycle:
            handed = this_cycle
        if handed:
            self._emit(
                Event(
                    ts=iso(),
                    agent="SCOUT",
                    level="info",
                    message=f"handed {handed} candidate(s) to FILTER ({processed} glavni, {launch_n} novi lans)",
                )
            )

    def process_candidate(self, cand: Candidate) -> None:
        self._shadow = {"score": None, "hard": False, "pullback": None, "higher": False, "vwap": False, "ema": False}
        try:
            self._process_candidate(cand)
        finally:
            self._watch(cand)

    def _process_candidate(self, cand: Candidate) -> None:
        lane = getattr(cand, "lane", "glavni") or "glavni"
        view = self._apply_overlay()
        result, ev = (self.filter.check_launch(cand) if lane == "novi_lans" else self.filter.check(cand))
        ev.extra = {**(ev.extra or {}), "lane": lane, "chain": getattr(cand, "chain", "solana")}
        self._emit(ev)
        if result.creator and cand.mint in self.risk.banned_tokens:
            # if we already marked this mint as a liquidity rug, ban its creator
            self.filter.blacklisted_creators.add(result.creator)
        if not result.passed:
            self._shadow["hard"] = _hard_kill(result.kill_reasons)
            self.reject("reject_filter", cand.mint, cand.symbol, result.kill_reasons, details={"source": result.data_source, "lane": lane, "chain": getattr(cand, "chain", "solana")})
            return

        # Analyst
        analysis, ev = self.analyst.score(cand, result)
        self._emit(ev)
        self._shadow["score"] = analysis.total
        need, learn_why = learning.threshold(self.state.setdefault("learning", {"buckets": {}}), analysis.total, analysis.structure_label, lane)
        need = max(need, view["min_score"])
        if analysis.passed and analysis.total < need:
            analysis.passed = False
            learn_why = learn_why or f"prag različice {need:.2f}"
            ev = Event(
                ts=iso(), agent="ANALYST", level="kill",
                message=f"{cand.symbol}  {learn_why}",
                mint=cand.mint, symbol=cand.symbol,
            )
            self._emit(ev)
        if not analysis.passed:
            reasons = [
                f"score {analysis.total:.2f} < {need:.2f}",
                analysis.structure_label,
            ] + [f"{c.name}={c.score:.2f} ({c.note})" for c in analysis.components]
            if learn_why:
                reasons.append(learn_why)
            self.reject("reject_analyst", cand.mint, cand.symbol, reasons, score=analysis.total, details={"lane": lane, "chain": getattr(cand, "chain", "solana")})
            return

        tight = _overlay_block(cand, lane, view)
        if tight:
            ev = Event(ts=iso(), agent="ANALYST", level="kill", message=f"{cand.symbol}  {tight}", mint=cand.mint, symbol=cand.symbol, extra={"lane": lane})
            self._emit(ev)
            self.reject("reject_strategy", cand.mint, cand.symbol, [tight], score=analysis.total, details={"lane": lane, "chain": getattr(cand, "chain", "solana")})
            return

        if lane == "novi_lans":
            self._emit(Event(
                ts=iso(), agent="ANALYST", level="info",
                message=f"{cand.symbol}  NOVI LANS  čas vstopa je v pravilih pasu, brez umika glavnega pasu",
                mint=cand.mint, symbol=cand.symbol, extra={"lane": lane},
            ))
        else:
            ok_time, time_reasons, setup = timing_evaluate(
                cand, self.market, self.sim, self.state["data_source"], self.maker_memory,
                pullback_min=view["pullback_min"],
            )
            _note_timing(self._shadow, time_reasons)
            if not ok_time:
                ev = Event(
                    ts=iso(), agent="ANALYST", level="kill",
                    message=f"{cand.symbol}  čas vstopa  {'; '.join(time_reasons)}",
                    mint=cand.mint, symbol=cand.symbol,
                    extra={"reasons": time_reasons, "lane": lane},
                )
                self._emit(ev)
                self.reject("reject_timing", cand.mint, cand.symbol, time_reasons, score=analysis.total, details={"lane": lane, "chain": getattr(cand, "chain", "solana")})
                return
            analysis.structure_label = setup

        pause = self.market_note.get("pause_reason") or ""
        if pause:
            ev = Event(
                ts=iso(), agent="RISK", level="kill",
                message=f"{cand.symbol}  pavza  {pause}",
                mint=cand.mint, symbol=cand.symbol,
            )
            self._emit(ev)
            self.reject("reject_market", cand.mint, cand.symbol, [pause], score=analysis.total, details={"lane": lane, "chain": getattr(cand, "chain", "solana")})
            return

        # Risk gate
        has_open = any(p.mint == cand.mint for p in self.positions)
        ok, why = self.risk.allow_new_entry(cand.mint, self.equity, has_open)
        if not ok:
            ev = Event(ts=iso(), agent="RISK", level="kill", message=f"{cand.symbol}  blocked  {why}", mint=cand.mint, symbol=cand.symbol)
            self._emit(ev)
            self.reject("reject_risk", cand.mint, cand.symbol, [why], score=analysis.total, details={"lane": lane, "chain": getattr(cand, "chain", "solana")})
            return

        # Executor (paper)
        pos, ev = self.executor.try_entry(cand, analysis, self.equity, self.cash, self.positions, mid_price=cand.price_usd)
        self._emit(ev)
        if pos is None:
            self.reject("reject_executor", cand.mint, cand.symbol, [ev.message], score=analysis.total, details={"lane": lane, "chain": getattr(cand, "chain", "solana")})
            return
        self.cash -= pos.size_usd
        self.positions.append(pos)
        self.logger.decision(
            "entry",
            pos.mint,
            pos.symbol,
            pos.entry_reasons,
            score=pos.analyst_score,
            details={"size_usd": pos.size_usd, "fill": pos.fill_price, "paper": True, "pct": pos.size_usd / (pos.size_usd + self.cash + sum(p.mark_value() for p in self.positions))},
        )

    def _apply_overlay(self) -> dict:
        view = strateg.live_view(self.state)
        self.risk.trail_pct = view["trail"]
        self.risk.time_stop_sec = view["time_min"] * 60.0
        self.risk.tp1 = view["tp"]
        return view

    def _watch(self, cand: Candidate) -> None:
        shadow = getattr(self, "_shadow", None) or {}
        buys = cand.buys_5m if cand.buys_5m is not None else cand.buys_1h
        sells = cand.sells_5m if cand.sells_5m is not None else cand.sells_1h
        ratio = buys / (buys + sells) if buys is not None and sells is not None and (buys + sells) > 0 else None
        age = cand.age_seconds()
        buyers = cand.buyers_5m if cand.buyers_5m is not None else cand.buyers_1h
        strateg.observe(
            self.state,
            {
                "mint": cand.mint,
                "symbol": cand.symbol,
                "chain": getattr(cand, "chain", "solana"),
                "lane": getattr(cand, "lane", "glavni"),
                "price": cand.price_usd,
                "score": shadow.get("score"),
                "liquidity": cand.liquidity_usd,
                "volume_1h": cand.volume_1h_usd,
                "buyers": buyers,
                "age_min": None if age is None else age / 60.0,
                "buy_ratio": ratio,
                "pullback": shadow.get("pullback"),
                "higher_low": bool(shadow.get("higher")),
                "vwap_ok": bool(shadow.get("vwap")),
                "ema_ok": bool(shadow.get("ema")),
                "hard_kill": bool(shadow.get("hard")),
            },
            time.time(),
        )

    def _resolve_shadows(self) -> None:
        book = strateg.ensure(self.state)
        now = time.time()
        due = [row for row in book.get("pending") or [] if now >= float(row.get("due") or 0)][:8]
        if not due:
            return
        prices: dict[tuple[str, str], float] = {}
        if self.state.get("data_source") == "SIM":
            got = self.sim.prices_for([row["mint"] for row in due])
            for row in due:
                px = got.get(row["mint"])
                if px:
                    prices[(row.get("chain") or "solana", row["mint"])] = float(px)
        else:
            by_chain: dict[str, list[str]] = {}
            for row in due:
                by_chain.setdefault(row.get("chain") or "solana", []).append(row["mint"])
            for chain, mints in by_chain.items():
                pairs = self.market.dex_tokens(mints, "bsc" if chain == "bsc" else "solana")
                best: dict[str, tuple[float, float]] = {}
                for pair in pairs:
                    got = candidate_from_dex_pair(pair)
                    if not got or got.price_usd <= 0:
                        continue
                    prev = best.get(got.mint)
                    if not prev or got.liquidity_usd > prev[0]:
                        best[got.mint] = (got.liquidity_usd, got.price_usd)
                for mint, (_liq, px) in best.items():
                    prices[(chain, mint)] = px
        pnls = [float(t.pnl_pct) for t in self.closed]
        if pnls:
            book["baseline_expectancy"] = sum(pnls) / len(pnls)
        closed = strateg.resolve(self.state, now, lambda chain, mint: prices.get((chain, mint)))
        if closed:
            self._emit(Event(ts=iso(), agent="LOGGER", level="info", message=f"senca zaprla {closed} izidov, brez kapitala"))

    def poll_positions(self) -> None:
        self._apply_overlay()
        self._resolve_shadows()
        if self.state["data_source"] == "SIM":
            self.sim.tick({p.mint for p in self.positions})
            prices = self.sim.prices_for([p.mint for p in self.positions])
        else:
            prices = self.market.prices_for([p.mint for p in self.positions])
            if len(prices) < len(self.positions):
                missing = [p.symbol for p in self.positions if p.mint not in prices]
                if missing:
                    self._emit(
                        Event(
                            ts=iso(),
                            agent="RISK",
                            level="warn",
                            message=f"ni podatka (price) for {', '.join(missing)} — holds unmarked",
                        )
                    )

        still: list[Position] = []
        for pos in self.positions:
            mid = prices.get(pos.mint)
            if mid is None or mid <= 0:
                still.append(pos)
                continue
            reason = self.risk.check_exit(pos, mid)
            if not reason:
                still.append(pos)
                continue
            if reason.startswith("PARTIAL"):
                trade, ev = self.risk.take_partial(pos, mid, reason)
                self._emit(ev)
                self.cash += trade.proceeds_usd
                self.logger.trade(trade)
                still.append(pos)
                continue
            trade, ev = self.risk.close(pos, mid, reason)
            self._emit(ev)
            self.cash += trade.proceeds_usd
            self.closed.append(trade)
            self.logger.trade(trade)
            learning.note_close(
                self.state.setdefault("learning", {"buckets": {}}),
                pos.analyst_score,
                pos.setup,
                trade.pnl_usd > 0,
                trade.pnl_usd,
                getattr(pos, "lane", "glavni"),
            )
            book = strateg.ensure(self.state)
            rolled = strateg.note_live_result(book, float(trade.pnl_pct))
            if rolled:
                self._emit(Event(ts=iso(), agent="LOGGER", level="warn", message=rolled))
        self.positions = still
        self.risk.rollover_day(self.equity)
        if self.positions:
            bits = "  ".join(f"{p.symbol} {p.mark_pnl_pct():+.2%}{' trail' if p.trail_armed else ''}" for p in self.positions)
            self._emit(
                Event(ts=iso(), agent="RISK", level="info", message=f"marks  {bits}"),
                echo=self.args.headless,
            )

    def _refresh_market(self, cands: list[Candidate]) -> None:
        """Pause new entries when SOL is falling hard or memecoin breadth is weak."""
        sol = None
        if self.state.get("data_source") != "SIM":
            try:
                sol = self.market.sol_change_1h()
            except Exception:
                sol = None
        changes = [c.price_change_1h for c in cands if c.price_change_1h is not None]
        breadth = None
        if len(changes) >= config.BREADTH_MIN_SAMPLE:
            breadth = sum(1 for c in changes if c > 0) / len(changes)
        reason = ""
        if sol is not None and sol <= config.SOL_1H_PAUSE_PCT:
            reason = f"SOL 1h {sol:.1f}% — pavza novih vstopov"
        elif breadth is not None and breadth < config.MIN_BREADTH:
            reason = f"širina memecoinov {breadth:.0%} — pavza novih vstopov"
        self.market_note = {
            "sol_1h": sol,
            "breadth": None if breadth is None else round(breadth, 4),
            "paused": bool(reason),
            "pause_reason": reason,
        }
        if reason:
            self._emit(Event(ts=iso(), agent="RISK", level="warn", message=reason))

    def snapshot_dict(self) -> dict:
        hist = [row[1] for row in (self.state.get("equity_history") or []) if len(row) >= 2]
        if not hist:
            hist = [self.equity]
        cd = self.risk.cooldown_until
        cd_disp = local_ts(cd) if cd and self.risk.cooldown_active() else ""
        return {
            "data_source": self.state["data_source"],
            "fallback_note": self.fallback_note,
            "uptime": self.uptime(),
            "cash": self.cash,
            "equity": self.equity,
            "starting_balance": float(self.state.get("starting_balance") or 0),
            "day_pnl_pct": self.risk.daily_pnl_pct(self.equity),
            "open_n": len(self.positions),
            "closed_n": len(self.closed),
            "wins": self.wins(),
            "positions": [deepcopy(p) for p in self.positions],
            "events": list(self.events),
            "rejections": list(self.rejections),
            "equity_series": hist,
            "daily_brake": self.risk.daily_brake_active(self.equity),
            "cooldown": cd_disp,
        }

    # ----- run ---------------------------------------------------------
    def run(self) -> int:
        self._emit(
            Event(
                ts=iso(),
                agent="LOGGER",
                level="info",
                message=(
                    f"{config.PAPER_BANNER}  source={self.state['data_source']}  "
                    f"cash ${self.cash:.2f}  poll {self.poll_sec:.0f}s"
                ),
            )
        )
        # Stamp status before the first scan so the publisher can show the job is alive.
        self.persist(force=True)
        # First scout immediately so a short --minutes run still does work
        self.scout_cycle()
        self.last_scout = time.time()
        self.poll_positions()
        self.last_poll = time.time()
        self.persist(force=True)

        deadline = None
        if self.args.minutes and self.args.minutes > 0:
            deadline = time.time() + self.args.minutes * 60.0

        if self.args.snapshot and self.args.headless and not self.args.minutes:
            # snapshot-only: one cycle is enough
            return 0

        if self.args.headless:
            return self._run_headless(deadline)
        return self._run_ui(deadline)

    def _should_stop(self, deadline: Optional[float]) -> bool:
        if self.stop:
            return True
        if deadline is not None and time.time() >= deadline:
            return True
        return False

    def _run_headless(self, deadline: Optional[float]) -> int:
        while not self._should_stop(deadline):
            now = time.time()
            if now - self.last_scout >= self.scout_sec:
                self.last_scout = now
                self.scout_cycle()
            if now - self.last_poll >= self.poll_sec:
                self.last_poll = now
                self.poll_positions()
                self.persist()
            time.sleep(0.4)
        self.persist(force=True)
        self._emit(
            Event(
                ts=iso(),
                agent="LOGGER",
                level="info",
                message=(
                    f"stop  equity {_fmt(self.equity)}  open {len(self.positions)}  "
                    f"closed {len(self.closed)}  source={self.state['data_source']}"
                ),
            )
        )
        return 0

    def _run_ui(self, deadline: Optional[float]) -> int:
        from rich.console import Console
        from rich.live import Live

        console = Console()
        view = DeskView(console)
        with Live(view.render(self.snapshot_dict()), console=console, refresh_per_second=4, screen=True) as live:
            while not self._should_stop(deadline):
                now = time.time()
                if now - self.last_scout >= self.scout_sec:
                    self.last_scout = now
                    self.scout_cycle()
                if now - self.last_poll >= self.poll_sec:
                    self.last_poll = now
                    self.poll_positions()
                    self.persist()
                live.update(view.render(self.snapshot_dict()))
                time.sleep(0.25)
        self.persist(force=True)
        console.print(f"[bold]stopped[/]  equity {_fmt(self.equity)}  closed {len(self.closed)}  source={self.state['data_source']}")
        return 0


def _hard_kill(reasons: list[str]) -> bool:
    blob = " ".join(reasons).lower()
    keys = ("honeypot", "freeze", "kuje", "mint", "cannot", "prodati", "proxy", "blacklist", "hidden", "pausable", "cooldown")
    return any(key in blob for key in keys)


def _note_timing(shadow: dict, reasons: list[str]) -> None:
    for part in reasons:
        match = re.search(r"umik\s+(-?\d+(?:[.,]\d+)?)\s*%", part)
        if match:
            shadow["pullback"] = float(match.group(1).replace(",", ".")) / 100.0
        if "višji minimum" in part and "ni višjega" not in part:
            shadow["higher"] = True
        if part.startswith("nad VWAP") or " nad VWAP" in part:
            shadow["vwap"] = True
        if "nad EMA9" in part and "ni nad" not in part:
            shadow["ema"] = True


def _overlay_block(cand: Candidate, lane: str, view: dict) -> Optional[str]:
    """Extra live checks from a promoted overlay. They only reject more."""
    if view["min_liquidity"] and (cand.liquidity_usd or 0) < view["min_liquidity"]:
        return f"likvidnost ${cand.liquidity_usd:,.0f} < ${view['min_liquidity']:,.0f} (strožja različica)"
    if lane == "novi_lans":
        buyers = cand.buyers_5m if cand.buyers_5m is not None else cand.buyers_1h
        if buyers is None or buyers < view["min_buyers"]:
            return f"kupci {buyers if buyers is not None else '—'} < {view['min_buyers']:.0f} (strožja različica)"
        return None
    age = cand.age_seconds()
    if age is None or age < view["min_age_min"] * 60:
        return f"starost pod {view['min_age_min']:.0f} min (strožja različica)"
    if view["min_buy_ratio"] > 0.5:
        buys = cand.buys_5m if cand.buys_5m is not None else cand.buys_1h
        sells = cand.sells_5m if cand.sells_5m is not None else cand.sells_1h
        if buys is None or sells is None or (buys + sells) <= 0 or buys / (buys + sells) < view["min_buy_ratio"]:
            return f"nakup/prodaja pod {view['min_buy_ratio']:.0%} (strožja različica)"
    return None


def _assign_lane(cand: Candidate) -> None:
    """Young coins, bonding curves, and four.meme pre-bond use the launch lane."""
    age = cand.age_seconds()
    dex = (cand.dex or "").lower()
    via = str((cand.raw or {}).get("via") or "")
    bonding = dex in {"pumpfun", "pump.fun"} or (dex.startswith("pump") and "swap" not in dex)
    four = ("four" in dex or "four.meme" in via) and "pancake" not in dex
    young = age is not None and age < config.MIN_TOKEN_AGE_MIN * 60
    fresh = age is None and ("new_pools" in via or four)
    curve = (bonding or four) and (age is None or age < config.LAUNCH_MAX_AGE_MIN * 60)
    cand.lane = "novi_lans" if (young or fresh or curve) else "glavni"


def _fmt(x: float) -> str:
    return f"${x:,.2f}"


def main() -> int:
    args = parse_args()
    desk = Desk(args)

    def _handle(sig, _frame):
        desk.stop = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    try:
        rc = desk.run()
    finally:
        desk.persist(force=True)
        if args.snapshot:
            path = args.snapshot
            if not path.endswith(".svg"):
                # still write svg alongside if they asked for png
                svg_path = str(Path(path).with_suffix(".svg"))
            else:
                svg_path = path
            render_snapshot_svg(desk.snapshot_dict(), svg_path)
            print(f"wrote snapshot {svg_path}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
