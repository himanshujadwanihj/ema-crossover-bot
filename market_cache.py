"""
market_cache.py — In-memory market analysis cache for instant Telegram filtering.
Maintains latest EMA status across 1H, 4H, 8H, 1D, 1W for all scanned coins.
Supports ANY single timeframe, any combination, and full confluence queries.
"""

import zoneinfo
from datetime import datetime, timezone
from indicators import _compute_indicators

ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
TF_HOURS = {"15m": 0.25, "1H": 1, "4H": 4, "1D": 24, "1W": 168}
VALID_TFS = ["15m", "1H", "4H", "1D", "1W"]

# In-memory dictionary: symbol -> market status
MARKET_CACHE = {}

def update_symbol_cache(symbol: str, tf_data: dict) -> None:
    """Updates in-memory market cache for a single symbol using its fetched timeframes."""
    if not tf_data:
        return
        
    tf_summary = {}
    latest_price = 0.0
    
    for tf in VALID_TFS:
        df = tf_data.get(tf)
        if df is None or len(df) < 15:
            tf_summary[tf] = {
                "trend": "Neutral",
                "detail": "Neutral",
                "bars_ago": -1,
                "pct_move": 0.0
            }
            continue
            
        try:
            df = _compute_indicators(df)
            row = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
            current_close = row['close']
            if latest_price == 0.0:
                latest_price = current_close
                
            ema9 = row.get('ema_9', 0.0)
            ema26 = row.get('ema_26', 0.0)
            trend = "Bullish" if ema9 > ema26 else "Bearish"
            
            bars_ago = -1
            cross_price = None
            cross_dt = None
            
            # Find most recent crossover
            for i in range(len(df)-2, 1, -1):
                cur = df.iloc[i]
                prev = df.iloc[i-1]
                if trend == "Bullish" and prev['ema_9'] <= prev['ema_26'] and cur['ema_9'] > cur['ema_26']:
                    bars_ago = (len(df) - 2) - i
                    cross_price = cur['close']
                    cross_dt = df.index[i]
                    break
                elif trend == "Bearish" and prev['ema_9'] >= prev['ema_26'] and cur['ema_9'] < cur['ema_26']:
                    bars_ago = (len(df) - 2) - i
                    cross_price = cur['close']
                    cross_dt = df.index[i]
                    break
                    
            pct_move = 0.0
            detail_str = ""
            if cross_dt is not None and cross_price is not None and bars_ago >= 0:
                if isinstance(cross_dt, str):
                    dt = datetime.fromisoformat(cross_dt.replace("Z", "+00:00"))
                else:
                    dt = cross_dt
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ist_dt = dt.astimezone(ist_tz)
                elapsed_hrs = bars_ago * TF_HOURS.get(tf, 1)
                
                now_ist = datetime.now(timezone.utc).astimezone(ist_tz)
                time_fmt = ist_dt.strftime("%d-%b %H:%M IST")
                pct_move = ((current_close - cross_price) / cross_price) * 100
                detail_str = f"{pct_move:+.2f}% | {time_fmt}"
            else:
                detail_str = f"{trend} Structure"
                
            raw_rsi = float(row.get('rsi', 0.0) or 0.0)
            raw_adx = float(row.get('adx', 0.0) or 0.0)
            tf_summary[tf] = {
                "trend": trend,
                "ema9": ema9,
                "ema26": ema26,
                "rsi": raw_rsi if raw_rsi == raw_rsi else 0.0,
                "adx": raw_adx if raw_adx == raw_adx else 0.0,
                "bars_ago": bars_ago,
                "cross_price": cross_price,
                "detail": detail_str,
                "pct_move": pct_move
            }
        except Exception:
            tf_summary[tf] = {
                "trend": "Neutral",
                "detail": "Data Error",
                "bars_ago": -1,
                "pct_move": 0.0
            }
            
    MARKET_CACHE[symbol] = {
        "price": latest_price,
        "updated_at": datetime.now(timezone.utc),
        "timeframes": tf_summary
    }
    
    if "BTC" in symbol.upper() and "USDT" in symbol.upper():
        update_btc_pulse(tf_data)

_BTC_PULSE = {}

