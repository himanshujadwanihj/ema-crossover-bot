import json

with open("raw_backtest_retests.json", "r", encoding="utf-8") as f:
    trades = json.load(f)

def calc_drawdown_and_streaks(t_list):
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    cur_loss_streak = 0
    max_loss_streak = 0

    for t in t_list:
        r = t["r_mult"]
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

        if t["outcome"] == "SL":
            cur_loss_streak += 1
            if cur_loss_streak > max_loss_streak:
                max_loss_streak = cur_loss_streak
        elif t["is_win"]:
            cur_loss_streak = 0

    return max_dd, max_loss_streak

def f_wick_25(t):
    return (t["lower_wick_ratio"] >= 0.25 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.25)

def f_wick_35(t):
    return (t["lower_wick_ratio"] >= 0.35 if t["direction"] == "BUY" else t["upper_wick_ratio"] >= 0.35)

def f_inst(t):
    return (t["lower_wick_ratio"] >= 0.25 and t["close_pos_ratio"] >= 0.40) if t["direction"] == "BUY" else (t["upper_wick_ratio"] >= 0.25 and t["close_pos_ratio"] <= 0.60)

dd_base, streak_base = calc_drawdown_and_streaks(trades)
dd_25, streak_25 = calc_drawdown_and_streaks([t for t in trades if f_wick_25(t)])
dd_35, streak_35 = calc_drawdown_and_streaks([t for t in trades if f_wick_35(t)])
dd_inst, streak_inst = calc_drawdown_and_streaks([t for t in trades if f_inst(t)])

print(f"BASELINE:             Max Drawdown = -{dd_base:.1f}R (-Rs.{dd_base*2500:,.0f}) | Max Loss Streak = {streak_base} consecutive losses")
print(f"WICK >= 25%:          Max Drawdown = -{dd_25:.1f}R (-Rs.{dd_25*2500:,.0f}) | Max Loss Streak = {streak_25} consecutive losses")
print(f"WICK >= 35%:          Max Drawdown = -{dd_35:.1f}R (-Rs.{dd_35*2500:,.0f}) | Max Loss Streak = {streak_35} consecutive losses")
print(f"INSTITUTIONAL CLOSE:  Max Drawdown = -{dd_inst:.1f}R (-Rs.{dd_inst*2500:,.0f}) | Max Loss Streak = {streak_inst} consecutive losses")
