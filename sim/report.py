"""End-of-day paper report. Written once after 21:00 Europe/Ljubljana."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def report_due(now: datetime, done: list[str]) -> bool:
    day = now.date().isoformat()
    return now.hour >= 21 and day not in done


def build_report(day: str, st: dict, learning: dict | None, regime: dict | None) -> dict[str, Any]:
    closed = list(st.get("closed") or [])
    today = [t for t in closed if str(t.get("closed_at") or "").startswith(day)]
    pnls = []
    for trade in today:
        try:
            pnls.append((float(trade.get("pnl_usd") or 0), trade))
        except (TypeError, ValueError):
            continue
    best = max(pnls, key=lambda item: item[0])[1] if pnls else None
    worst = min(pnls, key=lambda item: item[0])[1] if pnls else None
    lessons = list((learning or {}).get("lessons") or [])[-12:]
    day_state = st.get("day") or {}
    return {
        "date": day,
        "pnl_usd": round(sum(p for p, _t in pnls), 4),
        "trades": len(today),
        "best": _brief(best),
        "worst": _brief(worst),
        "lessons": lessons,
        "regime": regime or {},
        "brake_events": list(day_state.get("brake_events") or []),
        "scam_rejects": int(day_state.get("scam_rejects") or 0),
        "note": "SIMULACIJA",
    }


def _brief(trade: dict | None) -> dict | None:
    if not trade:
        return None
    return {
        "symbol": trade.get("symbol"),
        "pnl_usd": trade.get("pnl_usd"),
        "pnl_pct": trade.get("pnl_pct"),
        "reason": trade.get("reason"),
        "chain": trade.get("chain"),
        "category": trade.get("category"),
    }


def render_md(report: dict) -> str:
    best = report.get("best") or {}
    worst = report.get("worst") or {}
    lessons = report.get("lessons") or []
    lines = [
        f"# Poročilo {report.get('date')}",
        "",
        "Simulacija, virtualni denar.",
        "",
        f"- Dnevni P/L: {report.get('pnl_usd')} $",
        f"- Posli: {report.get('trades')}",
        f"- Najboljši: {best.get('symbol') or '—'} {best.get('pnl_usd') if best else ''}",
        f"- Najslabši: {worst.get('symbol') or '—'} {worst.get('pnl_usd') if worst else ''}",
        f"- Režim: {report.get('regime')}",
        f"- Zavora: {', '.join(report.get('brake_events') or []) or 'ni'}",
        f"- Zavrnitve prevare: {report.get('scam_rejects')}",
        "",
        "## Lekcije",
        "",
    ]
    if not lessons:
        lines.append("Ni novih lekcij.")
    for row in lessons:
        lines.append(f"- {row.get('text') or ''}")
    lines.append("")
    return "\n".join(lines)


def write_report(directory: Path, report: dict) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    day = report["date"]
    json_path = directory / f"{day}.json"
    md_path = directory / f"{day}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_md(report), encoding="utf-8")
    return f"reports/{day}.json"
