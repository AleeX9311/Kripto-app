"""Scout — scan new Solana pairs on Pump.fun / Raydium / PumpSwap via public APIs."""

from __future__ import annotations

from typing import Optional

import config
from market import Market, candidate_from_dex_pair
from models import Candidate, Event, iso
from simfeed import SimFeed


def chain_count(rows: list[Candidate], chain: str) -> int:
    return sum(1 for row in rows if getattr(row, "chain", "solana") == chain)


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
            message=f"LIVE scan  {len(found)} parov  SOL {chain_count(found, 'solana')} · BNB {chain_count(found, 'bsc')}",
        )
        return found, ev

    def _scan_live(self) -> list[Candidate]:
        found: list[Candidate] = []
        errors = []
        for chain, hints in (
            ("solana", ("pump", "raydium", "moonshot", "launch")),
            ("bsc", ("four", "pancake", "cake")),
        ):
            try:
                found.extend(self._scan_chain(chain, hints))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{chain}: {exc}")
        if chain_count(found, "bsc") == 0:
            try:
                found.extend(self.market.fourmeme_launches())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"four.meme: {exc}")
        if errors:
            self.last_error = "; ".join(errors)
        elif not found:
            self.last_error = self.market.last_error or "empty live scan"
        return found

    def _scan_chain(self, chain: str, hints: tuple[str, ...]) -> list[Candidate]:
        network = "bsc" if chain == "bsc" else "solana"
        by_mint: dict[str, Candidate] = {}

        def add(cands: list[Candidate]) -> None:
            for c in cands:
                if not c or c.chain != chain or not c.mint or c.mint.lower() in {q.lower() for q in config.QUOTE_MINTS}:
                    continue
                sym = (c.symbol or "").strip()
                name = (c.name or "").strip().lower()
                if sym in config.SKIP_SYMBOLS or name in config.SKIP_NAMES:
                    continue
                prev = by_mint.get(c.mint.lower())
                if not prev or (c.volume_1h_usd, c.liquidity_usd) > (prev.volume_1h_usd, prev.liquidity_usd):
                    by_mint[c.mint.lower()] = c

        add(self.market.candidates_from_gecko("new_pools", network))
        add(self.market.candidates_from_gecko("trending_pools", network))
        profiles = self.market.dex_token_profiles_latest(chain)
        mints = [p.get("tokenAddress") for p in profiles if p.get("tokenAddress")]
        if mints:
            add(self.market.hydrate_candidates(mints[:30], chain))
        query = "four.meme" if chain == "bsc" else "pump"
        add([c for p in self.market.dex_search(query, chain) if (c := candidate_from_dex_pair(p))])
        if chain == "bsc":
            add(self.market.fourmeme_launches())
            add([c for p in self.market.dex_search("pancake", chain) if (c := candidate_from_dex_pair(p))])

        def _key(c: Candidate):
            preferred = 0 if any(x in (c.dex or "") for x in hints) else 1
            return (preferred, -c.volume_1h_usd, -c.liquidity_usd)

        ordered = sorted(by_mint.values(), key=_key)
        fresh = sorted(by_mint.values(), key=lambda c: c.age_seconds() if c.age_seconds() is not None else 10**12)
        picked = []
        seen = set()
        for c in fresh[:6] + ordered:
            if c.mint.lower() in seen:
                continue
            seen.add(c.mint.lower())
            picked.append(c)
            if len(picked) >= config.MAX_SCOUT_PER_CHAIN:
                break
        return picked
