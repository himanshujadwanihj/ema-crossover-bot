with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/indicators.py', 'r', encoding='utf-8') as f:
    content = f.read()

target_buy = """        if len(all_bsl) >= 1:
            tp1 = max(all_bsl[0], min_tp1)  # Best Liquidity Pool Target at or above 1.5x R:R
            lbl1 = "Liquidity Pool Target 1"
        else:
            tp1 = entry + (1.6 * risk)
            lbl1 = "1.6 R:R Expansion Target 1\""""

repl_buy = """        if len(all_bsl) >= 1:
            raw_tp1 = max(all_bsl[0], min_tp1)
            tp1 = max(raw_tp1 * 0.998, min_tp1)  # 0.20% Front-running buffer
            lbl1 = "Liquidity Pool Target 1 (0.2% Front-Run)"
        else:
            tp1 = entry + (1.6 * risk)
            lbl1 = "1.6 R:R Expansion Target 1\""""

target_sell = """        if len(all_ssl) >= 1:
            tp1 = min(all_ssl[0], max_tp1)  # Best Liquidity Pool Target at or below 1.5x R:R
            lbl1 = "Liquidity Pool Target 1"
        else:
            tp1 = entry - (1.6 * risk)
            lbl1 = "1.6 R:R Expansion Target 1\""""

repl_sell = """        if len(all_ssl) >= 1:
            raw_tp1 = min(all_ssl[0], max_tp1)
            tp1 = min(raw_tp1 * 1.002, max_tp1)  # 0.20% Front-running buffer
            lbl1 = "Liquidity Pool Target 1 (0.2% Front-Run)"
        else:
            tp1 = entry - (1.6 * risk)
            lbl1 = "1.6 R:R Expansion Target 1\""""

if target_buy in content and target_sell in content:
    content = content.replace(target_buy, repl_buy, 1)
    content = content.replace(target_sell, repl_sell, 1)
    with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/indicators.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: Front-running buffer applied to indicators.py")
else:
    print("ERROR: Target patterns not matched")