def update_btc_pulse(tf_data: dict) -> None:
    global _BTC_PULSE
    try:
        df_15m = tf_data.get("15M") if tf_data.get("15M") is not None else tf_data.get("15m")
        df_1h = tf_data.get("1H") if tf_data.get("1H") is not None else tf_data.get("1h")
        df_4h = tf_data.get("4H") if tf_data.get("4H") is not None else tf_data.get("4h")
        
        dumping_15m = False
        pumping_15m = False
        if df_15m is not None and len(df_15m) >= 5:
            df_15m_ind = _compute_indicators(df_15m)
            c = df_15m_ind.iloc[-1]
            c_open = float(c['open'])
            c_close = float(c['close'])
            c_ema9 = float(c.get('ema_9', c_close))
            # Dumping if red candle closing below 9 EMA
            if c_close < c_open and c_close < c_ema9:
                dumping_15m = True
            # Pumping if green candle closing above 9 EMA
            elif c_close > c_open and c_close > c_ema9:
                pumping_15m = True

        btc_1h_bullish = False
        btc_1h_bearish = False
        if df_1h is not None and len(df_1h) >= 26:
            df_1h_ind = _compute_indicators(df_1h)
            c1 = df_1h_ind.iloc[-1]
            c1_close = float(c1['close'])
            c1_ema9 = float(c1.get('ema_9', c1_close))
            c1_ema26 = float(c1.get('ema_26', c1_close))
            if c1_close > c1_ema26 and c1_ema9 >= c1_ema26:
                btc_1h_bullish = True
            elif c1_close < c1_ema26 and c1_ema9 <= c1_ema26:
                btc_1h_bearish = True

        btc_4h_ret = 0.0
        if df_4h is not None and len(df_4h) >= 2:
            btc_4h_ret = float(((df_4h['close'].iloc[-1] - df_4h['close'].iloc[-2]) / df_4h['close'].iloc[-2]) * 100.0)

        _BTC_PULSE = {
            "btc_15m_dumping": dumping_15m,
            "btc_15m_pumping": pumping_15m,
            "btc_1h_bullish": btc_1h_bullish,
            "btc_1h_bearish": btc_1h_bearish,
            "btc_4h_ret": btc_4h_ret,
            "updated_at": datetime.now(timezone.utc)
        }
    except Exception:
        pass

def is_commodity_symbol(symbol: str) -> bool:
    """
    Identifies whether a symbol belongs to the Commodities asset class
    (Gold, Silver, Crude Oil, Natural Gas, Brent, etc.)
    Decoupled from BTC correlation gates per user directive.
    """
    if not symbol:
        return False
    s = str(symbol).upper().strip()
    clean = s.replace("/", "").replace("-", "").replace(":", "").replace("_", "")
    
    known_commodities = {
        "XAUUSD", "XAUUSDT", "XAUTUSDT", "XAUTUSD", "GOLD", "GOLDUSDT",
        "NATGASUSDT", "NATGASUSD", "NATGAS", "NGF",
        "CLUSDT", "CLUSD", "CRUDEOIL", "CRUDEOILUSDT", "OILUSDT", "OILUSD", "WTIUSD", "WTICOUSD", "CLF",
        "XAGUSDT", "XAGUSD", "SILVERUSDT", "SILVERUSD", "SIF",
        "BZUSDT", "BZUSD", "BRENTUSDT", "BRENTUSD", "BZF",
        "COPPERUSDT", "COPPERUSD", "HGUSD", "HGF"
    }
    if clean in known_commodities or s in known_commodities:
        return True
        
    safe_prefixes = ("XAU", "XAUT", "GOLD", "NATGAS", "CRUDE", "WTI", "XAG", "SILVER", "BRENT")
    if any(clean.startswith(p) for p in safe_prefixes):
        return True
        
    # Crude Oil specific (avoids collision with CLV altcoin)
    if clean.startswith("CLUSD") or clean == "CL":
        return True
        
    return False


def is_stock_symbol(symbol: str) -> bool:
    """Identifies US Equity synthetics/tokens decoupled from BTC."""
    if not symbol:
        return False
    s = str(symbol).upper().strip()
    clean = s.replace("/", "").replace("-", "").replace(":", "").replace("_", "")
    stock_prefixes = (
        "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "GOOGL", "META", "AMD", "INTC",
        "MSTR", "COIN", "PLTR", "NFLX", "BABA", "DIS", "PYPL", "BA", "XOM",
        "SPY", "QQQ", "SOXL", "SOXS", "SPCX"
    )
    return any(clean.startswith(p) for p in stock_prefixes)


