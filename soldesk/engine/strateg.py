"""Shadow strategy lab. Paper only. Live rules can only get tighter.

Hard floors stay put: 8% size, 3% launch size, -6% main stop, -10% launch stop,
and honeypot / mint / freeze / cannot-sell kills. Variants never spend capital.
"""

from __future__ import annotations

import config
from models import iso

MIN_SAMPLE = 20
HORIZONS = (15, 30, 60)


def _variant(vid: str, label: str, *, promotable: bool = True, require_timing: bool = True, **params) -> dict:
    base = {
        "min_score": config.MIN_ANALYST_SCORE,
        "pullback_min": 0.03,
        "min_age_min": config.MIN_TOKEN_AGE_MIN,
        "min_buyers": config.LAUNCH_MIN_BUYERS,
        "min_liquidity": 0.0,
        "min_buy_ratio": 0.5,
        "stop": config.HARD_STOP_PCT,
        "tp": 0.15,
        "trail": config.TRAIL_PCT,
        "time_min": 40,
        "require_timing": require_timing,
        "promotable": promotable,
    }
    base.update(params)
    # A shadow stop can never sit past the user floor. Size is not a variant knob.
    base["stop"] = max(float(base["stop"]), config.HARD_STOP_PCT)
    base["min_score"] = max(float(base["min_score"]), config.MIN_ANALYST_SCORE)
    base["min_age_min"] = max(float(base["min_age_min"]), config.MIN_TOKEN_AGE_MIN)
    base["min_buyers"] = max(float(base["min_buyers"]), config.LAUNCH_MIN_BUYERS)
    base["trail"] = min(float(base["trail"]), config.TRAIL_PCT)
    base["time_min"] = min(float(base["time_min"]), 40)
    base["tp"] = min(float(base["tp"]), 0.15)
    return {"id": vid, "label": label, "params": base}


VARIANTS = [
    _variant("zdaj", "Trenutna pravila"),
    _variant("prag_065", "Analitik 0,65", min_score=0.65),
    _variant("prag_070", "Analitik 0,70", min_score=0.70),
    _variant("globji_umik", "Globji umik 8 %", pullback_min=0.08),
    _variant("vec_kupcev", "Več kupcev", min_buyers=80),
    _variant("vec_likvidnosti", "Višja likvidnost", min_liquidity=80_000.0),
    _variant("razmerje", "Nakup/prodaja 62 %", min_buy_ratio=0.62),
    _variant("starejsi", "Starost 90 min", min_age_min=90),
    _variant("ozji_sled", "Ožji sled 4 %", trail=0.04),
    _variant("krajsi_cas", "Krajši čas 20 min", time_min=20),
    _variant("hitrejsi_tp", "Prej delni izstop", tp=0.10),
    _variant("brez_umika", "Brez umika (samo senca)", promotable=False, require_timing=False),
]


def fresh() -> dict:
    return {
        "version": 1,
        "live": {},
        "history": [{"version": 1, "why": "začetek, privzete meje", "at": iso()}],
        "variants": {
            row["id"]: {"label": row["label"], "params": row["params"], "wins": 0, "losses": 0, "pnl": 0.0, "peak": 0.0, "equity": 0.0, "max_dd": 0.0}
            for row in VARIANTS
        },
        "pending": [],
        "lessons": [],
        "last_change": "začetek, privzete meje",
        "live_after": [],
    }


def ensure(state: dict) -> dict:
    book = state.get("strategy")
    if not isinstance(book, dict) or "variants" not in book:
        book = fresh()
        state["strategy"] = book
    for row in VARIANTS:
        book["variants"].setdefault(row["id"], {"label": row["label"], "params": row["params"], "wins": 0, "losses": 0, "pnl": 0.0, "peak": 0.0, "equity": 0.0, "max_dd": 0.0})
    book.setdefault("pending", [])
    book.setdefault("history", [])
    book.setdefault("lessons", [])
    book.setdefault("live", {})
    book.setdefault("live_after", [])
    book.setdefault("version", 1)
    return book


