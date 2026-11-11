
STOCK_ALIAS_MAP = {
    "NVDAUSDT": "NVDABUSDT", "TSLAUSDT": "TSLABUSDT", "AAPLUSDT": "AAPLBUSDT",
    "MSTRUSDT": "MSTRBUSDT", "AMZNUSDT": "AMZNBUSDT", "METAUSDT": "METABUSDT",
    "MSFTUSDT": "MSFTBUSDT", "GOOGLUSDT": "GOOGLBUSDT", "SPYUSDT": "SPYBUSDT",
    "QQQUSDT": "QQQBUSDT", "COINUSDT": "COINBUSDT", "AMDUSDT": "AMDBUSDT",
    "NFLXUSDT": "NFLXBUSDT", "BABAUSDT": "BABABUSDT",
}

STOCK_YFINANCE_MAP = {
    "NVDA": "NVDA", "NVDAB": "NVDA", "NVDAUSDT": "NVDA", "NVDABUSDT": "NVDA",
    "TSLA": "TSLA", "TSLAB": "TSLA", "TSLAUSDT": "TSLA", "TSLABUSDT": "TSLA",
    "AAPL": "AAPL", "AAPLB": "AAPL", "AAPLUSDT": "AAPL", "AAPLBUSDT": "AAPL",
    "MSTR": "MSTR", "MSTRB": "MSTR", "MSTRUSDT": "MSTR", "MSTRBUSDT": "MSTR",
    "AMZN": "AMZN", "AMZNB": "AMZN", "AMZNUSDT": "AMZN", "AMZNBUSDT": "AMZN",
    "META": "META", "METAB": "META", "METAUSDT": "META", "METABUSDT": "META",
    "MSFT": "MSFT", "MSFTB": "MSFT", "MSFTUSDT": "MSFT", "MSFTBUSDT": "MSFT",
    "GOOGL": "GOOGL", "GOOGLB": "GOOGL", "GOOGLUSDT": "GOOGL", "GOOGLBUSDT": "GOOGL",
    "SPY": "SPY", "SPYB": "SPY", "SPYUSDT": "SPY", "SPYBUSDT": "SPY",
    "QQQ": "QQQ", "QQQB": "QQQ", "QQQUSDT": "QQQ", "QQQBUSDT": "QQQ",
    "COIN": "COIN", "COINB": "COIN", "COINUSDT": "COIN", "COINBUSDT": "COIN",
    "AMD": "AMD", "AMDB": "AMD", "AMDUSDT": "AMD", "AMDBUSDT": "AMD",
    "NFLX": "NFLX", "NFLXB": "NFLX", "NFLXUSDT": "NFLX", "NFLXBUSDT": "NFLX",
    "BABA": "BABA", "BABAB": "BABA", "BABAUSDT": "BABA", "BABABUSDT": "BABA",
}

def _fetch_stock_yfinance_sync(ticker: str, interval: str, limit: int = 100):
    """Fallback fetcher for US Equities via yfinance when exchange token candle is unavailable."""
    try:
        import yfinance as yf
        import pandas as pd
        interval_map = {
            "5m": ("5d", "5m"), "15m": ("5d", "15m"), "30m": ("5d", "30m"),
            "1h": ("60d", "1h"), "4h": ("60d", "1h"), "1d": ("1y", "1d"), "1w": ("2y", "1wk")
        }
        period, itv = interval_map.get(interval.lower(), ("60d", "1h"))
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=itv)
        if df is not None and len(df) > 0:
            if interval.lower() == "4h":
                agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
                df = df.resample("4h").agg(agg).dropna()
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.columns = ["open", "high", "low", "close", "volume"]
            df.index = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
            df.index.name = "timestamp"
            res_df = df.astype(float)
            return res_df.iloc[-limit:] if (limit and len(res_df) > limit) else res_df
    except Exception as e:
        logger.debug(f"yfinance fallback error for {ticker}: {e}")
    return None

"""
fetcher.py — Fetch OHLCV data from Binance Futures API via direct async HTTP calls.
Provides complete coverage for Futures USDT contracts (AIN/USDT, BTC/USDT, SOL/USDT, etc.).
"""

