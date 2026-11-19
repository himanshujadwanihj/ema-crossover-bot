"""
config.py — Central configuration for the EMA crossover bot.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "8921341725:AAFRPRj9xxPIuEyNLTR8Iob0TtIC54n1hfE")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "771305593")
if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "8921341725":
    TELEGRAM_CHAT_ID = "771305593"

TELEGRAM_NEWS_CHANNEL_ID: str = os.getenv("TELEGRAM_NEWS_CHANNEL_ID", "-1004354909509")
TELEGRAM_SIGNAL_CHANNEL_ID: str = "-1004463573705"   # EMA Cross Channel
TELEGRAM_SCANNER_CHANNEL_ID: str = "-1004332146822"  # EMA Scanner Channel
TELEGRAM_TRADING_CHANNEL_ID: str = "771305593"        # Personal DM Chat ID for Trading Activity

# ── New Filter Mini App Bot (AlphaCryptoSignals @Alpha_Crypto_SignalBot) ───────
FILTER_BOT_TOKEN: str = os.getenv("FILTER_BOT_TOKEN", "8990708294:AAHQAUYGrFSPs7Pi2bAYNvLexheJeqZeAMM")

# ── Shark Exchange Auto-Trader Settings ────────────────────────────────────────
AUTO_TRADING_ENABLED: bool = True   # Live Real Money Auto-Trading on Shark Exchange
PAPER_TRADING_ENABLED: bool = False  # User Directive: Paper trading completely paused; 100% focus on Live Trading
SHARK_API_KEY: str         = os.getenv("SHARK_API_KEY", "052e905d55872cc219fdf28b92012287")
SHARK_API_SECRET: str      = os.getenv("SHARK_API_SECRET", "b8c561b45bd6328ea3aa5cbb789847e4")
SHARK_BASE_URL: str        = os.getenv("SHARK_BASE_URL", "https://api.sharkexchange.in")
AUTO_TRADE_MARGIN_INR: float = float(os.getenv("AUTO_TRADE_MARGIN_INR", "1500.0"))
AUTO_TRADE_LEVERAGE: int     = int(os.getenv("AUTO_TRADE_LEVERAGE", "10"))
AUTO_TRADE_RISK_PER_TRADE_PCT: float = float(os.getenv("AUTO_TRADE_RISK_PER_TRADE_PCT", "0.025"))  # 2.5% Account Risk per Trade
AUTO_TRADE_MIN_MARGIN_INR: float     = float(os.getenv("AUTO_TRADE_MIN_MARGIN_INR", "500.0"))       # Min Margin per Trade
AUTO_TRADE_MAX_MARGIN_PCT: float     = float(os.getenv("AUTO_TRADE_MAX_MARGIN_PCT", "0.20"))       # Max 20% Account Margin Cap per Slot
AUTO_TRADE_MAX_CONCURRENT: int = 999999  # User Directive: Portfolio cap completely removed from everywhere
AUTO_TRADE_EXCLUDED_SYMBOLS: list = [
    "ASTER/USDT", "ASTR/USDT", "AST/USDT", "ASTER", "ASTR", "AST",
    "PAXG/USDT", "PAXGUSDT", "PAXG"
]

# ── US Stock Tokens Exclusion (Crypto Only) ───────────────────────────────────
# IMPORTANT: Only include tokens that are UNAMBIGUOUSLY US stock synthetics.
# Short bare names (COIN, AMD, BA etc.) could collide with real crypto tokens.
# Rule: bare names only if they have NO crypto equivalent; B-suffix variants are always safe.
US_STOCK_TOKENS: list = [
    # Unambiguous full names (no crypto collision risk)
    "GOOGL", "GOOGLB", "AAPL", "AAPLB", "TSLA", "TSLAB", "NVDA", "NVDAB",
    "AMZN", "AMZNB", "MSFT", "MSFTB", "META", "METAB", "SPCX", "SPCXB",
    # COIN/COINB: COINB is the synthetic (safe). Bare COIN removed — COINUSDT is real crypto.
    "COINB",
    # MSTR has no crypto equivalent
    "MSTR", "MSTRB",
    # AMD ambiguous — AMDB only (synthetic suffix)
    "AMDB",
    # HOOD ambiguous — HOODB only
    "HOODB",
    "NFLX", "NFLXB",
    # BABA ambiguous — BABAB only
    "BABAB",
    "PLTR", "PLTRB", "CRCL", "CRCLB",
    # DIS/BA/XOM/INTC/PYPL — ambiguous short names, keep only B-suffix synthetics
    "DISB", "PYPLB", "INTCB", "BAB", "XOMB",
    # SPY/QQQ/SOXL/SNDK — ETF tokens (no crypto equivalent at current market)
    "SPY", "SPYB", "QQQ", "QQQB", "SOXL", "SOXLB", "SNDK", "SNDKB",
]

# ── 15-Minute Allowed Coins ───────────────────────────────────────────────────
# User Directive: 5M and 15M standard scalps are strictly reserved for Gold only!
# All other coins on 15M are exclusively reserved for Squeeze Breakouts.
ALLOWED_15M_COINS: set = {
    "XAU/USD", "XAUT/USDT", "XAUTUSDT"
}

# ── EMA Settings ──────────────────────────────────────────────────────────────
EMA_FAST: int = 9
EMA_SLOW: int = 26

# ── Timeframes ────────────────────────────────────────────────────────────────
TIMEFRAMES: list = [
    ("15m", "15m", None),
    ("1H",  "1h",  None),
    ("4H",  "4h",  None),
    ("1D",  "1d",  None),
    ("1W",  "1w",  None),
]

# ⚙️ Scan Settings ⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️⚙️
SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "1"))
MAX_PAIRS: int             = int(os.getenv("MAX_PAIRS", "25"))

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Kolkata")

# ── OHLCV fetch limit ─────────────────────────────────────────────────────────
OHLCV_LIMIT: int = 150

# ── Validation ────────────────────────────────────────────────────────────────
def clean_contract_symbol(symbol: str) -> str:
    """Standardizes any pair format (e.g. 'SOL/USDT', 'SOL/USDT:USDT', 'SOL-USDT') to 'SOLUSDT'."""
    s = str(symbol).split(':')[0]
    return s.replace('/', '').replace('-', '').upper()


def to_shark_symbol(symbol: str) -> str:
    """
    Translates bot internal tickers / Binance symbols to Shark Exchange native contracts:
    - MUB/USDT, MUBUSDT, MUBARAK/USDT -> MUBARAKUSDT
    - PEPE/USDT, PEPEUSDT -> 1000PEPEUSDT
    - SHIB/USDT, SHIBUSDT -> 1000SHIBUSDT
    - BONK/USDT, BONKUSDT -> 1000BONKUSDT
    - FLOKI/USDT, FLOKIUSDT -> 1000FLOKIUSDT
    - CAT/USDT, CATUSDT, SIMONSCAT/USDT -> 1000CATUSDT
    - RATS/USDT, RATSUSDT -> 1000RATSUSDT
    - SATS/USDT, SATSUSDT -> 1000SATSUSDT
    - BABYDOGE/USDT, BABYDOGEUSDT -> 1MBABYDOGEUSDT
    - RAY/USDT, RAYUSDT -> RAYSOLUSDT
    - MATIC/USDT, MATICUSDT -> POLUSDT
    - RNDR/USDT, RNDRUSDT -> RENDERUSDT
    - XAU/USD, XAUUSD, XAUT/USDT -> XAUUSDT
    - XAG/USD, XAGUSD, XAG/USDT -> XAGUSDT
    - CL/USD, CLUSDT, CRUDEOIL -> CLUSDT
    - NATGAS/USD, NATGASUSDT -> NATGASUSDT
    - BZ/USD, BRENT -> BZUSDT
    - XPD/USD, PALLADIUM -> XPDUSDT
    - XPT/USD, PLATINUM -> XPTUSDT
    - US Stock Tokens: AAPLB/USDT -> AAPLUSDT, etc.
    """
    clean = clean_contract_symbol(symbol)
    
    # 1. Crypto ticker aliases & Meme Multiplier contracts on Shark Exchange
    crypto_alias_map = {
        # Mubarak Token (Binance MUB / MUBARAK -> Shark MUBARAKUSDT)
        "MUBUSDT": "MUBARAKUSDT",
        "MUBARAKUSDT": "MUBARAKUSDT",
        "MUB": "MUBARAKUSDT",
        "MUBARAK": "MUBARAKUSDT",

        # 1000x & 1Mx Meme Multiplier Tokens
        "PEPEUSDT": "1000PEPEUSDT",
        "PEPE": "1000PEPEUSDT",
        "1000PEPEUSDT": "1000PEPEUSDT",
        "1000PEPE": "1000PEPEUSDT",

        "SHIBUSDT": "1000SHIBUSDT",
        "SHIB": "1000SHIBUSDT",
        "1000SHIBUSDT": "1000SHIBUSDT",
        "1000SHIB": "1000SHIBUSDT",

        "BONKUSDT": "1000BONKUSDT",
        "BONK": "1000BONKUSDT",
        "1000BONKUSDT": "1000BONKUSDT",
        "1000BONK": "1000BONKUSDT",

        "FLOKIUSDT": "1000FLOKIUSDT",
        "FLOKI": "1000FLOKIUSDT",
        "1000FLOKIUSDT": "1000FLOKIUSDT",
        "1000FLOKI": "1000FLOKIUSDT",

        "CATUSDT": "1000CATUSDT",
        "CAT": "1000CATUSDT",
        "SIMONSCATUSDT": "1000CATUSDT",
        "SIMONSCAT": "1000CATUSDT",
        "1000CATUSDT": "1000CATUSDT",
        "1000CAT": "1000CATUSDT",

        "RATSUSDT": "1000RATSUSDT",
        "RATS": "1000RATSUSDT",
        "1000RATSUSDT": "1000RATSUSDT",
        "1000RATS": "1000RATSUSDT",

        "SATSUSDT": "1000SATSUSDT",
        "SATS": "1000SATSUSDT",
        "1000SATSUSDT": "1000SATSUSDT",
        "1000SATS": "1000SATSUSDT",

        "BABYDOGEUSDT": "1MBABYDOGEUSDT",
        "BABYDOGE": "1MBABYDOGEUSDT",
        "1MBABYDOGEUSDT": "1MBABYDOGEUSDT",
        "1MBABYDOGE": "1MBABYDOGEUSDT",

        # Raydium (RAY on Binance -> RAYSOL on Shark)
        "RAYUSDT": "RAYSOLUSDT",
        "RAY": "RAYSOLUSDT",
        "RAYSOLUSDT": "RAYSOLUSDT",
        "RAYSOL": "RAYSOLUSDT",

        # Rebranded tokens
        "MATICUSDT": "POLUSDT",
        "MATIC": "POLUSDT",
        "POLUSDT": "POLUSDT",
        "POL": "POLUSDT",

        "RNDRUSDT": "RENDERUSDT",
        "RNDR": "RENDERUSDT",
        "RENDERUSDT": "RENDERUSDT",
        "RENDER": "RENDERUSDT",
    }
    if clean in crypto_alias_map:
        return crypto_alias_map[clean]

    # 2. Commodities mapping to USDT pairs on Shark
    if clean in ("XAUUSD", "XAUTUSDT", "XAU", "GOLDUSDT", "GOLD"):
        return "XAUUSDT"
    if clean in ("XAGUSD", "XAGUSDT", "XAG", "SILVERUSDT", "SILVER"):
        return "XAGUSDT"
    if clean in ("CLUSD", "CL", "CRUDEOILUSDT", "CRUDEOIL"):
        return "CLUSDT"
    if clean in ("NATGASUSD", "NATGAS", "NATGASUSDT"):
        return "NATGASUSDT"
    if clean in ("BZUSD", "BZ", "BRENTUSDT", "BRENT", "BZUSDT"):
        return "BZUSDT"
    if clean in ("XPDUSD", "XPD", "PALLADIUMUSDT", "PALLADIUM", "XPDUSDT"):
        return "XPDUSDT"
    if clean in ("XPTUSD", "XPT", "PLATINUMUSDT", "PLATINUM", "XPTUSDT"):
        return "XPTUSDT"
        
    # 3. US Stock Tokens: strip synthetic 'B' before USDT for Shark Exchange
    stock_b_map = {
        "AAPLBUSDT": "AAPLUSDT", "NVDABUSDT": "NVDAUSDT", "TSLABUSDT": "TSLAUSDT",
        "MSTRBUSDT": "MSTRUSDT", "AMZNBUSDT": "AMZNUSDT", "METABUSDT": "METAUSDT",
        "MSFTBUSDT": "MSFTUSDT", "GOOGLBUSDT": "GOOGLUSDT", "SPYBUSDT": "SPYUSDT",
        "QQQBUSDT": "QQQUSDT", "COINBUSDT": "COINUSDT", "AMDBUSDT": "AMDUSDT",
        "SNDKBUSDT": "SNDKUSDT", "SOXLBUSDT": "SOXLUSDT", "BABABUSDT": "BABAUSDT",
        "DISBUSDT": "DISUSDT", "PYPLBUSDT": "PYPLUSDT", "BABUSDT": "BAUSDT",
        "XOMBUSDT": "XOMUSDT", "PLTRBUSDT": "PLTRUSDT"
    }
    if clean in stock_b_map:
        return stock_b_map[clean]
        
    if clean.endswith("BUSDT") and len(clean) > 5:
        base = clean[:-5]
        # Only strip the synthetic 'B' suffix for known US stock tokens, never for crypto (e.g. BNB→BN, ARB→AR, SHIB→SHI)
        us_tokens_set = {s.upper().replace("B", "", 1) if s.endswith("B") else s.upper() for s in US_STOCK_TOKENS}
        us_tokens_set |= {s.upper() for s in US_STOCK_TOKENS}
        if base in us_tokens_set or (base + "B") in {s.upper() for s in US_STOCK_TOKENS}:
            return f"{base}USDT"
        
    return clean


def get_symbol_price_multiplier(symbol: str, price: float) -> float:
    """
    Returns the price multiplier needed when converting from Binance Spot / unscaled ticker
    to Shark Exchange contract sizing:
    - 1000PEPEUSDT: 1000.0 if price < 0.0005 else 1.0
    - 1000SHIBUSDT: 1000.0 if price < 0.001 else 1.0
    - 1000BONKUSDT: 1000.0 if price < 0.001 else 1.0
    - 1000FLOKIUSDT: 1000.0 if price < 0.005 else 1.0
    - 1000CATUSDT: 1000.0 if price < 0.001 else 1.0
    - 1000RATSUSDT: 1000.0 if price < 0.01 else 1.0
    - 1000SATSUSDT: 1000.0 if price < 0.0001 else 1.0
    - 1MBABYDOGEUSDT: 1000000.0 if price < 0.0001 else 1.0
    """
    clean = to_shark_symbol(symbol)
    p = float(price or 0.0)
    if p <= 0:
        return 1.0
        
    if clean == "1000PEPEUSDT" and p < 0.0005:
        return 1000.0
    if clean == "1000SHIBUSDT" and p < 0.001:
        return 1000.0
    if clean == "1000BONKUSDT" and p < 0.001:
        return 1000.0
    if clean == "1000FLOKIUSDT" and p < 0.005:
        return 1000.0
    if clean == "1000CATUSDT" and p < 0.001:
        return 1000.0
    if clean == "1000RATSUSDT" and p < 0.01:
        return 1000.0
    if clean == "1000SATSUSDT" and p < 0.0001:
        return 1000.0
    if clean == "1MBABYDOGEUSDT" and p < 0.0001:
        return 1000000.0
        
    return 1.0


def validate_config() -> None:
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is not set in .env")
    if not TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID is not set in .env")
    if errors:
        raise ValueError("Configuration errors:\n  " + "\n  ".join(errors))



def is_weekend_trading_blocked(symbol: str) -> bool:
    """
    STRICT USER DIRECTIVE:
    'no trdes of comodities and btc and eth on sat and sunday in any case'
    Blocks trades for:
      - Commodities (Gold, Silver, Crude Oil, Natural Gas)
      - BTC (Bitcoin)
      - ETH (Ethereum)
      - US Stock Tokens (Closed on weekends)
    On Saturday and Sunday in either UTC or IST timezone.
    """
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    is_sat_or_sun = now_utc.weekday() in (5, 6)
    if not is_sat_or_sun:
        try:
            import zoneinfo
            now_ist = now_utc.astimezone(zoneinfo.ZoneInfo("Asia/Kolkata"))
            is_sat_or_sun = now_ist.weekday() in (5, 6)
        except Exception:
            pass

    if not is_sat_or_sun:
        return False

    clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "").split(":")[0]
    base = clean.replace("USDT", "").replace("USD", "").replace("FDUSD", "")
    
    # 1. Exact BTC and ETH
    if base in ("BTC", "ETH"):
        return True

    # 2. Commodities (Gold, Silver, Crude Oil, Natgas)
    if any(clean.startswith(c) for c in ["XAU", "XAUT", "XAG", "NATGAS", "CL", "CRUDEOIL"]) or base in ("GOLD", "SILVER", "OIL"):
        return True

    # 3. US Equities / Stocks (Exact symbol matching - avoids substring collisions like HBAR/BAT matching BA)
    us_stocks_exact = {
        "NVDA", "TSLA", "MSTR", "AAPL", "AMZN", "GOOGL", "META", "MSFT", 
        "COINB", "AMDB", "QQQ", "SPY", "BABAB", "INTCB", "DISB", "PYPLB", 
        "BAB", "XOMB", "SOXL", "SNDKB"
    }
    if base in us_stocks_exact or (len(base) > 3 and base.endswith("B") and base[:-1] in us_stocks_exact):
        return True

    return False


def is_allowed_trading_asset(symbol: str) -> bool:
    """
    STRICT USER DIRECTIVE:
    'and from now for signal we are sending as it is but trading will be done in only top 25 coins in volume comodities and us tokens thats it nothng else'

    Trading is strictly restricted to:
      1. Top 25 Crypto Coins by 24h Volume (Binance Futures)
      2. Commodities (Spot Gold, Tether Gold, Silver, Crude Oil, Natural Gas)
      3. US Stock Tokens (NVDA, TSLA, MSTR, AAPL, AMZN, GOOGL, META, MSFT, COIN, AMD, QQQ, SPY, etc.)
    Everything else is 100% blocked from paper trading / live trading execution.
    """
    if not symbol:
        return False

    clean = symbol.upper().replace("/", "").replace("_", "").replace("-", "").split(":")[0]

    # 1. Commodities (Gold, Silver, Crude Oil, Natural Gas, Copper, Bronze)
    if any(c in clean for c in ["XAU", "XAUT", "GOLD", "XAG", "SILVER", "NATGAS", "CL", "OIL", "WTI", "BRENT", "COPPER", "BRONZE", "HG"]):
        return True

    # 2. US Equities / Stocks: AUTO-TRADING STOPPED per User Directive
    # Signals are still broadcast to Telegram, but automated trade execution is disabled.
    # NOTE: Use exact base-asset matching (not substring) to avoid false positives like
    #       SHIB→SHI, ARB→AR, BNB→BN, TRB→TR, METAM→META collision etc.
    us_stock_set = {s.upper() for s in US_STOCK_TOKENS}
    base = clean.replace("USDT", "").replace("USD", "").replace("FDUSD", "")
    if base in us_stock_set:
        return False
    # Also block B-suffix synthetics (e.g. NVDABUSDT → base=NVDAB → base[:-1]=NVDA in set)
    if len(base) > 3 and base.endswith("B") and base[:-1] in us_stock_set:
        return False

    # 3. Top 25 Crypto Coins by 24h Volume
    try:
        from fetcher import get_top_pairs_by_volume
        top_25 = get_top_pairs_by_volume(top_n=25)
        top_25_clean = {s.upper().replace("/", "").replace("_", "").replace("-", "").split(":")[0] for s in top_25}
        if clean in top_25_clean:
            return True
    except Exception:
        pass

    # Static fallback for Top 25 crypto volume if network query unavailable
    fallback_top_25 = {
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
        "NEARUSDT", "DOTUSDT", "LTCUSDT", "SHIBUSDT", "TRXUSDT",
        "UNIUSDT", "AAVEUSDT", "ARBUSDT", "POLUSDT", "RENDERUSDT",
        "TAOUSDT", "JUPUSDT", "ONDOUSDT", "OPUSDT", "PEPEUSDT"
    }
    return clean in fallback_top_25
