import json
import os

def analyze_breakdown(trades_file):
    with open(trades_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    items = data.get('trades') or data if isinstance(data, dict) else data

    # Analyze by timeframe
    tf_counts = {}
    for item in items:
        tf = str(item.get('tf', 'UNKNOWN')).upper()
        out = str(item.get('norm_outcome') or item.get('outcome') or '').upper()
        if tf not in tf_counts:
            tf_counts[tf] = {'total': 0, 'wins': 0, 'losses': 0}
        
        is_win = ('TP1' in out or 'TP2' in out or 'TP3' in out or 'FULL WIN' in out)
        is_loss = ('SL' in out)

        if is_win or is_loss:
            tf_counts[tf]['total'] += 1
            if is_win: tf_counts[tf]['wins'] += 1
            else: tf_counts[tf]['losses'] += 1

    print("--- TIMEFRAME PERFORMANCE BREAKDOWN ---")
    for tf, d in sorted(tf_counts.items()):
        wr = (d['wins'] / d['total'] * 100.0) if d['total'] > 0 else 0
        print(f"Timeframe {tf:5s}: {d['total']:3d} trades | {d['wins']:3d} wins | {d['losses']:3d} losses | Win Rate: {wr:.1f}%")

def simulate_risk_levels(trades_file, tfs=None):
    with open(trades_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    items = data.get('trades') or data if isinstance(data, dict) else data

    if tfs:
        items = [x for x in items if str(x.get('tf', '')).upper() in tfs]

    maker_fee_rate = 0.0002
    taker_fee_rate = 0.0005
    leverage = 10

    print(f"\n--- TESTING RISK % LEVELS (Trades: {len(items)}, TFs: {tfs or 'ALL'}) ---")
    for risk_pct_level in [0.005, 0.010, 0.015, 0.020, 0.025]:
        cap = 100000.0
        max_cap = cap
        max_dd_pct = 0.0
        wins = 0
        losses = 0

        for item in items:
            entry = float(item.get('entry') or 0)
            sl = float(item.get('sl') or 0)
            tp1 = float(item.get('tp1') or 0)
            tp2 = float(item.get('tp2') or 0)
            tp3 = float(item.get('tp3') or 0)
            direction = str(item.get('direction', 'BUY')).upper()
            out = str(item.get('norm_outcome') or item.get('outcome') or '').upper()

            if entry <= 0 or sl <= 0 or entry == sl:
                continue

            sl_dist = abs(entry - sl) / entry
            if sl_dist <= 0: sl_dist = 0.015

            target_risk_inr = cap * risk_pct_level
            calc_notional = target_risk_inr / sl_dist
            calc_margin = calc_notional / leverage
            margin_inr = max(500.0, min(calc_margin, cap * 0.20))
            notional_inr = margin_inr * leverage

            entry_fee = notional_inr * maker_fee_rate

            if 'TP3' in out or 'FULL WIN' in out:
                r1 = ((tp1 - entry) / entry) if direction == 'BUY' else ((entry - tp1) / entry)
                f1 = (notional_inr * 0.40) * taker_fee_rate
                p1 = (notional_inr * 0.40) * r1 - f1

                r2 = ((tp2 - entry) / entry) if direction == 'BUY' else ((entry - tp2) / entry)
                f2 = (notional_inr * 0.30) * taker_fee_rate
                p2 = (notional_inr * 0.30) * r2 - f2

                r3 = ((tp3 - entry) / entry) if direction == 'BUY' else ((entry - tp3) / entry)
                f3 = (notional_inr * 0.30) * taker_fee_rate
                p3 = (notional_inr * 0.30) * r3 - f3

                net = p1 + p2 + p3 - entry_fee
                cap += net
                wins += 1
            elif 'TP2' in out:
                r1 = ((tp1 - entry) / entry) if direction == 'BUY' else ((entry - tp1) / entry)
                f1 = (notional_inr * 0.40) * taker_fee_rate
                p1 = (notional_inr * 0.40) * r1 - f1

                r2 = ((tp2 - entry) / entry) if direction == 'BUY' else ((entry - tp2) / entry)
                f2 = (notional_inr * 0.30) * taker_fee_rate
                p2 = (notional_inr * 0.30) * r2 - f2

                f3 = (notional_inr * 0.30) * taker_fee_rate
                p3 = (notional_inr * 0.30) * r1 - f3

                net = p1 + p2 + p3 - entry_fee
                cap += net
                wins += 1
            elif 'TP1' in out:
                r1 = ((tp1 - entry) / entry) if direction == 'BUY' else ((entry - tp1) / entry)
                f1 = (notional_inr * 0.40) * taker_fee_rate
                p1 = (notional_inr * 0.40) * r1 - f1

                f_rem = (notional_inr * 0.60) * taker_fee_rate
                net = p1 - f_rem - entry_fee
                cap += net
                if net >= 0: wins += 1
                else: losses += 1
            elif 'SL' in out:
                f_sl = notional_inr * taker_fee_rate
                net = (notional_inr * (-sl_dist)) - f_sl - entry_fee
                cap += net
                losses += 1
            else:
                continue

            if cap > max_cap: max_cap = cap
            dd = (max_cap - cap) / max_cap * 100.0 if max_cap > 0 else 0
            if dd > max_dd_pct: max_dd_pct = dd

        net_pnl = cap - 100000.0
        roi = net_pnl / 1000.0
        total = wins + losses
        wr = (wins / total * 100.0) if total > 0 else 0
        print(f"Risk: {risk_pct_level*100:4.1f}% | Win Rate: {wr:4.1f}% | Final Cap: Rs {cap:10,.2f} | Net PnL: Rs {net_pnl:+10,.2f} ({roi:+6.2f}%) | Max DD: {max_dd_pct:5.1f}%")

if __name__ == '__main__':
    base_dir = r"C:\Users\himan\.gemini\antigravity\scratch\binance_ema_bot"
    f_recent = os.path.join(base_dir, "evaluated_since_yesterday.json")
    f_365 = os.path.join(base_dir, "evaluated_real_trajectories.json")

    print("=== RECENT SIGNALS ANALYSIS ===")
    analyze_breakdown(f_recent)
    simulate_risk_levels(f_recent)
    simulate_risk_levels(f_recent, tfs=['1H', '4H', '1D'])
