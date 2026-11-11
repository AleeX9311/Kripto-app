"""Filter — hard kills. Missing data is a FAIL (ni podatka). Capital preservation first."""

from __future__ import annotations

from typing import Any, Optional

import config
from market import Market
from models import Candidate, Event, FilterCheck, FilterResult, iso
from simfeed import SimFeed


def _holder_pct(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 1:
        return num * 100.0
    return num


def _fourmeme_prebond(cand: Candidate) -> bool:
    dex = (cand.dex or "").lower()
    via = str((cand.raw or {}).get("via") or "").lower()
    listed = (cand.raw or {}).get("listedPancake")
    if listed is False or str(listed).lower() == "false":
        return True
    return ("four" in dex or "four.meme" in via) and "pancake" not in dex


def bsc_security_checks(info: Optional[dict], launch: bool, prebond: bool) -> list[FilterCheck]:
    """GoPlus BSC rules. A missing report fails closed."""
    if not isinstance(info, dict) or not info:
        return [FilterCheck("goplus", False, "ni podatka (GoPlus)", None)]
    checks: list[FilterCheck] = []

    def flag(name: str) -> Optional[str]:
        if name not in info or info.get(name) in (None, ""):
            return None
        return str(info.get(name)).lower()

    honeypot = flag("is_honeypot")
    if honeypot is None:
        checks.append(FilterCheck("honeypot", False, "ni podatka (honeypot)", None))
    elif honeypot in ("1", "true"):
        checks.append(FilterCheck("honeypot", False, "honeypot", True))
    else:
        checks.append(FilterCheck("honeypot", True, "ni honeypot", False))
    cannot = flag("cannot_sell_all")
    if cannot is None:
        checks.append(FilterCheck("cannot_sell", False, "ni podatka (prodaja)", None))
    elif cannot in ("1", "true"):
        checks.append(FilterCheck("cannot_sell", False, "ni mogoče prodati", True))
    else:
        checks.append(FilterCheck("cannot_sell", True, "prodaja je mogoča", False))
    try:
        buy_tax = float(info.get("buy_tax") or 0)
        sell_tax = float(info.get("sell_tax") or 0)
    except (TypeError, ValueError):
        buy_tax = sell_tax = None
    if info.get("buy_tax") in (None, "") or info.get("sell_tax") in (None, ""):
        checks.append(FilterCheck("tax", False, "ni podatka (davek)", None))
    elif buy_tax is None or sell_tax is None or buy_tax > 0.10 or sell_tax > 0.10:
        checks.append(FilterCheck("tax", False, f"davek nakup {buy_tax} prodaja {sell_tax} > 10%", None))
    else:
        checks.append(FilterCheck("tax", True, f"davek nakup {buy_tax:.1%} prodaja {sell_tax:.1%}", None))
    for name, label in (
        ("is_mintable", "lastnik lahko kuje"),
        ("hidden_owner", "skriti lastnik"),
        ("is_proxy", "proxy"),
        ("is_blacklisted", "črna lista"),
        ("trading_cooldown", "cooldown trgovanja"),
        ("transfer_pausable", "trgovanje se da ustaviti"),
    ):
        value = flag(name)
        if value is None:
            checks.append(FilterCheck(name, False, f"ni podatka ({label})", None))
        elif value in ("1", "true"):
            checks.append(FilterCheck(name, False, label, True))
        else:
            checks.append(FilterCheck(name, True, f"ni: {label}", False))
    if launch and prebond:
        checks.append(FilterCheck("lp_lock", True, "four.meme pred vezavo — LP lock ne velja", "curve"))
    else:
        locked = _bsc_lp_locked(info)
        if locked is None:
            checks.append(FilterCheck("lp_lock", False, "ni podatka (LP lock/burn)", None))
        elif locked < 0.80:
            checks.append(FilterCheck("lp_lock", False, f"LP ni zaklenjen/zažgan ({locked:.0%})", locked))
        else:
            checks.append(FilterCheck("lp_lock", True, f"LP zaklenjen/zažgan {locked:.0%}", locked))
    top10 = _bsc_top10(info)
    if top10 is None:
        checks.append(FilterCheck("top10_holders", False, "ni podatka (top 10 holders)", None))
    elif top10 > config.MAX_TOP10_HOLDER_PCT:
        checks.append(FilterCheck("top10_holders", False, f"top 10 holders {top10:.1f}% > {config.MAX_TOP10_HOLDER_PCT:.0f}%", top10))
    else:
        checks.append(FilterCheck("top10_holders", True, f"top 10 holders {top10:.1f}%", top10))
    rugs = info.get("honeypot_with_same_creator")
    if rugs in (None, ""):
        checks.append(FilterCheck("dev_rug_link", False, "ni podatka (povezava z rugi)", None))
    else:
        try:
            n = int(float(rugs))
        except (TypeError, ValueError):
            n = None
        if n is None:
            checks.append(FilterCheck("dev_rug_link", False, "ni podatka (povezava z rugi)", rugs))
        elif n > 0:
            checks.append(FilterCheck("dev_rug_link", False, f"ustvarjalec povezan z {n} rugi", n))
        else:
            checks.append(FilterCheck("dev_rug_link", True, "ni povezave z rugi", 0))
    return checks


def _bsc_lp_locked(info: dict) -> Optional[float]:
    holders = info.get("lp_holders") or []
    if not isinstance(holders, list) or not holders:
        return None
    locked = 0.0
    seen = False
    dead = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        pct = _holder_pct(holder.get("percent"))
        if pct is None:
            continue
        seen = True
        addr = str(holder.get("address") or "").lower()
        tag = str(holder.get("tag") or "").lower()
        is_locked = str(holder.get("is_locked", "0")).lower() in ("1", "true")
        if addr in dead or "burn" in tag or "lock" in tag or "dead" in tag or is_locked:
            locked += pct / 100.0
    return locked if seen else None


def _bsc_top10(info: dict) -> Optional[float]:
    holders = info.get("holders") or []
    if not isinstance(holders, list) or not holders:
        return None
    lp_addrs = {str(h.get("address") or "").lower() for h in (info.get("lp_holders") or []) if isinstance(h, dict)}
    dead = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
    shares = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        addr = str(holder.get("address") or "").lower()
        tag = str(holder.get("tag") or "").lower()
        locked = str(holder.get("is_locked", "0")).lower() in ("1", "true")
        if addr in dead or addr in lp_addrs or locked or any(word in tag for word in ("lp", "lock", "burn", "dead", "null", "pancake", "four")):
            continue
        pct = _holder_pct(holder.get("percent"))
        if pct is None:
            continue
        shares.append(pct)
    if not shares:
        return None
    return float(sum(sorted(shares, reverse=True)[:10]))


def _excluded_holder(owner: str, row: dict, known_lp: set[str], mint: Optional[str]) -> bool:
    """LP, bonding-curve, and known program accounts are not holders."""
    if not owner or owner == mint:
        return True
    if owner in config.BURN_ADDRESSES or owner in config.PROTOCOL_ACCOUNTS or owner in known_lp:
        return True
    blob = " ".join(str(v).lower() for v in row.values() if isinstance(v, (str, int, float)))
    return any(word in blob for word in ("bonding", "bonding curve", "curve", "amm vault", "liquidity pool", "lp vault"))


def _as_addr(value: Any) -> Optional[str]:
    if value is None or value is False:
        return None
    if isinstance(value, str):
        v = value.strip()
        if not v or v in {"11111111111111111111111111111111", "null", "None"}:
            return None
        return v
    if isinstance(value, dict):
        return _as_addr(value.get("address") or value.get("pubkey") or value.get("owner"))
    return str(value) if value else None


class Filter:
    def __init__(
        self,
        market: Market,
        sim: Optional[SimFeed] = None,
        blacklisted_creators: Optional[set[str]] = None,
        observed_liquidity: Optional[dict[str, float]] = None,
        data_source: str = "LIVE",
    ) -> None:
        self.market = market
        self.sim = sim
        self.data_source = data_source
        self.blacklisted_creators = blacklisted_creators if blacklisted_creators is not None else set()
        self.observed_liquidity = observed_liquidity if observed_liquidity is not None else {}

    def note_liquidity(self, mint: str, liquidity_usd: float) -> None:
        if liquidity_usd <= 0:
            return
        prev = self.observed_liquidity.get(mint)
        # track the peak so an 80% collapse vs peak counts as a rug
        if prev is None or liquidity_usd > prev:
            self.observed_liquidity[mint] = liquidity_usd

    def detect_liquidity_rugs(self, candidates: list[Candidate]) -> list[str]:
        """If liquidity collapsed >80% vs peak, blacklist the creator when known later."""
        rugged_mints: list[str] = []
        for c in candidates:
            peak = self.observed_liquidity.get(c.mint)
            if peak and peak > 0 and c.liquidity_usd <= peak * (1 - config.RUG_LIQUIDITY_COLLAPSE_PCT):
                rugged_mints.append(c.mint)
            self.note_liquidity(c.mint, c.liquidity_usd)
        return rugged_mints

    def check(self, cand: Candidate) -> tuple[FilterResult, Event]:
        if getattr(cand, "chain", "solana") == "bsc":
            return self.check_bsc(cand, launch=False)
        checks: list[FilterCheck] = []
        creator: Optional[str] = None
        top10: Optional[float] = None
        mint_revoked: Optional[bool] = None
        freeze_set: Optional[bool] = None
        lp_ok: Optional[bool] = None
        report_src = "none"

        # 1) 1h volume — cheap, no extra API
        vol = cand.volume_1h_usd
        if vol is None:
            checks.append(FilterCheck("volume_1h", False, "ni podatka (1h volume)", None))
        elif vol < config.MIN_VOLUME_1H_USD:
            checks.append(
                FilterCheck(
                    "volume_1h",
                    False,
                    f"1h volume ${vol:,.0f} < ${config.MIN_VOLUME_1H_USD:,.0f}",
                    vol,
                )
            )
        else:
            checks.append(FilterCheck("volume_1h", True, f"1h volume ${vol:,.0f}", vol))

        age = cand.age_seconds()
        if age is None:
            checks.append(FilterCheck("age", False, "ni podatka (starost)", None))
        elif age < config.MIN_TOKEN_AGE_MIN * 60:
            checks.append(
                FilterCheck(
                    "age",
                    False,
                    f"starost {age / 60:.0f} min < {config.MIN_TOKEN_AGE_MIN:.0f} min",
                    age,
                )
            )
        else:
            checks.append(FilterCheck("age", True, f"starost {age / 60:.0f} min", age))
        h1 = cand.price_change_1h
        if age is not None and age < 3 * 3600 and h1 is not None and h1 <= config.DUMP_AFTER_BOND_1H:
            checks.append(
                FilterCheck(
                    "dump_after_bond",
                    False,
                    f"dump po lansiranju, 1h {h1:.1f}%",
                    h1,
                )
            )

        # If volume or age already failed, skip expensive rugcheck/RPC.
        # because we did not look them up. Reasons stay precise: volume kill is enough.
        volume_failed = any(c.name in {"volume_1h", "age", "dump_after_bond"} and not c.passed for c in checks)
        if volume_failed:
            passed = False
            result = FilterResult(
                mint=cand.mint,
                symbol=cand.symbol,
                passed=False,
                checks=checks,
                volume_1h_usd=vol,
                data_source="volume-only",
            )
            reasons = "; ".join(result.kill_reasons)
            ev = Event(
                ts=iso(),
                agent="FILTER",
                level="kill",
                message=f"{cand.symbol}  KILL  {reasons}",
                mint=cand.mint,
                symbol=cand.symbol,
                extra={"reasons": result.kill_reasons},
            )
            return result, ev

        safety = self._safety(cand)
        report_src = safety.get("source") or "none"
        creator = safety.get("creator")

        # 2) mint authority must be revoked
        mint_set = safety.get("mint_authority_set")
        if mint_set is None:
            mint_revoked = None
            checks.append(FilterCheck("mint_authority", False, "ni podatka (mint authority)", None))
        elif mint_set:
            mint_revoked = False
            checks.append(FilterCheck("mint_authority", False, "mint authority not revoked", True))
        else:
            mint_revoked = True
            checks.append(FilterCheck("mint_authority", True, "mint authority revoked", False))

        # 3) freeze authority must NOT be set
        frz = safety.get("freeze_authority_set")
        if frz is None:
            freeze_set = None
            checks.append(FilterCheck("freeze_authority", False, "ni podatka (freeze authority)", None))
        elif frz:
            freeze_set = True
            checks.append(FilterCheck("freeze_authority", False, "freeze authority is set", True))
        else:
            freeze_set = False
            checks.append(FilterCheck("freeze_authority", True, "freeze authority unset", False))

        # 4) LP locked or burned
        lp_ok = safety.get("lp_locked_or_burned")
        lp_detail = safety.get("lp_detail")
        if lp_ok is None:
            checks.append(FilterCheck("lp_lock", False, "ni podatka (LP lock/burn)", None))
        elif not lp_ok:
            checks.append(
                FilterCheck("lp_lock", False, f"LP not locked/burned{(' — ' + str(lp_detail)) if lp_detail else ''}", lp_detail)
            )
        else:
            checks.append(FilterCheck("lp_lock", True, f"LP locked/burned{(' — ' + str(lp_detail)) if lp_detail else ''}", lp_detail))

        # 5) top 10 holders above the configured cap (35%)
        top10 = safety.get("top10_pct")
        if top10 is None:
            checks.append(FilterCheck("top10_holders", False, "ni podatka (top 10 holders)", None))
        elif top10 > config.MAX_TOP10_HOLDER_PCT:
            checks.append(
                FilterCheck(
                    "top10_holders",
                    False,
                    f"top 10 holders {top10:.1f}% > {config.MAX_TOP10_HOLDER_PCT:.0f}%",
                    top10,
                )
            )
        else:
            checks.append(FilterCheck("top10_holders", True, f"top 10 holders {top10:.1f}%", top10))

        # 6) dev wallet linked to previous rugs
        rug_link, rug_reason = self._dev_rug_link(safety, creator)
        if rug_link is None:
            checks.append(FilterCheck("dev_rug_link", False, "ni podatka (creator history)", None))
        elif rug_link:
            checks.append(FilterCheck("dev_rug_link", False, rug_reason or "dev wallet linked to previous rugs", creator))
        else:
            checks.append(FilterCheck("dev_rug_link", True, "no creator rug link found", creator))

        self._launch_risks(checks, safety)

        passed = all(c.passed for c in checks)
        result = FilterResult(
            mint=cand.mint,
            symbol=cand.symbol,
            passed=passed,
            checks=checks,
            creator=creator,
            top10_pct=top10,
            mint_revoked=mint_revoked,
            freeze_set=freeze_set,
            lp_locked_or_burned=lp_ok,
            volume_1h_usd=vol,
            data_source=report_src,
        )
        if passed:
            ev = Event(
                ts=iso(),
                agent="FILTER",
                level="ok",
                message=f"{cand.symbol}  PASS  ({report_src})",
                mint=cand.mint,
                symbol=cand.symbol,
            )
        else:
            reasons = "; ".join(result.kill_reasons)
            ev = Event(
                ts=iso(),
                agent="FILTER",
                level="kill",
                message=f"{cand.symbol}  KILL  {reasons}",
                mint=cand.mint,
                symbol=cand.symbol,
                extra={"reasons": result.kill_reasons},
            )
        return result, ev

    def check_bsc(self, cand: Candidate, launch: bool) -> tuple[FilterResult, Event]:
        """BSC filter via GoPlus. Missing security data fails closed."""
        checks: list[FilterCheck] = []
        if not launch:
            vol = cand.volume_1h_usd
            if vol is None:
                checks.append(FilterCheck("volume_1h", False, "ni podatka (1h volume)", None))
            elif vol < config.MIN_VOLUME_1H_USD:
                checks.append(FilterCheck("volume_1h", False, f"1h volume ${vol:,.0f} < ${config.MIN_VOLUME_1H_USD:,.0f}", vol))
            else:
                checks.append(FilterCheck("volume_1h", True, f"1h volume ${vol:,.0f}", vol))
            age = cand.age_seconds()
            if age is None:
                checks.append(FilterCheck("age", False, "ni podatka (starost)", None))
            elif age < config.MIN_TOKEN_AGE_MIN * 60:
                checks.append(FilterCheck("age", False, f"starost {age / 60:.0f} min < {config.MIN_TOKEN_AGE_MIN:.0f} min", age))
            else:
                checks.append(FilterCheck("age", True, f"starost {age / 60:.0f} min", age))
            if any(not c.passed for c in checks):
                return self._finish_chain(cand, checks, "bsc-cheap", launch)
        else:
            if self.data_source != "SIM" and cand.buyers_1h is None and cand.buyers_5m is None:
                stats = self.market.token_pool_stats(cand.mint, "bsc")
                if stats.get("buyers_1h") is not None:
                    cand.buyers_1h = stats["buyers_1h"]
                if stats.get("buyers_5m") is not None:
                    cand.buyers_5m = stats["buyers_5m"]
                if not cand.volume_5m_usd and stats.get("volume_5m"):
                    cand.volume_5m_usd = stats["volume_5m"]
                if not cand.pair_address and stats.get("pool"):
                    cand.pair_address = stats["pool"]
            age = cand.age_seconds()
            minutes = None if age is None else age / 60.0
            volume = self._launch_volume(cand, minutes)
            need = config.LAUNCH_MIN_VOLUME_USD if minutes is None else config.LAUNCH_MIN_VOLUME_USD * min(max(minutes, 1.0), config.LAUNCH_VOLUME_WINDOW_MIN) / config.LAUNCH_VOLUME_WINDOW_MIN
            if volume is None:
                checks.append(FilterCheck("launch_volume", False, "ni podatka (volumen lansiranja)", None))
            elif volume < need:
                checks.append(FilterCheck("launch_volume", False, f"volumen ${volume:,.0f} < ${need:,.0f} za starost", volume))
            else:
                checks.append(FilterCheck("launch_volume", True, f"volumen ${volume:,.0f}", volume))
            buyers = cand.buyers_5m if cand.buyers_5m is not None else cand.buyers_1h
            if buyers is None:
                checks.append(FilterCheck("launch_buyers", False, "ni podatka (unikatni kupci)", None))
            elif buyers < config.LAUNCH_MIN_BUYERS:
                checks.append(FilterCheck("launch_buyers", False, f"unikatni kupci {buyers} < {config.LAUNCH_MIN_BUYERS}", buyers))
            else:
                checks.append(FilterCheck("launch_buyers", True, f"unikatni kupci {buyers}", buyers))
            multiple = self._launch_multiple(cand)
            if multiple is None:
                checks.append(FilterCheck("launch_multiple", False, "ni podatka (cena od lansiranja)", None))
            elif multiple > config.LAUNCH_MAX_MULTIPLE:
                checks.append(FilterCheck("launch_multiple", False, f"že {multiple:.1f}x od lansiranja > {config.LAUNCH_MAX_MULTIPLE:.0f}x", multiple))
            else:
                checks.append(FilterCheck("launch_multiple", True, f"{multiple:.2f}x od lansiranja", multiple))
            if any(not c.passed for c in checks):
                return self._finish_chain(cand, checks, "bsc-launch-cheap", True)
        info = self.market.goplus_bsc(cand.mint) if self.data_source != "SIM" else None
        checks.extend(bsc_security_checks(info, launch=launch, prebond=_fourmeme_prebond(cand)))
        return self._finish_chain(cand, checks, "goplus" if info else "goplus-missing", launch)

    def _finish_chain(self, cand: Candidate, checks: list[FilterCheck], source: str, launch: bool) -> tuple[FilterResult, Event]:
        passed = all(c.passed for c in checks)
        result = FilterResult(
            mint=cand.mint, symbol=cand.symbol, passed=passed, checks=checks,
            volume_1h_usd=cand.volume_1h_usd, data_source=source,
        )
        label = "NOVI LANS" if launch else "GLAVNI"
        lane = "novi_lans" if launch else "glavni"
        chain = getattr(cand, "chain", "solana")
        if passed:
            ev = Event(ts=iso(), agent="FILTER", level="ok", message=f"{cand.symbol}  {label} PASS  ({source})", mint=cand.mint, symbol=cand.symbol, extra={"lane": lane, "chain": chain})
        else:
            reasons = "; ".join(result.kill_reasons)
            ev = Event(ts=iso(), agent="FILTER", level="kill", message=f"{cand.symbol}  {label} KILL  {reasons}", mint=cand.mint, symbol=cand.symbol, extra={"lane": lane, "chain": chain, "reasons": result.kill_reasons})
        return result, ev

    def check_launch(self, cand: Candidate) -> tuple[FilterResult, Event]:
        if getattr(cand, "chain", "solana") == "bsc":
            return self.check_bsc(cand, launch=True)
        checks: list[FilterCheck] = []
        if self.data_source != "SIM" and cand.buyers_1h is None and cand.buyers_5m is None:
            stats = self.market.token_pool_stats(cand.mint, "bsc" if getattr(cand, "chain", "") == "bsc" else "solana")
            if stats.get("buyers_1h") is not None:
                cand.buyers_1h = stats["buyers_1h"]
            if stats.get("buyers_5m") is not None:
                cand.buyers_5m = stats["buyers_5m"]
            if not cand.volume_5m_usd and stats.get("volume_5m"):
                cand.volume_5m_usd = stats["volume_5m"]
            if not cand.volume_1h_usd and stats.get("volume_1h"):
                cand.volume_1h_usd = stats["volume_1h"]
            if not cand.pair_address and stats.get("pool"):
                cand.pair_address = stats["pool"]
        age = cand.age_seconds()
        minutes = None if age is None else age / 60.0
        volume = self._launch_volume(cand, minutes)
        need = config.LAUNCH_MIN_VOLUME_USD
        if minutes is not None:
            need = config.LAUNCH_MIN_VOLUME_USD * min(max(minutes, 1.0), config.LAUNCH_VOLUME_WINDOW_MIN) / config.LAUNCH_VOLUME_WINDOW_MIN
        if volume is None:
            checks.append(FilterCheck("launch_volume", False, "ni podatka (volumen lansiranja)", None))
        elif volume < need:
            checks.append(FilterCheck("launch_volume", False, f"volumen ${volume:,.0f} < ${need:,.0f} za starost", volume))
        else:
            checks.append(FilterCheck("launch_volume", True, f"volumen ${volume:,.0f} (prag ${need:,.0f})", volume))
        buyers = cand.buyers_5m if cand.buyers_5m is not None else cand.buyers_1h
        if buyers is None:
            checks.append(FilterCheck("launch_buyers", False, "ni podatka (unikatni kupci)", None))
        elif buyers < config.LAUNCH_MIN_BUYERS:
            checks.append(FilterCheck("launch_buyers", False, f"unikatni kupci {buyers} < {config.LAUNCH_MIN_BUYERS}", buyers))
        else:
            checks.append(FilterCheck("launch_buyers", True, f"unikatni kupci {buyers}", buyers))
        multiple = self._launch_multiple(cand)
        if multiple is None:
            checks.append(FilterCheck("launch_multiple", False, "ni podatka (cena od lansiranja)", None))
        elif multiple > config.LAUNCH_MAX_MULTIPLE:
            checks.append(FilterCheck("launch_multiple", False, f"že {multiple:.1f}x od lansiranja > {config.LAUNCH_MAX_MULTIPLE:.0f}x", multiple))
        else:
            checks.append(FilterCheck("launch_multiple", True, f"{multiple:.2f}x od lansiranja", multiple))
        if any(not c.passed for c in checks):
            return self._finish(cand, checks, None, "launch-cheap", False)

        safety = self._safety(cand)
        creator = safety.get("creator")
        mint_set = safety.get("mint_authority_set")
        if mint_set is None:
            checks.append(FilterCheck("mint_authority", False, "ni podatka (mint authority)", None))
        elif mint_set:
            checks.append(FilterCheck("mint_authority", False, "mint authority not revoked", True))
        else:
            checks.append(FilterCheck("mint_authority", True, "mint authority revoked", False))
        frz = safety.get("freeze_authority_set")
        if frz is None:
            checks.append(FilterCheck("freeze_authority", False, "ni podatka (freeze authority)", None))
        elif frz:
            checks.append(FilterCheck("freeze_authority", False, "freeze authority is set", True))
        else:
            checks.append(FilterCheck("freeze_authority", True, "freeze authority unset", False))
        bonding = self._on_bonding_curve(cand, safety)
        lp_ok = safety.get("lp_locked_or_burned")
        if bonding:
            checks.append(FilterCheck("lp_lock", True, "bonding curve — LP lock ne velja", "curve"))
        elif lp_ok is None:
            checks.append(FilterCheck("lp_lock", False, "ni podatka (LP lock/burn)", None))
        elif not lp_ok:
            checks.append(FilterCheck("lp_lock", False, "LP not locked/burned", safety.get("lp_detail")))
        else:
            checks.append(FilterCheck("lp_lock", True, "LP locked/burned", safety.get("lp_detail")))
        dev = safety.get("creator_pct")
        if dev is None:
            checks.append(FilterCheck("dev_hold", False, "ni podatka (delež razvijalca)", None))
        elif dev > config.LAUNCH_MAX_DEV_PCT:
            checks.append(FilterCheck("dev_hold", False, f"razvijalec drži {dev:.1f}% > {config.LAUNCH_MAX_DEV_PCT:.0f}%", dev))
        else:
            checks.append(FilterCheck("dev_hold", True, f"razvijalec drži {dev:.1f}%", dev))
        top10 = safety.get("top10_pct")
        if top10 is None:
            checks.append(FilterCheck("top10_holders", False, "ni podatka (top 10 holders)", None))
        elif top10 > config.MAX_TOP10_HOLDER_PCT:
            checks.append(FilterCheck("top10_holders", False, f"top 10 holders {top10:.1f}% > {config.MAX_TOP10_HOLDER_PCT:.0f}%", top10))
        else:
            checks.append(FilterCheck("top10_holders", True, f"top 10 holders {top10:.1f}% (brez krivulje/LP)", top10))
        bundle = safety.get("bundle_pct")
        if safety.get("bundle_flag") and bundle is None:
            checks.append(FilterCheck("bundle", False, "bundle/sniper oznaka brez deleža", None))
        elif bundle is not None and bundle > config.LAUNCH_MAX_BUNDLE_PCT:
            checks.append(FilterCheck("bundle", False, f"bundle/sniper {bundle:.1f}% > {config.LAUNCH_MAX_BUNDLE_PCT:.0f}%", bundle))
        elif not safety.get("history_evaluated"):
            checks.append(FilterCheck("bundle", False, "ni podatka (bundle/sniper)", None))
        else:
            checks.append(FilterCheck("bundle", True, f"bundle/sniper {float(bundle or 0):.1f}%", bundle))
        if safety.get("dev_sold") is True:
            checks.append(FilterCheck("dev_sold", False, "razvijalec je že prodal", True))
        elif not safety.get("history_evaluated"):
            checks.append(FilterCheck("dev_sold", False, "ni podatka (dev prodaja)", None))
        else:
            checks.append(FilterCheck("dev_sold", True, "ni sledi prodaje razvijalca", False))
        rug_link, rug_reason = self._dev_rug_link(safety, creator)
        if rug_link is None:
            checks.append(FilterCheck("dev_rug_link", False, "ni podatka (creator history)", None))
        elif rug_link:
            checks.append(FilterCheck("dev_rug_link", False, rug_reason or "dev wallet linked to previous rugs", creator))
        else:
            checks.append(FilterCheck("dev_rug_link", True, "no creator rug link found", creator))
        return self._finish(cand, checks, safety, safety.get("source") or "launch", bonding)

    def _finish(self, cand: Candidate, checks: list[FilterCheck], safety: Optional[dict], source: str, bonding: bool) -> tuple[FilterResult, Event]:
        passed = all(c.passed for c in checks)
        result = FilterResult(
            mint=cand.mint,
            symbol=cand.symbol,
            passed=passed,
            checks=checks,
            creator=None if not safety else safety.get("creator"),
            top10_pct=None if not safety else safety.get("top10_pct"),
            mint_revoked=None if not safety else (False if safety.get("mint_authority_set") else True if safety.get("mint_authority_set") is False else None),
            freeze_set=None if not safety else safety.get("freeze_authority_set"),
            lp_locked_or_burned=True if bonding else (None if not safety else safety.get("lp_locked_or_burned")),
            volume_1h_usd=cand.volume_1h_usd,
            data_source=source,
        )
        lane = "NOVI LANS"
        if passed:
            ev = Event(ts=iso(), agent="FILTER", level="ok", message=f"{cand.symbol}  {lane} PASS  ({source})", mint=cand.mint, symbol=cand.symbol, extra={"lane": "novi_lans"})
        else:
            reasons = "; ".join(result.kill_reasons)
            ev = Event(ts=iso(), agent="FILTER", level="kill", message=f"{cand.symbol}  {lane} KILL  {reasons}", mint=cand.mint, symbol=cand.symbol, extra={"lane": "novi_lans", "reasons": result.kill_reasons})
        return result, ev

    def _launch_volume(self, cand: Candidate, minutes: Optional[float]) -> Optional[float]:
        if minutes is not None and minutes <= 20 and cand.volume_5m_usd:
            return float(cand.volume_5m_usd) * min(max(minutes, 1.0) / 5.0, 3.0)
        if cand.volume_1h_usd:
            return float(cand.volume_1h_usd)
        if cand.volume_5m_usd:
            return float(cand.volume_5m_usd)
        return None

    def _launch_multiple(self, cand: Candidate) -> Optional[float]:
        if cand.pair_address and self.data_source != "SIM":
            rows = self.market.gecko_ohlcv(cand.pair_address, aggregate=1, limit=40, network="bsc" if getattr(cand, "chain", "") == "bsc" else "solana")
            if rows:
                rows = sorted(rows, key=lambda row: row[0])
                first = float(rows[0][1] or 0)
                last = float(rows[-1][4] or 0)
                if first > 0 and last > 0:
                    return last / first
        age = cand.age_seconds()
        if age is not None and age <= 3600 and cand.price_change_1h is not None:
            return 1.0 + cand.price_change_1h / 100.0
        if age is not None and age <= 900 and cand.price_change_5m is not None:
            return 1.0 + cand.price_change_5m / 100.0
        return None

    def _on_bonding_curve(self, cand: Candidate, safety: dict) -> bool:
        dex = (cand.dex or "").lower()
        if dex in {"pumpfun", "pump.fun"} or (dex.startswith("pump") and "swap" not in dex):
            return True
        return bool(safety.get("bonding_curve"))

    def _holder_flags(self, holders: list, creator: Optional[str], known_lp: set[str]) -> tuple[Optional[float], Optional[float], bool]:
        if not isinstance(holders, list):
            return None, None, False
        creator_pct = None
        bundle = 0.0
        flagged = False
        seen = False
        for h in holders:
            if not isinstance(h, dict):
                continue
            owner = _as_addr(h.get("owner") or h.get("address") or h.get("pubkey") or h.get("account"))
            try:
                pct = float(h.get("pct") if h.get("pct") is not None else h.get("percentage") if h.get("percentage") is not None else h.get("percent"))
            except (TypeError, ValueError):
                continue
            seen = True
            text = " ".join(str(v).lower() for v in h.values() if isinstance(v, str))
            if h.get("insider") or any(word in text for word in ("sniper", "bundle", "insider")):
                flagged = True
                if owner and not _excluded_holder(owner, h, known_lp, None):
                    bundle += pct
            if creator and owner == creator:
                creator_pct = pct
        if creator and creator_pct is None and seen:
            creator_pct = 0.0
        return creator_pct, (bundle if seen else None), flagged

    def _launch_risks(self, checks: list[FilterCheck], safety: dict) -> None:
        """Bundled or sniper-heavy launches and concentrated LP fail closed."""
        if not safety.get("history_evaluated"):
            checks.append(FilterCheck("bundle", False, "ni podatka (bundle/sniper)", None))
            checks.append(FilterCheck("lp_concentration", False, "ni podatka (koncentracija LP)", None))
            return
        blob = " ".join(
            (r.get("name") if isinstance(r, dict) else str(r)) or ""
            for r in (safety.get("risks") or [])
        ).lower()
        if any(word in blob for word in ("bundle", "sniper", "insider")):
            checks.append(FilterCheck("bundle", False, "bundled/sniper lansiranje (rugcheck)", blob[:80]))
        else:
            checks.append(FilterCheck("bundle", True, "ni bundle/sniper opozorila", None))
        if any(word in blob for word in ("concentrated lp", "lp concentration", "single lp", "lp owned by")):
            checks.append(FilterCheck("lp_concentration", False, "koncentrirana LP", blob[:80]))
        else:
            checks.append(FilterCheck("lp_concentration", True, "LP ni označena kot koncentrirana", None))

    def _dev_rug_link(self, safety: dict, creator: Optional[str]) -> tuple[Optional[bool], str]:
        if creator and creator in self.blacklisted_creators:
            return True, f"dev wallet linked to previous rugs (local blacklist {creator[:8]}…)"
        if "prior_rugs" in safety or "creator_rugged" in safety or "risks" in safety or "creator" in safety:
            if safety.get("creator_rugged") or int(safety.get("prior_rugs") or 0) > 0:
                n = int(safety.get("prior_rugs") or 0)
                return True, f"dev wallet linked to previous rugs ({n} prior)"
            risks = safety.get("risks") or []
            for r in risks:
                text = (r if isinstance(r, str) else " ".join(str(x) for x in (r.values() if isinstance(r, dict) else [r]))).lower()
                if any(k in text for k in ("prior rug", "previous rug", "rugged previous", "creator rugged", "known scammer")):
                    return True, "dev wallet linked to previous rugs (rugcheck risks)"
            # We had enough data to evaluate and found no link
            return False, ""
        if creator is None and safety.get("source") in {"none", None}:
            return None, "ni podatka"
        # Creator known, no risk payload — still a data gap for "linked to previous rugs"
        # Capital preservation: if rugcheck returned a report with a creator and risks list
        # (even empty), treat as evaluated. If we only have a creator and nothing else, fail.
        if safety.get("history_evaluated"):
            return False, ""
        return None, "ni podatka (creator history)"

    def _safety(self, cand: Candidate) -> dict[str, Any]:
        if self.data_source == "SIM" and self.sim is not None:
            report = self.sim.safety_report(cand.mint)
            if report is None:
                return {"source": "sim-missing"}
            return {**report, "source": "SIM", "history_evaluated": True}

        report = self.market.rugcheck_report(cand.mint)
        if report:
            parsed = self._parse_rugcheck(report)
            parsed["source"] = "rugcheck"
            return parsed

        # Fallback: Solana public RPC (mint/freeze + top holders only). LP lock / creator history stay unknown.
        parsed: dict[str, Any] = {"source": "rpc-fallback"}
        auth = self.market.mint_authorities(cand.mint)
        if auth is not None:
            parsed["mint_authority_set"] = bool(auth.get("mint_authority_set"))
            parsed["freeze_authority_set"] = bool(auth.get("freeze_authority_set"))
        top10 = self._top10_from_rpc(cand.mint)
        if top10 is not None:
            parsed["top10_pct"] = top10
        # LP and creator history cannot be reliably recovered from these two RPC calls
        return parsed

    def _parse_rugcheck(self, report: dict) -> dict[str, Any]:
        token = report.get("token") or {}
        mint_auth = _as_addr(token.get("mintAuthority") or report.get("mintAuthority"))
        freeze_auth = _as_addr(token.get("freezeAuthority") or report.get("freezeAuthority"))

        markets = report.get("markets") or report.get("pools") or []
        lp_ok: Optional[bool] = None
        lp_detail = None
        known_lp_accounts: set[str] = set()
        if isinstance(markets, list) and markets:
            locked_flags = []
            for m in markets:
                if not isinstance(m, dict):
                    continue
                for key in ("pubkey", "address", "mintLP", "lpMint", "liquidityA", "liquidityB"):
                    addr = _as_addr(m.get(key))
                    if addr:
                        known_lp_accounts.add(addr)
                for key, val in m.items():
                    if key.lower() in {"pubkey", "address", "mintlp", "lpmint", "liquiditya", "liquidityb"} or "lp" in key.lower():
                        addr = _as_addr(val)
                        if addr:
                            known_lp_accounts.add(addr)
                lp = m.get("lp") if isinstance(m.get("lp"), dict) else m
                pct = lp.get("lpLockedPct") if isinstance(lp, dict) else None
                if pct is None and isinstance(m, dict):
                    pct = m.get("lpLockedPct") or m.get("lockedPct")
                burned = False
                if isinstance(m, dict):
                    burned = bool(m.get("lpBurned") or m.get("burned") or (lp or {}).get("lpBurned"))
                try:
                    pct_f = float(pct) if pct is not None else None
                except (TypeError, ValueError):
                    pct_f = None
                if burned or (pct_f is not None and pct_f >= 95.0):
                    locked_flags.append(True)
                    lp_detail = f"locked {pct_f:.0f}%" if pct_f is not None else "burned"
                elif pct_f is not None:
                    locked_flags.append(False)
                    lp_detail = f"locked {pct_f:.0f}%"
            if locked_flags:
                lp_ok = any(locked_flags)  # at least one market fully locked/burned
                if not lp_ok and lp_detail is None:
                    lp_detail = "no locked/burned LP market"

        holders = report.get("topHolders") or report.get("holders") or []
        top10 = self._sum_top10(holders, known_lp_accounts, report.get("mint"))

        creator = _as_addr(report.get("creator") or report.get("deployer") or (report.get("tokenMeta") or {}).get("updateAuthority"))
        creator_pct, bundle_pct, bundle_flag = self._holder_flags(holders, creator, known_lp_accounts)
        risks = report.get("risks") or []
        prior = 0
        creator_rugged = bool(report.get("rugged"))
        for r in risks:
            name = (r.get("name") if isinstance(r, dict) else str(r)) or ""
            if "rugged" in name.lower():
                creator_rugged = True
        extra_tokens = report.get("creatorTokens") or report.get("creatorHistory") or []
        if isinstance(extra_tokens, list):
            for t in extra_tokens:
                if isinstance(t, dict) and (t.get("rugged") or t.get("rug") or (t.get("status") or "") == "rugged"):
                    prior += 1
                    creator_rugged = True

        return {
            "mint_authority_set": bool(mint_auth),
            "freeze_authority_set": bool(freeze_auth),
            "lp_locked_or_burned": lp_ok,
            "lp_detail": lp_detail,
            "top10_pct": top10,
            "creator": creator,
            "risks": risks,
            "prior_rugs": prior,
            "creator_rugged": creator_rugged and prior > 0,  # current token rugged != prior rugs; prior only
            "history_evaluated": True,
            "creator_pct": creator_pct,
            "bundle_pct": bundle_pct,
            "bundle_flag": bundle_flag or any("bundle" in ((r.get("name") if isinstance(r, dict) else str(r)) or "").lower() or "sniper" in ((r.get("name") if isinstance(r, dict) else str(r)) or "").lower() or "insider" in ((r.get("name") if isinstance(r, dict) else str(r)) or "").lower() for r in risks),
            "dev_sold": any(k in " ".join((r.get("name") if isinstance(r, dict) else str(r)) or "" for r in risks).lower() for k in ("dev sold", "creator sold", "sold tokens", "developer sold")),
            "bonding_curve": any(k in " ".join(str(m).lower() for m in markets) for k in ("bonding", "pump.fun curve")),
        }

    def _sum_top10(self, holders: list, known_lp: set[str], mint: Optional[str]) -> Optional[float]:
        if not isinstance(holders, list) or not holders:
            return None
        rows = []
        for h in holders:
            if not isinstance(h, dict):
                continue
            owner = _as_addr(h.get("owner") or h.get("address") or h.get("pubkey") or h.get("account"))
            if not owner:
                continue
            if _excluded_holder(owner, h, known_lp, mint):
                continue
            pct = h.get("pct") or h.get("percentage") or h.get("percent")
            try:
                pct_f = float(pct) if pct is not None else None
            except (TypeError, ValueError):
                pct_f = None
            rows.append((owner, pct_f, h))
        if not rows:
            return None
        # If percentages missing, we cannot compute a reliable top-10 share
        if any(p is None for _, p, _ in rows):
            return None
        # unique owners, take top 10 by pct
        by_owner: dict[str, float] = {}
        for owner, pct, _ in rows:
            by_owner[owner] = by_owner.get(owner, 0.0) + float(pct)
        top = sorted(by_owner.values(), reverse=True)[:10]
        return float(sum(top))

    def _top10_from_rpc(self, mint: str) -> Optional[float]:
        largest = self.market.token_largest_accounts(mint)
        supply = self.market.token_supply(mint)
        if not largest or not supply or supply <= 0:
            return None
        shares = []
        for acc in largest:
            try:
                amount = float((acc.get("uiAmount") if isinstance(acc.get("uiAmount"), (int, float)) else None) or (acc.get("uiAmountString") or 0))
            except (TypeError, ValueError):
                # rpc value is often {"address": "...", "amount": "123", "decimals": 6, "uiAmount": 0.12}
                val = acc.get("uiAmount")
                try:
                    amount = float(val)
                except (TypeError, ValueError):
                    continue
            owner = acc.get("address") or ""
            if _excluded_holder(owner, acc, set(), mint):
                continue
            shares.append(100.0 * amount / supply)
        if not shares:
            return None
        return float(sum(sorted(shares, reverse=True)[:10]))
