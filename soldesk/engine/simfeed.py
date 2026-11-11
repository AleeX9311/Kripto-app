"""Clearly labelled SIMULATED market feed. Used with --sim or when live APIs fail.

Generates a mix of tokens that fail each filter, a low-scoring pump, and a few
tokens that pass so paper trades and the -6% stop can be verified. Prices are
scripted; they are not a forecast of any real market.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from models import Candidate


def _mint(tag: str) -> str:
    """Deterministic fake base58-ish mint, clearly not a real account."""
    digest = hashlib.sha256(f"SIMULATED::{tag}".encode()).hexdigest()
    # prefix SIM so no one confuses this with a live address
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    body = "".join(alphabet[int(digest[i : i + 2], 16) % len(alphabet)] for i in range(0, 64, 2))
    return ("SIM" + tag.replace("-", "")[:6] + body)[:44]


def _created(minutes_ago: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return ts.isoformat()


class SimFeed:
    """In-memory universe. Tick() advances scripted prices for open interest."""

    def __init__(self, seed: int = 7) -> None:
        self.started = time.time()
        self.tick_n = 0
        self._liq_observe: dict[str, float] = {}
        self.universe = self._build()
        # mint -> price path controller
        self._paths: dict[str, dict] = {}
        for c in self.universe:
            self._paths[c.mint] = {"base": c.price_usd, "kind": c.symbol}

    def _build(self) -> list[Candidate]:
        rows: list[Candidate] = []

        def add(**kw) -> Candidate:
            c = Candidate(**kw, source="SIM")
            rows.append(c)
            return c

        add(
            mint=_mint("MINTAUTH"),
            symbol="SIM-MINT",
            name="Simulated mint authority open",
            pair_address=_mint("PAIR-MINT"),
            dex="raydium",
            price_usd=0.00042,
            liquidity_usd=180_000,
            volume_1h_usd=95_000,
            volume_24h_usd=310_000,
            volume_5m_usd=8_000,
            price_change_5m=1.2,
            price_change_1h=4.0,
            buys_1h=210,
            sells_1h=180,
            buyers_1h=90,
            sellers_1h=70,
            pair_created_at=_created(180),
            fdv_usd=900_000,
        )
        add(
            mint=_mint("FREEZE"),
            symbol="SIM-FRZ",
            name="Simulated freeze authority set",
            pair_address=_mint("PAIR-FRZ"),
            dex="pumpswap",
            price_usd=0.0011,
            liquidity_usd=210_000,
            volume_1h_usd=120_000,
            volume_24h_usd=400_000,
            volume_5m_usd=9_500,
            price_change_5m=-0.8,
            price_change_1h=3.1,
            buys_1h=150,
            sells_1h=140,
            buyers_1h=60,
            sellers_1h=55,
            pair_created_at=_created(240),
        )
        add(
            mint=_mint("LPOPEN"),
            symbol="SIM-LP",
            name="Simulated unlocked LP",
            pair_address=_mint("PAIR-LP"),
            dex="raydium",
            price_usd=0.00077,
            liquidity_usd=150_000,
            volume_1h_usd=80_000,
            volume_24h_usd=220_000,
            volume_5m_usd=6_000,
            price_change_5m=0.4,
            price_change_1h=2.0,
            buys_1h=100,
            sells_1h=90,
            buyers_1h=40,
            sellers_1h=38,
            pair_created_at=_created(400),
        )
        add(
            mint=_mint("WHALES"),
            symbol="SIM-WHL",
            name="Simulated concentrated holders",
            pair_address=_mint("PAIR-WHL"),
            dex="raydium",
            price_usd=0.0025,
            liquidity_usd=300_000,
            volume_1h_usd=140_000,
            volume_24h_usd=500_000,
            volume_5m_usd=12_000,
            price_change_5m=2.0,
            price_change_1h=6.0,
            buys_1h=260,
            sells_1h=200,
            buyers_1h=110,
            sellers_1h=90,
            pair_created_at=_created(600),
        )
        add(
            mint=_mint("LOWVOL"),
            symbol="SIM-VOL",
            name="Simulated thin volume",
            pair_address=_mint("PAIR-VOL"),
            dex="pumpfun",
            price_usd=0.00003,
            liquidity_usd=40_000,
            volume_1h_usd=12_400,
            volume_24h_usd=28_000,
            volume_5m_usd=900,
            price_change_5m=8.0,
            price_change_1h=22.0,
            buys_1h=40,
            sells_1h=25,
            buyers_1h=20,
            sellers_1h=12,
            pair_created_at=_created(25),
        )
        add(
            mint=_mint("DEVRUG"),
            symbol="SIM-RUG",
            name="Simulated creator with prior rugs",
            pair_address=_mint("PAIR-RUG"),
            dex="pumpswap",
            price_usd=0.00019,
            liquidity_usd=260_000,
            volume_1h_usd=200_000,
            volume_24h_usd=800_000,
            volume_5m_usd=20_000,
            price_change_5m=5.0,
            price_change_1h=18.0,
            buys_1h=400,
            sells_1h=280,
            buyers_1h=180,
            sellers_1h=100,
            pair_created_at=_created(90),
        )
        add(
            mint=_mint("NODATA"),
            symbol="SIM-NA",
            name="Simulated missing safety data",
            pair_address=_mint("PAIR-NA"),
            dex="raydium",
            price_usd=0.0005,
            liquidity_usd=190_000,
            volume_1h_usd=70_000,
            volume_24h_usd=210_000,
            volume_5m_usd=5_000,
            price_change_5m=0.0,
            price_change_1h=1.0,
            buys_1h=80,
            sells_1h=80,
            buyers_1h=30,
            sellers_1h=30,
            pair_created_at=_created(200),
        )
        add(
            mint=_mint("PUMPCHASE"),
            symbol="SIM-PMP",
            name="Simulated vertical pump (low structure)",
            pair_address=_mint("PAIR-PMP"),
            dex="raydium",
            price_usd=0.0088,
            liquidity_usd=420_000,
            volume_1h_usd=1_200_000,
            volume_24h_usd=2_400_000,
            volume_5m_usd=280_000,
            price_change_5m=62.0,
            price_change_1h=140.0,
            buys_1h=900,
            sells_1h=200,
            buyers_1h=400,
            sellers_1h=80,
            pair_created_at=_created(50),
            fdv_usd=8_800_000,
        )
        # Passes filters, good structure (pullback / higher low) — then sold into a decline
        add(
            mint=_mint("PASS1"),
            symbol="SIM-P1",
            name="Simulated higher-low setup",
            pair_address=_mint("PAIR-P1"),
            dex="raydium",
            price_usd=1.00,
            liquidity_usd=650_000,
            volume_1h_usd=310_000,
            volume_24h_usd=1_200_000,
            volume_5m_usd=22_000,
            price_change_5m=-3.5,
            price_change_1h=8.0,
            price_change_6h=14.0,
            buys_1h=420,
            sells_1h=310,
            buyers_1h=190,
            sellers_1h=140,
            pair_created_at=_created(36 * 60),
            fdv_usd=2_400_000,
        )
        # Passes filters, then trends up so the trailing stop can arm
        add(
            mint=_mint("PASS2"),
            symbol="SIM-P2",
            name="Simulated steady bid",
            pair_address=_mint("PAIR-P2"),
            dex="pumpswap",
            price_usd=0.50,
            liquidity_usd=800_000,
            volume_1h_usd=400_000,
            volume_24h_usd=1_500_000,
            volume_5m_usd=30_000,
            price_change_5m=1.1,
            price_change_1h=5.5,
            price_change_6h=9.0,
            buys_1h=500,
            sells_1h=360,
            buyers_1h=220,
            sellers_1h=150,
            pair_created_at=_created(48 * 60),
            fdv_usd=3_100_000,
        )
        return rows

    def candidates(self) -> list[Candidate]:
        # Prices already updated in-place by tick()
        # Re-list a "new" thin volume token after a few ticks so scout stays active
        return list(self.universe)

    def tick(self, open_mints: Optional[set[str]] = None) -> None:
        self.tick_n += 1
        t = self.tick_n
        for c in self.universe:
            kind = c.symbol
            if kind == "SIM-P1":
                # Decline ~1.2% per tick from the fill area so the hard stop triggers
                c.price_usd = max(0.0001, 1.00 * (0.988 ** t))
                c.price_change_5m = -1.2 * min(t, 8)
                c.price_change_1h = 8.0 - 1.5 * t
            elif kind == "SIM-P2":
                # Climb toward +16% in 5 ticks so the trail can arm after +12%,
                # then fade so a 6% trailing stop is hit before a short --minutes run ends.
                if t <= 5:
                    c.price_usd = 0.50 * (1.0 + 0.032 * t)  # +16% at t=5
                else:
                    peak = 0.50 * (1.0 + 0.032 * 5)
                    c.price_usd = peak * (0.96 ** (t - 5))  # ~4% off peak per tick
                c.price_change_5m = 3.0 if t <= 5 else -4.0
                c.price_change_1h = (c.price_usd / 0.50 - 1) * 100
            elif kind == "SIM-PMP":
                c.price_usd *= 1.01
            else:
                # mild noise, stays away from looking like a signal
                c.price_usd *= 1.0 + 0.001 * math.sin(t + len(kind))

    def price(self, mint: str) -> Optional[float]:
        for c in self.universe:
            if c.mint == mint:
                return c.price_usd
        return None

    def prices_for(self, mints: list[str]) -> dict[str, float]:
        out = {}
        for m in mints:
            px = self.price(m)
            if px is not None:
                out[m] = px
        return out

    def find(self, mint: str) -> Optional[Candidate]:
        for c in self.universe:
            if c.mint == mint:
                return c
        return None

    # Simulated rugcheck-style reports so Filter can exercise every branch
    def safety_report(self, mint: str) -> Optional[dict]:
        c = self.find(mint)
        if not c:
            return None
        sym = c.symbol
        if sym == "SIM-NA":
            return None  # missing data -> ni podatka
        creator = {
            "SIM-MINT": "SIMCREATOR_MINTAUTH_XXXX",
            "SIM-FRZ": "SIMCREATOR_FREEZE_XXXXXX",
            "SIM-LP": "SIMCREATOR_UNLOCKED_XXXXX",
            "SIM-WHL": "SIMCREATOR_WHALES_XXXXXX",
            "SIM-VOL": "SIMCREATOR_THINVOL_XXXXX",
            "SIM-RUG": "SIMCREATOR_KNOWNRUG_XXXX",
            "SIM-PMP": "SIMCREATOR_CLEAN_PUMP_XX",
            "SIM-P1": "SIMCREATOR_CLEAN_PASS1_X",
            "SIM-P2": "SIMCREATOR_CLEAN_PASS2_X",
        }.get(sym, "SIMCREATOR_GENERIC_XXXX")
        mint_set = sym == "SIM-MINT"
        freeze_set = sym == "SIM-FRZ"
        lp_locked = sym != "SIM-LP"
        top10 = 45.0 if sym == "SIM-WHL" else 12.5
        prior_rugs = 3 if sym == "SIM-RUG" else 0
        return {
            "simulated": True,
            "mint": mint,
            "creator": creator,
            "mint_authority_set": mint_set,
            "freeze_authority_set": freeze_set,
            "lp_locked_or_burned": lp_locked,
            "top10_pct": top10,
            "prior_rugs": prior_rugs,
            "risks": (["creator rugged previous tokens"] if prior_rugs else []),
        }

    def ohlcv(self, mint: str) -> Optional[list[list]]:
        c = self.find(mint)
        if not c:
            return None
        # Build 80 five-minute candles ending now. Values describe STRUCTURE only.
        now = int(time.time())
        step = 300
        candles = []
        px = c.price_usd
        if c.symbol == "SIM-PMP":
            # almost-vertical series of green candles
            price = px / (1.08 ** 20)
            for i in range(80):
                ts = now - (79 - i) * step
                o = price
                price = price * 1.08 if i >= 60 else price * 1.005
                candles.append([ts, o, price, o, price, 1000])
        elif c.symbol in {"SIM-P1", "SIM-P2"}:
            # dip, higher low, modest bounce — not a vertical extension
            for i in range(80):
                ts = now - (79 - i) * step
                # two swing lows: first at i=40, higher low at i=60
                wave = 1.0
                if i < 40:
                    wave = 1.0 - 0.18 * (i / 40)
                elif i < 60:
                    wave = 0.82 + 0.10 * ((i - 40) / 20)
                else:
                    wave = 0.92 - 0.04 * ((i - 60) / 20)  # pullback, still above 0.82
                o = px * wave
                candles.append([ts, o, o * 1.01, o * 0.99, o, 800])
        else:
            for i in range(80):
                ts = now - (79 - i) * step
                o = px * (0.98 + 0.0003 * i)
                candles.append([ts, o, o * 1.005, o * 0.995, o, 200])
        return candles
