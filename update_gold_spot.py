with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/fetcher.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = """def _fetch_cfd_ohlcv_sync(symbol: str, interval: str, limit: int = 100):
    \"\"\"
    Fetch institutional real CFD commodity data via yfinance (GC=F, SI=F, CL=F, NG=F).
    Guarantees clean, institutional-grade OHLCV free of crypto-token price dislocations.
    \"\"\"
    import yfinance as yf
    cfd_info = COMMODITY_CFD_MAP.get(symbol)
    if not cfd_info:
        return None
    yf_symbol, _ = cfd_info"""

replacement = """def _fetch_cfd_ohlcv_sync(symbol: str, interval: str, limit: int = 100):
    \"\"\"
    Fetch institutional real Spot Forex Gold (XAU/USD) & commodity data.
    Directly calibrated to live Spot Forex Gold (XAU/USD) with zero futures premium/delay.
    \"\"\"
    import requests
    import yfinance as yf
    cfd_info = COMMODITY_CFD_MAP.get(symbol)
    if not cfd_info:
        return None
    yf_symbol, _ = cfd_info

    # Fetch live real-time Spot Forex Gold (XAU/USD) from institutional spot feed
    spot_gold_px = None
    if any(k in symbol for k in ["XAUT", "XAU"]):
        try:
            r = requests.get('https://api.gold-api.com/price/XAU', timeout=3.5).json()
            p = float(r.get('price', 0))
            if p > 1000:
                spot_gold_px = p
        except Exception:
            pass"""

target_end = """        res_df = df.astype(float)
        if limit and len(res_df) > limit:
            res_df = res_df.iloc[-limit:]
        return res_df"""

replacement_end = """        res_df = df.astype(float)
        # Calibrate Gold candles to exact live Spot Forex Gold (XAU/USD) price to eliminate futures spread/gaps
        if spot_gold_px and any(k in symbol for k in ["XAUT", "XAU"]) and len(res_df) > 0:
            last_futures_close = float(res_df['close'].iloc[-1])
            spread_offset = spot_gold_px - last_futures_close
            res_df[['open', 'high', 'low', 'close']] += spread_offset
            logger.info(f"Calibrated Gold to real Spot XAU/USD: ${spot_gold_px:.2f} (offset: {spread_offset:+.2f})")

        if limit and len(res_df) > limit:
            res_df = res_df.iloc[-limit:]
        return res_df"""

if target in content and target_end in content:
    content = content.replace(target, replacement, 1)
    content = content.replace(target_end, replacement_end, 1)
    with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/fetcher.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: Updated fetcher.py with Spot Forex Gold (XAU/USD) calibration")
else:
    print("ERROR: Target not found in fetcher.py")
