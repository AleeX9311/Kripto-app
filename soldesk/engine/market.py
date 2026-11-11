"""Public no-key market data clients with rate limits, caching, and graceful failure.

Never used to send transactions. Read-only HTTP + public RPC account reads.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

import config
from models import Candidate, utcnow


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        item = self._data.get(key)
        if not item:
            return None
        expires, value = item
        if expires < time.time():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (time.time() + ttl, value)


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()


class Http:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.HTTP_USER_AGENT,
                "Accept": "application/json",
            }
        )
        self.cache = TTLCache()
        self.dex_rl = RateLimiter(config.DEX_MIN_INTERVAL_SEC)
        self.gecko_rl = RateLimiter(config.GECKO_MIN_INTERVAL_SEC)
        self.rug_rl = RateLimiter(config.RUGCHECK_MIN_INTERVAL_SEC)
        self.rpc_rl = RateLimiter(config.RPC_MIN_INTERVAL_SEC)
        self.last_error: Optional[str] = None

    def _get(self, limiter: RateLimiter, url: str, params: Optional[dict] = None) -> Optional[Any]:
        cache_key = url + "|" + str(sorted((params or {}).items()))
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        delay = 2.0
        for attempt in range(4):
            limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=config.HTTP_TIMEOUT_SEC)
            except requests.RequestException as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                return None
            if resp.status_code == 429 and attempt < 3:
                self.last_error = f"429 rate limited: {url}"
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            break
        try:
            if resp.status_code >= 400:
                self.last_error = f"HTTP {resp.status_code}: {url}"
                return None
            data = resp.json()
            ttl = config.CACHE_TTL_SEC
            if "rugcheck" in url:
                ttl = config.RUGCHECK_TTL_SEC
            if "ohlcv" in url:
                ttl = config.OHLCV_TTL_SEC
            self.cache.set(cache_key, data, ttl)
            self.last_error = None
            return data
        except requests.RequestException as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        except ValueError as exc:
            self.last_error = f"JSON error: {exc}"
            return None

    def _post(self, limiter: RateLimiter, url: str, payload: dict) -> Optional[Any]:
        limiter.wait()
        try:
            resp = self.session.post(url, json=payload, timeout=config.HTTP_TIMEOUT_SEC)
            if resp.status_code >= 400:
                self.last_error = f"HTTP {resp.status_code}: {url}"
                return None
            self.last_error = None
            return resp.json()
        except requests.RequestException as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        except ValueError as exc:
            self.last_error = f"JSON error: {exc}"
            return None


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_ms(ms: Any) -> Optional[str]:
    try:
        if value := ms:
            return datetime_from_ms(int(value))
    except (TypeError, ValueError):
        return None
    return None


def datetime_from_ms(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _pick_base_quote(pair: dict) -> tuple[dict, dict]:
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    quote_addr = (quote.get("address") or "")
    if quote_addr not in config.QUOTE_MINTS and (base.get("address") or "") in config.QUOTE_MINTS:
        return quote, base
    return base, quote


def _is_quote(addr: str) -> bool:
    if not addr:
        return False
    if addr in config.QUOTE_MINTS:
        return True
    return addr.lower() in {item.lower() for item in config.QUOTE_MINTS}


def candidate_from_dex_pair(pair: dict, source: str = "LIVE") -> Optional[Candidate]:
    chain = (pair.get("chainId") or "").lower()
    if not pair or chain not in {"solana", "bsc"}:
        return None
    dex = (pair.get("dexId") or "").lower()
    base, quote = _pick_base_quote(pair)
    mint = base.get("address") or ""
    if not mint or _is_quote(mint):
        return None
    sym = (base.get("symbol") or "").strip()
    name = (base.get("name") or "").strip().lower()
    if sym in config.SKIP_SYMBOLS or name in config.SKIP_NAMES:
        return None
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    h1 = txns.get("h1") or {}
    m5 = txns.get("m5") or {}
    liq = pair.get("liquidity") or {}
    created = pair.get("pairCreatedAt")
    return Candidate(
        mint=mint,
        symbol=(base.get("symbol") or "???")[:16],
        name=(base.get("name") or base.get("symbol") or "unknown")[:40],
        pair_address=pair.get("pairAddress") or "",
        dex=dex or "unknown",
        price_usd=_f(pair.get("priceUsd")),
        liquidity_usd=_f(liq.get("usd")),
        volume_1h_usd=_f(vol.get("h1")),
        volume_24h_usd=_f(vol.get("h24")),
        volume_5m_usd=_f(vol.get("m5")),
        price_change_5m=_f(chg.get("m5")) if chg.get("m5") is not None else None,
        price_change_1h=_f(chg.get("h1")) if chg.get("h1") is not None else None,
        price_change_6h=_f(chg.get("h6")) if chg.get("h6") is not None else None,
        price_change_24h=_f(chg.get("h24")) if chg.get("h24") is not None else None,
        buys_1h=_i(h1.get("buys")),
        sells_1h=_i(h1.get("sells")),
        buys_5m=_i(m5.get("buys")),
        sells_5m=_i(m5.get("sells")),
        buyers_1h=None,
        sellers_1h=None,
        pair_created_at=_iso_ms(created) if created else None,
        fdv_usd=_f(pair.get("fdv")) if pair.get("fdv") is not None else None,
        quote_mint=quote.get("address"),
        source=source,
        chain=chain,
        raw={"via": "dexscreener", "pair": pair.get("url") or ""},
    )


class Market:
    def __init__(self) -> None:
        self.http = Http()
        self.live_ok = False
        self.last_source_error: Optional[str] = None

    @property
    def last_error(self) -> Optional[str]:
        return self.http.last_error or self.last_source_error

    # ----- DexScreener -------------------------------------------------
    def dex_token_profiles_latest(self, chain: str = "solana") -> list[dict]:
        data = self.http._get(
            self.http.dex_rl, f"{config.DEXSCREENER_BASE}/token-profiles/latest/v1"
        )
        if not isinstance(data, list):
            return []
        return [x for x in data if (x.get("chainId") or "").lower() == chain]

    def dex_tokens(self, mints: list[str], chain: str = "solana") -> list[dict]:
        """Hydrate up to 30 mints in one call. Returns pair objects."""
        out: list[dict] = []
        uniq = []
        seen = set()
        for m in mints:
            if m and m not in seen:
                seen.add(m)
                uniq.append(m)
        for i in range(0, len(uniq), 30):
            chunk = uniq[i : i + 30]
            url = f"{config.DEXSCREENER_BASE}/tokens/v1/{chain}/{','.join(chunk)}"
            data = self.http._get(self.http.dex_rl, url)
            if isinstance(data, list):
                out.extend(data)
            elif isinstance(data, dict):
                # some versions wrap as {pairs: [...]}
                pairs = data.get("pairs") or data.get("items") or []
                if isinstance(pairs, list):
                    out.extend(pairs)
        return out

    def dex_search(self, query: str, chain: str = "solana") -> list[dict]:
        data = self.http._get(
            self.http.dex_rl,
            f"{config.DEXSCREENER_BASE}/latest/dex/search",
            params={"q": query},
        )
        if not isinstance(data, dict):
            return []
        return [p for p in (data.get("pairs") or []) if (p.get("chainId") or "").lower() == chain]

    def dex_pair(self, pair_address: str) -> Optional[dict]:
        data = self.http._get(
            self.http.dex_rl,
            f"{config.DEXSCREENER_BASE}/latest/dex/pairs/solana/{pair_address}",
        )
        if not isinstance(data, dict):
            return None
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None

    def dex_token_pairs(self, mint: str) -> list[dict]:
        # fallback single-token endpoint
        data = self.http._get(
            self.http.dex_rl, f"{config.DEXSCREENER_BASE}/latest/dex/tokens/{mint}"
        )
        if isinstance(data, dict):
            return [p for p in (data.get("pairs") or []) if (p.get("chainId") or "").lower() == "solana"]
        return []

    # ----- GeckoTerminal -----------------------------------------------
    def gecko_pools(self, kind: str, network: str = "solana") -> list[dict]:
        """kind: new_pools | trending_pools"""
        data = self.http._get(
            self.http.gecko_rl,
            f"{config.GECKO_BASE}/networks/{network}/{kind}",
            params={"page": 1, "include": "base_token,quote_token,dex"},
        )
        if not isinstance(data, dict):
            return []
        included = {item.get("id"): item for item in (data.get("included") or [])}
        rows = []
        for item in data.get("data") or []:
            rows.append({"pool": item, "included": included})
        return rows

    def gecko_ohlcv(self, pool_address: str, aggregate: int = 5, limit: int = 80, network: str = "solana") -> Optional[list[list]]:
        """Returns list of [ts, o, h, l, c, vol] or None if unavailable."""
        data = self.http._get(
            self.http.gecko_rl,
            f"{config.GECKO_BASE}/networks/{network}/pools/{pool_address}/ohlcv/minute",
            params={"aggregate": aggregate, "limit": limit, "currency": "usd"},
        )
        if not isinstance(data, dict):
            return None
        attrs = (data.get("data") or {}).get("attributes") or {}
        series = attrs.get("ohlcv_list")
        if not isinstance(series, list) or not series:
            return None
        return series

    def candidates_from_gecko(self, kind: str, network: str = "solana") -> list[Candidate]:
        rows = self.gecko_pools(kind, network)
        out: list[Candidate] = []
        for row in rows:
            pool = row["pool"]
            included = row["included"]
            attrs = pool.get("attributes") or {}
            rel = pool.get("relationships") or {}
            base_rel = ((rel.get("base_token") or {}).get("data") or {})
            quote_rel = ((rel.get("quote_token") or {}).get("data") or {})
            dex_rel = ((rel.get("dex") or {}).get("data") or {})
            base = included.get(base_rel.get("id")) or {}
            quote = included.get(quote_rel.get("id")) or {}
            base_attrs = base.get("attributes") or {}
            quote_attrs = quote.get("attributes") or {}
            base_mint = base_attrs.get("address") or (base_rel.get("id") or "").split("_", 1)[-1]
            quote_mint = quote_attrs.get("address") or ""
            if not base_mint or _is_quote(base_mint):
                # flip if needed
                if quote_mint and not _is_quote(quote_mint):
                    base_mint, quote_mint = quote_mint, base_mint
                    base_attrs, quote_attrs = quote_attrs, base_attrs
                else:
                    continue
            vol = attrs.get("volume_usd") or {}
            tx = attrs.get("transactions") or {}
            h1 = tx.get("h1") or {}
            m5 = tx.get("m5") or {}
            created = attrs.get("pool_created_at")
            out.append(
                Candidate(
                    mint=base_mint,
                    symbol=(base_attrs.get("symbol") or "???")[:16],
                    name=(base_attrs.get("name") or base_attrs.get("symbol") or "unknown")[:40],
                    pair_address=attrs.get("address") or "",
                    dex=(dex_rel.get("id") or "unknown").lower(),
                    price_usd=_f(attrs.get("base_token_price_usd")),
                    liquidity_usd=_f(attrs.get("reserve_in_usd")),
                    volume_1h_usd=_f(vol.get("h1")),
                    volume_24h_usd=_f(vol.get("h24")),
                    volume_5m_usd=_f(vol.get("m5")),
                    price_change_5m=None,
                    price_change_1h=None,
                    price_change_6h=None,
                    price_change_24h=None,
                    buys_1h=_i(h1.get("buys")),
                    sells_1h=_i(h1.get("sells")),
                    buys_5m=_i(m5.get("buys")),
                    sells_5m=_i(m5.get("sells")),
                    buyers_5m=_i(m5.get("buyers")),
                    buyers_1h=_i(h1.get("buyers")),
                    sellers_1h=_i(h1.get("sellers")),
                    pair_created_at=created,
                    fdv_usd=_f(attrs.get("fdv_usd")) if attrs.get("fdv_usd") else None,
                    quote_mint=quote_mint or None,
                    source="LIVE",
                    chain="bsc" if network == "bsc" else "solana",
                    raw={"via": f"geckoterminal:{network}:{kind}"},
                )
            )
        return out

    # ----- RugCheck ----------------------------------------------------
    def goplus_bsc(self, contract: str) -> Optional[dict]:
        addr = (contract or "").lower()
        if not addr:
            return None
        data = self.http._get(self.http.rug_rl, config.GOPLUS_BSC_URL.format(addr=addr))
        if not isinstance(data, dict):
            return None
        result = data.get("result") or {}
        if not isinstance(result, dict):
            return None
        info = result.get(addr) or result.get(contract)
        if info is None and len(result) == 1:
            info = next(iter(result.values()))
        return info if isinstance(info, dict) else None

    def fourmeme_launches(self) -> list[Candidate]:
        """four.meme newest tokens. An empty list means the endpoint was blocked or down."""
        data = self.http._get(
            self.http.dex_rl,
            config.FOURMEME_LIST_URL,
            params={"orderBy": "TimeDesc", "listedPancake": "false", "pageIndex": 1, "pageSize": 20},
        )
        rows = []
        if isinstance(data, dict):
            rows = data.get("data") or data.get("list") or data.get("rows") or []
            if isinstance(rows, dict):
                rows = rows.get("list") or rows.get("tokenList") or []
        out: list[Candidate] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            mint = row.get("address") or row.get("tokenAddress") or row.get("contractAddress") or ""
            if not mint or _is_quote(mint):
                continue
            out.append(Candidate(
                mint=mint,
                symbol=str(row.get("symbol") or row.get("shortName") or "???")[:16],
                name=str(row.get("name") or row.get("tokenName") or "four.meme")[:40],
                pair_address=str(row.get("pairAddress") or ""),
                dex="four.meme",
                price_usd=_f(row.get("price") or row.get("priceUsd")),
                liquidity_usd=_f(row.get("liquidity") or row.get("liquidityUsd")),
                volume_1h_usd=_f(row.get("volume") or row.get("volume1h") or 0),
                pair_created_at=row.get("createDate") or row.get("createdAt"),
                source="LIVE",
                chain="bsc",
                raw={"via": "four.meme", "listedPancake": row.get("listedPancake")},
            ))
        return out

    def rugcheck_report(self, mint: str) -> Optional[dict]:
        return self.http._get(
            self.http.rug_rl, f"{config.RUGCHECK_BASE}/tokens/{mint}/report"
        )

    # ----- Solana RPC fallback (read-only) -----------------------------
    def rpc(self, method: str, params: list) -> Optional[Any]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        data = self.http._post(self.http.rpc_rl, config.SOLANA_RPC, payload)
        if not isinstance(data, dict) or "error" in data:
            if isinstance(data, dict):
                self.last_source_error = str(data.get("error"))
            return None
        return data.get("result")

    def token_largest_accounts(self, mint: str) -> Optional[list[dict]]:
        result = self.rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        if not isinstance(result, dict):
            return None
        value = result.get("value")
        return value if isinstance(value, list) else None

    def token_supply(self, mint: str) -> Optional[float]:
        result = self.rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
        if not isinstance(result, dict):
            return None
        value = (result.get("value") or {}).get("uiAmount")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def mint_authorities(self, mint: str) -> Optional[dict]:
        """Parse SPL token mint account for mint/freeze authority. None if unreadable."""
        result = self.rpc(
            "getAccountInfo",
            [mint, {"encoding": "base64", "commitment": "confirmed"}],
        )
        if not isinstance(result, dict) or not result.get("value"):
            return None
        try:
            import base64

            b64, _enc = result["value"]["data"]
            raw = base64.b64decode(b64)
        except (KeyError, ValueError, TypeError):
            return None
        # Classic SPL mint is 82 bytes; Token-2022 is longer but shares the header
        if len(raw) < 82:
            return None
        try:
            mint_opt = int.from_bytes(raw[0:4], "little")
            freeze_opt = int.from_bytes(raw[46:50], "little")
            # COption: 0 = None (revoked / unset), 1 = Some
            return {
                "mint_authority_set": mint_opt == 1,
                "freeze_authority_set": freeze_opt == 1,
                "decimals": raw[44],
            }
        except Exception:
            return None

    # ----- Prices for open positions -----------------------------------
    def prices_for(self, mints: list[str]) -> dict[str, float]:
        """Best-effort USD mid prices keyed by mint."""
        out: dict[str, float] = {}
        if not mints:
            return out
        pairs = self.dex_tokens(mints)
        # pick highest-liquidity pair per mint
        best: dict[str, tuple[float, float]] = {}
        for pair in pairs:
            cand = candidate_from_dex_pair(pair)
            if not cand or cand.price_usd <= 0:
                continue
            prev = best.get(cand.mint)
            if not prev or cand.liquidity_usd > prev[0]:
                best[cand.mint] = (cand.liquidity_usd, cand.price_usd)
        for mint, (_liq, px) in best.items():
            out[mint] = px
        return out

    def hydrate_candidates(self, mints: list[str], chain: str = "solana") -> list[Candidate]:
        pairs = self.dex_tokens(mints, chain)
        by_mint: dict[str, Candidate] = {}
        for pair in pairs:
            cand = candidate_from_dex_pair(pair)
            if not cand:
                continue
            existing = by_mint.get(cand.mint)
            if not existing or cand.liquidity_usd > existing.liquidity_usd:
                by_mint[cand.mint] = cand
        return list(by_mint.values())

    def token_pool_stats(self, mint: str, network: str = "solana") -> dict:
        """Unique buyers and volume from the token's busiest Gecko pool. Empty if missing."""
        data = self.http._get(
            self.http.gecko_rl,
            f"{config.GECKO_BASE}/networks/{network}/tokens/{mint}/pools",
            params={"page": 1},
        )
        pools = (data or {}).get("data") if isinstance(data, dict) else None
        if not pools:
            return {}
        attrs = (pools[0].get("attributes") or {})
        tx = attrs.get("transactions") or {}
        h1 = tx.get("h1") or {}
        m5 = tx.get("m5") or {}
        vol = attrs.get("volume_usd") or {}
        return {
            "buyers_1h": _i(h1.get("buyers")),
            "buyers_5m": _i(m5.get("buyers")),
            "volume_1h": _f(vol.get("h1"), 0) or None,
            "volume_5m": _f(vol.get("m5"), 0) or None,
            "pool": attrs.get("address") or "",
        }

    def sol_change_1h(self) -> Optional[float]:
        """SOL 1h percent change from the deepest SOL/USDC pair. None if missing."""
        pairs = self.dex_search("SOL")
        best = None
        best_liq = -1.0
        for pair in pairs:
            base = (pair.get("baseToken") or {}).get("address") or ""
            quote = (pair.get("quoteToken") or {}).get("address") or ""
            if config.SOL_MINT not in {base, quote}:
                continue
            liq = _f(((pair.get("liquidity") or {}).get("usd")))
            chg = (pair.get("priceChange") or {}).get("h1")
            if chg is None or liq < best_liq:
                continue
            best_liq = liq
            best = _f(chg)
        return best

    def ping(self) -> bool:
        """Cheap live check used at startup to decide LIVE vs fallback."""
        data = self.http._get(
            self.http.dex_rl,
            f"{config.DEXSCREENER_BASE}/token-profiles/latest/v1",
        )
        ok = data is not None
        self.live_ok = ok
        if not ok:
            self.last_source_error = self.http.last_error or "DexScreener unreachable"
        return ok
