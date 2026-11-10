"""Paper-only learning journal. Never loosens safety or global risk rules.

A buy still needs at least 4 of 5 YES votes, and VARNOST can always veto.
Learning may only add extra refusals, raise minimums, or tighten exits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import config
from agents import AGENT_IDS, AGENT_LABELS, entry_allowed

ROOT = Path(__file__).resolve().parent
LEARN_PATH = ROOT / "learning.json"

# name, default, hard floor, hard ceiling, tighten direction (+1 raises, -1 lowers)
KNOBS: dict[str, dict[str, tuple[float, float, float, int]]] = {
    "kit": {"min_ratio": (float(config.MIN_BUY_SELL_RATIO), 1.0, 3.0, 1)},
    "trend": {"min_ch5": (0.0, -1.0, 8.0, 1)},
    "varnost": {
        "min_liq": (float(config.MIN_LIQUIDITY_USD), float(config.MIN_LIQUIDITY_USD), 150_000.0, 1),
        "max_holder": (0.20, 0.05, 0.20, -1),
    },
    "bik_medved": {"max_ch1h": (float(config.MAX_CHANGE_1H) * 100.0, 40.0, float(config.MAX_CHANGE_1H) * 100.0, -1)},
    "izvajanje": {"max_slip": (0.02, 0.008, 0.02, -1)},
}
GLOBAL = {
    "stop_loss": (float(config.STOP_LOSS), float(config.STOP_LOSS), -0.03, 1),  # +1 moves toward zero, never past -0.08
    "take_profit": (float(config.TAKE_PROFIT), 0.08, float(config.TAKE_PROFIT), -1),
    "time_stop_h": (4.0, 1.0, 4.0, -1),
    "max_open": (float(config.MAX_OPEN), 1.0, float(config.MAX_OPEN), -1),
    "entry_cost": (float(config.ENTRY_COST), float(config.ENTRY_COST), 0.04, 1),
}
FEATURE_SL = {
    "liq_usd": "likvidnost",
    "vol24": "24h promet",
    "vol_liq": "promet/likvidnost",
    "age_min": "starost para",
    "ch5": "5m sprememba",
    "ch1h": "1h sprememba",
    "ch24": "24h sprememba",
    "buy_ratio": "razmerje nakupov",
    "holder": "največji imetnik",
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def default_params() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for agent, knobs in KNOBS.items():
        out[agent] = {name: spec[0] for name, spec in knobs.items()}
    out["global"] = {name: spec[0] for name, spec in GLOBAL.items()}
    return out


def fresh_book() -> dict[str, Any]:
    trust = {agent: {"alpha": 2.0, "beta": 2.0} for agent in AGENT_IDS}
    params = default_params()
    return {
        "version": 1,
        "samples": 0,
        "trust": trust,
        "params": params,
        "history": [{"version": 1, "why": "začetek, privzete meje", "params": params}],
        "journal": [],
        "pending": [],
        "lessons": [],
        "avoid": [],
        "since_adjust": {agent: 0 for agent in AGENT_IDS},
        "global_since": 0,
    }


def load_book(path: Path | None = None) -> dict[str, Any]:
    dest = path or LEARN_PATH
    if not dest.exists():
        return fresh_book()
    try:
        raw = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fresh_book()
    book = fresh_book()
    if isinstance(raw, dict):
        book.update(raw)
    book["params"] = _merge_params(book.get("params") or {})
    book["trust"] = _merge_trust(book.get("trust") or {})
    book.setdefault("journal", [])
    book.setdefault("pending", [])
    book.setdefault("lessons", [])
    book.setdefault("avoid", [])
    book.setdefault("history", [])
    book.setdefault("since_adjust", {agent: 0 for agent in AGENT_IDS})
    return book


def save_book(book: dict[str, Any], path: Path | None = None) -> None:
    dest = path or LEARN_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(dest)


def _merge_params(raw: dict) -> dict[str, dict[str, float]]:
    base = default_params()
    for agent, knobs in KNOBS.items():
        got = raw.get(agent) or {}
        for name, spec in knobs.items():
            default, lo, hi, _direction = spec
            try:
                value = float(got.get(name, default))
            except (TypeError, ValueError):
                value = default
            # Never store a looser value than the hard bound.
            base[agent][name] = _clamp(value, lo, hi)
    got_g = raw.get("global") or {}
    for name, spec in GLOBAL.items():
        default, lo, hi, _direction = spec
        try:
            value = float(got_g.get(name, default))
        except (TypeError, ValueError):
            value = default
        base["global"][name] = _clamp(value, lo, hi)
    return base


def _merge_trust(raw: dict) -> dict[str, dict[str, float]]:
    out = {}
    for agent in AGENT_IDS:
        got = raw.get(agent) or {}
        try:
            alpha = max(1.0, float(got.get("alpha", 2)))
            beta = max(1.0, float(got.get("beta", 2)))
        except (TypeError, ValueError):
            alpha, beta = 2.0, 2.0
        out[agent] = {"alpha": alpha, "beta": beta}
    return out


def trust_score(book: dict, agent: str) -> float:
    row = book["trust"][agent]
    return row["alpha"] / (row["alpha"] + row["beta"])


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _holder_pct(votes: dict) -> float | None:
    text = str(((votes.get("varnost") or {}).get("numbers") or {}).get("goplus") or "")
    if "top_holder=" not in text:
        return None
    try:
        raw = text.split("top_holder=", 1)[1].split()[0].replace("%", "")
        value = float(raw)
        return value / 100.0 if value > 1 else value
    except (IndexError, ValueError):
        return None


def snapshot(pair: dict, votes: dict, category: str, chain: str, params: dict | None = None) -> dict[str, Any]:
    """Feature vector at the moment of a decision. Numbers come only from the pair and votes."""
    params = params or default_params()
    liq = _f(((pair.get("liquidity") or {}).get("usd")))
    vol24 = _f(((pair.get("volume") or {}).get("h24")))
    pc = pair.get("priceChange") or {}
    b5 = _f((((pair.get("txns") or {}).get("m5") or {}).get("buys")))
    s5 = _f((((pair.get("txns") or {}).get("m5") or {}).get("sells")))
    age = ((votes.get("varnost") or {}).get("numbers") or {}).get("age_min")
    created = pair.get("pairCreatedAt")
    if age in (None, "", -1) and created:
        import time
        age = (time.time() * 1000 - _f(created)) / 60000.0
    agent_rows = {}
    for agent in AGENT_IDS:
        vote = votes.get(agent) or {}
        agent_rows[agent] = {
            "vote": vote.get("vote") or "NO",
            "score": _score(agent, vote, params),
        }
    yes = sum(1 for row in agent_rows.values() if row["vote"] == "YES")
    return {
        "chain": chain or "bsc",
        "category": category or "",
        "liq_usd": round(liq, 2),
        "vol24": round(vol24, 2),
        "vol_liq": round(vol24 / liq, 4) if liq else None,
        "age_min": None if age in (None, "") else round(_f(age), 1),
        "ch5": _f(pc.get("m5")),
        "ch1h": _f(pc.get("h1")),
        "ch24": _f(pc.get("h24")),
        "buy_ratio": round(b5 / s5, 4) if s5 else (99.0 if b5 else 0.0),
        "holder": _holder_pct(votes),
        "votes": agent_rows,
        "yes_count": yes,
        "price": _f(pair.get("priceUsd")),
    }


def _score(agent: str, vote: dict, params: dict) -> float:
    nums = vote.get("numbers") or {}
    if agent == "kit":
        need = params["kit"]["min_ratio"] or 1
        raw = _f(nums.get("ratio_5m")) / need
    elif agent == "trend":
        raw = 0.5 + (_f(nums.get("ch5")) - params["trend"]["min_ch5"]) / 10.0
    elif agent == "varnost":
        raw = 0.85 if vote.get("vote") == "YES" else 0.15
    elif agent == "bik_medved":
        cap = params["bik_medved"]["max_ch1h"] or 1
        raw = 1.0 - _f(nums.get("ch1h")) / cap
    else:
        cap = params["izvajanje"]["max_slip"] or 0.02
        raw = 1.0 - _f(nums.get("slip")) / cap
    return round(_clamp(raw, 0.0, 1.0), 4)


def _bucket(features: dict) -> str:
    liq = _f(features.get("liq_usd"))
    ch = _f(features.get("ch1h"))
    if liq < 50_000:
        liq_band = "liq<50k"
    elif liq < 150_000:
        liq_band = "liq50-150k"
    else:
        liq_band = "liq>150k"
    if ch < 0:
        ch_band = "1h<0"
    elif ch < 20:
        ch_band = "1h0-20"
    else:
        ch_band = "1h>20"
    return f"{features.get('chain') or 'bsc'}|{features.get('category') or '?'}|{liq_band}|{ch_band}"


def explain_feature(features: dict) -> tuple[str, str]:
    """Pick the most extreme real feature. No invented values."""
    ranked = []
    if features.get("holder") is not None and _f(features.get("holder")) >= 0.15:
        ranked.append((abs(_f(features.get("holder"))), "holder", f"{_f(features.get('holder')):.0%}"))
    if abs(_f(features.get("ch1h"))) >= 15:
        ranked.append((abs(_f(features.get("ch1h"))) / 20, "ch1h", f"{_f(features.get('ch1h')):.1f}%"))
    if _f(features.get("liq_usd")) and _f(features.get("liq_usd")) < 60_000:
        ranked.append((1.2, "liq_usd", f"${_f(features.get('liq_usd')):.0f}"))
    if _f(features.get("buy_ratio")) < 1.2:
        ranked.append((1.1, "buy_ratio", f"{_f(features.get('buy_ratio')):.2f}"))
    if abs(_f(features.get("ch5"))) >= 4:
        ranked.append((abs(_f(features.get("ch5"))) / 8, "ch5", f"{_f(features.get('ch5')):.1f}%"))
    if not ranked:
        return "ch1h", f"{_f(features.get('ch1h')):.1f}%"
    ranked.sort(reverse=True)
    key, text = ranked[0][1], ranked[0][2]
    return key, text


def lesson_text(agent: str, vote: str, good: bool, feature_key: str, feature_text: str) -> str:
    label = AGENT_LABELS.get(agent, agent)
    name = FEATURE_SL.get(feature_key, feature_key)
    if vote == "YES" and not good:
        return f"Lekcija {label}: rekel je DA, izid je bil slab. Najbolj izstopa {name} ({feature_text})."
    if vote == "NO" and not good:
        return f"Lekcija {label}: rekel je NE, a pozneje je šlo navzgor. Najbolj izstopa {name} ({feature_text})."
    if vote == "YES":
        return f"Lekcija {label}: DA je bil upravičen. {name} ({feature_text})."
    return f"Lekcija {label}: NE je bil upravičen. {name} ({feature_text})."


def _push_lesson(book: dict, text: str, agent: str, when: str) -> None:
    book["lessons"].append({"time": when, "agent": agent, "text": text})
    book["lessons"] = book["lessons"][-40:]


def _credit(book: dict, agent: str, hit: bool) -> None:
    row = book["trust"][agent]
    if hit:
        row["alpha"] = round(row["alpha"] + 1, 4)
    else:
        row["beta"] = round(row["beta"] + 1, 4)


def on_open(book: dict, features: dict, symbol: str, when: str) -> dict:
    snap = dict(features)
    snap["symbol"] = symbol
    snap["opened_at"] = when
    snap["version"] = book["version"]
    return snap


def on_reject(book: dict, features: dict, symbol: str, pair: str, chain: str, when: str, check_after_ts: float) -> None:
    book["pending"].append({
        "symbol": symbol,
        "pair": pair,
        "chain": chain,
        "price": features.get("price"),
        "features": features,
        "check_after": check_after_ts,
        "time": when,
    })
    book["pending"] = book["pending"][-80:]


def on_close(book: dict, features: dict, outcome: dict, when: str) -> None:
    """Credit agents, write lessons, then maybe tighten."""
    won = _f(outcome.get("pnl_pct")) > 0
    key, text = explain_feature(features)
    votes = features.get("votes") or {}
    for agent in AGENT_IDS:
        vote = (votes.get(agent) or {}).get("vote") or "NO"
        if vote == "YES":
            _credit(book, agent, won)
            good = won
        else:
            _credit(book, agent, not won)
            good = not won
        _push_lesson(book, lesson_text(agent, vote, good, key, text), agent, when)
        book["since_adjust"][agent] = int(book["since_adjust"].get(agent, 0)) + 1
    record = dict(features)
    record["outcome"] = outcome
    record["mae_pct"] = outcome.get("mae_pct")
    record["mfe_pct"] = outcome.get("mfe_pct")
    record["version"] = book.get("version", 1)
    book["journal"].append(record)
    book["journal"] = book["journal"][-120:]
    book["samples"] = len(book["journal"]) + sum(1 for row in book["pending"] if row.get("resolved"))
    book["global_since"] = int(book.get("global_since") or 0) + 1
    _refresh_avoid(book)
    _maybe_tighten(book, when)
    _maybe_rollback(book, when)


def resolve_pending(book: dict, now_ts: float, fetch_price: Callable[[str, str], float | None], when: str) -> int:
    """Score rejected names once about four hours have passed. Returns how many resolved."""
    done = 0
    for row in book["pending"]:
        if row.get("resolved") or _f(row.get("check_after")) > now_ts:
            continue
        price = fetch_price(str(row.get("pair") or ""), str(row.get("chain") or "bsc"))
        entry = _f(row.get("price"))
        if not price or entry <= 0:
            continue
        change = (price / entry - 1.0) * 100.0
        missed = change >= 8.0
        features = row.get("features") or {}
        key, text = explain_feature(features)
        votes = features.get("votes") or {}
        for agent in AGENT_IDS:
            vote = (votes.get(agent) or {}).get("vote") or "NO"
            if vote != "NO":
                continue
            # A NO that missed a rally is a miss. A NO before a drop is a hit.
            _credit(book, agent, not missed)
            good = not missed
            _push_lesson(
                book,
                lesson_text(agent, "NO", good, key, text) + f" Cena {change:+.1f} % po 4 h.",
                agent,
                when,
            )
        row["resolved"] = True
        row["later_pct"] = round(change, 3)
        row["missed_winner"] = missed
        done += 1
    book["samples"] = len(book["journal"]) + sum(1 for row in book["pending"] if row.get("resolved"))
    return done


def _closed_pnls(book: dict) -> list[float]:
    return [_f((row.get("outcome") or {}).get("pnl_pct")) for row in book["journal"]]


def _refresh_avoid(book: dict) -> None:
    groups: dict[str, list[float]] = {}
    for row in book["journal"]:
        groups.setdefault(_bucket(row), []).append(_f((row.get("outcome") or {}).get("pnl_pct")))
    all_pnls = _closed_pnls(book)
    overall = (sum(1 for x in all_pnls if x > 0) / len(all_pnls)) if all_pnls else 0.5
    avoid = []
    for key, pnls in groups.items():
        if len(pnls) < 5:
            continue
        mean = sum(pnls) / len(pnls)
        wins = sum(1 for x in pnls if x > 0) / len(pnls)
        if mean < 0 and wins < max(0.25, overall - 0.15):
            avoid.append({
                "key": key,
                "trades": len(pnls),
                "mean_pnl": round(mean, 3),
                "win_rate": round(wins, 3),
                "why": "vsaj 5 poslov, povprečje pod ničlo in slabši zadetek od knjige",
            })
    book["avoid"] = avoid


def matches_avoid(book: dict, features: dict) -> str | None:
    key = _bucket(features)
    for row in book.get("avoid") or []:
        if row.get("key") == key:
            return key
    return None


def _tightened(agent: str, name: str, value: float) -> bool:
    default, _lo, _hi, direction = KNOBS[agent][name]
    if direction > 0:
        return value > default + 1e-9
    return value < default - 1e-9


def retune_votes(book: dict, votes: dict, features: dict) -> dict:
    """Flip a YES to NO when a learned, tighter threshold is missed. Never flips NO to YES."""
    params = book.get("params") or default_params()
    out = {agent: dict(vote) for agent, vote in votes.items()}

    def deny(agent: str, reason: str) -> None:
        row = dict(out.get(agent) or {})
        if row.get("vote") != "YES":
            return
        row["vote"] = "NO"
        row["reason"] = reason
        out[agent] = row

    ratio_bar = _f(params["kit"]["min_ratio"])
    if _tightened("kit", "min_ratio", ratio_bar) and _f(features.get("buy_ratio")) < ratio_bar:
        deny("kit", "učenje: razmerje nakupov pod mejo")
    ch5_bar = _f(params["trend"]["min_ch5"])
    if _tightened("trend", "min_ch5", ch5_bar) and _f(features.get("ch5")) <= ch5_bar:
        deny("trend", "učenje: 5m pod mejo")
    liq_bar = _f(params["varnost"]["min_liq"])
    if _tightened("varnost", "min_liq", liq_bar) and _f(features.get("liq_usd")) < liq_bar:
        deny("varnost", "učenje: likvidnost pod mejo")
    holder_bar = _f(params["varnost"]["max_holder"])
    holder = features.get("holder")
    if _tightened("varnost", "max_holder", holder_bar) and holder is not None and _f(holder) > holder_bar:
        deny("varnost", "učenje: imetnik nad mejo")
    ch1_bar = _f(params["bik_medved"]["max_ch1h"])
    if _tightened("bik_medved", "max_ch1h", ch1_bar) and _f(features.get("ch1h")) > ch1_bar:
        deny("bik_medved", "učenje: 1h nad mejo")
    slip_bar = _f(params["izvajanje"]["max_slip"])
    slip = _f(((votes.get("izvajanje") or {}).get("numbers") or {}).get("slip"), -1)
    if _tightened("izvajanje", "max_slip", slip_bar) and slip >= 0 and slip > slip_bar:
        deny("izvajanje", "učenje: zdrs nad mejo")
    return out


def decide(book: dict, votes: dict, features: dict) -> tuple[bool, str]:
    """4/5 and VARNOST stay mandatory. Weights and avoid-patterns can only refuse."""
    if not entry_allowed(votes):
        if (votes.get("varnost") or {}).get("vote") != "YES":
            return False, "VARNOST zavrača"
        return False, "manj kot 4/5"
    blocked = matches_avoid(book, features)
    if blocked:
        return False, f"izogibaj se {blocked}"
    yes_w = 0.0
    all_w = 0.0
    for agent in AGENT_IDS:
        weight = trust_score(book, agent)
        all_w += weight
        if (votes.get(agent) or {}).get("vote") == "YES":
            yes_w += weight
    if all_w and (yes_w / all_w) < 0.62:
        return False, "utež DA je prenizka"
    return True, "4/5 in VARNOST"


def _step(value: float, spec: tuple[float, float, float, int]) -> float:
    default, lo, hi, direction = spec
    scale = abs(value)
    if scale < 1e-9:
        scale = max(abs(default), abs(hi - lo) * 0.05, 0.1)
    delta = scale * 0.10
    nxt = value + delta if direction > 0 else value - delta
    return round(_clamp(nxt, lo, hi), 6)


def _maybe_tighten(book: dict, when: str) -> None:
    changed = False
    why_parts = []
    params = book["params"]
    for agent, knobs in KNOBS.items():
        if int(book["since_adjust"].get(agent, 0)) < 5:
            continue
        relevant = []
        for row in book["journal"]:
            vote = ((row.get("votes") or {}).get(agent) or {}).get("vote")
            if vote == "YES":
                relevant.append(_f((row.get("outcome") or {}).get("pnl_pct")))
        if len(relevant) < 5:
            book["since_adjust"][agent] = 0
            continue
        mean = sum(relevant[-8:]) / min(8, len(relevant))
        wins = sum(1 for x in relevant[-8:] if x > 0) / min(8, len(relevant))
        if mean >= 0 or wins >= 0.45:
            book["since_adjust"][agent] = 0
            continue
        name, spec = next(iter(knobs.items()))
        old = float(params[agent][name])
        new = _step(old, spec)
        if abs(new - old) < 1e-9:
            book["since_adjust"][agent] = 0
            continue
        params[agent][name] = new
        book["since_adjust"][agent] = 0
        changed = True
        why_parts.append(f"{AGENT_LABELS[agent]} {name} {old:.4g} → {new:.4g}")
    if int(book.get("global_since") or 0) >= 5:
        mae = [
            _f(row.get("mae_pct"))
            for row in book["journal"]
            if _f((row.get("outcome") or {}).get("pnl_pct")) <= 0 and row.get("mae_pct") is not None
        ]
        if len(mae) >= 5 and (sum(mae[-5:]) / 5) < -10:
            spec = GLOBAL["stop_loss"]
            old = float(params["global"]["stop_loss"])
            new = _step(old, spec)
            # Tighter stop is greater than -0.08 (closer to zero) and never below the floor.
            if new > old + 1e-6:
                params["global"]["stop_loss"] = new
                changed = True
                why_parts.append(f"stop {old:.3f} → {new:.3f}")
        book["global_since"] = 0
    if not changed:
        return
    if changed:
        book["version"] = int(book.get("version") or 1) + 1
        why = "zaostritev po vsaj 5 poslih: " + "; ".join(why_parts)
        book["history"].append({"version": book["version"], "why": why, "at": when, "params": json.loads(json.dumps(params))})
        book["history"] = book["history"][-20:]
        _push_lesson(book, "Lekcija knjige: " + why, "varnost", when)


def _maybe_rollback(book: dict, when: str) -> None:
    version = int(book.get("version") or 1)
    if version <= 1 or book.get("rolled_from") == version:
        return
    current = [row for row in book["journal"] if int(row.get("version") or 1) == version]
    if len(current) < 10:
        return
    previous = version - 1
    prior = [row for row in book["journal"] if int(row.get("version") or 1) == previous]
    if len(prior) < 3:
        return
    mean_now = sum(_f((row.get("outcome") or {}).get("pnl_pct")) for row in current[-10:]) / 10
    mean_old = sum(_f((row.get("outcome") or {}).get("pnl_pct")) for row in prior[-10:]) / len(prior[-10:])
    if mean_now >= mean_old:
        return
    old_params = None
    for row in book["history"]:
        if int(row.get("version") or 0) == previous:
            old_params = row.get("params")
    if not old_params:
        return
    book["params"] = _merge_params(old_params)
    book["version"] = version + 1
    book["rolled_from"] = version
    why = f"povratek na različico {previous}: zadnjih 10 poslov ({mean_now:.2f} %) je slabših od prejšnje ({mean_old:.2f} %)"
    book["history"].append({"version": book["version"], "why": why, "at": when, "params": book["params"]})
    _push_lesson(book, "Lekcija knjige: " + why, "varnost", when)


def stop_loss(book: dict | None) -> float:
    if not book:
        return float(config.STOP_LOSS)
    learned = _f((book.get("params") or {}).get("global", {}).get("stop_loss"), config.STOP_LOSS)
    # max() keeps the tighter (less negative) stop and refuses anything looser than config.
    return max(float(config.STOP_LOSS), learned)


def time_stop_sec(book: dict | None) -> float:
    if not book:
        return float(config.TIME_STOP_SEC)
    hours = _f((book.get("params") or {}).get("global", {}).get("time_stop_h"), 4)
    return min(float(config.TIME_STOP_SEC), max(1.0, hours) * 3600)


def max_open(book: dict | None) -> int:
    if not book:
        return int(config.MAX_OPEN)
    learned = int(_f((book.get("params") or {}).get("global", {}).get("max_open"), config.MAX_OPEN))
    return max(1, min(int(config.MAX_OPEN), learned))


def public_summary(book: dict | None) -> dict[str, Any]:
    if not book:
        book = fresh_book()
    last = book["history"][-1] if book.get("history") else {"version": 1, "why": "začetek"}
    trust = []
    for agent in AGENT_IDS:
        row = book["trust"][agent]
        trust.append({
            "id": agent,
            "label": AGENT_LABELS[agent],
            "score": round(trust_score(book, agent), 4),
            "alpha": row["alpha"],
            "beta": row["beta"],
        })
    return {
        "version": book.get("version", 1),
        "samples": int(book.get("samples") or 0),
        "trust": trust,
        "lessons": list(book.get("lessons") or [])[-8:],
        "change": last.get("why") or "",
        "avoid": list(book.get("avoid") or [])[:8],
        "params": book.get("params") or default_params(),
    }
