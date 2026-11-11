# Solana memecoin desk — paper trading only

A local, fully paper-trading desk with six agents and hard handoffs:

1. **Scout** — scans new Solana pairs (Pump.fun, Raydium, PumpSwap) via public, no-key APIs.
2. **Filter** — kills anything that fails mint/freeze, LP lock, holder concentration, $50k 1h volume, or a creator linked to prior rugs. Missing data is a fail (`ni podatka`).
3. **Analyst** — scores survivors 0–1 from current structure (liquidity, turnover, flow, pullback / higher-low vs vertical pump, age, holders). Under 0.6 = no trade. This is **not a price forecast**.
4. **Executor** — paper-only entries, **at most 8% of current equity**, never more. Simulated fill with 1% slippage + 0.25% fee. Max 5 positions, one per token.
5. **Risk** — hard stop at −6%, trailing stop arms only after +12% (6% off the peak), no averaging down, no re-entry after a stop, 15-minute cooldown after two consecutive losses, daily loss brake at −5% of start-of-day equity.
6. **Logger** — every trade and every rejection goes to `logs/decisions.jsonl` and `logs/trades.csv`; equity to `logs/equity.csv`. `state.json` lets a restart resume the book.

**This process never signs a transaction, never loads a wallet, and never touches a private key.**

## Start

```bash
cd /path/to/sol-desk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Live public data (falls back to a clearly labelled SIMULATED feed if the network fails)
python main.py

# Force the simulated feed
python main.py --sim

# Custom virtual balance (default 1000 USD)
python main.py --sim --balance 1000

# Headless (plain log lines) for a fixed number of minutes
python main.py --headless --minutes 5
python main.py --sim --headless --minutes 2
```

In the live UI, press `Ctrl-C` to quit. `q` is also printed as a hint; `Ctrl-C` is the supported stop.

`--fresh` ignores an existing `state.json` and opens a new paper book.

## Data sources

Live mode, all public and unauthenticated:

- [DexScreener](https://api.dexscreener.com) — `token-profiles/latest`, `tokens/v1/solana/<mints>`, `latest/dex/search`, `latest/dex/pairs/solana/<pair>`
- [GeckoTerminal](https://api.geckoterminal.com/api/v2) — `networks/solana/new_pools`, `trending_pools`, and 5-minute OHLCV for structure scoring
- [RugCheck](https://api.rugcheck.xyz/v1/tokens/<mint>/report) — mint/freeze authority, LP lock, top holders, creator risks
- Solana public RPC (`https://api.mainnet-beta.solana.com`) — `getAccountInfo`, `getTokenLargestAccounts`, `getTokenSupply` as a fallback when RugCheck is unavailable

Requests are rate-limited and cached. If the live scan fails, the desk switches to a **SIMULATED** feed and says so in the header and the logs.

## Honest limitations

- Pump.fun coins still in (or just after) the bonding curve almost never pass the $50k 1h-volume rule or a real LP lock. That is by design: the Filter is here to reject, not to force trades.
- The 35% top-10 holder cap still rejects concentrated coins. Many otherwise liquid pairs will be killed, or fail with `ni podatka` when holder data is missing.
- When RugCheck is down or rate-limited, LP lock and creator history usually cannot be proven from RPC alone, so those checks fail closed (`ni podatka`).
- Scout is periodic (tens of seconds), not a websocket firehose. “New” is as new as the public REST endpoints.
- Price structure uses 5-minute candles when GeckoTerminal allows it, otherwise a DexScreener percent-change approximation. Neither is a prediction of what the coin will do next.
- Simulated fills (slippage + fee) are a model, not a replay of a real AMM curve.
- Restart resumes the paper book from `state.json`. Use `--fresh` to start over.

## Layout

```
main.py            orchestrator + CLI
config.py          every threshold
models.py          shared dataclasses
market.py          live HTTP + RPC clients
simfeed.py         labelled simulated universe
state_store.py     state.json
terminal_ui.py     rich Live layout
agents/scout.py
agents/filter.py
agents/analyst.py
agents/executor.py
agents/risk.py
agents/logger.py
logs/              decisions.jsonl  trades.csv  equity.csv
state.json         written at runtime
```
