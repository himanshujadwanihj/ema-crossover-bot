# -*- coding: utf-8 -*-
"""
web_dashboard.py — Real-time HTTP dashboard & live performance API for AlphaSniper.
Serves index.html on GET / and live performance JSON on GET /api/live-stats.
Includes CORS headers so both Railway domain and GitHub Pages can query live data.
"""

import os
import json
import logging
from datetime import datetime, timezone
import zoneinfo
from aiohttp import web

logger = logging.getLogger("web_dashboard")

def get_live_performance_payload() -> dict:
    """Extracts 100% genuine, unvarnished performance and trade lifecycle data."""
    try:
        from paper_trader import load_wallet, INITIAL_CAPITAL, NOTIONAL_INR, USD_INR_RATE
        wallet = load_wallet()
    except Exception as e:
        logger.error(f"Error loading wallet for live dashboard: {e}")
        wallet = {}
        INITIAL_CAPITAL = 100000.0
        USD_INR_RATE = 88.50

    balance = float(wallet.get("balance", 100000.0) or 100000.0)
    initial_cap = float(wallet.get("initial_capital", 100000.0) or 100000.0)
    total_roi = ((balance - initial_cap) / initial_cap) * 100.0
    fees_paid = float(wallet.get("total_fees_paid", 0.0) or 0.0)

    active = list(wallet.get("active_positions", []))
    resting = list(wallet.get("resting_orders", []))
    closed = list(wallet.get("closed_trades", []))

    CANCELLED_STATUSES = {
        "CANCELLED_30MIN_EXPIRED", "CANCELLED_20MIN_EXPIRED",
        "CANCELLED_SIGNAL_REVERSAL", "CANCELLED_UNFILLED", "PENDING_LIMIT"
    }

    # Only genuinely filled and executed trades count toward Win/Loss Scoreboard
    exec_trades = [
        t for t in closed
        if t.get("status", "") not in CANCELLED_STATUSES
        and "CANCEL" not in str(t.get("status", ""))
        and t.get("symbol") != "PROFIT_INJECTION"
        and float(t.get("cumulative_pnl", 0.0) or 0.0) != 0.0
    ]

    wins = sum(1 for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) > 0)
    losses = sum(1 for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) < 0)
    total_trades = len(exec_trades)
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_wins = sum(float(t.get("cumulative_pnl", 0.0) or 0.0) for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) > 0)
    gross_losses = sum(abs(float(t.get("cumulative_pnl", 0.0) or 0.0)) for t in exec_trades if float(t.get("cumulative_pnl", 0.0) or 0.0) < 0)
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)

    # Real breakdown of cancelled / expired limit orders (30-min rule)
    cancelled_30m = [
        t for t in closed
        if "30MIN" in str(t.get("status", "")) or "20MIN" in str(t.get("status", "")) or t.get("status") == "CANCELLED_UNFILLED"
    ]
    cancelled_reversals = [
        t for t in closed
        if "REVERSAL" in str(t.get("status", ""))
    ]

    now_utc = datetime.now(timezone.utc)
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = now_utc.astimezone(ist_tz)

    # Format active positions cleanly
    formatted_active = []
    for p in active:
        sym = p.get("symbol", "")
        sig = p.get("signal", "BUY")
        entry = float(p.get("entry_limit", 0.0) or 0.0)
        sl = float(p.get("current_sl") or p.get("sl", 0.0) or 0.0)
        tp1 = float(p.get("tp1", 0.0) or 0.0)
        tp2 = float(p.get("tp2", 0.0) or p.get("tp3", 0.0) or 0.0)
        filled_at = p.get("filled_at", "")
        formatted_active.append({
            "symbol": sym,
            "signal": sig,
            "entry": entry,
            "current_sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "filled_at": filled_at
        })

    # Format resting limit orders cleanly
    formatted_resting = []
    for r in resting:
        sym = r.get("symbol", "")
        sig = r.get("signal", "BUY")
        entry = float(r.get("entry_limit", 0.0) or 0.0)
        created_at = r.get("created_at", "")
        rem_mins = 30
        if created_at:
            try:
                c_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if c_dt.tzinfo is None: c_dt = c_dt.replace(tzinfo=timezone.utc)
                elapsed_m = int((now_utc - c_dt).total_seconds() / 60)
                rem_mins = max(0, 30 - elapsed_m)
            except Exception:
                pass
        formatted_resting.append({
            "symbol": sym,
            "signal": sig,
            "entry": entry,
            "created_at": created_at,
            "minutes_remaining": rem_mins
        })

    # Format recent closed trades
    formatted_closed = []
    for t in exec_trades[-12:][::-1]:
        sym = t.get("symbol", "")
        sig = t.get("signal", "")
        entry = float(t.get("entry_limit", 0.0) or 0.0)
        status = t.get("status", "")
        pnl = float(t.get("cumulative_pnl", 0.0) or 0.0)
        fee = float(t.get("fee_paid", 0.0) or 0.0)
        closed_at = t.get("closed_at", "")
        formatted_closed.append({
            "symbol": sym,
            "signal": sig,
            "entry": entry,
            "status": status,
            "pnl_inr": round(pnl, 2),
            "fee_inr": round(fee, 2),
            "closed_at": closed_at
        })

    # Format recent cancelled 30m orders
    formatted_cancelled = []
    for c in cancelled_30m[-12:][::-1]:
        sym = c.get("symbol", "")
        sig = c.get("signal", "")
        entry = float(c.get("entry_limit", 0.0) or 0.0)
        closed_at = c.get("closed_at", "")
        formatted_cancelled.append({
            "symbol": sym,
            "signal": sig,
            "entry": entry,
            "reason": "Price did not retest limit entry within 30 minutes (0 Risk / Order Cancelled)",
            "cancelled_at": closed_at
        })

    return {
        "status": "online",
        "engine": "AlphaSniper 9/26 EMA Momentum + 1H Retest Engine",
        "timestamp_utc": now_utc.isoformat(),
        "timestamp_ist": now_ist.strftime("%d %b %Y, %I:%M:%S %p IST"),
        "wallet": {
            "balance_inr": balance,
            "initial_capital_inr": initial_cap,
            "total_roi_pct": round(total_roi, 2),
            "fees_paid_inr": round(fees_paid, 2)
        },
        "scorecard": {
            "wins": wins,
            "losses": losses,
            "total_executed": total_trades,
            "win_rate_pct": round(win_rate, 1),
            "gross_wins_inr": round(gross_wins, 2),
            "gross_losses_inr": round(gross_losses, 2),
            "profit_factor": round(profit_factor, 2)
        },
        "lifecycle_funnel": {
            "active_in_trade_count": len(active),
            "resting_limits_waiting_count": len(resting),
            "cancelled_30m_unfilled_count": len(cancelled_30m),
            "cancelled_signal_reversals_count": len(cancelled_reversals),
            "truth_audit": "100% Genuine Metrics: Bot places limit orders at retest levels. When price runs away without pulling back, the 30-min rule auto-cancels the order. Only filled limit orders become Executed Trades."
        },
        "active_positions": formatted_active,
        "resting_orders": formatted_resting,
        "recent_executed_trades": formatted_closed,
        "recent_cancelled_orders": formatted_cancelled
    }


async def api_live_stats_handler(request: web.Request) -> web.Response:
    """Returns real-time performance JSON with CORS enabled."""
    data = get_live_performance_payload()
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-cache, no-store, must-revalidate"
    }
    return web.json_response(data, headers=headers)


async def index_handler(request: web.Request) -> web.Response:
    """Serves index.html directly from local directory."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(base_dir, "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return web.Response(text=html_content, content_type="text/html")
    return web.Response(text="<h1>AlphaSniper Live Engine Online</h1><p>index.html not found</p>", content_type="text/html")


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})



async def ip_handler(request: web.Request) -> web.Response:
    """Queries public IP of the Railway container."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.ipify.org?format=json", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                return web.json_response({
                    "ip": data.get("ip"),
                    "provider": "Railway Cloud Outbound",
                    "note": "Dynamic IP: changes upon container restart or redeploy"
                })
    except Exception as e:
        return web.json_response({"error": str(e)})


def setup_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/client_presentation_dashboard.html", index_handler)
    app.router.add_get("/api/live-stats", api_live_stats_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ip", ip_handler)
    app.router.add_get("/api/ip", ip_handler)
    return app
