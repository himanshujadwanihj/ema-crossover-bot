import json
import os

def run_simulation(trades_file, risk_mode='dynamic', initial_cap=100000.0):
    if not os.path.exists(trades_file):
        return None

    with open(trades_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    items = data.get('trades') or data if isinstance(data, dict) else data

    cap = initial_cap
    wins = 0
    losses = 0
    breakevens = 0
    total_closed = 0
    max_cap = initial_cap
    max_dd_inr = 0.0
    max_dd_pct = 0.0

    maker_fee_rate = 0.0002
    taker_fee_rate = 0.0005
    leverage = 10

    trade_details = []

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
        
        # Calculate SL distance %
        risk_pct = abs(entry - sl) / entry
        if risk_pct <= 0:
            risk_pct = 0.015

        # Determine Position Size
        if risk_mode == 'fixed':
            margin_inr = 1500.0
            notional_inr = margin_inr * leverage
        else: # dynamic 2.5% risk
            target_risk_inr = cap * 0.025
            calc_notional = target_risk_inr / risk_pct
            calc_margin = calc_notional / leverage
            # Clamps: min 500, max 20% of capital
            margin_inr = max(500.0, min(calc_margin, cap * 0.20))
            notional_inr = margin_inr * leverage

        # Maker entry fee
        entry_fee = notional_inr * maker_fee_rate
        trade_pnl = -entry_fee

        if 'TP3' in out or 'FULL WIN' in out:
            # TP1: 40%
            r1 = ((tp1 - entry) / entry) if direction == 'BUY' else ((entry - tp1) / entry)
            f1 = (notional_inr * 0.40) * taker_fee_rate
            p1 = (notional_inr * 0.40) * r1 - f1

            # TP2: 30%
            r2 = ((tp2 - entry) / entry) if direction == 'BUY' else ((entry - tp2) / entry)
            f2 = (notional_inr * 0.30) * taker_fee_rate
            p2 = (notional_inr * 0.30) * r2 - f2

            # TP3: 30%
            r3 = ((tp3 - entry) / entry) if direction == 'BUY' else ((entry - tp3) / entry)
            f3 = (notional_inr * 0.30) * taker_fee_rate
            p3 = (notional_inr * 0.30) * r3 - f3

            net = p1 + p2 + p3 - entry_fee
            cap += net
            wins += 1
            total_closed += 1
            trade_pnl = net
        elif 'TP2' in out:
            # TP1: 40%
            r1 = ((tp1 - entry) / entry) if direction == 'BUY' else ((entry - tp1) / entry)
            f1 = (notional_inr * 0.40) * taker_fee_rate
            p1 = (notional_inr * 0.40) * r1 - f1

            # TP2: 30%
            r2 = ((tp2 - entry) / entry) if direction == 'BUY' else ((entry - tp2) / entry)
            f2 = (notional_inr * 0.30) * taker_fee_rate
            p2 = (notional_inr * 0.30) * r2 - f2

            # Remaining 30% stopped out at TP1 price
            f3 = (notional_inr * 0.30) * taker_fee_rate
            p3 = (notional_inr * 0.30) * r1 - f3

            net = p1 + p2 + p3 - entry_fee
            cap += net
            wins += 1
            total_closed += 1
            trade_pnl = net
        elif 'TP1' in out:
            # TP1: 40%
            r1 = ((tp1 - entry) / entry) if direction == 'BUY' else ((entry - tp1) / entry)
            f1 = (notional_inr * 0.40) * taker_fee_rate
            p1 = (notional_inr * 0.40) * r1 - f1

            # Remaining 60% trailed to entry (breakeven exit)
            f_rem = (notional_inr * 0.60) * taker_fee_rate
            net = p1 - f_rem - entry_fee
            cap += net
            if net >= 0:
                wins += 1
            else:
                losses += 1
            total_closed += 1
            trade_pnl = net
        elif 'SL' in out:
            # Full SL Hit (100% position)
            loss_pct = -risk_pct
            f_sl = notional_inr * taker_fee_rate
            net = (notional_inr * loss_pct) - f_sl - entry_fee
            cap += net
            losses += 1
            total_closed += 1
            trade_pnl = net
        else:
            continue

        if cap > max_cap:
            max_cap = cap
        dd_inr = max_cap - cap
        dd_pct = (dd_inr / max_cap) * 100.0 if max_cap > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_inr = dd_inr

    roi_pct = ((cap - initial_cap) / initial_cap) * 100.0
    win_rate = (wins / total_closed * 100.0) if total_closed > 0 else 0.0
    return {
        'trades': total_closed,
        'wins': wins,
        'losses': losses,
        'win_rate': win_rate,
        'final_cap': cap,
        'net_pnl': cap - initial_cap,
        'roi_pct': roi_pct,
        'max_dd_pct': max_dd_pct,
        'max_dd_inr': max_dd_inr
    }

if __name__ == '__main__':
    base_dir = r"C:\Users\himan\.gemini\antigravity\scratch\binance_ema_bot"
    f365 = os.path.join(base_dir, "evaluated_real_trajectories.json")
    f_recent = os.path.join(base_dir, "evaluated_since_yesterday.json")

    print("=================================================================")
    print("BACKTEST: ALL 365 RECORDED HISTORICAL SIGNALS")
    print("=================================================================")
    res_fixed = run_simulation(f365, 'fixed')
    res_dyn = run_simulation(f365, 'dynamic')

    print(f"OLD MODEL (Fixed Rs 1,500 Margin @ 10x):")
    print(f"  Closed Trades:   {res_fixed['trades']}")
    print(f"  Wins:            {res_fixed['wins']} ({res_fixed['win_rate']:.1f}% Win Rate)")
    print(f"  Losses:          {res_fixed['losses']}")
    print(f"  Initial Capital: Rs 100,000.00 INR")
    print(f"  Final Capital:   Rs {res_fixed['final_cap']:,.2f} INR")
    print(f"  Net PnL:         Rs {res_fixed['net_pnl']:,.2f} INR ({res_fixed['roi_pct']:+.2f}%)")
    print(f"  Max Drawdown:    {res_fixed['max_dd_pct']:.2f}% (Rs {res_fixed['max_dd_inr']:,.2f} INR)")

    print(f"\nNEW MODEL (Dynamic 2.5% Account Risk-Based Sizing):")
    print(f"  Closed Trades:   {res_dyn['trades']}")
    print(f"  Wins:            {res_dyn['wins']} ({res_dyn['win_rate']:.1f}% Win Rate)")
    print(f"  Losses:          {res_dyn['losses']}")
    print(f"  Initial Capital: Rs 100,000.00 INR")
    print(f"  Final Capital:   Rs {res_dyn['final_cap']:,.2f} INR")
    print(f"  Net PnL:         Rs {res_dyn['net_pnl']:,.2f} INR ({res_dyn['roi_pct']:+.2f}%)")
    print(f"  Max Drawdown:    {res_dyn['max_dd_pct']:.2f}% (Rs {res_dyn['max_dd_inr']:,.2f} INR)")

    print("\n=================================================================")
    print("BACKTEST: RECENT LIVE SIGNALS (evaluated_since_yesterday)")
    print("=================================================================")
    res_rec_fixed = run_simulation(f_recent, 'fixed')
    res_rec_dyn = run_simulation(f_recent, 'dynamic')

    print(f"OLD MODEL (Fixed Rs 1,500 Margin @ 10x):")
    print(f"  Closed Trades:   {res_rec_fixed['trades']}")
    print(f"  Wins:            {res_rec_fixed['wins']} ({res_rec_fixed['win_rate']:.1f}% Win Rate)")
    print(f"  Net PnL:         Rs {res_rec_fixed['net_pnl']:,.2f} INR ({res_rec_fixed['roi_pct']:+.2f}%)")
    print(f"  Max Drawdown:    {res_rec_fixed['max_dd_pct']:.2f}%")

    print(f"\nNEW MODEL (Dynamic 2.5% Account Risk-Based Sizing):")
    print(f"  Closed Trades:   {res_rec_dyn['trades']}")
    print(f"  Wins:            {res_rec_dyn['wins']} ({res_rec_dyn['win_rate']:.1f}% Win Rate)")
    print(f"  Net PnL:         Rs {res_rec_dyn['net_pnl']:,.2f} INR ({res_rec_dyn['roi_pct']:+.2f}%)")
    print(f"  Max Drawdown:    {res_rec_dyn['max_dd_pct']:.2f}%")
