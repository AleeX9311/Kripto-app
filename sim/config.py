"""Paper-trader configuration. Simulation only — no wallets, no keys, no real orders.
Focus: BNB Smart Chain (BSC / chainId 'bsc').
"""

from datetime import datetime, timezone, timedelta

# End of simulation window (Europe/Ljubljana = CET UTC+1 in November)
END_AT = datetime(2026, 11, 16, 20, 45, 0, tzinfo=timezone(timedelta(hours=1)))

CHAIN_ID = "bsc"
CHAIN_LABEL = "BSC"

START_EUR = 100.0
POSITION_PCT = 0.10          # 10% of equity per position
MIN_POSITION_USD = 3.0
MAX_OPEN = 5
REENTRY_COOLDOWN_SEC = 6 * 3600

# Costs: 1% slippage+fees each side
ENTRY_COST = 0.01
EXIT_COST = 0.01

# Exit rules
# Widest support stop. A deeper level skips the trade. Learning may only tighten.
STOP_LOSS = -0.15
STOP_MIN = -0.05             # stop cannot sit closer than 5%
STOP_FALLBACK = -0.10        # used when no support is found
RISK_PCT = 0.012             # loss at the stop is at most 1.2% of equity
MIN_REWARD_RISK = 1.5
TAKE_PROFIT = 0.20          # +20%
TRAIL_ACTIVATE = 0.10        # after +10%
TRAIL_PCT = 0.10             # 10% from peak
TIME_STOP_SEC = 4 * 3600

# Entry filters
MIN_LIQUIDITY_USD = 30_000
MIN_PAIR_AGE_MIN = 30
MIN_TXNS_5M = 20
MIN_TXNS_1H = 50
MIN_BUY_SELL_RATIO = 1.3
MIN_VOL_24H = 50_000
MAX_CHANGE_1H = 3.0         # reject if +300% in 1h (overextended)
MAX_FDV = 50_000_000
MIN_FDV = 50_000
MAX_MCAP_LIQ_RATIO = 50     # skip if mcap/liq too high

LOOP_SEC = 60
STATE_FILE = "state.json"
STATUS_FILE = "status.json"
LOG_FILE = "trader.log"
PID_FILE = "trader.pid"
# Fixed end string published in status.json (Europe/Ljubljana, CET in November)
ENDS_AT_ISO = "2026-11-16T20:45:00+01:00"

DEX_BASE = "https://api.dexscreener.com"
BINANCE_EURUSDT = "https://api.binance.com/api/v3/ticker/price?symbol=EURUSDT"
# GoPlus token security for BSC (chain 56), no key required for basic use
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses={addr}"

# Soft rate limiting
REQUEST_PAUSE = 0.35
