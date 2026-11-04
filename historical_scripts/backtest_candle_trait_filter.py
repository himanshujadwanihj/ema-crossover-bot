import requests
import pandas as pd
import numpy as np

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT",
    "NEARUSDT", "DOTUSDT", "PEPEUSDT", "APTUSDT", "TAOUSDT",
    "LTCUSDT", "FETUSDT", "ENAUSDT", "AAVEUSDT", "WIFUSDT",
    "RENDERUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "TIAUSDT"
]

session = requests.Session()

def fetch_klines(symbol, interval="1h", limit=1000):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        r = session.get(url, timeout=12)
        if r.status_code == 200:
            df = pd.DataFrame(r.json(), columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "q_vol", "trades", "tb_vol", "tb_q_vol", "ignore"
            ])
            df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df.set_index("time", inplace=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
    return None

def compute_indicators(df):
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_26"] = df["close"].ewm(span=26, adjust=False).mean()
    df["vol_sma"] = df["volume"].rolling(20).mean()
    
    # ATR for stop loss
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df

all_raw_trades = []

print("Fetching historical data and running baseline EMA pullback scanner...")

for sym in SYMBOLS:
    df = fetch_klines(sym, "1h", limit=1000)
    if df is None or len(df) < 100:
        continue
    df = compute_indicators(df)

    # Scan through candles
    touch_threshold = 0.0075 # 0.75% for 1H
    
    last_trade_bar = -999

    for i in range(30, len(df) - 30):
        # Spacing to prevent multiple alerts on same ongoing move
        if i - last_trade_bar < 4:
            continue

        row = df.iloc[i]
        prev_row = df.iloc[i-1]
        
        ema9 = row["ema_9"]
        ema26 = row["ema_26"]
        trend = "BULLISH" if ema9 > ema26 else "BEARISH"
        candle_range = max(row["high"] - row["low"], 1e-8)

        tapped_26 = (row["low"] <= ema26 <= row["high"]) and (abs(row["close"] - ema26) / ema26 <= touch_threshold)
        tapped_9 = (row["low"] <= ema9 <= row["high"]) and (abs(row["close"] - ema9) / ema9 <= touch_threshold)

        if not (tapped_26 or tapped_9):
            continue

        target_ema = ema26 if tapped_26 else ema9
        prev_close = prev_row["close"]
        cur_close = row["close"]

        # Current Baseline pullback logic in bot:
        is_support = False
        is_resistance = False

        if trend == "BULLISH":
            if prev_close >= target_ema * 0.998:
                lower_wick_ratio = (cur_close - row["low"]) / candle_range
                if cur_close >= target_ema * 0.998 or lower_wick_ratio >= 0.25:
                    is_support = True
        elif trend == "BEARISH":
            if prev_close <= target_ema * 1.002:
                upper_wick_ratio = (row["high"] - cur_close) / candle_range
                if cur_close <= target_ema * 1.002 or upper_wick_ratio >= 0.25:
                    is_resistance = True

        if not (is_support or is_resistance):
            continue

        direction = "BUY" if is_support else "SELL"
        entry = cur_close
        atr = row["atr"] if not np.isnan(row["atr"]) else entry * 0.015

        # Stop loss and take profit
        if direction == "BUY":
            # SL = below low + 1.5 ATR buffer
            sl = min(row["low"] * 0.998, entry - 1.5 * atr)
            risk_dist = entry - sl
            tp1 = entry + 1.2 * risk_dist
            tp2 = entry + 2.0 * risk_dist
            tp3 = entry + 3.0 * risk_dist
        else:
            sl = max(row["high"] * 1.002, entry + 1.5 * atr)
            risk_dist = sl - entry
            tp1 = entry - 1.2 * risk_dist
            tp2 = entry - 2.0 * risk_dist
            tp3 = entry - 3.0 * risk_dist

        if risk_dist <= 0 or sl <= 0:
            continue

        # Evaluate outcome over next 24 candles
        window = df.iloc[i+1 : i+25]
        tp1_hit, tp2_hit, tp3_hit, sl_hit = False, False, False, False

        for w_idx in range(len(window)):
            w_row = window.iloc[w_idx]
            if direction == "BUY":
                if w_row["low"] <= sl:
                    sl_hit = True
                    break
                if w_row["high"] >= tp1: tp1_hit = True
                if w_row["high"] >= tp2: tp2_hit = True
                if w_row["high"] >= tp3: tp3_hit = True
            else:
                if w_row["high"] >= sl:
                    sl_hit = True
                    break
                if w_row["low"] <= tp1: tp1_hit = True
                if w_row["low"] <= tp2: tp2_hit = True
                if w_row["low"] <= tp3: tp3_hit = True

        if sl_hit and not tp1_hit:
            outcome = "SL"
            r_mult = -1.0
        elif tp3_hit:
            outcome = "TP3"
            r_mult = 2.4 # Blended R-multiples across TP scale
        elif tp2_hit:
            outcome = "TP2"
            r_mult = 1.6
        elif tp1_hit:
            outcome = "TP1"
            r_mult = 0.5
        else:
            outcome = "RUNNING"
            r_mult = 0.0

        is_win = outcome.startswith("TP")

        # Candle traits measurements:
        lower_wick_ratio = (min(row["open"], row["close"]) - row["low"]) / candle_range if row["close"] >= row["open"] else (row["close"] - row["low"]) / candle_range
        upper_wick_ratio = (row["high"] - max(row["open"], row["close"])) / candle_range if row["close"] <= row["open"] else (row["high"] - row["close"]) / candle_range
        body_ratio = abs(row["close"] - row["open"]) / candle_range
        close_pos_ratio = (row["close"] - row["low"]) / candle_range # 1.0 = closed at high, 0.0 = closed at low
        is_red_body = row["close"] < row["open"]
        is_green_body = row["close"] > row["open"]
        vol_ratio = row["volume"] / row["vol_sma"] if row["vol_sma"] > 0 else 1.0

        trade_record = {
            "symbol": sym,
            "time": df.index[i],
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "outcome": outcome,
            "is_win": is_win,
            "r_mult": r_mult,
            "candle_range": candle_range,
            "lower_wick_ratio": lower_wick_ratio,
            "upper_wick_ratio": upper_wick_ratio,
            "body_ratio": body_ratio,
            "close_pos_ratio": close_pos_ratio,
            "is_red_body": is_red_body,
            "is_green_body": is_green_body,
            "vol_ratio": vol_ratio
        }

        all_raw_trades.append(trade_record)
        last_trade_bar = i

print(f"Total Raw EMA Retest Trades Found across 25 coins: {len(all_raw_trades)}")

# Save to disk
import json
def clean_val(v):
    if isinstance(v, pd.Timestamp):
        return str(v)
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return float(v)
    return v

with open("raw_backtest_retests.json", "w", encoding="utf-8") as f:
    json.dump([
        {k: clean_val(v) for k, v in t.items()}
        for t in all_raw_trades
    ], f, indent=2)

# Evaluation function
def evaluate_strategy(name, filter_func, trades):
    passed_trades = [t for t in trades if filter_func(t)]
    rejected_trades = [t for t in trades if not filter_func(t)]

    # Metrics
    raw_wins = [t for t in trades if t["is_win"]]
    raw_losses = [t for t in trades if t["outcome"] == "SL"]
    
    passed_wins = [t for t in passed_trades if t["is_win"]]
    passed_losses = [t for t in passed_trades if t["outcome"] == "SL"]

    missed_wins = [t for t in rejected_trades if t["is_win"]]
    avoided_losses = [t for t in rejected_trades if t["outcome"] == "SL"]

    # R-multiples & PnL
    raw_total_r = sum(t["r_mult"] for t in trades)
    passed_total_r = sum(t["r_mult"] for t in passed_trades)
    
    # 2.5% Account Risk Simulation (INR 100,000)
    def sim_inr(t_list, start_cap=100000.0):
        cap = start_cap
        for t in t_list:
            if t["outcome"] == "SL":
                cap -= cap * 0.025 # 2.5% loss
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
    print(f"📊 STRATEGY: {name}")
    print("=" * 75)
    print(f"• Total Trades Taken:      {len(passed_trades)} (vs {len(trades)} in Baseline)")
    print(f"• Win Rate:                {wr_passed:.1f}% (Baseline was {wr_raw:.1f}%) -> Diff: {wr_passed - wr_raw:+.1f}%")
    print(f"• Total Wins Taken:        {len(passed_wins)}")
    print(f"• Total Losses Taken:      {len(passed_losses)}")
    print(f"• ❌ PROFITABLE TRADES MISSED:  {len(missed_wins)} (Lost opportunities)")
    print(f"• ✅ LOSING TRADES AVOIDED:     {len(avoided_losses)} (Stop-Losses saved!)")
    print(f"• Net Saved Ratio:         Saved {len(avoided_losses)} losses by giving up {len(missed_wins)} wins")
    print(f"• Total R-Multiple:        {passed_total_r:+.1f}R (Baseline was {raw_total_r:+.1f}R)")
    print(f"• Profit Factor:           {profit_factor_passed:.2f} (Baseline was {profit_factor_raw:.2f})")
    print(f"• Net PnL (Rs.100k Account): +Rs.{passed_pnl_inr:,.2f} (Baseline was +Rs.{raw_pnl_inr:,.2f})")
    print(f"• PnL Difference:          {passed_pnl_inr - raw_pnl_inr:+,.2f} INR")
    print()

# Filter Definitions:
# 1. Baseline: All raw trades
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

# 4. Filter 3: Anti-Dump Candle (No large opposing Marubozu > 50%)
def f_anti_dump(t):
    if t["direction"] == "BUY":
        # Cannot be heavy red body > 50%
        if t["is_red_body"] and t["body_ratio"] >= 0.50:
            return False
        return True
    else:
        # Cannot be heavy green body > 50%
        if t["is_green_body"] and t["body_ratio"] >= 0.50:
            return False
        return True

evaluate_strategy("FILTER 3: Anti-Dump Candle (Block heavy body > 50%)", f_anti_dump, all_raw_trades)

# 5. Filter 4: Combined Rejection (Wick >= 30% + Close in Favorable Half)
def f_combined_rejection(t):
    if t["direction"] == "BUY":
        return t["lower_wick_ratio"] >= 0.30 and t["close_pos_ratio"] >= 0.45 and not (t["is_red_body"] and t["body_ratio"] >= 0.55)
    else:
        return t["upper_wick_ratio"] >= 0.30 and t["close_pos_ratio"] <= 0.55 and not (t["is_green_body"] and t["body_ratio"] >= 0.55)

evaluate_strategy("FILTER 4: Institutional Rejection (Wick >= 30% + Close in Favorable Half + Anti-Dump)", f_combined_rejection, all_raw_trades)

# 6. Filter 5: Institutional Rejection + Volume Exhaustion (Vol <= 1.5x SMA)
def f_institutional_vol(t):
    if not f_combined_rejection(t):
        return False
    return t["vol_ratio"] <= 1.6

evaluate_strategy("FILTER 5: Full Institutional (Rejection Wick + Anti-Dump + Volume Exhaustion)", f_institutional_vol, all_raw_trades)
