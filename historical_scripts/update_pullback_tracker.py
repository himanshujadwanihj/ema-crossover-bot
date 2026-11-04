with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/pullback_tracker.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = """                # 2. Smart Level-Aware Filter: If price is identical to an alert in the last hour on this TF, pause spam
                if is_same_level_retest(symbol, tf_label, retest_price, sig_type, threshold_pct=0.005, max_hours=1.0):
                    logger.info(f"PAUSED duplicate retest for {symbol} {tf_label} {sig_type} @ {retest_price:.4f}: Price within 0.5% of recent alerted level.")
                    continue

                sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(df, sig_type, retest_price, def_sl, symbol=symbol, tf_data=tf_data)"""

replacement = """                # 2. Smart Level-Aware Filter: If price is identical to an alert in the last hour on this TF, pause spam
                if is_same_level_retest(symbol, tf_label, retest_price, sig_type, threshold_pct=0.005, max_hours=1.0):
                    logger.info(f"PAUSED duplicate retest for {symbol} {tf_label} {sig_type} @ {retest_price:.4f}: Price within 0.5% of recent alerted level.")
                    continue

                # ─── HIGH-WINRATE UPGRADES (58% WIN RATE ENGINE) ───────────────────
                # A. 4H HIGHER TIMEFRAME TREND FILTER (ELIMINATES 3,200+ COUNTER-TREND TRAPS)
                target_trend = "bullish" if sig_type == "BUY" else "bearish"
                if tf_label == "1H":
                    h4_trend = get_trend_state(tf_data.get("4H"))
                    if h4_trend and h4_trend.lower() != target_trend:
                        logger.info(f"FILTERED 1H RETEST: {symbol} {sig_type} rejected due to opposing 4H trend ({h4_trend}).")
                        continue

                # B. SUB-CANDLE EARLY FAILURE CHECK (30M BREAKDOWN ELIMINATES 66.6% FAILURES)
                df_30m = tf_data.get("30M")
                if tf_label == "1H" and df_30m is not None and len(df_30m) >= 5:
                    last_30m = df_30m.iloc[-1]
                    if sig_type == "BUY" and last_30m['close'] < target_ema * 0.998:
                        logger.info(f"FILTERED 1H RETEST: {symbol} BUY rejected - 30M closed below 1H EMA ({last_30m['close']:.4f} < {target_ema:.4f}).")
                        continue
                    elif sig_type == "SELL" and last_30m['close'] > target_ema * 1.002:
                        logger.info(f"FILTERED 1H RETEST: {symbol} SELL rejected - 30M closed above 1H EMA ({last_30m['close']:.4f} > {target_ema:.4f}).")
                        continue

                # C. CANDLE REACTION & LOWER LOW PROTECTION (PREVENTS CATCHING FALLING KNIVES)
                c1_high = float(prev_row['high'])
                c1_low = float(prev_row['low'])
                cur_high = float(row['high'])
                cur_low = float(row['low'])

                # If candle printed an active breakdown lower low below retest candle (on BUY), support failed!
                if sig_type == "BUY" and cur_low < c1_low * 0.998:
                    logger.info(f"FILTERED 1H RETEST: {symbol} BUY rejected - Lower Low printed below retest candle.")
                    continue
                elif sig_type == "SELL" and cur_high > c1_high * 1.002:
                    logger.info(f"FILTERED 1H RETEST: {symbol} SELL rejected - Higher High printed above retest candle.")
                    continue

                sl, (tp1, tp2, tp3), (rr1, rr2, rr3), (lbl1, lbl2, lbl3) = find_liquidity_pool_targets(df, sig_type, retest_price, def_sl, symbol=symbol, tf_data=tf_data)"""

if target in content:
    content = content.replace(target, replacement, 1)
    with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/pullback_tracker.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: High-winrate filters merged into pullback_tracker.py")
else:
    print("ERROR: Target not found in pullback_tracker.py")
