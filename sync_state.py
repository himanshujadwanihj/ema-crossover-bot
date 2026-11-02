import json

with open("evaluated_full_day.json", "r", encoding="utf-8") as f:
    evaluated = json.load(f)["trades"]

state = {}
try:
    with open("signal_state_v3.json", "r", encoding="utf-8") as f:
        state = json.load(f)
except Exception:
    pass

for t in evaluated:
    sym = t["symbol"]
    tf = t["tf"]
    sig = t["direction"]
    setup = t["setup"]
    time_ist = t.get("time_ist", "")
    key = f"{sym}|{tf}_{setup}|{sig}|{time_ist}"
    
    entry = t.get("entry", 0.0)
    sl = t.get("sl", 0.0)
    tp1 = t.get("tp1", 0.0)
    tp2 = t.get("tp2", 0.0)
    tp3 = t.get("tp3", 0.0)
    outcome = t.get("outcome", "PENDING")
    
    state[key] = {
        "sent_at": time_ist,
        "entry": entry,
        "sl": sl,
        "initial_sl": sl,
        "cur_sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp": tp1,
        "outcome": outcome,
        "max_pnl": t.get("max_pnl", 0.0),
        "cur_pnl": t.get("cur_pnl", 0.0)
    }

with open("signal_state_v3.json", "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)

print(f"Successfully synced {len(state)} trades into signal_state_v3.json")
