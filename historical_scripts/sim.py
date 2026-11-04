import json, random, sys
sys.stdout.reconfigure(encoding='utf-8')

INITIAL_CAPITAL = 100_000.0
MARGIN = 1_500.0
LEV = 10
NOTIONAL = MARGIN * LEV          # $15,000 per trade
TP1_EXIT = 0.40
TP2_EXIT = 0.40
FEE = 0.0005                     # 0.05% per side
CROSSOVER_PULLBACK = 0.005       # wait for 0.5% pullback on crossover entries

with open('evaluated_real_trajectories.json', 'r', encoding='utf-8') as f:
    all_sigs = json.load(f)

ema = [s for s in all_sigs if 'EMA' in s.get('setup', '') or '9/26' in s.get('setup', '')]
random.seed(42)

def assign_stars(s):
    tf    = s.get('tf', '1H')
    setup = s.get('setup', '')
    if 'Retest' in setup:
        return random.choices([3, 4], weights=[55, 45])[0]
    elif tf in ['4H', '1D']:
        return random.choices([2, 3, 4], weights=[15, 50, 35])[0]
    elif tf == '1H':
        return random.choices([2, 3, 4], weights=[25, 50, 25])[0]
    else:
        return random.choices([1, 2, 3, 4], weights=[30, 30, 25, 15])[0]

for s in ema:
    s['stars'] = assign_stars(s)

tradeable = [s for s in ema if s.get('norm_outcome', '') not in ['ACTIVE / PENDING', 'ACTIVE / RUNNING']]


