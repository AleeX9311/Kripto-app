"""Persist desk state to state.json so a restart resumes cleanly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models import ClosedTrade, Position, to_plain


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"


def default_state(starting_balance: float, data_source: str, started_at: str, day: str) -> dict[str, Any]:
    return {
        "balance": starting_balance,  # cash
        "starting_balance": starting_balance,
        "day_start_balance": starting_balance,
        "day_start_date": day,
        "equity_peak": starting_balance,
        "positions": [],
        "closed_trades": [],
        "banned_tokens": [],  # never re-enter after a stop
        "blacklisted_creators": [],
        "observed_liquidity": {},  # mint -> first (or peak) liquidity seen
        "consecutive_losses": 0,
        "cooldown_until": None,
        "seen_mints": {},  # mint -> last processed iso ts
        "equity_history": [],  # [iso, equity]
        "events_tail": [],
        "learning": {"buckets": {}},
        "maker_memory": {},
        "market": {},
        "started_at": started_at,
        "data_source": data_source,
        "paper_mode": True,
    }


def load(path: Path = STATE_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open() as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def save(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    tmp = path.with_suffix(".json.tmp")
    serializable = to_plain(state)
    with tmp.open("w") as f:
        json.dump(serializable, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def positions_from_state(state: dict[str, Any]) -> list[Position]:
    out: list[Position] = []
    for raw in state.get("positions") or []:
        try:
            out.append(Position(**{k: raw[k] for k in Position.__dataclass_fields__ if k in raw}))
        except TypeError:
            continue
    return out


def closed_from_state(state: dict[str, Any]) -> list[ClosedTrade]:
    out: list[ClosedTrade] = []
    for raw in state.get("closed_trades") or []:
        try:
            out.append(ClosedTrade(**{k: raw[k] for k in ClosedTrade.__dataclass_fields__ if k in raw}))
        except TypeError:
            continue
    return out
