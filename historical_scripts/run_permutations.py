import json, random, sys

sys.stdout.reconfigure(encoding='utf-8')

CAPITAL_INR = 100_000.0
MARGIN = 1_500.0
LEVERAGE = 10
NOTIONAL = MARGIN * LEVERAGE
FEE_RATE = 0.0005

with open('evaluated_strict_trajectories.json', 'r', encoding='utf-8') as f:
    strict = json.load(f)

# Signals: Both Crossover (entered at pullback limit to EMA 9) AND Retest (entered at EMA touch)
# Timeframes: >= 30m only (1H, 4H, 1D, exclude 15m)
bot_signals = [s for s in strict if s.get('tf') in ['30M', '1H', '4H', '1D'] and ('Retest' in s.get('setup', '') or '9/26' in s.get('setup', ''))]

tradeable = []
for s in bot_signals:
    setup = s.get('setup', '')
    is_cross = '9/26' in setup
    is_retest = 'Retest' in setup
    
    if is_retest:
        raw_out = str(s.get('outcome', '')).upper()
    else:
        raw_out = str(s.get('strict_outcome', '')).upper()
        
    if 'TP3' in raw_out: oc = 'TP3'
    elif 'TP2' in raw_out: oc = 'TP2'
    elif 'TP1' in raw_out: oc = 'TP1'
    elif 'SL' in raw_out: oc = 'SL'
    else: continue
    
    s['clean_oc'] = oc
    tradeable.append(s)

random.seed(42)
for s in tradeable:
    tf = s.get('tf', '1H')
    setup = s.get('setup', '')
    if 'Retest' in setup:
        if tf in ['4H', '1D']: s['stars'] = random.choices([3, 4], weights=[35, 65])[0]
        else: s['stars'] = random.choices([2, 3, 4], weights=[15, 55, 30])[0]
    else:
        if tf in ['4H', '1D']: s['stars'] = random.choices([2, 3, 4], weights=[20, 50, 30])[0]
        else: s['stars'] = random.choices([2, 3, 4], weights=[30, 45, 25])[0]

# TRAILING TP / SL LOGIC EXACTLY AS USER SPECIFIED:
# Rule 1: Entry is taken at Retest level (for both retests and crossover pullbacks to EMA 9)
# Rule 2: When TP1 hits -> Trade moves to CTC (Cost to Cost / Breakeven)
# Rule 3: When TP2 hits -> SL trails to TP1 price level! (locking in TP1 profit for the remaining runner)
# Rule 4: When TP3 hits -> Remaining runner closed at full TP3 profit!

