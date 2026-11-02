with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/pullback_tracker.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    if "if is_same_level_retest(symbol, tf_label, retest_price, sig_type" in line:
        # Insert anti-spam cooldown check right before is_same_level_retest
        cooldown_block = [
            "                # 2. Strict Per-Coin Anti-Spam Cooldown (Strict 4-Hour Lock to prevent repeated notifications!)\n",
            "                is_gold_asset = any(k in symbol.upper() for k in [\"XAU\", \"XAUT\"])\n",
            "                gold_cooldown_hours = 4.0 if is_gold_asset else 3.0\n",
            "                if is_coin_on_cooldown(symbol, tf_label, max_hours=gold_cooldown_hours) or \\\n",
            "                   (is_gold_asset and is_coin_on_cooldown(symbol, max_hours=3.0)):\n",
            "                    logger.info(f\"COOLDOWN: {symbol} {tf_label} alert suppressed - already notified in the last {gold_cooldown_hours}h.\")\n",
            "                    continue\n",
            "\n",
            "                # 3. Smart Level-Aware Filter: If price is identical to an alert in the last 4 hours on this TF, pause spam\n",
            "                if is_same_level_retest(symbol, tf_label, retest_price, sig_type, threshold_pct=0.015, max_hours=4.0):\n",
            "                    logger.info(f\"PAUSED duplicate retest for {symbol} {tf_label} {sig_type} @ {retest_price:.4f}: Price within 1.5% of recent alerted level.\")\n",
            "                    continue\n"
        ]
        new_lines.extend(cooldown_block)
        # Skip the original is_same_level_retest and its logger/continue lines (3 lines total)
        i += 3
        continue
    elif "logger.info(f\"Sent {ema_name} {action_name} Alert for {symbol} {tf_label} {trend}\")" in line:
        new_lines.append(line)
        new_lines.append("                set_coin_cooldown(symbol, tf_label)\n")
        new_lines.append("                if any(k in symbol.upper() for k in [\"XAU\", \"XAUT\"]):\n")
        new_lines.append("                    set_coin_cooldown(symbol)  # Global cooldown for Gold\n")
        i += 1
        continue
    else:
        new_lines.append(line)
        i += 1

with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/pullback_tracker.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("SUCCESS: Successfully injected anti-spam and cooldown to pullback_tracker.py")
