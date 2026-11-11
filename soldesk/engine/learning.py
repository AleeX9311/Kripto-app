"""Per-setup trust. A weak bucket can only raise the analyst floor."""

from __future__ import annotations

import config


def bucket_key(score: float, setup: str) -> str:
    lo = max(0.0, min(0.9, int(float(score) * 10) / 10.0))
    return f"{lo:.2f}-{lo + 0.10:.2f}|{setup or 'drugo'}"


def note_close(book: dict, score: float, setup: str, won: bool, pnl_usd: float) -> None:
    stats = book.setdefault("buckets", {})
    key = bucket_key(score, setup)
    row = stats.setdefault(key, {"wins": 0, "losses": 0, "pnl": 0.0})
    if won:
        row["wins"] = int(row["wins"]) + 1
    else:
        row["losses"] = int(row["losses"]) + 1
    row["pnl"] = round(float(row["pnl"]) + float(pnl_usd), 4)


def threshold(book: dict, score: float, setup: str) -> tuple[float, str]:
    """Return (required score, reason). Never below the config floor."""
    base = float(config.MIN_ANALYST_SCORE)
    row = (book.get("buckets") or {}).get(bucket_key(score, setup))
    if not row:
        return base, ""
    n = int(row.get("wins") or 0) + int(row.get("losses") or 0)
    if n < int(config.LEARN_MIN_TRADES):
        return base, ""
    rate = int(row.get("wins") or 0) / n
    if rate >= float(config.LEARN_LOSE_WIN_RATE):
        return base, ""
    raised = min(float(config.LEARN_SCORE_CAP), base + float(config.LEARN_SCORE_STEP))
    return raised, f"vzorec {setup or 'drugo'} {rate:.0%} na {n} poslih — prag {raised:.2f}"


def public_rows(book: dict) -> list[dict]:
    rows = []
    for key, row in (book.get("buckets") or {}).items():
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        n = wins + losses
        rows.append({
            "bucket": key,
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 4) if n else None,
            "pnl": round(float(row.get("pnl") or 0), 2),
        })
    rows.sort(key=lambda item: (-item["n"], item["bucket"]))
    return rows
