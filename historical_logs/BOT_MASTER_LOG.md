# 🤖 BOT MASTER LOG — OFFICIAL PRODUCTION FREEZE & MASTER LOCK
# File: C:\Users\himan\.gemini\antigravity\scratch\binance_ema_bot\BOT_MASTER_LOG.md
# Locked: 2026-09-30 23:20 IST

---

## ⚠️ CRITICAL RULES — HARD-LOCKED (NEVER ALTER WITHOUT AUDIT)

1. **3-TP Hybrid System (40% TP1, 30% TP2, 30% TP3)**:
   - **Entry**: EMA 9 Pullback Limit Order (buys the dip / sells the rally).
   - **TP1 Hit (40% closed)** → SL moves to Entry Price (Break-Even CTC Shield).
   - **TP2 Hit (30% closed)** → SL moves to TP1 Price (Guaranteed Profit Shield).
   - **TP3 Hit (30% closed)** → All remaining orders cleaned up (Full Win completed).
2. **Every trade MUST have 1 SL + 3 TPs at all times.** The 15-second background auto-healer restores missing SL/TPs automatically.
3. **Max 4 Active Concurrent Trades Hard Cap.** Hard-enforced by `_get_execution_lock()` turnstile in `executor.py` and `AUTO_TRADE_MAX_CONCURRENT = 4` in `config.py`.
4. **Auto-Trade Sizing**: Fixed **₹1,500 INR (~$14.71 USDT)** per position @ **10x Leverage**.
5. **BTC Volatility Shock Shield (30-Min Auto-Pause Guard)**:
   - Evaluates BTC 15M candle range $\ge 1.5\%$ or body move $\ge 1.2\%$.
   - Pauses new candidate auto-trade scanning for 30 minutes to protect live money from market whipsaws.
   - **Active Position Protection**: `resolve_pending_trades_async` and `tp_monitor_worker` remain 100% FULLY ARMED during the 30-min pause.
6. **Shark Exchange quirks — HARD-LOCKED:**
   - `quantity` is always POSITIVE even for SHORT positions.
   - Direction determined via `positionType == "SHORT"`.
   - Identification of bracket orders via `placeType == "POSITION"`.
   - API responses strictly extracted via `response.get("data", [])`.
   - Error 3023 (duplicate SL) prevented by deleting old SL order FIRST before placing new SL.
   - Error 3011 (SL activates instantly) handled with 0.3% price fallback buffer.

---

## 📱 TELEGRAM CHANNEL ROUTING — PERMANENTLY HARDCODED

| Target | Chat ID | Content |
|---|---|---|
| Main Trading Channel | `-1004332146822` | Shark Order Fills, TP Hits, SL Trails, BTC Shock Alerts, Crossovers |
| EMA Crossovers Channel | `-1004463573705` | Fresh 9/26 EMA Crossover Signal Cards |

---

## 📂 CORE SYSTEM FILES

| File | Purpose |
|---|---|
| `config.py` | Environment parameters, leverage (10x), max trades (4), margin (1500 INR) |
| `executor.py` | Shark Exchange REST API calls: entry, SL, TP, step-trailing, turnstile lock |
| `main.py` | Main scan loop, BTC shock check, 15s TP/SL monitor, trade resolution |
| `indicators.py` | Technical indicators, Liquidity Pool TP calculator, BTC Shock Shield |
| `pullback_tracker.py` | Retest trailing SL engine & watchlist |
| `telegram_bot.py` | Hardcoded Telegram alert chunking & channel routing |
| `state.py` | Atomic JSON state persistence (`signal_state_v3.json`) |
| `demo_run_simulation.py` | 16/16 Unit Test Suite |

---

## ⚙️ PRODUCTION LOCK STATE (as of 2026-09-30 23:20 IST)

- **Auto-Trading Status:** 🟢 ACTIVE (`AUTO_TRADING_ENABLED = True`)
- **Portfolio Hard Cap:** 🟢 4 Concurrent Active Slots (`AUTO_TRADE_MAX_CONCURRENT = 4`)
- **Margin per Trade:** 🟢 ₹1,500 INR @ 10x Leverage
- **BTC Shock Guard:** 🟢 ACTIVE (30-Minute Auto-Pause Window)
- **Background Auto-Healer:** 🟢 ACTIVE (15-second loop)
- **Unit Test Suite:** 🟢 16/16 PASSED
- **Deployment Platform:** 🟢 RAILWAY PRODUCTION LIVE
