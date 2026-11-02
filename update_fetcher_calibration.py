with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/fetcher.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = """            df.index.name = "timestamp"
            res_df = df.astype(float)
            return res_df.iloc[-limit:] if (limit and len(res_df) > limit) else res_df"""

replacement = """            df.index.name = "timestamp"
            res_df = df.astype(float)
            
            # Calibrate to real-time Spot Forex Gold (XAU/USD) to eliminate futures spread/gaps
            try:
                import requests
                r_spot = requests.get('https://api.gold-api.com/price/XAU', timeout=3.5).json()
                spot_px = float(r_spot.get('price', 0))
                if spot_px > 1000 and len(res_df) > 0:
                    last_c = float(res_df['close'].iloc[-1])
                    spread_offset = spot_px - last_c
                    res_df[['open', 'high', 'low', 'close']] += spread_offset
                    logger.info(f"Calibrated Gold to real Spot XAU/USD: ${spot_px:.2f} (offset: {spread_offset:+.2f})")
            except Exception as e_spot:
                logger.warning(f"Spot Gold calibration error: {e_spot}")

            return res_df.iloc[-limit:] if (limit and len(res_df) > limit) else res_df"""

if target in content:
    content = content.replace(target, replacement, 1)
    with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/fetcher.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: Updated fetcher.py with Spot Forex Gold calibration")
else:
    print("ERROR: Target not found in fetcher.py")
