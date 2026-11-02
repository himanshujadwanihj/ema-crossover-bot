import json

with open("raw_backtest_retests.json", "r", encoding="utf-8") as f:
    trades = json.load(f)

# Let's check other factors that might actually move the needle:
# For example, does setup on EMA 26 perform differently than EMA 9?
# Does high ATR vs low ATR perform differently?
# Does trend alignment matter?

print(f"Total trades: {len(trades)}")
buy_trades = [t for t in trades if t["direction"] == "BUY"]
sell_trades = [t for t in trades if t["direction"] == "SELL"]

wr_buy = sum(1 for t in buy_trades if t["is_win"]) / len(buy_trades) * 100
wr_sell = sum(1 for t in sell_trades if t["is_win"]) / len(sell_trades) * 100
print(f"BUY Win Rate: {wr_buy:.1f}% ({len(buy_trades)} trades)")
print(f"SELL Win Rate: {wr_sell:.1f}% ({len(sell_trades)} trades)")

# What about Vol Ratio: high volume vs low volume
low_vol = [t for t in trades if t["vol_ratio"] < 1.0]
high_vol = [t for t in trades if t["vol_ratio"] >= 1.5]
wr_lvol = sum(1 for t in low_vol if t["is_win"]) / len(low_vol) * 100
wr_hvol = sum(1 for t in high_vol if t["is_win"]) / len(high_vol) * 100
print(f"Low Volume (< 1.0x SMA) Win Rate: {wr_lvol:.1f}% ({len(low_vol)} trades)")
print(f"High Volume (>= 1.5x SMA) Win Rate: {wr_hvol:.1f}% ({len(high_vol)} trades)")