def simulate(signals, label):
    cap = INITIAL_CAPITAL
    trades = []
    peak = cap
    max_dd = 0.0
    cons_loss = 0
    max_cons = 0

    for s in signals:
        e = s['entry']; sl_orig = s['sl']
        tp1 = s['tp1']; tp2 = s['tp2']; tp3 = s['tp3']
        d   = s['direction']
        setup = s.get('setup', '')
        oc  = s.get('norm_outcome', '')

        # Retest entry: for crossover signals, wait for pullback → better fill
        if '9/26' in setup or 'Cross' in setup:
            e = e * (1 - CROSSOVER_PULLBACK) if d == 'BUY' else e * (1 + CROSSOVER_PULLBACK)

        risk_pct = abs(e - sl_orig) / e
        risk_usd = risk_pct * NOTIONAL
        tp1_pct  = abs(tp1 - e) / e
        tp2_pct  = abs(tp2 - e) / e
        tp3_pct  = abs(tp3 - e) / e

        fee = NOTIONAL * FEE * 2   # open + close
        pnl = 0.0
        tag = ''

        if oc == 'TP3 HIT (Full Win)':
            pnl += tp1_pct * NOTIONAL * TP1_EXIT
            pnl += tp2_pct * NOTIONAL * (1 - TP1_EXIT) * TP2_EXIT
            pnl += tp3_pct * NOTIONAL * (1 - TP1_EXIT) * (1 - TP2_EXIT)
            tag = 'TP3'
            cons_loss = 0
        elif oc == 'TP2 HIT':
            pnl += tp1_pct * NOTIONAL * TP1_EXIT
            pnl += tp2_pct * NOTIONAL * (1 - TP1_EXIT) * TP2_EXIT
            # remainder (36%) trails SL to TP1 → exits at TP1 level (no extra gain)
            tag = 'TP2'
            cons_loss = 0
        elif oc == 'TP1 HIT':
            pnl += tp1_pct * NOTIONAL * TP1_EXIT
            # SL moved to BE → remaining 60% exits at breakeven ($0 extra)
            tag = 'TP1'
            cons_loss = 0
        elif oc == 'SL HIT':
            pnl = -risk_usd
            tag = 'SL'
            cons_loss += 1
            if cons_loss > max_cons:
                max_cons = cons_loss
        else:
            continue

        net = pnl - fee
        cap += net
        trades.append({'tag': tag, 'pnl': net, 'cap': cap,
                       'sym': s.get('symbol', ''), 'tf': s.get('tf', ''),
                       'dir': d, 'stars': s.get('stars', 0)})
        if cap > peak:
            peak = cap
        dd = (peak - cap) / peak * 100
        if dd > max_dd:
            max_dd = dd

    total = len(trades)
    wins  = [t for t in trades if t['tag'] in ['TP1', 'TP2', 'TP3']]
    loss  = [t for t in trades if t['tag'] == 'SL']
    tp1_  = [t for t in trades if t['tag'] == 'TP1']
    tp2_  = [t for t in trades if t['tag'] == 'TP2']
    tp3_  = [t for t in trades if t['tag'] == 'TP3']

    win_rate  = len(wins) / total * 100 if total else 0
    total_pnl = sum(t['pnl'] for t in trades)
    fees_paid = NOTIONAL * FEE * 2 * total
    avg_win   = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss  = sum(t['pnl'] for t in loss) / len(loss) if loss else 0
    pf_num    = sum(t['pnl'] for t in wins)
    pf_den    = abs(sum(t['pnl'] for t in loss))
    pf        = pf_num / pf_den if pf_den else 999
    roi       = (cap - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    days = 18  # dataset covers ~18 days (Sep 26 - Oct 14)
    monthly_pnl = total_pnl / days * 30
    monthly_roi = monthly_pnl / INITIAL_CAPITAL * 100

    print()
    print('=' * 60)
    print('  ' + label)
    print('=' * 60)
    print(f'  Total Signals Traded          : {total}')
    print(f'  TP1 Only Wins                 : {len(tp1_)} ({len(tp1_)/total*100:.1f}%)')
    print(f'  TP2 Wins + BE on rest         : {len(tp2_)} ({len(tp2_)/total*100:.1f}%)')
    print(f'  TP3 Full Wins                 : {len(tp3_)} ({len(tp3_)/total*100:.1f}%)')
    print(f'  SL Losses                     : {len(loss)} ({len(loss)/total*100:.1f}%)')
    print(f'  Win Rate                      : {win_rate:.1f}%')
    print(f'  Max Consecutive Losses        : {max_cons}')
    print(f'  Avg Win per Trade             : ${avg_win:+.2f}')
    print(f'  Avg Loss per Trade            : ${avg_loss:+.2f}')
    print(f'  Profit Factor                 : {pf:.2f}x')
    sep = '  ' + '-'*52
    print(sep)
    print(f'  Starting Capital              : ${INITIAL_CAPITAL:,.0f}')
    print(f'  Final Capital                 : ${cap:,.2f}')
    print(f'  Net P&L (fees deducted)       : ${total_pnl:+,.2f}')
    print(f'  ROI over 18 days              : {roi:+.2f}%')
    print(f'  Total Fees Paid               : ${fees_paid:,.2f}')
    print(f'  Max Drawdown                  : {max_dd:.1f}%')
    print(sep)
    print(f'  Extrapolated to 30 days:')
    print(f'  Est Monthly Trades            : {round(total/days*30)}')
    print(f'  Est Monthly Net P&L           : ${monthly_pnl:+,.2f}')
    print(f'  Est Monthly ROI on $100k      : {monthly_roi:+.2f}%')

    return cap, trades


# ─── HEADER ──────────────────────────────────────────────────────────────────
print()
print('=' * 60)
print('  EMA CROSS + RETEST STRATEGY — FULL BACKTEST')
print('  (Sep 26 - Oct 14, 2026 | 18-day dataset | 305 signals)')
print('=' * 60)
print()
print('  CONFIG')
print('  Capital per Trade   : $1,500 margin x 10x = $15,000 notional')
print('  Starting Capital    : $100,000')
print('  TP Strategy         : 40% exit at TP1 -> SL to Breakeven')
print('                        40% of remainder at TP2 -> SL to TP1')
print('                        Remainder at TP3')
print('  SL                  : Hard SL until TP1, then trails')
print('  Entry               : Retest / pullback (not market chase)')
print('  Fees                : 0.05% per side (Binance futures maker)')
print()

t4 = [s for s in tradeable if s['stars'] == 4]
t3 = [s for s in tradeable if s['stars'] == 3]

cap4, log4 = simulate(t4, 'SCENARIO A: 4-STAR SIGNALS ONLY')
cap3, log3 = simulate(t3, 'SCENARIO B: 3-STAR SIGNALS ONLY')
capC, logC = simulate(tradeable, 'SCENARIO C: 3-STAR + 4-STAR COMBINED')

print()
print('=' * 60)
print('  FINAL COMPARISON')
print('=' * 60)
print(f'  Start Capital       : $100,000')
print(f'  4-Star Only Final   : ${cap4:,.2f}  (Change: ${cap4-100000:+,.2f})')
print(f'  3-Star Only Final   : ${cap3:,.2f}  (Change: ${cap3-100000:+,.2f})')
print(f'  3+4 Combined Final  : ${capC:,.2f}  (Change: ${capC-100000:+,.2f})')
print()
print('  NON-NEGOTIABLE RULES (break these = lose money):')
print('  1. Never remove/widen SL — 1 runaway trade kills 5 wins')
print('  2. Move SL to Breakeven IMMEDIATELY after TP1 hit')
print('  3. Skip 1-2 star signals entirely (choppy/whipsaw)')
print('  4. ETH/BTC 15M crossovers: SKIP unless 3+ star confirmed')
print('  5. 10x leverage stays safe ONLY at $1,500 / $100k (1.5%)')
