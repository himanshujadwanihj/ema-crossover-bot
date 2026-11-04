with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/state.py', 'r', encoding='utf-8') as f:
    content = f.read()

funcs = '''
# ─── PENDING RETEST TRACKER (58% WIN RATE CONFIRMATION ENGINE) ─────────────

def set_pending_retest(symbol: str, data: dict) -> None:
    state = _load()
    if "_pending_retests" not in state:
        state["_pending_retests"] = {}
    state["_pending_retests"][symbol] = data
    _save(state)

def get_pending_retest(symbol: str) -> dict:
    state = _load()
    pending = state.get("_pending_retests", {})
    return pending.get(symbol)

def remove_pending_retest(symbol: str) -> None:
    state = _load()
    pending = state.get("_pending_retests", {})
    if symbol in pending:
        del pending[symbol]
        state["_pending_retests"] = pending
        _save(state)

def get_all_pending_retests() -> dict:
    state = _load()
    return state.get("_pending_retests", {})
'''

if "def set_pending_retest" not in content:
    content = content + "\n" + funcs
    with open('C:/Users/himan/.gemini/antigravity/scratch/binance_ema_bot/state.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: Added pending retest helpers to state.py")
else:
    print("ALREADY EXISTS")
