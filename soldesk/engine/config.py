"""All tunable thresholds for the paper-trading Solana memecoin desk.

PAPER MODE IS MANDATORY. This process never signs a transaction, never
loads a wallet, and never touches a private key.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Paper-trading lock (do not weaken)
# ---------------------------------------------------------------------------
PAPER_MODE = True
PAPER_BANNER = "PAPER MODE · NO REAL TRANSACTIONS · VIRTUAL USD"
STARTING_BALANCE_USD = 1000.0

# ---------------------------------------------------------------------------
# Executor / sizing
# ---------------------------------------------------------------------------
MAX_POSITION_PCT = 0.08          # max 8% of current equity per trade
MAX_CONCURRENT_POSITIONS = 5
ONE_POSITION_PER_TOKEN = True
ENTRY_SLIPPAGE_PCT = 0.01        # 1%
ENTRY_FEE_PCT = 0.0025           # 0.25%
EXIT_SLIPPAGE_PCT = 0.01
EXIT_FEE_PCT = 0.0025
# Reject if the intended size would be more than this share of pool liquidity
MAX_TRADE_VS_LIQUIDITY = 0.03

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
HARD_STOP_PCT = -0.06            # realized loss incl. slip+fee, not the mid print
TRAIL_ACTIVATE_PCT = 0.12        # trail arms only after +12% net
TRAIL_PCT = 0.06                 # 6% drop from peak once armed
BREAKEVEN_ARM_PCT = 0.10         # after +10% net, a return to flat exits
PARTIAL_TP = (0.15, 0.30)        # sell 1/3 of the original size at each
PARTIAL_FRACTION = 1.0 / 3.0
TIME_STOP_SEC = 40 * 60          # flat for 30–45 min
TIME_STOP_FLAT_PCT = 0.03
MIN_TOKEN_AGE_MIN = 45           # main lane only; the launch lane has no minimum age
DUMP_AFTER_BOND_1H = -25.0       # young coin already down hard
SOL_1H_PAUSE_PCT = -2.5          # pause entries if SOL 1h is down at least this many percent
MIN_BREADTH = 0.35               # pause if fewer than this share of scanned coins are green 1h
BREADTH_MIN_SAMPLE = 8
DAILY_LOSS_BRAKE_PCT = -0.05     # stop new entries at -5% vs start-of-day
CONSECUTIVE_LOSS_LIMIT = 2
CONSECUTIVE_LOSS_COOLDOWN_SEC = 15 * 60
# After a stop, never re-enter the same token in this desk instance
BAN_TOKEN_AFTER_STOP = True

# ---------------------------------------------------------------------------
# Filter (capital preservation: missing data = FAIL)
# ---------------------------------------------------------------------------
MIN_VOLUME_1H_USD = 50_000.0
MAX_TOP10_HOLDER_PCT = 75.0          # both lanes; LP, curve, and programs are excluded first
# Liquidity collapse vs the first observed liquidity for that mint
RUG_LIQUIDITY_COLLAPSE_PCT = 0.80

# ---------------------------------------------------------------------------
# Launch lane (NOVI LANS). Fresh coins only. Never loosens the 8% size cap.
# ---------------------------------------------------------------------------
LAUNCH_MAX_AGE_MIN = 180
LAUNCH_MAX_DEV_PCT = 10.0
LAUNCH_MAX_BUNDLE_PCT = 25.0
LAUNCH_MIN_BUYERS = 40
LAUNCH_MIN_VOLUME_USD = 8_000.0   # scaled to the first 15 minutes of life
LAUNCH_VOLUME_WINDOW_MIN = 15.0
LAUNCH_MAX_MULTIPLE = 5.0         # skip once price is already >5x from launch
LAUNCH_MAX_POSITION_PCT = 0.03    # 3% of equity; still never above MAX_POSITION_PCT
LAUNCH_MAX_POSITIONS = 2
LAUNCH_HARD_STOP_PCT = -0.10      # net, including slip and fee
LAUNCH_PARTIAL_PCT = 0.30         # sell half once at +30% net
LAUNCH_PARTIAL_FRACTION = 0.50
LAUNCH_TRAIL_ACTIVATE_PCT = 0.20
LAUNCH_TIME_STOP_SEC = 20 * 60
LAUNCH_BATCH = 8
LAUNCH_RECHECK_SEC = 90

# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------
MIN_ANALYST_SCORE = 0.60         # floor; learning may only raise a bucket
LEARN_MIN_TRADES = 6
LEARN_LOSE_WIN_RATE = 0.35
LEARN_SCORE_STEP = 0.05
LEARN_SCORE_CAP = 0.85
ANALYST_WEIGHTS = {
    "liquidity": 0.20,
    "turnover": 0.20,
    "flow": 0.20,
    "structure": 0.20,
    "age": 0.10,
    "holders": 0.10,
}

# ---------------------------------------------------------------------------
# Loop timings
# ---------------------------------------------------------------------------
SCOUT_INTERVAL_SEC = 45
POSITION_POLL_SEC = 20
UI_REFRESH_SEC = 1.0
FILTER_BATCH_SIZE = 8

# ---------------------------------------------------------------------------
# HTTP / cache
# ---------------------------------------------------------------------------
HTTP_TIMEOUT_SEC = 12
HTTP_USER_AGENT = "SolDeskPaperTrader/1.0 (+paper-trading-only; no-wallet)"
DEX_MIN_INTERVAL_SEC = 0.25
GECKO_MIN_INTERVAL_SEC = 1.0
RUGCHECK_MIN_INTERVAL_SEC = 1.6
RPC_MIN_INTERVAL_SEC = 0.4
CACHE_TTL_SEC = 40
RUGCHECK_TTL_SEC = 180
OHLCV_TTL_SEC = 90
MAX_SCOUT_CANDIDATES = 24

# Public no-key endpoints
DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

# Display
TIMEZONE = "Europe/Ljubljana"
EVENT_HISTORY = 80
EQUITY_HISTORY_MAX = 600
REJECTION_HISTORY = 40

# Programs, pools, and the bonding curve are not "holders" for the top-10 cap.
PROTOCOL_ACCOUNTS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp2",  # Raydium AMM v4
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium authority
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca whirlpool
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
}

# Known burn / null addresses (excluded from holder concentration)
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",  # wSOL mint, not a holder
}

# Quote mints we treat as the other side of a memecoin pair
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}

# Symbols/names we never treat as a memecoin candidate
SKIP_SYMBOLS = {
    "SOL", "WSOL", "wSOL", "USDC", "USDT", "USD1", "USDS", "DAI", "PYUSD",
}
SKIP_NAMES = {"solana", "usd coin", "tether", "wrapped solana", "wrapped sol"}


ALLOWED_DEXES = {
    "raydium",
    "pumpswap",
    "pumpfun",
    "pump.fun",
    "pumpswapamm",
    "meteora",
    "orca",
    "moonshot",
    "launchlab",
}
