with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/pullback_tracker.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = """                # 1. Deduplication per candle so each fresh pullback tap generates an alert
                dedup_ts = str(candle_time_utc)
                if is_already_sent(symbol, unique_key, sig_type, dedup_ts) or \
                   is_already_sent(symbol, unified_key, sig_type, dedup_ts):
                    continue

                # 2. Smart Level-Aware Filter: If price is identical to an alert in the last hour on this TF, pause spam
                if is_same_level_retest(symbol, tf_label, retest_price, sig_type, threshold_pct=0.015, max_hours=2.5):
                    logger.info(f"PAUSED duplicate retest for {symbol} {tf_label} {sig_type} @ {retest_price:.4f}: Price within 0.5% of recent alerted level.")
                    continue"""

replacement = """                # 1. Deduplication per candle so each fresh pullback tap generates an alert
                dedup_ts = str(candle_time_utc)
                if is_already_sent(symbol, unique_key, sig_type, dedup_ts) or \
                   is_already_sent(symbol, unified_key, sig_type, dedup_ts):
                    continue

                # 2. Strict Per-Coin Anti-Spam Cooldown (Strict 4-Hour Lock to prevent repeated notifications!)
                is_gold_asset = any(k in symbol.upper() for k in ["XAU", "XAUT"])
                gold_cooldown_hours = 4.0 if is_gold_asset else 3.0
                if is_coin_on_cooldown(symbol, tf_label, max_hours=gold_cooldown_hours) or \
                   (is_gold_asset and is_coin_on_cooldown(symbol, max_hours=3.0)):
                    logger.info(f"COOLDOWN: {symbol} {tf_label} alert suppressed - already notified in the last {gold_cooldown_hours}h.")
                    continue

                # 3. Smart Level-Aware Filter: If price is identical to an alert in the last 4 hours on this TF, pause spam
                if is_same_level_retest(symbol, tf_label, retest_price, sig_type, threshold_pct=0.015, max_hours=4.0):
                    logger.info(f"PAUSED duplicate retest for {symbol} {tf_label} {sig_type} @ {retest_price:.4f}: Price within 1.5% of recent alerted level.")
                    continue"""

target_mark = """                mark_sent(symbol, unique_key, sig_type, dedup_ts, entry=retest_price, sl=sl, tp=tp1, tp1=tp1, tp2=tp2, tp3=tp3)
                mark_sent(symbol, unified_key, sig_type, dedup_ts, entry=retest_price, sl=sl, tp=tp1, tp1=tp1, tp2=tp2, tp3=tp3)
                logger.info(f"Sent {ema_name} {action_name} Alert for {symbol} {tf_label} {trend}")"""

replacement_mark = """                mark_sent(symbol, unique_key, sig_type, dedup_ts, entry=retest_price, sl=sl, tp=tp1, tp1=tp1, tp2=tp2, tp3=tp3)
                mark_sent(symbol, unified_key, sig_type, dedup_ts, entry=retest_price, sl=sl, tp=tp1, tp1=tp1, tp2=tp2, tp3=tp3)
                set_coin_cooldown(symbol, tf_label)
                if any(k in symbol.upper() for k in ["XAU", "XAUT"]):
                    set_coin_cooldown(symbol)  # Global cooldown for Gold
                logger.info(f"Sent {ema_name} {action_name} Alert for {symbol} {tf_label} {trend}")"""

if target in content and target_mark in content:
    content = content.replace(target, replacement, 1)
    content = content.replace(target_mark, replacement_mark, 1)
    with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/pullback_tracker.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: Updated pullback_tracker.py with strict anti-spam cooldown")
else:
    print("ERROR: Target not found in pullback_tracker.py")