def would_enter(features: dict, params: dict) -> bool:
    """Shadow entry. Missing safety fields fail closed. No capital is used."""
    if features.get("hard_kill"):
        return False
    score = features.get("score")
    if score is None or float(score) < float(params["min_score"]):
        return False
    ratio = features.get("buy_ratio")
    if ratio is None or float(ratio) < float(params["min_buy_ratio"]):
        return False
    liq = features.get("liquidity")
    if liq is None or float(liq) < float(params["min_liquidity"]):
        return False
    if features.get("lane") == "novi_lans":
        buyers = features.get("buyers")
        if buyers is None or float(buyers) < float(params["min_buyers"]):
            return False
    else:
        age = features.get("age_min")
        if age is None or float(age) < float(params["min_age_min"]):
            return False
        if params.get("require_timing", True):
            pullback = features.get("pullback")
            if pullback is None or float(pullback) < float(params["pullback_min"]):
                return False
            if not features.get("higher_low") or not features.get("vwap_ok") or not features.get("ema_ok"):
                return False
    return True


def observe(state: dict, features: dict, now_ts: float) -> None:
    """Record a signal for later shadow scoring. No capital moves."""
    book = ensure(state)
    price = float(features.get("price") or 0)
    if price <= 0 or not features.get("mint"):
        return
    book["pending"].append({
        "mint": features["mint"],
        "symbol": features.get("symbol") or "",
        "chain": features.get("chain") or "solana",
        "lane": features.get("lane") or "glavni",
        "price": price,
        "ts": now_ts,
        "due": now_ts + 15 * 60,
        "horizon": 15,
        "path": [],
        "features": {
            k: features.get(k)
            for k in (
                "score", "liquidity", "volume_1h", "buyers", "age_min", "buy_ratio",
                "pullback", "higher_low", "vwap_ok", "ema_ok", "hard_kill", "lane", "chain",
            )
        },
        "taken": [row["id"] for row in VARIANTS if would_enter(features, row["params"])],
    })
    book["pending"] = book["pending"][-80:]


def _apply_path(entry: float, path: list, params: dict) -> float | None:
    if not path or entry <= 0:
        return None
    peak = entry
    for minutes, px in path:
        if px <= 0:
            continue
        peak = max(peak, px)
        ret = px / entry - 1.0
        if ret <= float(params["stop"]):
            return float(params["stop"])
        if ret >= float(params["tp"]):
            return float(params["tp"])
        if peak > entry and (peak - px) / peak >= float(params["trail"]) and ret > 0.05:
            return ret
        if minutes >= float(params["time_min"]) and abs(ret) < 0.03:
            return ret
    return path[-1][1] / entry - 1.0


def _credit(row: dict, ret: float) -> None:
    row["wins"] = int(row["wins"]) + (1 if ret > 0 else 0)
    row["losses"] = int(row["losses"]) + (0 if ret > 0 else 1)
    row["pnl"] = round(float(row["pnl"]) + ret, 6)
    row["gross_win"] = round(float(row.get("gross_win") or 0) + (ret if ret > 0 else 0), 6)
    row["gross_loss"] = round(float(row.get("gross_loss") or 0) + ((-ret) if ret < 0 else 0), 6)
    row["equity"] = round(float(row["equity"]) + ret, 6)
    row["peak"] = max(float(row["peak"]), float(row["equity"]))
    if row["peak"]:
        dd = (row["peak"] - row["equity"]) / max(abs(row["peak"]), 1e-9)
        row["max_dd"] = round(max(float(row["max_dd"]), dd), 4)


