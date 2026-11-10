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
    fetch_gt_pools,
    fetch_gt_top_pools,
    fetch_pair,
    fetch_token_boosts,
    fetch_token_boosts_top,
    fetch_token_pairs,
    fetch_token_profiles,
)
from agents import (
    AGENT_LABELS,
    classify,
    evaluate_agents,
    first_veto,
    is_stable,
    yes_count,
    note_votes,
    push_activity,
    push_candidate,
    push_equity,
)
from learning import (
    decide,
    load_book,
    max_open,
    on_close,
    on_open,
    on_reject,
    public_summary,
    resolve_pending,
    retune_votes,
    save_book,
    snapshot,
    stop_loss,
    time_stop_sec,
)
from status import write_status
from strategy import best_pair_for_token

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / config.STATE_FILE
SEED_PATH = ROOT / "seed-state.json"
STATUS_PATH = ROOT / config.STATUS_FILE

LJ = timezone(timedelta(hours=1))
_CAND_CACHE: tuple[float, list[dict]] | None = None
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
    log("WARN no usable state — starting a fresh €100 book")
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


UNIVERSE_TTL = 20 * 60
PASS_BATCH = 18
EVAL_CAP = 12


def _add_token(found: dict[str, dict], address: str | None, chain: str = "bsc", **flags: object) -> None:
    if not address:
        return
    address = str(address).strip()
    if chain == "bsc":
        if not address.lower().startswith("0x"):
            return
        key = "bsc:" + address.lower()
    else:
        if address.lower().startswith("0x") or len(address) < 32:
            return
        key = "solana:" + address
    symbol = str(flags.get("symbol") or "")
    if is_stable(symbol):
        return
    row = found.get(key)
    if row is None:
        found[key] = {
            "address": address if chain == "solana" else address.lower(),
            "chain": chain,
            "boosted": bool(flags.get("boosted")),
            "source": str(flags.get("source") or ""),
            "symbol": symbol,
            "mcap_usd": float(flags.get("mcap_usd") or 0),
        }
        return
    if flags.get("boosted"):
        row["boosted"] = True
    if flags.get("source") == "new" and row.get("source") != "boost":
        row["source"] = "new"
    if symbol and not row.get("symbol"):
        row["symbol"] = symbol


def refresh_universe(st: dict) -> list[dict]:
    """Broad BSC universe from live pool lists. Cached so passes stay inside rate limits."""
    cached = st.get("universe") or {}
    age = now_ts() - float(cached.get("fetched_at") or 0)
    old = cached.get("items") if isinstance(cached.get("items"), list) else []
    has_sol = any(isinstance(i, dict) and i.get("chain") == "solana" for i in old)
    if old and has_sol and age < UNIVERSE_TTL:
        return old

    found: dict[str, dict] = {}
    for network, pages in (("bsc", 5), ("solana", 4)):
        for page in range(1, pages + 1):
            for pool in fetch_gt_top_pools(page, network):
                if pool["reserve_usd"] and pool["reserve_usd"] < config.MIN_LIQUIDITY_USD:
                    continue
                if pool["vol24_usd"] and pool["vol24_usd"] < 20_000:
                    continue
                _add_token(
                    found, pool["address"], chain=network, source="volume",
                    symbol=pool["symbol"], mcap_usd=pool["mcap_usd"],
                )
        for addr in fetch_gt_pools("trending", network):
            _add_token(found, addr, chain=network, source="trending")
        for addr in fetch_gt_pools("new", network):
            _add_token(found, addr, chain=network, source="new")
    for item in fetch_token_boosts() + fetch_token_boosts_top():
        _add_token(
            found, item.get("tokenAddress"),
            chain=str(item.get("chainId") or "bsc"),
            boosted=True, source="boost",
        )
    for item in fetch_token_profiles():
        _add_token(
            found, item.get("tokenAddress"),
            chain=str(item.get("chainId") or "bsc"),
            source="profile",
        )

    items = list(found.values())[:800]
    st["universe"] = {"fetched_at": now_ts(), "items": items}
    log(
        "universe refresh "
        f"{sum(1 for i in items if i.get('chain')=='bsc')} BSC / "
        f"{sum(1 for i in items if i.get('chain')=='solana')} SOL"
    )
    return items


