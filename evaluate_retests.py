import json
import sys

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

with open("raw_backtest_retests.json", "r", encoding="utf-8") as f:
    all_raw_trades = json.load(f)

print(f"Loaded {len(all_raw_trades)} historical 1H EMA retest trades.")

def evaluate_strategy(name, filter_func, trades):
    passed_trades = [t for t in trades if filter_func(t)]
    rejected_trades = [t for t in trades if not filter_func(t)]

    raw_wins = [t for t in trades if t["is_win"]]
    raw_losses = [t for t in trades if t["outcome"] == "SL"]
    
    passed_wins = [t for t in passed_trades if t["is_win"]]
    passed_losses = [t for t in passed_trades if t["outcome"] == "SL"]

    missed_wins = [t for t in rejected_trades if t["is_win"]]
    avoided_losses = [t for t in rejected_trades if t["outcome"] == "SL"]

    raw_total_r = sum(t["r_mult"] for t in trades)
    passed_total_r = sum(t["r_mult"] for t in passed_trades)

    def sim_inr(t_list, start_cap=100000.0):
        cap = start_cap
        for t in t_list:
            if t["outcome"] == "SL":
                cap -= cap * 0.025
            elif t["outcome"] == "TP1":
                cap += cap * 0.025 * 0.5
            elif t["outcome"] == "TP2":
                cap += cap * 0.025 * 1.6
            elif t["outcome"] == "TP3":
                cap += cap * 0.025 * 2.4
        return cap - start_cap

    raw_pnl_inr = sim_inr(trades)
    passed_pnl_inr = sim_inr(passed_trades)

    wr_raw = (len(raw_wins) / (len(raw_wins) + len(raw_losses))) * 100 if (raw_wins or raw_losses) else 0
    wr_passed = (len(passed_wins) / (len(passed_wins) + len(passed_losses))) * 100 if (passed_wins or passed_losses) else 0

    profit_factor_raw = sum(t["r_mult"] for t in raw_wins) / abs(sum(t["r_mult"] for t in raw_losses)) if raw_losses else 999
    profit_factor_passed = sum(t["r_mult"] for t in passed_wins) / abs(sum(t["r_mult"] for t in passed_losses)) if passed_losses else 999

    print("=" * 75)
    print(f"STRATEGY: {name}")
    print("=" * 75)
    print(f"• Total Trades Taken:           {len(passed_trades)} (out of {len(trades)})")
    print(f"• Win Rate:                     {wr_passed:.2f}% (Baseline: {wr_raw:.2f}%) -> Boost: {wr_passed - wr_raw:+.2f}%")
    print(f"• Total Wins Taken:             {len(passed_wins)}")
    print(f"• Total Losses Taken:           {len(passed_losses)}")
    print(f"• [PROFITABLE TRADES MISSED]:   {len(missed_wins)} (Trades skipped that hit TP)")
    print(f"• [LOSING TRADES AVOIDED]:      {len(avoided_losses)} (SL hits saved!)")
    
    # Net trade trade-off ratio
    avoided_per_missed = (len(avoided_losses) / len(missed_wins)) if len(missed_wins) > 0 else 999
    print(f"• Efficiency Ratio:             Saved {avoided_per_missed:.2f} Stop-Losses for every 1 win missed")
    print(f"• Total Net R-Multiples:        {passed_total_r:+.2f}R (Baseline: {raw_total_r:+.2f}R)")
    print(f"• Profit Factor:                {profit_factor_passed:.2f} (Baseline: {profit_factor_raw:.2f})")
    print(f"• Net PnL (Rs.100k Account):    +Rs.{passed_pnl_inr:,.2f} (Baseline: +Rs.{raw_pnl_inr:,.2f})")
    print(f"• PnL Difference:               {passed_pnl_inr - raw_pnl_inr:+,.2f} INR")
    print()

# 1. Baseline
evaluate_strategy("BASELINE (CURRENT UNFILTERED BOT)", lambda t: True, all_raw_trades)

# 2. Filter 1: Rejection Wick >= 30%
def f_wick_30(t):
    if t["direction"] == "BUY":
        return t["lower_wick_ratio"] >= 0.30
    else:
        return t["upper_wick_ratio"] >= 0.30
evaluate_strategy("FILTER 1: Rejection Wick >= 30%", f_wick_30, all_raw_trades)

# 3. Filter 2: Rejection Wick >= 35%
def f_wick_35(t):
    if t["direction"] == "BUY":
        return t["lower_wick_ratio"] >= 0.35
    else:
        return t["upper_wick_ratio"] >= 0.35
evaluate_strategy("FILTER 2: Rejection Wick >= 35%", f_wick_35, all_raw_trades)

# 4. Filter 3: Anti-Dump Candle (Block heavy body > 50%)
def f_anti_dump(t):
    if t["direction"] == "BUY":
        if t["is_red_body"] and t["body_ratio"] >= 0.50:
            return False
        return True
    else:
        if t["is_green_body"] and t["body_ratio"] >= 0.50:
            return False
        return True
evaluate_strategy("FILTER 3: Anti-Dump Candle (Block heavy body > 50%)", f_anti_dump, all_raw_trades)

# 5. Filter 4: Rejection Wick >= 30% + Anti-Dump
def f_wick_plus_anti_dump(t):
    return f_wick_30(t) and f_anti_dump(t)
evaluate_strategy("FILTER 4: Rejection Wick >= 30% + Anti-Dump Candle", f_wick_plus_anti_dump, all_raw_trades)

# 6. Filter 5: Institutional Rejection (Wick >= 30% + Close in Favorable Half + Anti-Dump)
def f_combined_rejection(t):
    if t["direction"] == "BUY":
        return t["lower_wick_ratio"] >= 0.30 and t["close_pos_ratio"] >= 0.45 and not (t["is_red_body"] and t["body_ratio"] >= 0.50)
    else:
        return t["upper_wick_ratio"] >= 0.30 and t["close_pos_ratio"] <= 0.55 and not (t["is_green_body"] and t["body_ratio"] >= 0.50)
evaluate_strategy("FILTER 5: Institutional Rejection (Wick >= 30% + Close in Favorable Half + Anti-Dump)", f_combined_rejection, all_raw_trades)

# 7. Filter 6: Institutional Rejection + Volume Exhaustion (Vol <= 1.5x SMA)
def f_institutional_vol(t):
    if not f_combined_rejection(t):
        return False
    return t["vol_ratio"] <= 1.5
evaluate_strategy("FILTER 6: Full Institutional (Rejection + Anti-Dump + Volume Exhaustion <= 1.5x)", f_institutional_vol, all_raw_trades)
