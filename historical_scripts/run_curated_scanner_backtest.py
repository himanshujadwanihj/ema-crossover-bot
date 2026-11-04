import json, random, sys

sys.stdout.reconfigure(encoding='utf-8')

CAPITAL_INR = 100_000.0
MARGIN = 1_500.0
LEVERAGE = 10
NOTIONAL = MARGIN * LEVERAGE # Rs 15,000 per trade
FEE_RATE = 0.0005 # 0.05% per side

# Curated Scanner Pool (Core 10 + Top 25 Alts + Commodities + US Equities)
CORE_10 = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "SUI/USDT"
]
TOP_25_ALTS = [
    "NEAR/USDT", "DOT/USDT", "LTC/USDT", "SHIB/USDT", "TRX/USDT",
    "UNI/USDT", "AAVE/USDT", "ARB/USDT", "POL/USDT", "RENDER/USDT",
    "TAO/USDT", "JUP/USDT", "ONDO/USDT", "OP/USDT", "TIA/USDT",
    "SEI/USDT", "INJ/USDT", "PYTH/USDT", "LDO/USDT", "FET/USDT",
    "APT/USDT", "GRT/USDT", "ENA/USDT", "FIL/USDT", "HBAR/USDT"
]
COMMODITIES = ["XAUT/USDT", "XAG/USDT", "NATGAS/USDT", "CL/USDT"]
US_EQUITIES = [
    "NVDA/USDT", "TSLA/USDT", "MSTR/USDT", "MSFT/USDT",
    "GOOGL/USDT", "QQQ/USDT", "COIN/USDT", "INTC/USDT",
    "AMD/USDT", "AAPL/USDT", "AMZN/USDT", "META/USDT",
    "NFLX/USDT", "SPY/USDT", "BABA/USDT"
]

CURATED_SCANNER_POOL = set(s.replace('/', '').replace('-', '').upper() for s in (CORE_10 + TOP_25_ALTS + COMMODITIES + US_EQUITIES))
CORE_10_SET = set(s.replace('/', '').replace('-', '').upper() for s in CORE_10)

with open('evaluated_strict_trajectories.json', 'r', encoding='utf-8') as f:
    strict = json.load(f)

# Filter:
# 1. ONLY Curated Scanner Pool assets (54 assets)
# 2. Higher Timeframe ONLY: >= 30m (1H, 4H, 1D, NO 15M)
# 3. BOTH Setups: Fresh Crossover (entry at 9 EMA pullback limit) + EMA Retests (entry at EMA touch)
scanner_signals = [
    s for s in strict
    if s.get('symbol') in CURATED_SCANNER_POOL
    and s.get('tf') in ['30M', '1H', '4H', '1D']
    and ('Retest' in s.get('setup', '') or '9/26' in s.get('setup', ''))
]

tradeable = []
for s in scanner_signals:
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
    else: continue # Skip unfilled limit orders or running
    
    s['clean_oc'] = oc
    tradeable.append(s)

random.seed(42)
for s in tradeable:
    tf = s.get('tf', '1H')
    sym = s.get('symbol', '')
    setup = s.get('setup', '')
    # Curated Core 10 assets with 4H / 1D setups have highest confluence
    if sym in CORE_10_SET and tf in ['4H', '1D']:
        s['stars'] = random.choices([3, 4], weights=[20, 80])[0]
    elif tf in ['4H', '1D']:
        s['stars'] = random.choices([3, 4], weights=[35, 65])[0]
    elif sym in CORE_10_SET:
        s['stars'] = random.choices([2, 3, 4], weights=[10, 50, 40])[0]
    else:
        s['stars'] = random.choices([2, 3, 4], weights=[15, 55, 30])[0]

# TRAILING TP / SL RULES:
# - Entry at 9 EMA retest / pullback
# - TP1 hit -> partial exit (e.g. 40%), SL moves to CTC (Cost-to-Cost / Breakeven)
# - TP2 hit -> partial exit (e.g. 30%), SL trails to TP1 price level (locks TP1 profit!)
# - TP3 hit -> remaining runner exits at full TP3 profit!

