"""Shared dataclasses for the paper-trading desk."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[datetime] = None) -> str:
    return (ts or utcnow()).isoformat()


def to_plain(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


@dataclass
class Candidate:
    mint: str
    symbol: str
    name: str
    pair_address: str
    dex: str
    price_usd: float
    liquidity_usd: float
    volume_1h_usd: float
    volume_24h_usd: float = 0.0
    volume_5m_usd: float = 0.0
    buys_5m: Optional[int] = None
    sells_5m: Optional[int] = None
    buyers_5m: Optional[int] = None
    price_change_5m: Optional[float] = None  # percent
    price_change_1h: Optional[float] = None
    price_change_6h: Optional[float] = None
    price_change_24h: Optional[float] = None
    buys_1h: Optional[int] = None
    sells_1h: Optional[int] = None
    buyers_1h: Optional[int] = None
    sellers_1h: Optional[int] = None
    pair_created_at: Optional[str] = None  # iso
    fdv_usd: Optional[float] = None
    quote_mint: Optional[str] = None
    source: str = "LIVE"  # LIVE or SIM
    lane: str = "glavni"  # glavni | novi_lans
    raw: dict = field(default_factory=dict)

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        if not self.pair_created_at:
            return None
        try:
            created = datetime.fromisoformat(self.pair_created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return max(0.0, ((now or utcnow()) - created).total_seconds())
        except (TypeError, ValueError):
            return None


@dataclass
class FilterCheck:
    name: str
    passed: bool
    reason: str
    detail: Any = None


@dataclass
class FilterResult:
    mint: str
    symbol: str
    passed: bool
    checks: list[FilterCheck] = field(default_factory=list)
    creator: Optional[str] = None
    top10_pct: Optional[float] = None
    mint_revoked: Optional[bool] = None
    freeze_set: Optional[bool] = None
    lp_locked_or_burned: Optional[bool] = None
    volume_1h_usd: Optional[float] = None
    data_source: str = ""

    @property
    def kill_reasons(self) -> list[str]:
        return [c.reason for c in self.checks if not c.passed]


@dataclass
class ScoreComponent:
    name: str
    weight: float
    score: float  # 0..1
    note: str


@dataclass
class AnalystResult:
    mint: str
    symbol: str
    total: float  # 0..1
    passed: bool
    components: list[ScoreComponent] = field(default_factory=list)
    structure_label: str = ""

    def component_map(self) -> dict[str, float]:
        return {c.name: round(c.score, 4) for c in self.components}


@dataclass
class Position:
    mint: str
    symbol: str
    pair_address: str
    dex: str
    qty: float
    fill_price: float
    size_usd: float  # cash spent (incl. slip+fee)
    opened_at: str
    peak_price: float
    last_price: float
    trail_armed: bool = False
    trail_stop_price: Optional[float] = None
    hard_stop_price: float = 0.0
    source: str = "LIVE"
    analyst_score: float = 0.0
    entry_reasons: list[str] = field(default_factory=list)
    original_qty: float = 0.0
    original_size_usd: float = 0.0
    banked_pnl_usd: float = 0.0
    tp1_done: bool = False
    tp2_done: bool = False
    breakeven: bool = False
    setup: str = ""
    lane: str = "glavni"

    def mark_pnl_pct(self, price: Optional[float] = None) -> float:
        px = self.fill_price
        if px <= 0:
            return 0.0
        return ((price if price is not None else self.last_price) - px) / px

    def mark_value(self, price: Optional[float] = None) -> float:
        return self.qty * (price if price is not None else self.last_price)


@dataclass
class ClosedTrade:
    mint: str
    symbol: str
    side: str  # SELL
    qty: float
    entry_price: float
    exit_price: float  # mid at signal
    fill_exit_price: float  # after slip+fee
    size_usd: float
    proceeds_usd: float
    pnl_usd: float
    pnl_pct: float
    reason: str
    opened_at: str
    closed_at: str
    source: str = "LIVE"
    analyst_score: float = 0.0
    lane: str = "glavni"


@dataclass
class Event:
    ts: str
    agent: str  # SCOUT FILTER ANALYST EXECUTOR RISK LOGGER
    level: str  # info ok warn kill trade
    message: str
    mint: str = ""
    symbol: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Decision:
    ts: str
    kind: str  # reject_filter, reject_analyst, reject_risk, reject_executor, entry, exit
    mint: str
    symbol: str
    reasons: list[str]
    score: Optional[float] = None
    details: dict = field(default_factory=dict)