def _rotate(st: dict, items: list[dict], key: str) -> list[dict]:
    if not items:
        return []
    start = int(st.get(key) or 0) % len(items)
    batch = [dict(items[(start + i) % len(items)]) for i in range(min(PASS_BATCH, len(items)))]
    st[key] = (start + len(batch)) % len(items)
    return batch


def next_batch(st: dict) -> list[dict]:
    items = refresh_universe(st)
    bsc = [i for i in items if i.get("chain", "bsc") == "bsc"]
    sol = [i for i in items if i.get("chain") == "solana"]
    return _rotate(st, bsc, "cursor_bsc") + _rotate(st, sol, "cursor_sol")


def mark_positions(st: dict) -> None:
    for pos in st["open"]:
        pair = fetch_pair(pos["pair"], pos.get("chain") or "bsc")
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
        gross_pct = (px / float(pos["entry_price"]) - 1.0) * 100.0
        pos["pnl_pct_gross"] = gross_pct
        pos["mfe_pct"] = max(float(pos.get("mfe_pct", gross_pct)), gross_pct)
        pos["mae_pct"] = min(float(pos.get("mae_pct", gross_pct)), gross_pct)


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
        net = gross * (1.0 - float(pos.get("cost_rate") or config.EXIT_COST))
        cost = float(pos["cost_usd"])
        pnl = net - cost
        pnl_pct = (net / cost - 1.0) if cost else 0.0
        reason = None
        book = st.get("_learn")
        if pnl_pct <= stop_loss(book):
            reason = "stop_loss"
        elif pnl_pct >= config.TAKE_PROFIT:
            reason = "take_profit"
        elif (peak / entry - 1.0) >= config.TRAIL_ACTIVATE and px <= peak * (1.0 - config.TRAIL_PCT):
            reason = "trailing_stop"
        elif age >= time_stop_sec(book):
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
                "chain": pos.get("chain") or "bsc",
                "category": pos.get("category") or "",
                "mfe_pct": round(float(pos.get("mfe_pct", 0)), 4),
                "mae_pct": round(float(pos.get("mae_pct", 0)), 4),
            }
            if book and pos.get("learn"):
                on_close(book, pos["learn"], {
                    "pnl_pct": trade["pnl_pct"],
                    "pnl_usd": trade["pnl_usd"],
                    "reason": reason,
                    "held_sec": trade["held_sec"],
                    "mfe_pct": trade["mfe_pct"],
                    "mae_pct": trade["mae_pct"],
                }, trade["closed_at"])
            st["cash_usd"] = round(float(st["cash_usd"]) + net, 6)
            st["closed"].append(trade)
            st["stats"]["exits"] = int(st["stats"].get("exits", 0)) + 1
            st["cooldowns"][pos["token"].lower()] = now_ts() + config.REENTRY_COOLDOWN_SEC
            push_activity(st, {
                "time": trade["closed_at"],
                "type": "sell",
                "symbol": pos["symbol"],
                "reason": reason,
                "text": f"PRODAJA {pos['symbol']} · {reason}",
                "pnl_usd": trade["pnl_usd"],
            })
            log(
                f"EXIT {pos['symbol']} reason={reason} pnl=${pnl:.3f} ({pnl_pct*100:.2f}%) "
                f"cash=${st['cash_usd']:.2f}"
            )
        else:
            still.append(pos)
    st["open"] = still


