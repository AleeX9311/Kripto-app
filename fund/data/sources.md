# Viri

Every download is named, timestamped, and stored in UTC. Missing bars stay missing.

- Binance public klines via `data-api.binance.vision`, interval 1h, symbols BTC, ETH, SOL, BNB and the liquid alts. Spot only.
- EURUSDT from the same public ticker, so the book can be marked in euros.
- Hyperliquid `metaAndAssetCtxs` funding. Data only. No orders.
- Fair Economy weekly calendar for USD CPI and FOMC. Speeches are not events.
- GeckoTerminal trending pools and GoPlus security for the memecoin veto. A missing field fails closed.

Raw files live in `data/raw/`. Deduped files live in `data/clean/`. Neither is committed; the night job rebuilds them.

- 2026-11-18T19:51:39Z: Binance vision 1h BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT from 2022-01-01. Gaps are logged, not filled.

- 2026-11-19T16:12:43Z: Binance vision 1h BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT from 2022-01-01. Gaps are logged, not filled.
