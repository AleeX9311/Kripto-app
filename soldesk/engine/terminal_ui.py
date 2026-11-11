"""Rich live terminal for the paper-trading desk."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

import config
from models import Event, Position

TZ = ZoneInfo(config.TIMEZONE)

AGENT_STYLE = {
    "SCOUT": "cyan",
    "FILTER": "yellow",
    "ANALYST": "magenta",
    "EXECUTOR": "green",
    "RISK": "red",
    "LOGGER": "blue",
}

LEVEL_STYLE = {
    "ok": "green",
    "info": "cyan",
    "warn": "yellow",
    "kill": "red",
    "trade": "bold green",
}


def local_ts(iso_ts: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(TZ).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return iso_ts[11:19] if len(iso_ts) >= 19 else iso_ts


def sparkline(values: list[float], width: int = 48) -> str:
    if not values:
        return "·" * min(width, 8)
    bars = "▁▂▃▄▅▆▇█"
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = list(values)
        if len(sampled) < width:
            sampled = [sampled[0]] * (width - len(sampled)) + sampled
    lo, hi = min(sampled), max(sampled)
    span = hi - lo
    if span <= 0:
        return bars[3] * len(sampled)
    out = []
    for v in sampled:
        idx = int((v - lo) / span * (len(bars) - 1))
        out.append(bars[max(0, min(len(bars) - 1, idx))])
    return "".join(out)


def _fmt_usd(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _fmt_px(x: float) -> str:
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.4f}"
    if x >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8g}"


def _pnl_style(pct: float) -> str:
    if pct > 0.001:
        return "green"
    if pct < -0.001:
        return "red"
    return "white"


class DeskView:
    def __init__(self, console: Optional[Console] = None) -> None:
        self.console = console or Console()

    def render(self, snap: dict[str, Any]) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._header(snap), name="header", size=7),
            Layout(name="mid"),
            Layout(self._rejections(snap), name="rej", size=10),
            Layout(self._equity(snap), name="eq", size=5),
        )
        layout["mid"].split_row(
            Layout(self._activity(snap), name="act"),
            Layout(self._positions(snap), name="pos"),
        )
        return layout

    def _header(self, snap: dict[str, Any]) -> Panel:
        source = snap.get("data_source", "?")
        src_style = "bold green" if source == "LIVE" else "bold yellow"
        banner = Text(config.PAPER_BANNER, style="bold white on dark_red")
        title = Text()
        title.append("  SOLANA MEMECOIN DESK", style="bold white")
        title.append("   ·   6 agents, hard handoffs   ·   ", style="dim")
        title.append(f"data {source}", style=src_style)
        title.append(f"   uptime {snap.get('uptime', '—')}", style="dim")
        if snap.get("fallback_note"):
            title.append(f"   {snap['fallback_note']}", style="yellow")

        cash = snap["cash"]
        equity = snap["equity"]
        start = snap["starting_balance"]
        pnl = (equity - start) / start if start else 0.0
        day_pnl = snap.get("day_pnl_pct") or 0.0
        closed = snap.get("closed_n") or 0
        wins = snap.get("wins") or 0
        wr = (wins / closed * 100) if closed else 0.0

        stats = Table.grid(expand=True, padding=(0, 2))
        stats.add_column(ratio=1)
        stats.add_column(ratio=1)
        stats.add_column(ratio=1)
        stats.add_column(ratio=1)
        stats.add_column(ratio=1)
        stats.add_column(ratio=1)
        stats.add_row(
            Text.from_markup(f"[dim]CASH[/]\n[bold]{_fmt_usd(cash)}[/]"),
            Text.from_markup(f"[dim]EQUITY[/]\n[bold]{_fmt_usd(equity)}[/]"),
            Text.from_markup(f"[dim]PnL[/]\n[{_pnl_style(pnl)}]{pnl:+.2%}[/]"),
            Text.from_markup(f"[dim]DAY[/]\n[{_pnl_style(day_pnl)}]{day_pnl:+.2%}[/]"),
            Text.from_markup(f"[dim]OPEN / CLOSED[/]\n[bold]{snap.get('open_n', 0)}[/] / {closed}"),
            Text.from_markup(f"[dim]WIN RATE[/]\n[bold]{wr:.0f}%[/]  ({wins}W/{max(0, closed-wins)}L)"),
        )
        flags = []
        if snap.get("daily_brake"):
            flags.append("[bold red]DAILY LOSS BRAKE ON[/]")
        if snap.get("cooldown"):
            flags.append(f"[yellow]COOLDOWN until {snap['cooldown']}[/]")
        flag_line = Text.from_markup("  ".join(flags)) if flags else Text("capital preservation first · no forecasts", style="dim")
        body = Group(Align.center(banner), Align.center(title), Rule(style="dim"), stats, flag_line)
        return Panel(body, box=box.HEAVY, border_style="red", padding=(0, 1))

    def _activity(self, snap: dict[str, Any]) -> Panel:
        events: list[Event] = snap.get("events") or []
        table = Table(expand=True, box=box.SIMPLE, show_header=True, header_style="dim", pad_edge=False)
        table.add_column("time", width=8, no_wrap=True)
        table.add_column("agent", width=9, no_wrap=True)
        table.add_column("activity", ratio=1, overflow="ellipsis")
        # last N, newest at bottom
        for ev in events[-16:]:
            style = LEVEL_STYLE.get(ev.level, "white")
            agent_style = AGENT_STYLE.get(ev.agent, "white")
            table.add_row(
                Text(local_ts(ev.ts), style="dim"),
                Text(ev.agent, style=agent_style),
                Text(ev.message, style=style),
            )
        return Panel(table, title="AGENT ACTIVITY", title_align="left", border_style="cyan", box=box.ROUNDED)

    def _positions(self, snap: dict[str, Any]) -> Panel:
        positions: list[Position] = snap.get("positions") or []
        table = Table(expand=True, box=box.SIMPLE, show_header=True, header_style="dim", pad_edge=False)
        table.add_column("token", width=9, no_wrap=True)
        table.add_column("entry", justify="right")
        table.add_column("now", justify="right")
        table.add_column("pnl", justify="right")
        table.add_column("stop", justify="right")
        table.add_column("trail", justify="center")
        if not positions:
            table.add_row("—", "—", "—", "—", "—", "—")
        for p in positions:
            pnl = p.mark_pnl_pct()
            trail = "ARMED" if p.trail_armed else "off"
            trail_style = "bold green" if p.trail_armed else "dim"
            table.add_row(
                Text(p.symbol, style="bold"),
                _fmt_px(p.fill_price),
                _fmt_px(p.last_price),
                Text(f"{pnl:+.2%}", style=_pnl_style(pnl)),
                Text(_fmt_px(p.hard_stop_price), style="red"),
                Text(trail, style=trail_style),
            )
        return Panel(table, title="OPEN POSITIONS (paper)", title_align="left", border_style="green", box=box.ROUNDED)

    def _rejections(self, snap: dict[str, Any]) -> Panel:
        rows: list[dict] = snap.get("rejections") or []
        table = Table(expand=True, box=box.SIMPLE, show_header=True, header_style="dim", pad_edge=False)
        table.add_column("time", width=8, no_wrap=True)
        table.add_column("kind", width=16, no_wrap=True)
        table.add_column("token", width=10, no_wrap=True)
        table.add_column("reason", ratio=1, overflow="ellipsis")
        if not rows:
            table.add_row("—", "—", "—", "waiting for the first filter pass")
        for r in rows[-8:]:
            table.add_row(
                Text(local_ts(r.get("ts", "")), style="dim"),
                Text(r.get("kind", ""), style="yellow"),
                r.get("symbol") or "",
                Text("; ".join(r.get("reasons") or []), style="red"),
            )
        return Panel(table, title="RECENT REJECTIONS", title_align="left", border_style="yellow", box=box.ROUNDED)

    def _equity(self, snap: dict[str, Any]) -> Panel:
        hist: list[float] = snap.get("equity_series") or [snap.get("equity") or 0]
        start = snap.get("starting_balance") or 0
        last = hist[-1] if hist else 0
        line = sparkline(hist, width=64)
        txt = Text()
        txt.append("EQUITY  ", style="bold")
        txt.append(_fmt_usd(start), style="dim")
        txt.append("  " + line + "  ", style="cyan")
        txt.append(_fmt_usd(last), style="bold")
        txt.append("    q quit   paper trading only — no wallet, no keys, no forecasts", style="dim")
        return Panel(Align.center(txt), border_style="blue", box=box.ROUNDED, padding=(0, 1))


def render_snapshot_svg(snap: dict[str, Any], path: str, width: int = 140, height: int = 45) -> None:
    import io

    console = Console(
        record=True,
        width=width,
        height=height,
        force_terminal=True,
        color_system="truecolor",
        file=io.StringIO(),
    )
    view = DeskView(console)
    console.print(view.render(snap))
    console.save_svg(path, title="Solana paper desk — PAPER MODE")
