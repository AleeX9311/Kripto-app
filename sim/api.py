"""Public API helpers — no keys. BSC / DexScreener / GoPlus / Binance."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

import config

UA = "PaperTraderSim/1.0 (simulation-only; contact=local)"


def _get(url: str, timeout: float = 20.0) -> Any | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        return {"__error__": str(e)}


def _positive_price(value) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def fetch_binance_eur_usd() -> float | None:
    """USD for 1 EUR from Binance EURUSDT. None when the API is blocked or down."""
    data = _get(config.BINANCE_EURUSDT)
    if isinstance(data, dict) and "__error__" not in data and "price" in data:
        return _positive_price(data.get("price"))
    return None


def fetch_frankfurter_eur_usd() -> float | None:
    """ECB USD for 1 EUR. Used only when the book has no rate yet."""
    data = _get("https://api.frankfurter.dev/v1/latest?base=EUR&symbols=USD")
    if isinstance(data, dict) and "__error__" not in data:
        rates = data.get("rates")
        if isinstance(rates, dict):
            return _positive_price(rates.get("USD"))
    return None


def fetch_eur_usd() -> float | None:
    """Prefer Binance. Frankfurter is only a bootstrap when Binance has no price."""
    return fetch_binance_eur_usd() or fetch_frankfurter_eur_usd()


def fetch_token_boosts() -> list[dict]:
    data = _get(f"{config.DEX_BASE}/token-boosts/latest/v1")
    time.sleep(config.REQUEST_PAUSE)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict) and x.get("chainId") == config.CHAIN_ID]
    return []


def fetch_token_profiles() -> list[dict]:
    data = _get(f"{config.DEX_BASE}/token-profiles/latest/v1")
    time.sleep(config.REQUEST_PAUSE)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict) and x.get("chainId") == config.CHAIN_ID]
    return []


def fetch_token_pairs(addresses: list[str]) -> list[dict]:
    """Fetch BSC pairs for token addresses, 30 per DexScreener request."""
    if not addresses:
        return []
    out: list[dict] = []
    for i in range(0, len(addresses), 30):
        chunk = ",".join(addresses[i:i + 30])
        data = _get(f"{config.DEX_BASE}/latest/dex/tokens/{chunk}")
        time.sleep(config.REQUEST_PAUSE)
        if isinstance(data, dict) and isinstance(data.get("pairs"), list):
            out.extend(
                p for p in data["pairs"]
                if isinstance(p, dict) and p.get("chainId") == config.CHAIN_ID
            )
    return out


def fetch_pair(pair_address: str) -> dict | None:
    data = _get(f"{config.DEX_BASE}/latest/dex/pairs/{config.CHAIN_ID}/{pair_address}")
    time.sleep(config.REQUEST_PAUSE)
    if isinstance(data, dict) and isinstance(data.get("pairs"), list) and data["pairs"]:
        return data["pairs"][0]
    if isinstance(data, dict) and isinstance(data.get("pair"), dict):
        return data["pair"]
    return None



def fetch_token_boosts_top() -> list[dict]:
    data = _get(f"{config.DEX_BASE}/token-boosts/top/v1")
    time.sleep(config.REQUEST_PAUSE)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict) and x.get("chainId") == config.CHAIN_ID]
    return []


def fetch_search_pairs(query: str) -> list[dict]:
    from urllib.parse import quote
    data = _get(f"{config.DEX_BASE}/latest/dex/search?q={quote(query)}")
    time.sleep(config.REQUEST_PAUSE)
    if isinstance(data, dict) and isinstance(data.get("pairs"), list):
        return [p for p in data["pairs"] if isinstance(p, dict) and p.get("chainId") == config.CHAIN_ID]
    return []


def fetch_gt_top_pools(page: int = 1) -> list[dict]:
    """One page of the busiest BSC pools (about 20). No API key."""
    data = _get(f"https://api.geckoterminal.com/api/v2/networks/bsc/pools?page={int(page)}")
    time.sleep(config.REQUEST_PAUSE)
    out: list[dict] = []
    if not isinstance(data, dict):
        return out
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        rel = (item.get("relationships") or {}).get("base_token") or {}
        tid = ((rel.get("data") or {}).get("id") or "")
        if not str(tid).startswith("bsc_0x"):
            continue
        attrs = item.get("attributes") or {}
        name = str(attrs.get("name") or "")
        symbol = name.split("/")[0].strip().split()[0] if name else ""
        vol = attrs.get("volume_usd") or {}
        out.append({
            "address": tid.split("_", 1)[1],
            "symbol": symbol,
            "reserve_usd": _num(attrs.get("reserve_in_usd")),
            "vol24_usd": _num(vol.get("h24") if isinstance(vol, dict) else None),
            "mcap_usd": _num(attrs.get("market_cap_usd") or attrs.get("fdv_usd")),
        })
    return out


def _num(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_gt_bsc_pools(kind: str = "trending") -> list[str]:
    """GeckoTerminal BSC trending/new pools → base token addresses. No key."""
    path = "trending_pools" if kind == "trending" else "new_pools"
    data = _get(f"https://api.geckoterminal.com/api/v2/networks/bsc/{path}?page=1")
    time.sleep(config.REQUEST_PAUSE)
    out: list[str] = []
    if not isinstance(data, dict):
        return out
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        rel = (item.get("relationships") or {}).get("base_token") or {}
        tid = ((rel.get("data") or {}).get("id") or "")
        # id like "bsc_0xabc..."
        if "_" in tid:
            addr = tid.split("_", 1)[1]
            if addr.startswith("0x"):
                out.append(addr)
    return out


def goplus_ok(contract: str) -> tuple[bool, str]:
    """BSC token security via GoPlus. Return (ok, reason). Soft-pass if API fails."""
    addr = contract.lower()
    url = config.GOPLUS_URL.format(addr=addr)
    data = _get(url, timeout=15.0)
    time.sleep(config.REQUEST_PAUSE)
    if not isinstance(data, dict) or "__error__" in data:
        return True, "goplus_unavailable"
    if str(data.get("code")) not in ("1", "1.0") and data.get("code") != 1:
        if not data.get("result"):
            return True, f"goplus_code={data.get('code')}"
    result = data.get("result") or {}
    if isinstance(result, dict):
        info = result.get(addr) or result.get(contract) or None
        if info is None and len(result) == 1:
            info = next(iter(result.values()))
        if not isinstance(info, dict):
            return True, "goplus_no_info"
    else:
        return True, "goplus_bad_result"

    def flag(name: str) -> str:
        return str(info.get(name, "0")).lower()

    if flag("is_honeypot") in ("1", "true"):
        return False, "honeypot"
    if flag("cannot_sell_all") in ("1", "true"):
        return False, "cannot_sell_all"
    if flag("can_take_back_ownership") in ("1", "true"):
        return False, "can_take_back_ownership"
    if flag("is_proxy") in ("1", "true") and flag("is_open_source") in ("0", "false"):
        return False, "proxy_not_open_source"

    try:
        buy_tax = float(info.get("buy_tax") or 0)
        sell_tax = float(info.get("sell_tax") or 0)
    except (TypeError, ValueError):
        buy_tax, sell_tax = 0.0, 0.0
    if buy_tax > 0.10 or sell_tax > 0.10:
        return False, f"tax buy={buy_tax} sell={sell_tax}"

    owner = flag("owner_address")
    renounced = owner in (
        "",
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "null",
        "none",
    )
    if flag("is_mintable") in ("1", "true") and not renounced:
        return False, "mintable_not_renounced"
    if flag("owner_change_balance") in ("1", "true") and not renounced:
        return False, "owner_change_balance"
    if flag("hidden_owner") in ("1", "true"):
        return False, "hidden_owner"
    if flag("is_blacklisted") in ("1", "true"):
        return False, "blacklisted"
    if flag("transfer_pausable") in ("1", "true") and not renounced:
        return False, "transfer_pausable"

    return True, f"ok buy_tax={buy_tax} sell_tax={sell_tax}"
