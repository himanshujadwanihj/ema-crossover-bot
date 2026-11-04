import json

with open("deep_support_events.json", "r", encoding="utf-8") as f:
    events = json.load(f)

# Combo: Candle 2 Confirmation + 4H HTF Aligned
combo_trades = [e for e in events if e["candle2_confirmed"] and e["htf_aligned"]]
failed_trades = [e for e in events if not (e["candle2_confirmed"] and e["htf_aligned"])]

base_wins = sum(1 for e in events if e["is_win"])
base_losses = len(events) - base_wins

combo_wins = sum(1 for e in combo_trades if e["is_win"])
combo_losses = len(combo_trades) - combo_wins

# R-multiples: Win gives 1.2R (at TP1), Loss gives -1.0R
base_r = (base_wins * 1.2) - (base_losses * 1.0)
combo_r = (combo_wins * 1.2) - (combo_losses * 1.0)

base_pf = (base_wins * 1.2) / (base_losses * 1.0)
combo_pf = (combo_wins * 1.2) / (combo_losses * 1.0)

print(f"BASELINE:   Trades: {len(events)} | WinRate: {base_wins/len(events)*100:.1f}% | PF: {base_pf:.2f} | Net R: {base_r:+.1f}R")
print(f"COMBO 58%:  Trades: {len(combo_trades)} | WinRate: {combo_wins/len(combo_trades)*100:.1f}% | PF: {combo_pf:.2f} | Net R: {combo_r:+.1f}R")
print(f"Losses Eliminated: {base_losses - combo_losses} losing trades avoided!")