def is_btc_safe_for_altcoins(signal: str, symbol: str, alt_tf_data: dict = None) -> tuple:
    clean_sym = symbol.upper().replace("/", "").replace("-", "").replace(":", "").replace("_", "")
    
    # 1. BTC itself is exempt
    if clean_sym.startswith("BTC") or clean_sym.startswith("WBTC"):
        return True, "Exempt (BTC)"

    # 2. USER DIRECTIVE: Commodities are 100% exempt from BTC Micro-Gate & correlation
    if is_commodity_symbol(symbol):
        return True, f"Exempt (Commodity {symbol} decoupled from BTC)"

    # 3. US Equities are exempt
    if is_stock_symbol(symbol):
        return True, f"Exempt (Stock {symbol} decoupled from BTC)"

    pulse = _BTC_PULSE
    if not pulse:
        return True, "No BTC pulse yet"

    sig_clean = str(signal).upper()

    # Rule 1: BTC Micro-Gate (15M Correlation)
    if sig_clean in ["BUY", "LONG"]:
        if pulse.get("btc_15m_dumping", False):
            return False, "BTC currently dumping below 15M 9 EMA"
        if pulse.get("btc_1h_bearish", False):
            return False, "BTC 1H Macro is Bearish (Below 26 EMA) — Altcoin Longs prohibited"
    elif sig_clean in ["SELL", "SHORT"]:
        if pulse.get("btc_15m_pumping", False):
            return False, "BTC currently pumping above 15M 9 EMA"
        if pulse.get("btc_1h_bullish", False):
            return False, "BTC 1H Macro is Bullish (Above 26 EMA) — Altcoin Shorts prohibited"

    # Rule 2: Altcoin Relative Strength (vs BTC 4H)
    if sig_clean in ["BUY", "LONG"] and alt_tf_data:
        df_4h = alt_tf_data.get("4H") if alt_tf_data.get("4H") is not None else alt_tf_data.get("4h")
        if df_4h is not None and len(df_4h) >= 2:
            alt_4h_ret = float(((df_4h['close'].iloc[-1] - df_4h['close'].iloc[-2]) / df_4h['close'].iloc[-2]) * 100.0)
            btc_4h_ret = pulse.get("btc_4h_ret", 0.0)
            if alt_4h_ret < (btc_4h_ret - 2.5):
                return False, f"Altcoin severely underperforming BTC ({alt_4h_ret:+.1f}% vs BTC {btc_4h_ret:+.1f}%)"

    return True, "Safe"


def normalize_timeframe_tokens(tokens: list) -> list:
    """Takes any user input list like ['1h', '1d', '4H'] and normalizes to unique valid TFs in order."""
    normalized = []
    lookup = {"15m": "15m", "15M": "15m", "1H": "1H", "4H": "4H", "8H": "8H", "1D": "1D", "1W": "1W",
              "15": "15m", "1": "1H", "4": "4H", "8": "8H", "D": "1D", "W": "1W"}
    for raw in tokens:
        clean = raw.upper().strip().replace("/", "").replace("+", "").replace("-", "")
        # handle glued combos like '1H1D' or '1H4H'
        for tf in VALID_TFS:
            if tf in clean and tf not in normalized:
                normalized.append(tf)
        if clean in lookup and lookup[clean] not in normalized:
            normalized.append(lookup[clean])
            
    # Sort according to standard order
    return [tf for tf in VALID_TFS if tf in normalized]

def filter_market_cache(required_tfs: list, target_trend: str = "Bullish") -> tuple:
    """
    Filters MARKET_CACHE by matching ALL timeframes in required_tfs against target_trend.
    Returns (normalized_tfs, matched_coins_list).
    """
    norm_tfs = normalize_timeframe_tokens(required_tfs)
    if not norm_tfs:
        norm_tfs = ["1H"]
        
    target_trend_clean = target_trend.capitalize()
    matches = []
    
    for symbol, data in MARKET_CACHE.items():
        tfs = data.get("timeframes", {})
        matched = True
        for req in norm_tfs:
            if tfs.get(req, {}).get("trend") != target_trend_clean:
                matched = False
                break
        if matched:
            matches.append({
                "symbol": symbol,
                "price": data.get("price", 0.0),
                "details": {req: tfs.get(req, {}).get("detail", "") for req in norm_tfs}
            })
            
    # Sort matches alphabetically by symbol
    matches.sort(key=lambda x: x["symbol"])
    return norm_tfs, matches

