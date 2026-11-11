"""Scout — scan new Solana pairs on Pump.fun / Raydium / PumpSwap via public APIs."""

from __future__ import annotations

from typing import Optional

import config
from market import Market, candidate_from_dex_pair
from models import Candidate, Event, iso
from simfeed import SimFeed


class Scout:
    def __init__(self, market: Market, sim: Optional[SimFeed] = None, force_sim: bool = False) -> None:
        self.market = market
        self.sim = sim
        self.force_sim = force_sim
        self.data_source = "SIM" if force_sim else "LIVE"
        self.last_error: Optional[str] = None
        self.last_count = 0

    def choose_source(self) -> str:
        if self.force_sim:
            self.data_source = "SIM"
            return "SIM"
        if self.market.ping():
            self.data_source = "LIVE"
            return "LIVE"
        self.data_source = "SIM"
        self.last_error = self.market.last_error or "live APIs unreachable"
        return "SIM"

    def scan(self) -> tuple[list[Candidate], Event]:
        if self.data_source == "SIM" or self.force_sim:
            assert self.sim is not None, "sim feed required"
            found = self.sim.candidates()
            self.last_count = len(found)
            ev = Event(
                ts=iso(),
                agent="SCOUT",
                level="info",
                message=f"SIMULATED feed  {len(found)} pairs  (not live market data)",
            )
            return found, ev

        found = self._scan_live()
        if not found:
            # Network failed mid-run — fall back clearly labelled
            if self.sim is not None:
                self.data_source = "SIM"
                found = self.sim.candidates()
                self.last_count = len(found)
                ev = Event(
                    ts=iso(),
                    agent="SCOUT",
                    level="warn",
                    message=f"LIVE failed ({self.last_error or self.market.last_error or 'no data'}); fallback SIMULATED feed  {len(found)} pairs",
                )
                return found, ev
        self.last_count = len(found)
        ev = Event(
            ts=iso(),
            agent="SCOUT",
            level="ok",
            message=f"LIVE scan  {len(found)} unique Solana pairs (Pump.fun / Raydium / PumpSwap / others)",
        )
        return found, ev

    def _scan_live(self) -> list[Candidate]:
        by_mint: dict[str, Candidate] = {}
        errors = []

        def _usable(c: Candidate) -> bool:
            if not c.mint or c.mint in config.QUOTE_MINTS:
                return False
            sym = (c.symbol or "").strip()
            name = (c.name or "").strip().lower()
            if sym in config.SKIP_SYMBOLS or name in config.SKIP_NAMES:
                return False
            return True

        def add(cands: list[Candidate]) -> None:
            for c in cands:
                if not _usable(c):
                    continue
                prev = by_mint.get(c.mint)
                if not prev or (c.volume_1h_usd, c.liquidity_usd) > (prev.volume_1h_usd, prev.liquidity_usd):
                    by_mint[c.mint] = c

        try:
            add(self.market.candidates_from_gecko("new_pools"))
        except Exception as exc:  # noqa: BLE001 — scout must never crash the desk
            errors.append(f"gecko new_pools: {exc}")
        try:
            add(self.market.candidates_from_gecko("trending_pools"))
        except Exception as exc:
            errors.append(f"gecko trending: {exc}")

        try:
            profiles = self.market.dex_token_profiles_latest()
            mints = [p.get("tokenAddress") for p in profiles if p.get("tokenAddress")]
            if mints:
                add(self.market.hydrate_candidates(mints[:40]))
        except Exception as exc:
            errors.append(f"dex profiles: {exc}")

        try:
            # Avoid a broad "sol" search (it is dominated by SOL/USDC CLMM pools).
            pairs = self.market.dex_search("pump")
            add([c for p in pairs if (c := candidate_from_dex_pair(p))])
        except Exception as exc:
            errors.append(f"dex search: {exc}")

        if errors:
            self.last_error = "; ".join(errors)
        elif not by_mint:
            self.last_error = self.market.last_error or "empty live scan"

        # Prefer dexes named in the brief, ranked by 1h volume then liquidity
        def _key(c: Candidate):
            preferred = 0 if any(x in (c.dex or "") for x in ("pump", "raydium", "moonshot", "launch")) else 1
            return (preferred, -c.volume_1h_usd, -c.liquidity_usd)

        ordered = sorted(by_mint.values(), key=_key)
        return ordered[: config.MAX_SCOUT_CANDIDATES]