def resolve(state: dict, now_ts: float, price_of) -> int:
    """Fill forward prices. price_of(chain, mint) -> float or None. Returns how many closed."""
    book = ensure(state)
    done = 0
    still = []
    for row in book["pending"]:
        if now_ts < float(row.get("due") or 0):
            still.append(row)
            continue
        px = price_of(row.get("chain") or "solana", row.get("mint") or "")
        if not px:
            if now_ts - float(row["ts"]) > 80 * 60:
                continue
            still.append(row)
            continue
        horizon = int(row.get("horizon") or 15)
        row.setdefault("path", []).append([horizon, float(px)])
        if horizon >= 60:
            _close_shadow(book, row)
            _remember_forward(book, row)
            done += 1
            continue
        nxt = 30 if horizon == 15 else 60
        row["horizon"] = nxt
        row["due"] = float(row["ts"]) + nxt * 60
        still.append(row)
    book["pending"] = still
    maybe_promote(book)
    _lessons(book)
    return done


def _close_shadow(book: dict, pending: dict) -> None:
    entry = float(pending["price"])
    path = pending.get("path") or []
    features = pending.get("features") or {}
    for vid in pending.get("taken") or []:
        row = book["variants"].get(vid)
        if not row:
            continue
        ret = _apply_path(entry, path, row["params"])
        if ret is None:
            continue
        _credit(row, ret)
        row.setdefault("samples", []).append(_sample(features, ret))
        row["samples"] = row["samples"][-40:]


def _sample(features: dict, ret: float) -> dict:
    return {
        "chain": features.get("chain"),
        "lane": features.get("lane"),
        "ret": round(ret, 4),
        "score": features.get("score"),
        "liquidity": features.get("liquidity"),
        "buyers": features.get("buyers"),
        "buy_ratio": features.get("buy_ratio"),
    }


def _remember_forward(book: dict, pending: dict) -> None:
    path = pending.get("path") or []
    entry = float(pending.get("price") or 0)
    if not path or entry <= 0:
        return
    ret = float(path[-1][1]) / entry - 1.0
    book.setdefault("forwards", []).append(_sample(pending.get("features") or {}, ret))
    book["forwards"] = book["forwards"][-80:]


