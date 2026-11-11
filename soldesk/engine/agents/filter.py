"""Filter — hard kills. Missing data is a FAIL (ni podatka). Capital preservation first."""

from __future__ import annotations

from typing import Any, Optional

import config
from market import Market
from models import Candidate, Event, FilterCheck, FilterResult, iso
from simfeed import SimFeed


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
            if owner in config.BURN_ADDRESSES or owner == mint:
                continue
            if owner in known_lp:
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
            if owner in config.BURN_ADDRESSES or owner == mint:
                continue
            shares.append(100.0 * amount / supply)
        if not shares:
            return None
        return float(sum(sorted(shares, reverse=True)[:10]))