import ccxt
import pandas as pd
import asyncio
import aiohttp
import logging
import time
from config import OHLCV_LIMIT, MAX_PAIRS, TIMEFRAMES, clean_contract_symbol

logger = logging.getLogger(__name__)

# Initialize ccxt exchange instance for Binance Futures
exchange = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})

_cached_pairs = []
_last_fetch_time = 0.0
CACHE_DURATION_SECONDS = 3600.0  # Cache volume ranking for 1 hour


def get_top_pairs_by_volume(top_n: int = 100) -> list:
    """Return top N active USDT futures pairs ranked by 24H quote volume (cached for 1 hour)."""
    global _cached_pairs, _last_fetch_time
    now = time.time()

    # Return cached list if valid
    if _cached_pairs and (now - _last_fetch_time < CACHE_DURATION_SECONDS):
        return _cached_pairs[:top_n]

    try:
        # Fetch 24h ticker data directly from Binance Futures API for 100% accurate futures contract coverage
        import urllib.request
        import json

        ticker_urls = [
            "https://data-api.binance.vision/api/v3/ticker/24hr",
            "https://api.binance.me/api/v3/ticker/24hr",
            "https://fapi.binance.com/fapi/v1/ticker/24hr"
        ]
        raw_data = None
        for u in ticker_urls:
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    raw_data = json.loads(resp.read().decode("utf-8"))
                    if raw_data and isinstance(raw_data, list):
                        break
            except Exception:
                continue

        if not raw_data:
            raise RuntimeError("All ticker endpoints failed")

        pairs = []
        stable_prefixes = ("USD", "EUR", "GBP", "AUD", "CAD", "JPY", "CHF", "BRL", "TRY")
        known_stables = {"U", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDD", "RLUSD", "AEUR", "PYUSD", "USDE", "USD1", "USD0", "USDS", "WBTC", "CBETH", "WBETH", "STETH"}

        for item in raw_data:
            sym = item.get("symbol", "")
            if sym.endswith("USDT"):
                base_coin = sym[:-4].upper()
                if base_coin not in known_stables and not any(base_coin.startswith(p) for p in stable_prefixes):
                    quote_vol = float(item.get("quoteVolume") or 0.0)
                    if quote_vol >= 5000000.0:  # Minimum $5M 24h quote volume
                        formatted_symbol = f"{base_coin}/USDT"
                        pairs.append((formatted_symbol, quote_vol))

        pairs.sort(key=lambda x: x[1], reverse=True)
        top = [p[0] for p in pairs]

        if top:
            _cached_pairs = top
            _last_fetch_time = now
            logger.info(f"Refreshed top Futures pairs volume list from Binance (found {len(pairs)} pairs >= $5M USDT volume).")
            return top if (top_n is None or top_n <= 0) else top[:top_n]

    except Exception as e:
        logger.error(f"Failed to fetch Futures tickers directly: {e}. Trying ccxt fallback...")

    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()

        pairs = []
        stable_prefixes = ("USD", "EUR", "GBP", "AUD", "CAD", "JPY", "CHF", "BRL", "TRY")
        known_stables = {"U", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDD", "RLUSD", "AEUR", "PYUSD", "USDE", "USD1", "USD0", "USDS", "WBTC", "CBETH", "WBETH", "STETH"}
        
        for symbol, market in markets.items():
            base_coin = symbol.split("/")[0].upper()
            if (market.get("quote") == "USDT"
                    and market.get("active", False)
                    and "/" in symbol
                    and base_coin not in known_stables
                    and not any(base_coin.startswith(p) for p in stable_prefixes)):
                ticker = tickers.get(symbol, {})
                quote_vol = float(ticker.get("quoteVolume") or 0.0)
                if quote_vol >= 5000000.0:
                    pairs.append((symbol, quote_vol))

        pairs.sort(key=lambda x: x[1], reverse=True)
        top = [p[0] for p in pairs]
        
        if top:
            _cached_pairs = top
            _last_fetch_time = now
            logger.info(f"Refreshed top pairs volume list from Binance via ccxt (found {len(pairs)} pairs).")
            return top[:top_n]

    except Exception as e2:
        logger.error(f"ccxt fallback failed: {e2}")
        if _cached_pairs:
            return _cached_pairs[:top_n]
        return []

    return _cached_pairs[:top_n]


_ohlcv_cache = {}

# ── ONLY GOLD USES SPOT PRICE (XAU/USD); ALL REST USE USDT TOKENS ─────────────
SPOT_GOLD_MAP = {
    "XAU/USD":   "XAU/USDT:USDT",  # Live Spot Gold (XAU/USD)
    "XAUT/USDT": "XAUT/USDT",  # Live Tether Gold Token
}

_bybit_exchange = None

def _get_bybit_instance():
    global _bybit_exchange
    if _bybit_exchange is None:
        import ccxt
        _bybit_exchange = ccxt.bybit({"enableRateLimit": True, "timeout": 8000})
    return _bybit_exchange

def _fetch_spot_gold_sync(symbol: str, interval: str, limit: int = 100):
    """
    Fetches real Spot Gold (XAU/USD) price from real-time Forex spot feed, NOT Bybit crypto XAUUSDT.
    For XAUT/USDT, fetches the actual Tether Gold token from exchange spot.
    """
    clean_sym = symbol.upper().strip()
    is_token = "XAUT" in clean_sym

    # 1. TOKEN GOLD (XAUT/USDT): Fetch Tether Gold Token from Bybit Spot
    if is_token:
        try:
            ex = _get_bybit_instance()
            tf = interval.lower()
            if tf in ["15m", "30m", "1h", "4h", "1d", "1w", "5m"]:
                candles = ex.fetch_ohlcv("XAUT/USDT", tf, limit=limit or 100)
                if candles and len(candles) >= 1:
                    import pandas as pd
                    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                    df.set_index("timestamp", inplace=True)
                    return df.astype(float)
        except Exception as e_tok:
            logger.warning(f"XAUT/USDT Token fetch error: {e_tok}")

    # 2. REAL SPOT GOLD (XAU/USD): Fetch live London/NY Spot Gold, strictly NOT Bybit XAUUSDT
    try:
        import urllib.request, json
        import yfinance as yf
        import pandas as pd

        # 1. Primary: Direct TradingView OANDA CFD feed (Standard benchmark for Exness, OANDA, MetaTrader)
        spot_px = None
        tv_candle = None
        try:
            url_tv = 'https://scanner.tradingview.com/cfd/scan'
            headers_tv = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            tf_map = {'5m': '|5', '15m': '|15', '30m': '|30', '1h': '|60', '4h': '|240', '1d': ''}
            suffix = tf_map.get(interval.lower(), '|5')
            cols = ['close', f'close{suffix}', f'open{suffix}', f'high{suffix}', f'low{suffix}']
            payload = json.dumps({'symbols': {'tickers': ['OANDA:XAUUSD']}, 'columns': cols}).encode('utf-8')
            req_tv = urllib.request.Request(url_tv, data=payload, headers=headers_tv)
            res_tv = json.loads(urllib.request.urlopen(req_tv, timeout=3.5).read().decode('utf-8'))
            vals = res_tv['data'][0]['d']
            spot_px = float(vals[0])
            if vals[1] is not None and vals[2] is not None:
                tv_candle = {
                    'close': float(vals[1]),
                    'open': float(vals[2]),
                    'high': float(vals[3]),
                    'low': float(vals[4])
                }
        except Exception as e_tv:
            logger.debug(f"TradingView OANDA spot fetch skipped: {e_tv}")

        # 2. Secondary Spot Gold Fallback: api.gold-api.com (London/NY Institutional Spot)
        if not spot_px or spot_px < 1000:
            try:
                req = urllib.request.Request('https://api.gold-api.com/price/XAU', headers={'User-Agent': 'Mozilla/5.0'})
                r_spot = json.loads(urllib.request.urlopen(req, timeout=3.5).read())
                spot_px = float(r_spot.get('price', 0))
            except Exception as e_gapi:
                logger.debug(f"gold-api fallback error: {e_gapi}")

        # Fetch institutional candles
        interval_map = {
            "5m": ("5d", "5m"), "15m": ("5d", "15m"), "30m": ("5d", "30m"),
            "1h": ("60d", "1h"), "4h": ("60d", "1h"), "1d": ("1y", "1d"), "1w": ("2y", "1wk"),
        }
        period, itv = interval_map.get(interval.lower(), ("60d", "1h"))
        t = yf.Ticker("GC=F")
        df = t.history(period=period, interval=itv)
        if df is not None and len(df) > 0:
            if interval.lower() == "4h":
                agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
                df = df.resample("4h").agg(agg).dropna()
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.columns = ["open", "high", "low", "close", "volume"]
            df.index = df.index.tz_convert("UTC") if df.index.tz is not None else df.index.tz_localize("UTC")
            df.index.name = "timestamp"
            res_df = df.astype(float)

            if spot_px and spot_px > 1000 and len(res_df) > 0:
                last_c = float(res_df['close'].iloc[-1])
                spread_offset = spot_px - last_c
                res_df[['open', 'high', 'low', 'close']] += spread_offset
                
                # If exact TradingView OANDA candle is available, align the live candle exactly
                if tv_candle:
                    res_df.iloc[-1, res_df.columns.get_loc('open')] = tv_candle['open']
                    res_df.iloc[-1, res_df.columns.get_loc('high')] = max(tv_candle['high'], res_df['high'].iloc[-1])
                    res_df.iloc[-1, res_df.columns.get_loc('low')] = min(tv_candle['low'], res_df['low'].iloc[-1])
                    res_df.iloc[-1, res_df.columns.get_loc('close')] = tv_candle['close']
                    logger.info(f"Loaded Real Spot XAU/USD (TradingView OANDA CFD): ${tv_candle['close']:.2f}")
                else:
                    logger.info(f"Loaded Real Spot XAU/USD: ${spot_px:.2f} (Forex Spot London/NY)")

            return res_df.iloc[-limit:] if (limit and len(res_df) > limit) else res_df
    except Exception as e_spot:
        logger.warning(f"Real Spot XAU/USD fetch error: {e_spot}. Trying fallback...")

    # Fallback to token if spot API is temporarily unreachable
    try:
        ex = _get_bybit_instance()
        tf = interval.lower()
        candles = ex.fetch_ohlcv("XAUT/USDT", tf, limit=limit or 100)
        if candles and len(candles) >= 1:
            import pandas as pd
            df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            return df.astype(float)
    except Exception as e_fb:
        logger.error(f"Fallback gold fetch error: {e_fb}")

    return None

async def fetch_ohlcv_async(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int, semaphore: asyncio.Semaphore):
    """
    Fetch OHLCV candles asynchronously with TTL caching.
    Routes Commodities (Gold, Silver, Oil, Gas) to institutional CFD feeds.
    Routes Crypto to Binance Futures / Spot API.
    Returns DataFrame or None.
    """
    cache_key = f"{symbol}_{interval}"
    now = time.time()
    
    # TTL caching to stay well under rate limits
    ttl = 20.0  # 20 seconds for fast timeframes
    if interval.lower() in ["4h"]:
        ttl = 60.0  # 1 minute for 4H
    elif interval.lower() in ["1d", "1w"]:
        ttl = 300.0 # 5 minutes for 1D/1W
        
    if cache_key in _ohlcv_cache:
        cached_df, cached_time = _ohlcv_cache[cache_key]
        if now - cached_time < ttl:
            return cached_df

    # 1. SPOT GOLD ROUTE ONLY (XAU/USD / XAUT/USDT)
    if symbol in SPOT_GOLD_MAP or "XAU" in symbol.upper():
        loop = asyncio.get_running_loop()
        spot_df = await loop.run_in_executor(None, _fetch_spot_gold_sync, symbol, interval, limit)
        if spot_df is not None and len(spot_df) >= 5:
            _ohlcv_cache[cache_key] = (spot_df, time.time())
            return spot_df
        logger.warning(f"Spot gold route returned no data for {symbol} {interval}, falling back to Binance")

    # 2. ALL OTHER ASSETS USE NATIVE USDT TOKENS (Binance Futures API)
    binance_symbol = symbol.replace("/", "").upper()
    target_symbols = [binance_symbol]
    if binance_symbol in STOCK_ALIAS_MAP:
        target_symbols.insert(0, STOCK_ALIAS_MAP[binance_symbol])
    elif binance_symbol.endswith("BUSDT"):
        target_symbols.append(binance_symbol[:-5] + "USDT")
    vision_url = f"https://data-api.binance.vision/api/v3/klines?symbol={binance_symbol}&interval={interval}&limit={limit}"
    spot_me_url = f"https://api.binance.me/api/v3/klines?symbol={binance_symbol}&interval={interval}&limit={limit}"
    futures_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={binance_symbol}&interval={interval}&limit={limit}"
    spot_url = f"https://api.binance.com/api/v3/klines?symbol={binance_symbol}&interval={interval}&limit={limit}"
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with semaphore:
        await asyncio.sleep(0.10)  # Throttling to prevent Binance rate limit WAF bans
        try:
            # Try unblocked vision/me API first, then Futures/Spot
            urls_to_try = []
            for t_sym in target_symbols:
                urls_to_try.extend([
                    f"https://data-api.binance.vision/api/v3/klines?symbol={t_sym}&interval={interval}&limit={limit}",
                    f"https://api.binance.me/api/v3/klines?symbol={t_sym}&interval={interval}&limit={limit}",
                    f"https://fapi.binance.com/fapi/v1/klines?symbol={t_sym}&interval={interval}&limit={limit}",
                    f"https://api.binance.com/api/v3/klines?symbol={t_sym}&interval={interval}&limit={limit}",
                ])

            for k_url in urls_to_try:
                try:
                    async with session.get(k_url, headers=headers, timeout=5) as response:
                        if response.status == 200:
                            raw = await response.json()
                            if isinstance(raw, list) and len(raw) >= 5:
                                data = [
                                    [
                                        x[0], x[1], x[2], x[3], x[4], x[5],
                                        float(x[9]) if len(x) > 9 else (float(x[5]) * 0.5)
                                    ]
                                    for x in raw
                                ]
                                df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume", "taker_buy_vol"])
                                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                                df.set_index("timestamp", inplace=True)
                                res_df = df.astype(float)
                                _ohlcv_cache[cache_key] = (res_df, time.time())
                                return res_df
                except Exception:
                    continue

            # Fallback to Spot API
            async with session.get(spot_url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    raw = await response.json()
                    if isinstance(raw, list) and len(raw) >= 5:
                        data = [
                            [
                                x[0], x[1], x[2], x[3], x[4], x[5],
                                float(x[9]) if len(x) > 9 else (float(x[5]) * 0.5)
                            ]
                            for x in raw
                        ]
                        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume", "taker_buy_vol"])
                        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                        df.set_index("timestamp", inplace=True)
                        res_df = df.astype(float)
                        _ohlcv_cache[cache_key] = (res_df, time.time())
                        return res_df

            # Check yfinance fallback for US stocks
            yf_ticker = STOCK_YFINANCE_MAP.get(binance_symbol)
            if yf_ticker:
                loop = asyncio.get_running_loop()
                yf_df = await loop.run_in_executor(None, _fetch_stock_yfinance_sync, yf_ticker, interval, limit)
                if yf_df is not None and len(yf_df) >= 5:
                    _ohlcv_cache[cache_key] = (yf_df, time.time())
                    logger.info(f"Loaded US Stock {symbol} ({yf_ticker}) via yfinance fallback.")
                    return yf_df

            logger.warning(f"Failed to fetch OHLCV for {symbol} {interval} from Binance Futures/Spot")
            return None

        except Exception as e:
            logger.warning(f"Failed to fetch {symbol} {interval}: {e}")
            return None


async def fetch_all_timeframes_async(session: aiohttp.ClientSession, symbol: str, semaphore: asyncio.Semaphore) -> dict:
    """
    Fetch OHLCV for all configured timeframes asynchronously.
    Returns dict keyed by display label + "1H_raw" for SMC.
    """
    ALLOWED_15M_COINS = {
        "BTC/USDT", "ETH/USDT", "XAUT/USDT", "XAG/USDT", "BNB/USDT", "XRP/USDT", "SOL/USDT",
        "NVDA/USDT", "TSLA/USDT", "MSTR/USDT", "AAPL/USDT", "AMZN/USDT", "GOOGL/USDT", "META/USDT", "MSFT/USDT", "COIN/USDT", "AMD/USDT", "QQQ/USDT", "SPY/USDT"
    }
    results = {}
    intervals_to_fetch = {
        "30M": ("30m", OHLCV_LIMIT),
        "1H": ("1h", OHLCV_LIMIT),
        "4H": ("4h", OHLCV_LIMIT),
        "1D": ("1d", OHLCV_LIMIT),
        "1W": ("1w", OHLCV_LIMIT)
    }
    # Always fetch 15M for all scanned pairs to support 15M Squeeze Triggers & Pre-Squeeze Radar
    intervals_to_fetch["15M"] = ("15m", OHLCV_LIMIT)
    if symbol in ["XAU/USD", "XAUT/USDT", "XAUTUSDT"] or "XAU" in symbol.upper():
        intervals_to_fetch["5M"] = ("5m", OHLCV_LIMIT)
    
    tasks = {}
    for label, (interval, limit) in intervals_to_fetch.items():
        tasks[label] = asyncio.create_task(
            fetch_ohlcv_async(session, symbol, interval, limit, semaphore)
        )
    
    for label, task in tasks.items():
        results[label] = await task
        
    results["1H_raw"] = results.get("1H")
    return results


# ── FUTURES MICROSTRUCTURE INTEL (FUNDING RATE, OPEN INTEREST & SESSION) ──
_microstructure_cache = {}

def get_market_session_info() -> dict:
    """Returns current institutional trading session info based on IST / UTC."""
    import zoneinfo
    from datetime import datetime, timezone
    try:
        ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(timezone.utc).astimezone(ist_tz)
        hour = now_ist.hour + (now_ist.minute / 60.0)
        
        if 12.5 <= hour <= 16.5:
            return {"session_name": "London Institutional Killzone", "tag": "🏛️ London Killzone (Prime Momentum)", "is_prime": True}
        elif 18.5 <= hour <= 22.5:
            return {"session_name": "New York Institutional Killzone", "tag": "🗽 New York Killzone (Peak Liquidity)", "is_prime": True}
        elif 5.5 <= hour <= 11.5:
            return {"session_name": "Asian Session", "tag": "🌏 Asian Session (Accumulation / Range)", "is_prime": False}
        else:
            return {"session_name": "Global Inter-Session", "tag": "🌐 Global Session", "is_prime": False}
    except Exception:
        return {"session_name": "Global Session", "tag": "🌐 Global Session", "is_prime": False}


def calculate_cvd_metrics(df: pd.DataFrame, signal: str = "BUY") -> dict:
    """
    Point 4: Calculates Cumulative Volume Delta (CVD) & Orderbook Absorption.
    Detects aggressive buying/selling vs absorption/divergence traps.
    Returns metrics and human-friendly display tag.
    """
    if df is None or len(df) < 5:
        return {
            "cvd_display": "N/A (Standard Vol)",
            "cvd_chg": 0.0,
            "delta_pct": 0.0,
            "is_aligned": True,
            "absorption_flag": False,
            "absorption_tag": ""
        }
    try:
        vol = df["volume"]
        if "taker_buy_vol" in df.columns:
            buy_vol = df["taker_buy_vol"]
        else:
            buy_vol = vol * 0.5
        sell_vol = vol - buy_vol
        delta = buy_vol - sell_vol
        cvd = delta.cumsum()

        n = min(5, len(df))
        cvd_chg = float(cvd.iloc[-1] - cvd.iloc[-n])
        px_chg = float(df["close"].iloc[-1] - df["close"].iloc[-n])
        latest_delta = float(delta.iloc[-1])
        latest_vol = float(vol.iloc[-1])
        delta_pct = (latest_delta / latest_vol * 100.0) if latest_vol > 0 else 0.0

        absorption_flag = False
        absorption_tag = ""
        if px_chg > 0 and cvd_chg < 0:
            absorption_flag = True
            absorption_tag = "⚠️ Bearish Absorption (Limit Sellers Capping)"
        elif px_chg < 0 and cvd_chg > 0:
            absorption_flag = True
            absorption_tag = "🛡️ Bullish Absorption (Limit Buyers Defending)"

        is_buy = str(signal).upper() in ("BUY", "LONG")
        sign_d = "+" if delta_pct >= 0 else ""

        if is_buy:
            is_aligned = (cvd_chg > 0 or absorption_tag.startswith("🛡️"))
            if cvd_chg > 0 and latest_delta > 0:
                flow_status = f"{sign_d}{delta_pct:.1f}% Delta (Aggressive Buyers 🟢)"
            elif absorption_flag:
                flow_status = absorption_tag
            else:
                flow_status = f"{sign_d}{delta_pct:.1f}% Delta (Cautious Delta ⚠️)"
        else:
            is_aligned = (cvd_chg < 0 or absorption_tag.startswith("⚠️"))
            if cvd_chg < 0 and latest_delta < 0:
                flow_status = f"{sign_d}{delta_pct:.1f}% Delta (Aggressive Sellers 🔴)"
            elif absorption_flag:
                flow_status = absorption_tag
            else:
                flow_status = f"{sign_d}{delta_pct:.1f}% Delta (Cautious Delta ⚠️)"

        return {
            "cvd_display": flow_status,
            "cvd_chg": cvd_chg,
            "delta_pct": delta_pct,
            "is_aligned": is_aligned,
            "absorption_flag": absorption_flag,
            "absorption_tag": absorption_tag
        }
    except Exception as e:
        logger.warning(f"Error calculating CVD metrics: {e}")
        return {
            "cvd_display": "N/A (Standard Vol)",
            "cvd_chg": 0.0,
            "delta_pct": 0.0,
            "is_aligned": True,
            "absorption_flag": False,
            "absorption_tag": ""
        }

async def fetch_futures_microstructure_async(session: aiohttp.ClientSession, symbol: str, signal: str = "BUY", df: pd.DataFrame = None) -> dict:
    """
    Fetches live Funding Rate & Open Interest (OI) from Binance Futures (or Bybit fallback),
    evaluates squeeze conditions (Long-Squeeze vs Short-Squeeze), and returns whale insight.
    """
    global _microstructure_cache
    now = time.time()
    cache_key = f"{symbol}_{signal}"
    if cache_key in _microstructure_cache:
        cached_data, ts = _microstructure_cache[cache_key]
        if now - ts < 60.0:
            return cached_data

    clean_sym = symbol.replace("/", "").replace("-", "").upper()
    if ":" in clean_sym:
        clean_sym = clean_sym.split(":")[0]

    funding_rate = 0.0001 # Default 0.01%
    oi_change_1h = 0.0

    # 1. Binance Futures API
    own_session = False
    try:
        if session is None:
            session = aiohttp.ClientSession()
            own_session = True
        headers = {"User-Agent": "Mozilla/5.0"}
        f_url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={clean_sym}&limit=1"
        async with session.get(f_url, headers=headers, timeout=4) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    funding_rate = float(data[-1].get("fundingRate", 0.0001))
        
        oi_url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={clean_sym}&period=1h&limit=2"
        async with session.get(oi_url, headers=headers, timeout=4) as resp_oi:
            if resp_oi.status == 200:
                oi_hist = await resp_oi.json()
                if isinstance(oi_hist, list) and len(oi_hist) >= 2:
                    prev_oi = float(oi_hist[0].get("sumOpenInterest", 0.0))
                    curr_oi = float(oi_hist[1].get("sumOpenInterest", 0.0))
                    if prev_oi > 0:
                        oi_change_1h = ((curr_oi - prev_oi) / prev_oi) * 100.0
    except Exception:
        pass
    finally:
        if own_session and session:
            try:
                await session.close()
            except Exception:
                pass
        # Fallback to Bybit if Binance is unreachable
        try:
            ex = _get_bybit_instance()
            bybit_sym = f"{symbol}:USDT" if ":" not in symbol else symbol
            fr_info = ex.fetch_funding_rate(bybit_sym)
            if fr_info and fr_info.get("fundingRate") is not None:
                funding_rate = float(fr_info["fundingRate"])
        except Exception:
            pass

    funding_pct = funding_rate * 100.0
    is_squeeze = False
    market_condition = "⚖️ BALANCED EXPANSION"
    whale_insight = ""

    if signal is None:
        if funding_pct <= -0.015:
            is_squeeze = True
            is_buy = True
            market_condition = "🚀 HIGH PROBABILITY SHORT SQUEEZE"
            whale_insight = "Heavily negative funding indicates trapped retail shorts. Upward bounce triggers forced short liquidations."
        elif funding_pct >= 0.030:
            is_squeeze = True
            is_buy = False
            market_condition = "💥 HIGH PROBABILITY LONG FLUSH"
            whale_insight = "Retail longs paying extreme funding. 1H EMA breakdown triggers forced liquidation cascade."
        else:
            is_buy = True
    else:
        is_buy = str(signal).upper() in ("BUY", "LONG")

    if not is_buy: # SELL / SHORT
        if funding_pct >= 0.030:
            is_squeeze = True
            market_condition = "💥 HIGH PROBABILITY LONG FLUSH"
            whale_insight = "Retail longs paying extreme funding. 1H EMA breakdown triggers forced liquidation cascade."
        elif oi_change_1h >= 4.0:
            market_condition = "💎 INSTITUTIONAL FRESH SHORT"
            whale_insight = "Rising Open Interest confirms aggressive new institutional short positions."
        elif oi_change_1h <= -5.0:
            market_condition = "⚠️ LONG FLUSH CAPITULATION"
            whale_insight = "Longs closing out positions; watch for quick bottom bounce once selling pressure abates."
        else:
            market_condition = "⚖️ BALANCED SHORT EXECUTION"
            whale_insight = "Healthy futures positioning aligned with technical EMA breakdown."
    else: # BUY / LONG
        if funding_pct <= -0.015:
            is_squeeze = True
            market_condition = "🚀 HIGH PROBABILITY SHORT SQUEEZE"
            whale_insight = "Heavily negative funding indicates trapped retail shorts. Upward bounce triggers forced short liquidations."
        elif oi_change_1h >= 4.0:
            market_condition = "💎 INSTITUTIONAL FRESH LONG"
            whale_insight = "Rising Open Interest confirms real capital inflow and genuine spot/futures accumulation."
        elif oi_change_1h <= -5.0:
            market_condition = "⚠️ SHORT COVERING RALLY"
            whale_insight = "Shorts covering positions; lower conviction trend expansion, take profit promptly at TP1."
        else:
            market_condition = "⚖️ BALANCED LONG EXPANSION"
            whale_insight = "Healthy funding and steady open interest supporting technical EMA trend."

    cvd_metrics = calculate_cvd_metrics(df, signal=signal) if df is not None else {
        "cvd_display": "Active Delta Flow 🟢" if is_buy else "Active Delta Flow 🔴",
        "cvd_chg": 0.0,
        "delta_pct": 0.0,
        "is_aligned": True,
        "absorption_flag": False,
        "absorption_tag": ""
    }

    result = {
        "funding_rate": funding_rate,
        "funding_pct": funding_pct,
        "oi_change_1h_pct": oi_change_1h,
        "market_condition": market_condition,
        "is_squeeze": is_squeeze,
        "whale_insight": whale_insight,
        "session_info": get_market_session_info(),
        "cvd_metrics": cvd_metrics
    }
    
    _microstructure_cache[cache_key] = (result, now)
    return result