def _stats(row: dict) -> dict:
    n = int(row.get("wins") or 0) + int(row.get("losses") or 0)
    gross_loss = float(row.get("gross_loss") or 0)
    gross_win = float(row.get("gross_win") or 0)
    return {
        "n": n,
        "win_rate": (int(row["wins"]) / n) if n else None,
        "expectancy": (float(row["pnl"]) / n) if n else None,
        "max_dd": float(row.get("max_dd") or 0),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


def maybe_promote(book: dict) -> str | None:
    """Promote only a tighter overlay. Never touches size or the hard stops."""
    base = book["variants"].get("zdaj") or {}
    base_s = _stats(base)
    if not base_s["n"] or base_s["n"] < MIN_SAMPLE or base_s["win_rate"] is None:
        return None
    best = None
    best_edge = None
    for vid, row in book["variants"].items():
        if vid == "zdaj" or not row.get("params", {}).get("promotable", True):
            continue
        stats = _stats(row)
        if stats["n"] < MIN_SAMPLE or stats["win_rate"] is None or stats["expectancy"] is None:
            continue
        if stats["win_rate"] <= base_s["win_rate"]:
            continue
        if stats["expectancy"] <= base_s["expectancy"]:
            continue
        if stats["max_dd"] > base_s["max_dd"] + 0.02:
            continue
        edge = (float(stats["expectancy"]), float(stats["win_rate"]))
        if best_edge is None or edge > best_edge:
            best_edge = edge
            best = (vid, row, stats)
    if not best:
        return None
    _vid, row, stats = best
    previous = dict(book.get("live") or {})
    overlay = _tighten(previous, row["params"])
    if overlay == previous:
        return None
    if book.get("history"):
        book["history"][-1]["expectancy"] = book.get("baseline_expectancy")
        book["history"][-1]["live"] = previous
    book["version"] = int(book.get("version") or 1) + 1
    why = (
        f"promocija {row['label']}: zadetki {stats['win_rate']:.0%} in pričakovanje {stats['expectancy']:.3f} "
        f"sta boljša od trenutnih ({base_s['win_rate']:.0%}, {base_s['expectancy']:.3f}), n={stats['n']}. "
        f"Velikost in trdi stop ostaneta."
    )
    book["live"] = overlay
    book["last_change"] = why
    book["history"].append({"version": book["version"], "why": why, "at": iso(), "live": overlay})
    book["live_after"] = []
    return why


def note_live_result(book: dict, pnl_pct: float) -> str | None:
    """Rollback if the promoted live rules do worse than the previous version."""
    book = book if "version" in book else book
    if int(book.get("version") or 1) <= 1:
        return None
    book.setdefault("live_after", []).append(float(pnl_pct))
    book["live_after"] = book["live_after"][-12:]
    if len(book["live_after"]) < 8:
        return None
    history = book.get("history") or []
    if len(history) < 2:
        return None
    previous = history[-2]
    prev_exp = previous.get("expectancy")
    if prev_exp is None:
        prev_exp = 0.0
    now_mean = sum(book["live_after"]) / len(book["live_after"])
    if now_mean >= float(prev_exp):
        return None
    book["version"] = int(book["version"]) + 1
    book["live"] = dict(previous.get("live") or {})
    why = f"povratek: živa pravila {now_mean:.3f} so slabša od prejšnje različice ({float(prev_exp):.3f})"
    book["last_change"] = why
    book["history"].append({"version": book["version"], "why": why, "at": iso(), "live": book["live"]})
    book["live_after"] = []
    return why


def _tighten(current: dict, params: dict) -> dict:
    """Only copy values that are stricter than the hard floors and the current overlay."""
    out = dict(current)
    score = max(float(params["min_score"]), config.MIN_ANALYST_SCORE, float(current.get("min_score") or 0))
    if score > float(current.get("min_score") or config.MIN_ANALYST_SCORE):
        out["min_score"] = round(score, 2)
    age = max(float(params["min_age_min"]), config.MIN_TOKEN_AGE_MIN, float(current.get("min_age_min") or 0))
    if age > float(current.get("min_age_min") or config.MIN_TOKEN_AGE_MIN):
        out["min_age_min"] = age
    buyers = max(float(params["min_buyers"]), config.LAUNCH_MIN_BUYERS, float(current.get("min_buyers") or 0))
    if buyers > float(current.get("min_buyers") or config.LAUNCH_MIN_BUYERS):
        out["min_buyers"] = buyers
    liq = max(float(params["min_liquidity"]), float(current.get("min_liquidity") or 0))
    if liq > float(current.get("min_liquidity") or 30_000):
        out["min_liquidity"] = liq
    pull = max(float(params["pullback_min"]), float(current.get("pullback_min") or 0.03))
    if pull > float(current.get("pullback_min") or 0.03):
        out["pullback_min"] = pull
    trail = min(float(params["trail"]), float(current.get("trail") or config.TRAIL_PCT), config.TRAIL_PCT)
    if trail < float(current.get("trail") or config.TRAIL_PCT):
        out["trail"] = trail
    time_min = min(float(params["time_min"]), float(current.get("time_min") or 40))
    if time_min < float(current.get("time_min") or 40):
        out["time_min"] = time_min
    tp = min(float(params.get("tp") or 0.15), float(current.get("tp") or 0.15), 0.15)
    if tp < float(current.get("tp") or 0.15):
        out["tp"] = tp
    # Size and stops are intentionally absent. A looser value never lands here.
    out.pop("stop", None)
    out.pop("size", None)
    out.pop("launch_stop", None)
    return out


def live_view(state: dict) -> dict:
    """Effective paper rules. Every knob is clamped so a saved overlay cannot loosen a floor."""
    live = ensure(state).get("live") or {}
    return {
        "min_score": max(config.MIN_ANALYST_SCORE, float(live.get("min_score") or config.MIN_ANALYST_SCORE)),
        "min_age_min": max(config.MIN_TOKEN_AGE_MIN, float(live.get("min_age_min") or config.MIN_TOKEN_AGE_MIN)),
        "min_buyers": max(config.LAUNCH_MIN_BUYERS, float(live.get("min_buyers") or config.LAUNCH_MIN_BUYERS)),
        "min_liquidity": max(0.0, float(live.get("min_liquidity") or 0)),
        "min_buy_ratio": max(0.5, float(live.get("min_buy_ratio") or 0.5)),
        "pullback_min": max(0.03, float(live.get("pullback_min") or 0.03)),
        "trail": min(config.TRAIL_PCT, float(live.get("trail") or config.TRAIL_PCT)),
        "time_min": min(40.0, float(live.get("time_min") or 40)),
        "tp": min(0.15, float(live.get("tp") or 0.15)),
    }


def _lessons(book: dict) -> None:
    samples = list(book.get("forwards") or [])
    if len(samples) < 8:
        for row in book["variants"].values():
            samples.extend(row.get("samples") or [])
    if len(samples) < 8:
        book["lessons"] = ["Premalo podatkov za lekcije. Senca še nima osmih izidov."]
        return
    lessons = []
    for chain in ("solana", "bsc"):
        rows = [s for s in samples if s.get("chain") == chain]
        if len(rows) < 8:
            lessons.append(f"{'SOL' if chain == 'solana' else 'BNB'}: premalo podatkov.")
            continue
        wins = [s for s in rows if float(s["ret"]) > 0]
        losses = [s for s in rows if float(s["ret"]) <= 0]
        if not wins or not losses:
            lessons.append(f"{'SOL' if chain == 'solana' else 'BNB'}: izidi so enostranski, lekcija še ni trdna.")
            continue
        for key, text_hi, text_lo in (
            ("liquidity", "višja likvidnost je bila pogostejša pri dobitkih", "nižja likvidnost je bila pogostejša pri dobitkih"),
            ("buyers", "več unikatnih kupcev je bilo pogostejših pri dobitkih", "manj kupcev je bilo pogostejših pri dobitkih"),
            ("score", "višja ocena analitika je bila pogostejša pri dobitkih", "nižja ocena je bila pogostejša pri dobitkih"),
        ):
            wvals = [float(s[key]) for s in wins if s.get(key) is not None]
            lvals = [float(s[key]) for s in losses if s.get(key) is not None]
            if len(wvals) < 3 or len(lvals) < 3:
                continue
            wmean = sum(wvals) / len(wvals)
            lmean = sum(lvals) / len(lvals)
            if abs(wmean - lmean) / max(abs(lmean), 1) < 0.15:
                continue
            name = "SOL" if chain == "solana" else "BNB"
            lessons.append(f"{name}: {text_hi if wmean > lmean else text_lo}. Ni napoved.")
            break
    book["lessons"] = lessons[:6] or ["Ni jasne razlike med dobitki in izgubami. Premalo kontrasta."]


def public(book: dict | None) -> dict:
    book = book or fresh()
    rows = []
    for vid, row in book.get("variants", {}).items():
        stats = _stats(row)
        rows.append({
            "id": vid,
            "label": row.get("label") or vid,
            "n": stats["n"],
            "win_rate": None if stats["win_rate"] is None else round(stats["win_rate"], 4),
            "expectancy": None if stats["expectancy"] is None else round(stats["expectancy"], 4),
            "max_dd": round(stats["max_dd"], 4),
            "profit_factor": stats["profit_factor"],
            "ready": stats["n"] >= MIN_SAMPLE,
            "note": "premalo podatkov" if stats["n"] < MIN_SAMPLE else "vzorec zadostuje",
        })
    rows.sort(key=lambda item: (-(item["expectancy"] or -999), -item["n"]))
    return {
        "version": book.get("version") or 1,
        "live": book.get("live") or {},
        "last_change": book.get("last_change") or "",
        "variants": rows,
        "lessons": list(book.get("lessons") or ["Premalo podatkov za lekcije. Senca še nima osmih izidov."]),
        "pending": len(book.get("pending") or []),
        "floors": {
            "size": config.MAX_POSITION_PCT,
            "launch_size": config.LAUNCH_MAX_POSITION_PCT,
            "stop": config.HARD_STOP_PCT,
            "launch_stop": config.LAUNCH_HARD_STOP_PCT,
            "score": config.MIN_ANALYST_SCORE,
        },
    }