def maybe_enter(st: dict) -> None:
    when = now_iso()
    book = st.get("_learn")
    if len(st["open"]) >= max_open(book):
        push_activity(st, {
            "time": when, "type": "scan",
            "text": "Pregled ustavljen — odprtih je že največ 5 pozicij",
        })
        return
    if datetime.now(LJ) >= config.END_AT:
        return
    size = position_size_usd(st)
    if size <= 0 or float(st["cash_usd"]) < size:
        push_activity(st, {
            "time": when, "type": "scan",
            "text": "Pregled ustavljen — premalo gotovine za nov vstop",
        })
        return

    candidates = next_batch(st)
    st["stats"]["candidates_seen"] = int(st["stats"].get("candidates_seen", 0)) + len(candidates)
    push_activity(st, {
        "time": when,
        "type": "scan",
        "text": f"Kos {len(candidates)} žetonov iz {len((st.get('universe') or {}).get('items') or [])}",
    })
    if not candidates:
        log("no BSC boost/profile candidates")
        return

    pairs: list[dict] = []
    for chain_name in ("bsc", "solana"):
        addrs = [c["address"] for c in candidates if c.get("chain", "bsc") == chain_name]
        pairs.extend(fetch_token_pairs(addrs, chain_name))
    open_tokens = {str(p.get("token") or "").lower() for p in st["open"]}
    now = now_ts()
    evaluated = {"bsc": 0, "solana": 0}

    for cand in candidates:
        chain_name = cand.get("chain") or "bsc"
        if evaluated["bsc"] >= EVAL_CAP and evaluated["solana"] >= EVAL_CAP:
            break
        if evaluated.get(chain_name, 0) >= EVAL_CAP or len(st["open"]) >= max_open(st.get("_learn")):
            continue
        if float(st["cash_usd"]) < size:
            break
        addr = cand["address"]
        al = addr.lower()
        if al in open_tokens:
            continue
        if float(st["cooldowns"].get(al, 0)) > now:
            continue
        pair = best_pair_for_token(
            [p for p in pairs if p.get("chainId") == chain_name],
            addr,
        )
        if not pair:
            continue

        base = pair.get("baseToken") or {}
        symbol = base.get("symbol") or cand.get("symbol") or "?"
        if is_stable(symbol):
            continue
        liq = float(((pair.get("liquidity") or {}).get("usd")) or 0)
        vol24 = float(((pair.get("volume") or {}).get("h24")) or 0)
        if liq < config.MIN_LIQUIDITY_USD or vol24 < 20_000:
            continue
        token = base.get("address") or addr
        mcap = float(pair.get("marketCap") or pair.get("fdv") or cand.get("mcap_usd") or 0)
        category = classify(
            symbol, base.get("name") or "", token, mcap,
            boosted=bool(cand.get("boosted")), source=str(cand.get("source") or ""),
        )
        ok, votes = evaluate_agents(pair, {
            "boosted": bool(cand.get("boosted")),
            "size_usd": size,
            "run_goplus": True,
            "category": category,
            "chain": chain_name,
        })
        learn_book = st.get("_learn")
        features = snapshot(pair, votes, category, chain_name, (learn_book or {}).get("params"))
        learn_why = ""
        if learn_book:
            votes = retune_votes(learn_book, votes, features)
            features = snapshot(pair, votes, category, chain_name, learn_book.get("params"))
            ok, learn_why = decide(learn_book, votes, features)
        evaluated[chain_name] = evaluated.get(chain_name, 0) + 1
        stamp = now_iso()
        note_votes(st, symbol, votes, stamp)
        gp = (votes.get("varnost") or {}).get("numbers", {}).get("goplus")
        if gp:
            worked = "goplus_unavailable" not in str(gp) and not str(gp).startswith("goplus_code")
            if st["stats"].get("goplus_ok") is None or worked:
                st["stats"]["goplus_ok"] = worked
                st["stats"]["goplus_probe"] = gp
        info = pair.get("info") or {}
        change = (pair.get("priceChange") or {}).get("h24")
        record = {
            "time": stamp,
            "symbol": symbol,
            "name": base.get("name") or symbol,
            "url": pair.get("url"),
            "image": info.get("imageUrl") or "",
            "token": token,
            "chain": chain_name,
            "decision": "BUY" if ok else "VETO",
            "category": category,
            "price": float(pair.get("priceUsd") or 0),
            "liq_usd": liq,
            "vol24": vol24,
            "change24": float(change) if change not in (None, "") else None,
            "votes": votes,
        }
        push_candidate(st, record)
        yes_n = yes_count(votes)
        if not ok:
            if votes.get("varnost", {}).get("vote") != "YES":
                veto_id = "varnost"
            else:
                veto_id = first_veto(votes) or "kit"
            reason = (votes.get(veto_id) or {}).get("reason") or learn_why or "zavrnjeno"
            if learn_book:
                on_reject(
                    learn_book, features, symbol, str(pair.get("pairAddress") or ""),
                    chain_name, stamp, now_ts() + config.TIME_STOP_SEC,
                )
            st["stats"]["vetoes"] = int(st["stats"].get("vetoes", 0)) + 1
            push_activity(st, {
                "time": stamp,
                "type": "veto",
                "agent": veto_id,
                "symbol": symbol,
                "text": f"VETO {AGENT_LABELS.get(veto_id, veto_id)} · {symbol} · {yes_n}/5 · {reason}",
            })
            log(f"VETO {symbol} by {veto_id}: {reason}")
            continue

        px = float(pair.get("priceUsd") or 0)
        if px <= 0:
            continue
        liq_now = float(((pair.get("liquidity") or {}).get("usd")) or 0)
        cost_rate = config.ENTRY_COST
        if chain_name == "solana" and liq_now < 80_000:
            cost_rate = 0.02
        spend = min(size, float(st["cash_usd"]))
        eff_price = px * (1.0 + cost_rate)
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
            "mark_usd": spend,
            "peak_price": px,
            "opened_ts": now,
            "opened_at": stamp,
            "url": pair.get("url"),
            "image": (pair.get("info") or {}).get("imageUrl") or "",
            "chain": chain_name,
            "category": category,
            "liq_usd": liq_now,
            "cost_rate": cost_rate,
            "votes": votes,
            "learn": on_open(learn_book, features, symbol, stamp) if learn_book else features,
        }
        st["cash_usd"] = round(float(st["cash_usd"]) - spend, 6)
        st["open"].append(pos)
        open_tokens.add(token.lower())
        st["stats"]["entries"] = int(st["stats"].get("entries", 0)) + 1
        push_activity(st, {
            "time": stamp,
            "type": "buy",
            "symbol": symbol,
            "text": f"NAKUP {symbol} · {yes_n}/5 DA · ${spend:.2f}",
        })
        log(
            f"ENTER {symbol} spend=${spend:.2f} px={px:.10g} dex={pos['dex']} "
            f"pair={pos['pair']} cash=${st['cash_usd']:.2f}"
        )
        size = position_size_usd(st)
    st["last_scanned_bsc"] = int(evaluated.get("bsc") or 0)
    st["last_scanned_sol"] = int(evaluated.get("solana") or 0)
    st["last_scanned"] = st["last_scanned_bsc"] + st["last_scanned_sol"]
    push_activity(st, {
        "time": now_iso(),
        "type": "scan",
        "text": f"PREGLEDANIH BSC: {st['last_scanned_bsc']} · SOL: {st['last_scanned_sol']}",
    })


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