def format_price(p: float) -> str:
    if p is None or p == 0.0: return "0.00"
    if p >= 1000: return f"{p:,.2f}"
    elif p >= 1: return f"{p:.4f}"
    elif p >= 0.0001: return f"{p:.6f}"
    else: return f"{p:.8f}"

def format_filter_result_card(norm_tfs: list, matches: list, target_trend: str = "Bullish") -> str:
    """Renders a sleek, institutional, non-warping Telegram result card."""
    tf_header = " + ".join(norm_tfs)
    trend_tag = "BULLISH (LONG)" if target_trend.lower() == "bullish" else "BEARISH (SHORT)"
    icon = "🟢" if target_trend.lower() == "bullish" else "🔴"
    
    total_scanned = len(MARKET_CACHE)
    total_matched = len(matches)
    
    if len(norm_tfs) == 5:
        header_icon = "🔥 "
    elif len(norm_tfs) == 4:
        header_icon = "⚡ "
    else:
        header_icon = ""

    msg_lines = [
        f"<b>{header_icon}TIMEFRAME FILTER | {tf_header}</b>",
        f"{trend_tag} • {total_matched}/{total_scanned} COINS MATCHED\n"
    ]
    
    if not matches:
        msg_lines.append(f"No coins currently meet all <b>{tf_header}</b> {target_trend.lower()} requirements.")
        msg_lines.append(f"<i>(Total tracked assets: {total_scanned})</i>")
        return "\n".join(msg_lines)
        
    # Cap displayed results to 20 to stay well within Telegram message limits
    display_limit = 20
    for coin in matches[:display_limit]:
        sym = coin["symbol"]
        pr = format_price(coin["price"])
        msg_lines.append(f"• <b>{sym}</b> ({pr})")
        
        detail_items = list(coin["details"].items())
        for idx, (tf, detail) in enumerate(detail_items):
            prefix = "└" if idx == len(detail_items) - 1 else "├"
            msg_lines.append(f"  {prefix} <b>{tf}:</b> {icon} {detail}")
            
    if total_matched > display_limit:
        msg_lines.append(f"\n<i>...and {total_matched - display_limit} more coins matched.</i>")
        
    return "\n".join(msg_lines)


def summarize_market_regime_snapshot() -> dict:
    """
    Aggregates the current in-memory MARKET_CACHE across all scanned pairs to explain
    market-wide conditions (HTF conflicts, extended RSI extremes, choppy ADX, BTC structure).
    """
    btc_trends = {"1H": "?", "4H": "?", "1D": "?"}
    total_coins = 0
    htf_split_count = 0
    aligned_3star_count = 0
    overbought_count = 0
    oversold_count = 0
    choppy_adx_count = 0

    for sym, data in list(MARKET_CACHE.items()):
        tfs = data.get("timeframes") or {}
        if not tfs:
            continue
        total_coins += 1
        t1h = str((tfs.get("1H") or {}).get("trend") or "Neutral")
        t4h = str((tfs.get("4H") or {}).get("trend") or "Neutral")
        t1d = str((tfs.get("1D") or {}).get("trend") or "Neutral")

        if "BTC" in sym.upper() and "USDT" in sym.upper():
            btc_trends = {"1H": t1h, "4H": t4h, "1D": t1d}

        htf_vals = [t for t in (t1h, t4h, t1d) if t in ("Bullish", "Bearish")]
        if t1h in ("Bullish", "Bearish") and t1h == t4h == t1d:
            aligned_3star_count += 1
        elif len(set(htf_vals)) > 1:
            htf_split_count += 1

        rsi_1h = float((tfs.get("1H") or {}).get("rsi") or 0.0)
        adx_1h = float((tfs.get("1H") or {}).get("adx") or 0.0)
        if rsi_1h > 62.0:
            overbought_count += 1
        elif 0.0 < rsi_1h < 38.0:
            oversold_count += 1
        if 0.0 < adx_1h < 20.0:
            choppy_adx_count += 1

    return {
        "btc_trends": btc_trends,
        "total_coins": total_coins,
        "htf_split_count": htf_split_count,
        "aligned_3star_count": aligned_3star_count,
        "overbought_count": overbought_count,
        "oversold_count": oversold_count,
        "choppy_adx_count": choppy_adx_count,
    }
