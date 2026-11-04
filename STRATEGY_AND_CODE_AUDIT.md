# 🚀 BINANCE EMA 9/26 SNIPER BOT — MASTER STRATEGY & CODE AUDIT
**Generated on**: November 4, 2026
**System Status**: Production Ready | Verified & Synchronized

---

## 📋 Master Strategy-to-Code Verification Matrix

| # | Strategy Rule | Implementation Detail | Source File & Line Numbers |
|---|---|---|---|
| **1** | **Margin Sizing (2.0% Dynamic)** | Margin is strictly 2.0% of Available Free Margin on broker. `Margin = max(₹500, Avail * 0.02)`. | `executor.py` (Lines 380–420) & `paper_trader.py` (Lines 275–285) |
| **2** | **Leverage (10x Isolated)** | Fixed 10x Isolated Leverage. Sized as Notional = Margin × 10. | `config.py` (L30), `executor.py` (L422), `paper_trader.py` (L24) |
| **3** | **Uncapped Active Trades** | Zero artificial portfolio cap. Driven purely by broker free margin. Single Order Shield prevents duplicates. | `config.py` (L34), `executor.py` (L547–555), `main.py` (L1048) |
| **4** | **Signal Confluence** | Minimum 3★ (3/4) or 4★ (4/4) multi-timeframe trend alignment (1H, 4H, 1D, 1W) + Verified RSI Momentum Boost. | `main.py` (L992–1005), `paper_trader.py` (L174–202) |
| **5** | **RSI Momentum Bounds** | BUY: 45.0 <= RSI <= 72.0 | SELL: 28.0 <= RSI <= 55.0. Rejects exhausted tops and falling knives. | `main.py` (L998–1004), `paper_trader.py` (L187–202) |
| **6** | **Stop-Loss (26 EMA Baseline)** | Strictly placed below 26 EMA (BUY) or above 26 EMA (SELL) with 0.25*ATR buffer. Min floor 0.80%, Max cap 2.20%. | `indicators.py` (Lines 220–255 & 315–350) |
| **7** | **Liquidation Shield** | SL mathematically clamped >= 2.5% inside exchange liquidation price. | `executor.py` (Lines 140–220) |
| **8** | **Take-Profit (2-Tier Engine)** | TP1 (70% size, Min 1:2.0 RR) + TP2 (30% size runner, Min 1:3.0 RR). | `indicators.py` (Lines 260–290 & 350–385) |
| **9** | **Order Block Front-Running** | TP1 is placed 0.35% below resistance block (BUY) or 0.35% above support block (SELL) for guaranteed order book fill. | `indicators.py` (Lines 260–280 & 350–375) |
| **10** | **Trailing SL on TP1 Hit** | Once TP1 hits (70% booked), remaining 30% runner SL is moved to exact Entry Price (Cost-to-Cost / Breakeven). | `executor.py` (Lines 1775–1820), `paper_trader.py` (Lines 620–640) |
| **11** | **Instant Retest Alerts** | When an EMA 26 / EMA 9 retest occurs, alert card is dispatched immediately to Telegram DM and Scanner Channel. | `telegram_bot.py` (Lines 281–295), `pullback_tracker.py` (L416) |
| **12** | **12-Hour Stale Runner Rule** | Closes 30% runner at market if 12 hours elapse after TP1 without hitting TP2 or Breakeven SL. | `executor.py` (Lines 1450–1475), `paper_trader.py` (L1210–1225) |
| **13** | **20-Hour Position Timeout** | Closes stagnant positions at market if active for 20 hours without deciding. | `executor.py` (Lines 1435–1455), `paper_trader.py` (L1230–1270) |
| **14** | **30-Min Entry Limit Expiry** | Unfilled entry limit orders are cancelled directly on exchange after 30 minutes. | `executor.py` (Lines 1290–1370) |
| **15** | **BTC Volatility Shock Shield** | If BTC 15M candle range >= 1.5% or body move >= 1.2%, altcoin scanning pauses for 30 minutes. | `indicators.py` (Lines 730–765), `main.py` (L876) |
| **16** | **15-Sec Auto-Healing Loop** | Background loop continuously queries exchange positions & orders; restores any missing SL/TP bracket orders. | `executor.py` (Lines 1372–1540), `main.py` (L706) |

---

## 📁 Source Code Files Included

1. `main.py`: Main scanning loop, candidate sequencer, 15s auto-healing monitor.
2. `config.py`: Environment configuration, leverage, margins, Telegram channel IDs.
3. `executor.py`: Exchange connector, 2% dynamic margin sizing, order bracket creation, trailing SL, timeouts.
4. `indicators.py`: Crossovers, 26 EMA SL + 0.25*ATR buffer, Liquidity Pools & 0.35% Order Block front-running.
5. `pullback_tracker.py`: EMA 26 & EMA 9 support/resistance retest tracking & instant alert triggers.
6. `paper_trader.py`: Virtual paper trading engine (₹1,00,000 INR baseline), identical execution math.
7. `telegram_bot.py`: Hardcoded Telegram routing, instant alert cards, chunked digests.
8. `fetcher.py`: Async multi-timeframe OHLCV data fetcher (Binance Futures REST API).
9. `state.py`: Atomic JSON persistence (`signal_state_v3.json`).
10. `market_cache.py`: Micro-gates and in-memory relative strength caching.
11. `filter_bot.py`: Telegram subscriber filtering and notification dispatcher.
12. `volatility_dashboard.py`: Pinned 4H market regime & volatility dashboard.
13. `user_prefs.py` & `message_sync.py`: User state persistence & broadcast sync.
14. `dynamic_config.py`: Dynamic runtime threshold tuning.
