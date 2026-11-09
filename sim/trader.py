#!/usr/bin/env python3
"""Paper-trading memecoin simulator — BSC only. NO real money, wallets, or orders.

One invocation = one pass: load state, mark/exit open positions, scan for entries,
save state, write status.json. There is no sleep loop. After END_AT the pass
flattens once and later invocations leave that final status untouched.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
from api import (
    fetch_binance_eur_usd,
    fetch_eur_usd,
    fetch_frankfurter_eur_usd,
    fetch_gt_bsc_pools,
    fetch_pair,
    fetch_search_pairs,
    fetch_token_boosts,
    fetch_token_boosts_top,
    fetch_token_pairs,
    fetch_token_profiles,
)
from status import write_status
from strategy import all_checks, best_pair_for_token

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / config.STATE_FILE
SEED_PATH = ROOT / "seed-state.json"
STATUS_PATH = ROOT / config.STATUS_FILE

LJ = timezone(timedelta(hours=1))
_CAND_CACHE: tuple[float, list[str]] | None = None
_CAND_TTL = 180  # seconds


def now_ts() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now(LJ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    # stdout is redirected to trader.log by ensure_running.sh
    print(f"{now_iso()} {msg}", flush=True)



def default_state() -> dict:
    eur_usd = fetch_eur_usd() or 1.08
    start_usd = config.START_EUR * eur_usd
    return {
        "version": 1,
        "chain": config.CHAIN_ID,
        "started_at": now_iso(),
        "ends_at": config.END_AT.isoformat(),
        "eur_usd": eur_usd,
        "start_eur": config.START_EUR,
        "start_usd": round(start_usd, 4),
        "cash_usd": round(start_usd, 4),
        "open": [],          # positions
        "closed": [],        # trades
        "cooldowns": {},     # token -> unix ts when re-entry allowed
        "stats": {
            "loops": 0,
            "candidates_seen": 0,
            "entries": 0,
            "exits": 0,
            "vetoes": 0,
            "goplus_ok": None,
        },
        "last_loop_at": None,
        "notes": "PAPER ONLY — no real orders",
    }


def load_state() -> dict:
    """Prefer the live state file, then the shipped seed from the other machine."""
    for path in (STATE_PATH, SEED_PATH):
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict) and "cash_usd" in st:
                if path is SEED_PATH:
                    log("no state.json — continuing from seed-state.json")
                return st
        except (OSError, json.JSONDecodeError) as e:
            log(f"WARN bad {path.name}, skipping: {e}")
    log("WARN no usable state — starting a fresh €50 book")
    return default_state()


def save_state(st: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(STATE_PATH)


def equity_usd(st: dict) -> float:
    eq = float(st["cash_usd"])
    for p in st["open"]:
        eq += float(p.get("mark_usd", p.get("cost_usd", 0)))
    return eq


def position_size_usd(st: dict) -> float:
    eq = equity_usd(st)
    size = eq * config.POSITION_PCT
    return max(size, config.MIN_POSITION_USD) if eq >= config.MIN_POSITION_USD else 0.0


def collect_candidates() -> list[str]:
    """Gather BSC token addresses from several free public sources."""
    global _CAND_CACHE
    if _CAND_CACHE and now_ts() - _CAND_CACHE[0] < _CAND_TTL:
        return list(_CAND_CACHE[1])

    addrs: list[str] = []
    seen: set[str] = set()

    def add(a: str | None) -> None:
        if not a:
            return
        a = a.strip()
        al = a.lower()
        if not al.startswith("0x") or al in seen:
            return
        seen.add(al)
        addrs.append(a)

    for item in fetch_token_boosts() + fetch_token_boosts_top() + fetch_token_profiles():
        add(item.get("tokenAddress"))

    for a in fetch_gt_bsc_pools("trending") + fetch_gt_bsc_pools("new"):
        add(a)

    # Active BSC pairs via search (memes / pancake ecosystem)
    for q in ("cake", "floki", "bsc pepe", "baby doge", "pancake"):
        for p in fetch_search_pairs(q):
            base = (p.get("baseToken") or {}).get("address")
            add(base)
            if len(addrs) >= 60:
                break
        if len(addrs) >= 60:
            break

    out = addrs[:60]
    _CAND_CACHE = (now_ts(), out)
    return list(out)


def mark_positions(st: dict) -> None:
    for pos in st["open"]:
        pair = fetch_pair(pos["pair"])
        if not pair:
            continue
        px = float(pair.get("priceUsd") or 0)
        if px <= 0:
            continue
        pos["last_price"] = px
        tokens = float(pos["tokens"])
        # Mark value before exit costs
        pos["mark_usd"] = tokens * px
        peak = float(pos.get("peak_price", pos["entry_price"]))
        if px > peak:
            pos["peak_price"] = px
            peak = px
        pos["pnl_pct_gross"] = (px / float(pos["entry_price"]) - 1.0) * 100.0


def maybe_exit(st: dict) -> None:
    still = []
    for pos in st["open"]:
        px = float(pos.get("last_price") or 0)
        if px <= 0:
            still.append(pos)
            continue
        entry = float(pos["entry_price"])
        peak = float(pos.get("peak_price", entry))
        age = now_ts() - float(pos["opened_ts"])
        # Effective exit proceeds after cost
        gross = float(pos["tokens"]) * px
        net = gross * (1.0 - config.EXIT_COST)
        cost = float(pos["cost_usd"])
        pnl = net - cost
        pnl_pct = (net / cost - 1.0) if cost else 0.0
        reason = None
        if pnl_pct <= config.STOP_LOSS:
            reason = "stop_loss"
        elif pnl_pct >= config.TAKE_PROFIT:
            reason = "take_profit"
        elif (peak / entry - 1.0) >= config.TRAIL_ACTIVATE and px <= peak * (1.0 - config.TRAIL_PCT):
            reason = "trailing_stop"
        elif age >= config.TIME_STOP_SEC:
            reason = "time_stop"
        if reason:
            trade = {
                "symbol": pos["symbol"],
                "name": pos.get("name") or pos.get("symbol"),
                "token": pos["token"],
                "pair": pos["pair"],
                "entry_price": entry,
                "exit_price": px,
                "cost_usd": cost,
                "proceeds_usd": round(net, 6),
                "pnl_usd": round(pnl, 6),
                "pnl_pct": round(pnl_pct * 100, 4),
                "opened_at": pos["opened_at"],
                "closed_at": now_iso(),
                "held_sec": int(age),
                "reason": reason,
                "url": pos.get("url"),
            }
            st["cash_usd"] = round(float(st["cash_usd"]) + net, 6)
            st["closed"].append(trade)
            st["stats"]["exits"] = int(st["stats"].get("exits", 0)) + 1
            st["cooldowns"][pos["token"].lower()] = now_ts() + config.REENTRY_COOLDOWN_SEC
            log(
                f"EXIT {pos['symbol']} reason={reason} pnl=${pnl:.3f} ({pnl_pct*100:.2f}%) "
                f"cash=${st['cash_usd']:.2f}"
            )
        else:
            still.append(pos)
    st["open"] = still


def maybe_enter(st: dict) -> None:
    if len(st["open"]) >= config.MAX_OPEN:
        return
    if datetime.now(LJ) >= config.END_AT:
        return
    size = position_size_usd(st)
    if size <= 0 or float(st["cash_usd"]) < size:
        return

    addrs = collect_candidates()
    st["stats"]["candidates_seen"] = int(st["stats"].get("candidates_seen", 0)) + len(addrs)
    if not addrs:
        log("no BSC boost/profile candidates")
        return

    # Batch fetch pairs
    pairs = fetch_token_pairs(addrs)
    # Index by token
    open_tokens = {p["token"].lower() for p in st["open"]}
    now = now_ts()

    # Probe GoPlus once early if unknown
    goplus_probed = st["stats"].get("goplus_ok") is not None

    for addr in addrs:
        if len(st["open"]) >= config.MAX_OPEN:
            break
        if float(st["cash_usd"]) < size:
            break
        al = addr.lower()
        if al in open_tokens:
            continue
        cd = float(st["cooldowns"].get(al, 0))
        if cd > now:
            continue
        pair = best_pair_for_token(pairs, addr)
        if not pair:
            continue
        # Prefer pancake already in best_pair_for_token
        run_gp = True
        ok, details = all_checks(pair, run_goplus=run_gp)
        if not goplus_probed:
            # Infer from details
            gp_line = next((d for d in details if "goplus" in d.lower()), "")
            if "goplus_unavailable" in gp_line or "goplus_code" in gp_line:
                st["stats"]["goplus_ok"] = False
            elif "goplus=" in gp_line or "PASS safety" in " ".join(details):
                st["stats"]["goplus_ok"] = True
            goplus_probed = True
        if not ok:
            st["stats"]["vetoes"] = int(st["stats"].get("vetoes", 0)) + 1
            log(f"VETO {(pair.get('baseToken') or {}).get('symbol','?')}: {details[-1]}")
            continue

        base = pair.get("baseToken") or {}
        symbol = base.get("symbol") or "?"
        token = base.get("address") or addr
        px = float(pair.get("priceUsd") or 0)
        if px <= 0:
            continue
        # Apply entry cost: spend `size`, get tokens at worse price
        spend = min(size, float(st["cash_usd"]))
        eff_price = px * (1.0 + config.ENTRY_COST)
        tokens = spend / eff_price
        pos = {
            "symbol": symbol,
            "name": base.get("name") or symbol,
            "token": token,
            "pair": pair.get("pairAddress"),
            "dex": pair.get("dexId"),
            "entry_price": px,
            "eff_entry": eff_price,
            "tokens": tokens,
            "cost_usd": spend,
            "mark_usd": spend,  # until marked
            "peak_price": px,
            "opened_ts": now,
            "opened_at": now_iso(),
            "url": pair.get("url"),
            "checks": details,
        }
        st["cash_usd"] = round(float(st["cash_usd"]) - spend, 6)
        st["open"].append(pos)
        open_tokens.add(token.lower())
        st["stats"]["entries"] = int(st["stats"].get("entries", 0)) + 1
        log(
            f"ENTER {symbol} spend=${spend:.2f} px={px:.10g} dex={pos['dex']} "
            f"pair={pos['pair']} cash=${st['cash_usd']:.2f}"
        )
        # Refresh size after entry
        size = position_size_usd(st)


def probe_goplus(st: dict) -> None:
    """One-shot probe with a well-known BSC token (WBNB) to record API health."""
    if st["stats"].get("goplus_ok") is not None:
        return
    from api import goplus_ok

    wbnb = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
    ok, reason = goplus_ok(wbnb)
    # unavailable => False for "worked"; actual analysis that returns ok/reject => True
    worked = reason != "goplus_unavailable" and not str(reason).startswith("goplus_code")
    st["stats"]["goplus_ok"] = worked
    st["stats"]["goplus_probe"] = reason
    log(f"GoPlus probe WBNB: worked={worked} reason={reason}")


def loop_once(st: dict) -> None:
    # Keep the book's EUR rate when Binance is unreachable. Swapping in another
    # source would show a fake gain or loss against the €50 start.
    fx = fetch_binance_eur_usd()
    if fx:
        st["eur_usd"] = fx
    elif not st.get("eur_usd"):
        st["eur_usd"] = fetch_frankfurter_eur_usd() or fetch_eur_usd() or 1.08
        log(f"FX bootstrapped at {st['eur_usd']}")
    else:
        log(f"Binance FX unavailable — keeping {st.get('eur_usd')}")
    probe_goplus(st)
    mark_positions(st)
    maybe_exit(st)
    maybe_enter(st)
    st["stats"]["loops"] = int(st["stats"].get("loops", 0)) + 1
    st["last_loop_at"] = now_iso()
    eq = equity_usd(st)
    log(
        f"loop#{st['stats']['loops']} equity=${eq:.2f} "
        f"(€{eq/st['eur_usd']:.2f}) cash=${st['cash_usd']:.2f} "
        f"open={len(st['open'])} closed={len(st['closed'])}"
    )


def past_end() -> bool:
    return datetime.now(LJ) >= config.END_AT


def flatten_for_end(st: dict) -> None:
    """Close whatever is still open, once, when the simulation window has ended."""
    mark_positions(st)
    for pos in list(st["open"]):
        pos["opened_ts"] = now_ts() - config.TIME_STOP_SEC - 1
    maybe_exit(st)
    for pos in list(st["open"]):
        cost = float(pos.get("cost_usd") or 0)
        proceeds = float(pos.get("mark_usd", cost)) * (1.0 - config.EXIT_COST)
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100.0) if cost else 0.0
        st["cash_usd"] = round(float(st["cash_usd"]) + proceeds, 6)
        st["closed"].append({
            "symbol": pos.get("symbol"),
            "name": pos.get("name") or pos.get("symbol"),
            "token": pos.get("token"),
            "pair": pos.get("pair"),
            "entry_price": pos.get("entry_price"),
            "exit_price": pos.get("last_price"),
            "cost_usd": cost,
            "proceeds_usd": round(proceeds, 6),
            "pnl_usd": round(pnl, 6),
            "pnl_pct": round(pnl_pct, 4),
            "opened_at": pos.get("opened_at"),
            "closed_at": now_iso(),
            "held_sec": int(now_ts() - float(pos.get("opened_ts") or now_ts())),
            "reason": "sim_end",
            "url": pos.get("url"),
        })
        log(f"FLATTEN {pos.get('symbol')} reason=sim_end pnl=${pnl:.3f}")
    st["open"] = []


def main() -> int:
    log(f"PASS paper-trader chain={config.CHAIN_ID} end={config.END_AT.isoformat()}")
    st = load_state()

    if past_end():
        if st.get("finished"):
            log("END DATE already finalized — leaving the final status")
            if not STATUS_PATH.exists():
                write_status(st)
                log(f"rewrote missing {STATUS_PATH.name}")
            return 0
        log("END DATE reached — flattening once")
        try:
            flatten_for_end(st)
        except Exception:
            log("ERROR while flattening:\n" + traceback.format_exc())
            return 1
        st["finished"] = True
        st["last_loop_at"] = now_iso()
        save_state(st)
        write_status(st)
        log("final status written")
        return 0

    try:
        loop_once(st)
    except Exception:
        log("ERROR in pass:\n" + traceback.format_exc())
    save_state(st)
    payload = write_status(st)
    log(
        f"status equity=€{payload['equity_eur']:.2f} "
        f"({payload['pct_vs_eur50']:+.2f}%) "
        f"open={len(payload['open'])} closed={len(payload['closed'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