def _later_price(pair_addr: str, chain: str) -> float | None:
    pair = fetch_pair(pair_addr, chain)
    if not pair:
        return None
    px = float(pair.get("priceUsd") or 0)
    return px or None


def loop_once(st: dict) -> None:
    # Keep the book's EUR rate when Binance is unreachable. Swapping in another
    # source would show a fake gain or loss against the €100 start.
    fx = fetch_binance_eur_usd()
    if fx:
        st["eur_usd"] = fx
    elif not st.get("eur_usd"):
        st["eur_usd"] = fetch_frankfurter_eur_usd() or fetch_eur_usd() or 1.08
        log(f"FX bootstrapped at {st['eur_usd']}")
    else:
        log(f"Binance FX unavailable — keeping {st.get('eur_usd')}")
    probe_goplus(st)
    book = st.get("_learn")
    if book:
        resolve_pending(book, now_ts(), _later_price, now_iso())
    mark_positions(st)
    maybe_exit(st)
    maybe_enter(st)
    st["stats"]["loops"] = int(st["stats"].get("loops", 0)) + 1
    st["last_loop_at"] = now_iso()
    eq = equity_usd(st)
    eur = float(st.get("eur_usd") or 1.08) or 1.08
    push_equity(st, {
        "t": st["last_loop_at"],
        "usd": round(eq, 2),
        "eur": round(eq / eur, 2),
    })
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
        push_activity(st, {
            "time": now_iso(),
            "type": "sell",
            "symbol": pos.get("symbol"),
            "reason": "sim_end",
            "text": f"PRODAJA {pos.get('symbol')} · konec simulacije",
            "pnl_usd": round(pnl, 4),
        })
        log(f"FLATTEN {pos.get('symbol')} reason=sim_end pnl=${pnl:.3f}")
    st["open"] = []


def main() -> int:
    log(f"PASS paper-trader chain={config.CHAIN_ID} end={config.END_AT.isoformat()}")
    st = load_state()
    book = load_book()
    st["_learn"] = book

    def finish_books() -> None:
        st["learning_public"] = public_summary(book)
        save_book(book)
        st.pop("_learn", None)

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
        finish_books()
        save_state(st)
        write_status(st)
        log("final status written")
        return 0

    try:
        loop_once(st)
    except Exception:
        log("ERROR in pass:\n" + traceback.format_exc())
    finish_books()
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