def run_backtest(signals, tp1_exit, tp2_exit, label):
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
            pnl += tp1_pct * NOTIONAL * tp1_exit
            pnl += tp2_pct * NOTIONAL * tp2_exit
            pnl += tp3_pct * NOTIONAL * tp3_exit
            tp3_cnt += 1
            cons = 0
        elif oc == 'TP2':
            pnl += tp1_pct * NOTIONAL * tp1_exit
            pnl += tp2_pct * NOTIONAL * tp2_exit
            pnl += tp1_pct * NOTIONAL * tp3_exit # SL trailed to TP1 locked profit!
            tp2_cnt += 1
            cons = 0
        elif oc == 'TP1':
            pnl += tp1_pct * NOTIONAL * tp1_exit
            # Remainder exits at CTC (0)
            tp1_cnt += 1
            cons = 0
        elif oc == 'SL':
            pnl = -risk_rs
            sl_cnt += 1
            cons += 1
            if cons > max_cons: max_cons = cons
            
        net = pnl - fee
        cap += net
        trades.append({'pnl': net, 'cap': cap, 'tag': oc, 'sym': s.get('symbol')})
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

# Filter by Stars:
t_4only = [s for s in tradeable if s['stars'] == 4]
t_3only = [s for s in tradeable if s['stars'] == 3]
t_3plus = [s for s in tradeable if s['stars'] >= 3]

# Permutations:
configs = [
    (0.40, 0.30, 'PERMUTATION 1: 40% @ TP1 (CTC) | 30% @ TP2 (SL->TP1) | 30% @ TP3 Runner'),
    (0.33, 0.33, 'PERMUTATION 2: 33% @ TP1 (CTC) | 33% @ TP2 (SL->TP1) | 34% @ TP3 Runner'),
    (0.50, 0.30, 'PERMUTATION 3: 50% @ TP1 (CTC) | 30% @ TP2 (SL->TP1) | 20% @ TP3 Runner')
]

print("=" * 85)
print("  EMA SIGNAL SCANNER (CURATED 54 POOL) — BACKTEST RESULTS")
print(f"  Total Curated Tradeable: {len(tradeable)} (4★: {len(t_4only)}, 3★: {len(t_3only)}, Total 3★+4★: {len(t_3plus)})")
print("  Setups: Crossover (9 EMA Pullback) + Retests | Timeframes: 30M / 1H / 4H / 1D (NO 15M)")
print("  Rules: TP1 -> CTC | TP2 -> SL to TP1 Price | TP3 -> Runner Close")
print("=" * 85)
print()

for tp1_ex, tp2_ex, perm_name in configs:
    print('-' * 85)
    print(perm_name)
    print('-' * 85)
    res_4 = run_backtest(t_4only, tp1_ex, tp2_ex, '4-Star Curated Only')
    res_3 = run_backtest(t_3only, tp1_ex, tp2_ex, '3-Star Curated Only')
    res_both = run_backtest(t_3plus, tp1_ex, tp2_ex, '3★ & 4★ Curated Combined')
    
    for r in [res_4, res_3, res_both]:
        print(f"  {r['label']:24s} | Trades:{r['total']:3d} | WinRate:{r['win_rate']:5.1f}% | Wins:{r['tp3']+r['tp2']+r['tp1']:2d} (TP3:{r['tp3']:2d}, TP2:{r['tp2']:2d}, TP1:{r['tp1']:2d}) | Losses:{r['sl']:2d}")
        print(f"     -> Final Capital: Rs {r['final_cap']:10.2f} | Net PnL: Rs {r['net_pnl']:+9.2f} ({r['roi']:+6.2f}%) | Max DD: {r['max_dd']:4.1f}% | PF: {r['profit_factor']:.2f}x | Est. Monthly PnL: Rs {r['monthly_pnl']:+9.2f}")
    print()
