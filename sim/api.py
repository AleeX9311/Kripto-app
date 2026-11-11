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
    """Read-only GET. A 429 waits and retries; it does not change a trading decision."""
    delay = 1.5
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            return {"__error__": str(e)}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as e:
            return {"__error__": str(e)}
    return {"__error__": "429 rate limited"}


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


def _chain_rows(data: Any, chains: tuple[str, ...] = ("bsc", "solana")) -> list[dict]:
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict) and x.get("chainId") in chains]


def fetch_token_boosts() -> list[dict]:
    data = _get(f"{config.DEX_BASE}/token-boosts/latest/v1")
    time.sleep(config.REQUEST_PAUSE)
    return _chain_rows(data)


def fetch_token_profiles() -> list[dict]:
    data = _get(f"{config.DEX_BASE}/token-profiles/latest/v1")
    time.sleep(config.REQUEST_PAUSE)
    return _chain_rows(data)


def fetch_token_pairs(addresses: list[str], chain: str = "bsc") -> list[dict]:
    """Fetch pairs for token addresses, 30 per DexScreener request."""
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
                if isinstance(p, dict) and p.get("chainId") == chain
            )
    return out


def fetch_top_pool(token: str, chain: str = "bsc") -> str | None:
    """Most liquid GeckoTerminal pool address for a token, if the API answers."""
    network = "solana" if chain == "solana" else "bsc"
    data = _get(f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{token}/pools?page=1")
    time.sleep(config.REQUEST_PAUSE)
    if not isinstance(data, dict) or "__error__" in data:
        return None
    rows = data.get("data") or []
    if not rows or not isinstance(rows[0], dict):
        return None
    return ((rows[0].get("attributes") or {}).get("address")) or None


def fetch_ohlcv(pool: str, chain: str = "bsc", timeframe: str = "minute", aggregate: int = 15, limit: int = 200) -> list[list]:
    """GeckoTerminal candles, oldest first: [ts, open, high, low, close, volume]."""
    network = "solana" if chain == "solana" else "bsc"
    url = (
        "https://api.geckoterminal.com/api/v2/networks/"
        f"{network}/pools/{pool}/ohlcv/{timeframe}"
        f"?aggregate={int(aggregate)}&limit={int(limit)}&currency=usd"
    )
    data = _get(url)
    time.sleep(config.REQUEST_PAUSE)
    if not isinstance(data, dict) or "__error__" in data:
        return []
    rows = ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 6:
            out.append([float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
    out.sort(key=lambda r: r[0])
    return out


def fetch_pair(pair_address: str, chain: str = "bsc") -> dict | None:
    data = _get(f"{config.DEX_BASE}/latest/dex/pairs/{chain}/{pair_address}")
    time.sleep(config.REQUEST_PAUSE)
    if isinstance(data, dict) and isinstance(data.get("pairs"), list) and data["pairs"]:
        return data["pairs"][0]
    if isinstance(data, dict) and isinstance(data.get("pair"), dict):
        return data["pair"]
    return None



def fetch_token_boosts_top() -> list[dict]:
    data = _get(f"{config.DEX_BASE}/token-boosts/top/v1")
    time.sleep(config.REQUEST_PAUSE)
    return _chain_rows(data)


def fetch_search_pairs(query: str) -> list[dict]:
    from urllib.parse import quote
    data = _get(f"{config.DEX_BASE}/latest/dex/search?q={quote(query)}")
    time.sleep(config.REQUEST_PAUSE)
    if isinstance(data, dict) and isinstance(data.get("pairs"), list):
        return [p for p in data["pairs"] if isinstance(p, dict) and p.get("chainId") == config.CHAIN_ID]
    return []


def fetch_gt_top_pools(page: int = 1, network: str = "bsc") -> list[dict]:
    """One page of the busiest pools on a GeckoTerminal network (about 20)."""
    data = _get(f"https://api.geckoterminal.com/api/v2/networks/{network}/pools?page={int(page)}")
    time.sleep(config.REQUEST_PAUSE)
    out: list[dict] = []
    prefix = f"{network}_"
    if not isinstance(data, dict):
        return out
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        rel = (item.get("relationships") or {}).get("base_token") or {}
        tid = str(((rel.get("data") or {}).get("id") or ""))
        if not tid.startswith(prefix):
            continue
        address = tid[len(prefix):]
        if network == "bsc" and not address.startswith("0x"):
            continue
        attrs = item.get("attributes") or {}
        name = str(attrs.get("name") or "")
        symbol = name.split("/")[0].strip().split()[0] if name else ""
        vol = attrs.get("volume_usd") or {}
        out.append({
            "address": address,
            "symbol": symbol,
            "chain": network,
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


def fetch_gt_pools(kind: str = "trending", network: str = "bsc") -> list[str]:
    """GeckoTerminal trending/new pools → base token addresses. No key."""
    path = "trending_pools" if kind == "trending" else "new_pools"
    data = _get(f"https://api.geckoterminal.com/api/v2/networks/{network}/{path}?page=1")
    time.sleep(config.REQUEST_PAUSE)
    out: list[str] = []
    prefix = f"{network}_"
    if not isinstance(data, dict):
        return out
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        rel = (item.get("relationships") or {}).get("base_token") or {}
        tid = str(((rel.get("data") or {}).get("id") or ""))
        if not tid.startswith(prefix):
            continue
        addr = tid[len(prefix):]
        if network == "bsc" and not addr.startswith("0x"):
            continue
        if addr:
            out.append(addr)
    return out


def fetch_gt_bsc_pools(kind: str = "trending") -> list[str]:
    return fetch_gt_pools(kind, "bsc")


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
    from scam import lp_locked_fraction
    locked = lp_locked_fraction(info)
    if locked is not None and locked < 0.80:
        return False, f"lp_locked={locked:.0%}"

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

    note = f"ok buy_tax={buy_tax} sell_tax={sell_tax}"
    if locked is None:
        note += " lp=neznan"
    else:
        note += f" lp={locked:.0%}"
    return True, note


def _on(obj: object) -> bool:
    if isinstance(obj, dict):
        return str(obj.get("status", "0")).lower() in ("1", "true")
    return str(obj).lower() in ("1", "true")


def solana_security(info: dict) -> tuple[bool, str]:
    """Stricter GoPlus Solana checks. info is one token object, no network."""
    if _on(info.get("mintable")):
        return False, "mintable"
    if _on(info.get("freezable")):
        return False, "freezable"
    if _on(info.get("closable")):
        return False, "closable"
    if str(info.get("non_transferable", "0")) in ("1", "true"):
        return False, "non_transferable"
    mutable = info.get("metadata_mutable") or {}
    if isinstance(mutable, dict) and _on(mutable):
        auths = mutable.get("metadata_upgrade_authority") or []
        suspicious = False
        for auth in auths if isinstance(auths, list) else []:
            if isinstance(auth, dict) and int(auth.get("malicious_address") or 0) == 1:
                suspicious = True
        if suspicious:
            return False, "metadata_mutable_malicious"
    fee = info.get("transfer_fee")
    if isinstance(fee, dict) and fee:
        return False, "transfer_fee"
    hook = info.get("transfer_hook")
    if (isinstance(hook, list) and hook) or (isinstance(hook, dict) and hook):
        return False, "transfer_hook"
    percents: list[float] = []
    holders = info.get("holders") or []
    if isinstance(holders, list):
        for holder in holders[:10]:
            if not isinstance(holder, dict):
                continue
            try:
                percents.append(float(holder.get("percent") or 0))
            except (TypeError, ValueError):
                continue
    if percents and max(percents) > 0.30:
        return False, f"top_holder={max(percents):.0%}"
    if sum(percents[:5]) > 0.60:
        return False, "holder_concentration"
    return True, "ok solana"


def goplus_solana_ok(mint: str) -> tuple[bool, str]:
    """Solana token security via GoPlus. Soft-pass only if the API itself fails."""
    url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={mint}"
    data = _get(url, timeout=15.0)
    time.sleep(config.REQUEST_PAUSE)
    if not isinstance(data, dict) or "__error__" in data:
        return True, "goplus_unavailable"
    if str(data.get("code")) not in ("1", "1.0") and data.get("code") != 1:
        if not data.get("result"):
            return True, f"goplus_code={data.get('code')}"
    result = data.get("result") or {}
    info = None
    if isinstance(result, dict):
        info = result.get(mint) or result.get(mint.lower())
        if info is None and len(result) == 1:
            info = next(iter(result.values()))
    if not isinstance(info, dict):
        return True, "goplus_no_info"
    return solana_security(info)