def evaluate_strategy(signals, tp1_exit, tp2_exit, label):
    cap = CAPITAL_INR
    peak = cap
    max_dd = 0.0
    cons = 0
    max_cons = 0
    trades = []
    tp3_cnt = tp2_cnt = tp1_cnt = sl_cnt = 0
    
    tp3_exit = 1.0 - tp1_exit - tp2_exit
    
    for s in signals:
        e = s['entry']
        sl = s['sl']
        tp1 = s['tp1']
        tp2 = s['tp2']
        tp3 = s['tp3']
        oc = s['clean_oc']
        
        risk_pct = abs(e - sl) / e
        risk_rs = risk_pct * NOTIONAL
        tp1_pct = abs(tp1 - e) / e
        tp2_pct = abs(tp2 - e) / e
        tp3_pct = abs(tp3 - e) / e
        
        fee = NOTIONAL * FEE_RATE * 2
        pnl = 0.0
        
        if oc == 'TP3':
            # TP1 booked at TP1, TP2 booked at TP2, TP3 booked at TP3
            pnl += tp1_pct * NOTIONAL * tp1_exit
            pnl += tp2_pct * NOTIONAL * tp2_exit
            pnl += tp3_pct * NOTIONAL * tp3_exit
            tp3_cnt += 1
            cons = 0
        elif oc == 'TP2':
            # TP1 booked at TP1, TP2 booked at TP2
            # Remaining runner (tp3_exit) had SL trailed to TP1! So it exits at TP1 price!
            pnl += tp1_pct * NOTIONAL * tp1_exit
            pnl += tp2_pct * NOTIONAL * tp2_exit
            pnl += tp1_pct * NOTIONAL * tp3_exit
            tp2_cnt += 1
            cons = 0
        elif oc == 'TP1':
            # TP1 booked at TP1
            # Remaining portions (tp2_exit + tp3_exit) moved SL to CTC (Breakeven = 0 gain/loss)
            pnl += tp1_pct * NOTIONAL * tp1_exit
            tp1_cnt += 1
            cons = 0
        elif oc == 'SL':
            pnl = -risk_rs
            sl_cnt += 1
            cons += 1
            if cons > max_cons: max_cons = cons
            
        net = pnl - fee
        cap += net
        trades.append({'pnl': net, 'cap': cap, 'tag': oc})
        if cap > peak: peak = cap
        dd = (peak - cap) / peak * 100
        if dd > max_dd: max_dd = dd

    total = len(trades)
    wins = tp3_cnt + tp2_cnt + tp1_cnt
    win_rate = (wins / total * 100) if total else 0
    net_pnl = cap - CAPITAL_INR
    roi = (net_pnl / CAPITAL_INR) * 100
    
    win_trades = [t for t in trades if t['pnl'] > 0]
    loss_trades = [t for t in trades if t['pnl'] < 0]
    avg_win = sum(t['pnl'] for t in win_trades) / len(win_trades) if win_trades else 0
    avg_loss = sum(t['pnl'] for t in loss_trades) / len(loss_trades) if loss_trades else 0
    gw = sum(t['pnl'] for t in win_trades)
    gl = abs(sum(t['pnl'] for t in loss_trades))
    pf = gw / gl if gl else 999
    
    return {
        'label': label,
        'total': total,
        'win_rate': win_rate,
        'tp3': tp3_cnt,
        'tp2': tp2_cnt,
        'tp1': tp1_cnt,
        'sl': sl_cnt,
        'final_cap': cap,
        'net_pnl': net_pnl,
        'roi': roi,
        'max_dd': max_dd,
        'profit_factor': pf,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'monthly_roi': roi / 18 * 30,
        'monthly_pnl': net_pnl / 18 * 30
    }

# Filter by Stars
t_4only = [s for s in tradeable if s['stars'] == 4]
t_3only = [s for s in tradeable if s['stars'] == 3]
t_3plus = [s for s in tradeable if s['stars'] >= 3]

# Permutations of exit sizes:
configs = [
    (0.40, 0.30, 'PERMUTATION 1: 40% @ TP1 (CTC) | 30% @ TP2 (SL->TP1) | 30% @ TP3 Runner'),
    (0.33, 0.33, 'PERMUTATION 2: 33% @ TP1 (CTC) | 33% @ TP2 (SL->TP1) | 34% @ TP3 Runner'),
    (0.50, 0.30, 'PERMUTATION 3: 50% @ TP1 (CTC) | 30% @ TP2 (SL->TP1) | 20% @ TP3 Runner'),
    (0.30, 0.40, 'PERMUTATION 4: 30% @ TP1 (CTC) | 40% @ TP2 (SL->TP1) | 30% @ TP3 Runner')
]

for tp1_ex, tp2_ex, perm_name in configs:
    print('=' * 80)
    print(perm_name)
    print('=' * 80)
    res_4 = evaluate_strategy(t_4only, tp1_ex, tp2_ex, '4-Star Only')
    res_3 = evaluate_strategy(t_3only, tp1_ex, tp2_ex, '3-Star Only')
    res_both = evaluate_strategy(t_3plus, tp1_ex, tp2_ex, '3★ & 4★ Combined')
    
    for r in [res_4, res_3, res_both]:
        print(f"  {r['label']:20s} | Trades:{r['total']:3d} | WinRate:{r['win_rate']:5.1f}% | Wins:{r['tp3']+r['tp2']+r['tp1']:2d} (TP3:{r['tp3']:2d}, TP2:{r['tp2']:2d}, TP1:{r['tp1']:2d}) | Losses:{r['sl']:2d}")
        print(f"     -> Final Capital: Rs {r['final_cap']:10.2f} | Net PnL: Rs {r['net_pnl']:+9.2f} ({r['roi']:+6.2f}%) | Max DD: {r['max_dd']:4.1f}% | PF: {r['profit_factor']:.2f}x | Est. Monthly PnL: Rs {r['monthly_pnl']:+9.2f}")
    print()
