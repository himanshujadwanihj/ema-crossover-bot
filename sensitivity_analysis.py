import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("raw_backtest_retests.json", "r", encoding="utf-8") as f:
    trades = json.load(f)

print(f"Loaded {len(trades)} trades for detailed sensitivity analysis.\n")

def run_analysis(thresholds):
    base_wins = [t for t in trades if t["is_win"]]
    base_losses = [t for t in trades if t["outcome"] == "SL"]
    base_r = sum(t["r_mult"] for t in trades)
    base_wr = len(base_wins) / (len(base_wins) + len(base_losses)) * 100
    base_pf = sum(t["r_mult"] for t in base_wins) / abs(sum(t["r_mult"] for t in base_losses))

    print(f"{'Filter / Condition':<42} | {'Trades':<6} | {'WinRate':<7} | {'PF':<5} | {'Wins Missed':<11} | {'Losses Saved':<12} | {'Net R':<8} | {'Net PnL (Rs.2.5k/R)'}")
    print("-" * 125)
    print(f"{'BASELINE (Current Bot)':<42} | {len(trades):<6} | {base_wr:5.1f}%  | {base_pf:.2f} | {'0 (0%)':<11} | {'0 (0%)':<12} | {base_r:+7.1f}R | +Rs.{base_r * 2500:,.0f}")

    for name, f_func in thresholds.items():
        p_trades = [t for t in trades if f_func(t)]
        r_trades = [t for t in trades if not f_func(t)]

        p_wins = [t for t in p_trades if t["is_win"]]
        p_losses = [t for t in p_trades if t["outcome"] == "SL"]
        
        missed_wins = [t for t in r_trades if t["is_win"]]
        saved_losses = [t for t in r_trades if t["outcome"] == "SL"]

        p_r = sum(t["r_mult"] for t in p_trades)
        p_wr = len(p_wins) / (len(p_wins) + len(p_losses)) * 100 if (p_wins or p_losses) else 0
        p_pf = sum(t["r_mult"] for t in p_wins) / abs(sum(t["r_mult"] for t in p_losses)) if p_losses else 999

        pnl_inr = p_r * 2500.0

        pct_missed = (len(missed_wins) / len(base_wins)) * 100
        pct_saved = (len(saved_losses) / len(base_losses)) * 100

        print(f"{name:<42} | {len(p_trades):<6} | {p_wr:5.1f}%  | {p_pf:.2f} | {len(missed_wins)} ({pct_missed:.0f}%)    | {len(saved_losses)} ({pct_saved:.0f}%)     | {p_r:+7.1f}R | +Rs.{pnl_inr:,.0f}")

thresholds = {
    "Wick >= 15% (Gentle)": lambda t: (t["lower_wick_ratio"] >= 0.15 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.15),
    "Wick >= 20% (Mild)": lambda t: (t["lower_wick_ratio"] >= 0.20 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.20),
    "Wick >= 25% (Balanced)": lambda t: (t["lower_wick_ratio"] >= 0.25 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.25),
    "Wick >= 30% (Strict)": lambda t: (t["lower_wick_ratio"] >= 0.30 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.30),
    "Wick >= 35% (Ultra-Strict)": lambda t: (t["lower_wick_ratio"] >= 0.35 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.35),
    "Anti-Dump (No heavy body > 60%)": lambda t: not (t["is_red_body"] and t["body_ratio"] >= 0.60) if t["direction"] == "BUY" else not (t["is_green_body"] and t["body_ratio"] >= 0.60),
    "Anti-Dump (No heavy body > 50%)": lambda t: not (t["is_red_body"] and t["body_ratio"] >= 0.50) if t["direction"] == "BUY" else not (t["is_green_body"] and t["body_ratio"] >= 0.50),
    "Wick >= 25% + Anti-Dump (>60%)": lambda t: (t["lower_wick_ratio"] >= 0.25 and not (t["is_red_body"] and t["body_ratio"] >= 0.60)) if t["direction"] == "BUY" else (t["upper_wick_ratio"] >= 0.25 and not (t["is_green_body"] and t["body_ratio"] >= 0.60)),
    "Wick >= 25% + Vol <= 1.5x": lambda t: (t["lower_wick_ratio"] >= 0.25 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.25) and t["vol_ratio"] <= 1.5,
    "Wick >= 25% + Close Favorable": lambda t: (t["lower_wick_ratio"] >= 0.25 and t["close_pos_ratio"] >= 0.40) if t["direction"] == "BUY" else (t["upper_wick_ratio"] >= 0.25 and t["close_pos_ratio"] <= 0.60),
}

run_analysis(thresholds)
