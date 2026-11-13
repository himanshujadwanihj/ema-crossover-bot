"""
filter_bot.py — Interactive Filter & Clickable Watchlist Service for AlphaCryptoSignals Bot (@Alpha_Crypto_SignalBot).
Runs asynchronously alongside the main engine without impacting existing bots or channels.

Features:
  - Multi-Select Setup Types (Fresh Crossover & Live Retest)
  - Multi-Select Timeframes (15M, 1H, 4H, 1D, 1W)
  - Multi-Select Coin Groups (Core 10, Top 25 Alts, Commodities, US Stocks, Custom Watchlist)
  - Interactive Clickable Coin Picker for Custom Watchlist (Tap any coin to Add/Remove instantly)
  - Direct text entry: User can also simply type coin names in chat (e.g. 'BTC ETH SOL' or '+PEPE')
"""

import logging
import asyncio
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)
from config import FILTER_BOT_TOKEN
import user_prefs

logger = logging.getLogger("filter_bot")

_filter_app_instance = None

# Popular Trending & High-Beta coins for 1-Tap Quick Selection
POPULAR_CUSTOM_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "SUI", "PEPE",
    "NEAR", "RENDER", "TAO", "FET", "WIF", "AAVE", "LINK", "AVAX",
    "ENA", "APT", "INJ", "TIA", "SEI", "BONK", "FLOKI", "LTC"
]

def get_filter_menu_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    """Builds interactive toggle inline keyboard for user's filter settings."""
    prefs = user_prefs.get_user_prefs(chat_id)
    active_tfs = set(prefs.get("timeframes", list(user_prefs.DEFAULT_TIMEFRAMES)))
    active_sigs = set(prefs.get("signal_types", list(user_prefs.DEFAULT_SIGNAL_TYPES)))
    active_groups = set(prefs.get("coin_groups", list(user_prefs.DEFAULT_COIN_GROUPS)))

    # 1. Setup Types Multi-Select Buttons
    btn_cross = InlineKeyboardButton(
        "✅ Fresh Crossover" if "crossover" in active_sigs else "❌ Fresh Crossover",
        callback_data="sig_crossover"
    )
    btn_retest = InlineKeyboardButton(
        "✅ Live Retest" if "retest" in active_sigs else "❌ Live Retest",
        callback_data="sig_retest"
    )

    # Squeeze Alerts & Gold Scalp
    squeeze_active = prefs.get("squeeze_alerts", True)
    btn_squeeze = InlineKeyboardButton(
        "✅ 💥 Squeeze Alerts" if squeeze_active else "❌ 💥 Squeeze Alerts",
        callback_data="toggle_squeeze_alerts"
    )
    gold_scalp_active = prefs.get("gold_scalp_5m_15m", False)
    btn_gold_scalp = InlineKeyboardButton(
        "✅ 🥇 Gold Scalp" if gold_scalp_active else "🥇 Gold Scalp",
        callback_data="toggle_gold_scalp"
    )

    # 2. Confluence Stars Filter (3★ & 4★ vs 4★ Only)
    min_stars = prefs.get("min_stars", 3)
    btn_stars_3 = InlineKeyboardButton(
        "✅ 3★ & 4★ Stars" if min_stars == 3 else "3★ & 4★ Stars",
        callback_data="set_stars_3"
    )
    btn_stars_4 = InlineKeyboardButton(
        "✅ 4★ Stars Only" if min_stars == 4 else "4★ Stars Only",
        callback_data="set_stars_4"
    )

    # 4. Coin Groups Multi-Select Buttons
    btn_core = InlineKeyboardButton(
        "✅ Core 10 Majors" if "core_10" in active_groups else "❌ Core 10 Majors",
        callback_data="grp_core_10"
    )
    btn_alts = InlineKeyboardButton(
        "✅ Top 25 Alts" if "alts_25" in active_groups else "❌ Top 25 Alts",
        callback_data="grp_alts_25"
    )
    btn_comm = InlineKeyboardButton(
        "✅ Commodities" if "commodities" in active_groups else "❌ Commodities",
        callback_data="grp_commodities"
    )
    btn_us = InlineKeyboardButton(
        "✅ US Stocks" if "us_stocks" in active_groups else "❌ US Stocks",
        callback_data="grp_us_stocks"
    )
    btn_cust = InlineKeyboardButton(
        "✅ Custom Watchlist" if "custom" in active_groups else "❌ Custom Watchlist",
        callback_data="grp_custom"
    )
    btn_pick_cust = InlineKeyboardButton("🎯 Pick Custom Coins", callback_data="open_coin_picker")
    try:
        import fetcher
        live_futures_count = len(fetcher._cached_pairs) if fetcher._cached_pairs else 235
    except Exception:
        live_futures_count = 235
    btn_all_label = f"All Binance Futures ({live_futures_count}+ Coins, Cross Only)"
    btn_all_235 = InlineKeyboardButton(
        f"✅ {btn_all_label}" if "all_235" in active_groups else f"❌ {btn_all_label}",
        callback_data="grp_all_235"
    )

    # Master Alerts On/Off
    is_on = prefs.get("enabled", True)
    status_label = "🔔 Alerts: ACTIVE" if is_on else "🔕 Alerts: PAUSED"
    btn_status = InlineKeyboardButton(status_label, callback_data="toggle_enabled")

    keyboard = [
        [InlineKeyboardButton("📊 SETUP TYPES (Multi-Select)", callback_data="noop")],
        [btn_cross, btn_retest],
        [btn_squeeze, btn_gold_scalp],
        [InlineKeyboardButton("⭐ CONFLUENCE STARS (Min Quality)", callback_data="noop")],
        [btn_stars_3, btn_stars_4],
        [InlineKeyboardButton("🪙 COIN GROUPS (Multi-Select)", callback_data="noop")],
        [btn_core, btn_alts],
        [btn_comm, btn_us],
        [btn_cust, btn_pick_cust],
        [btn_all_235],
        [btn_status],
        [InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="refresh_menu")]
    ]

    return InlineKeyboardMarkup(keyboard)

def get_coin_picker_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    """Builds interactive coin buttons where user taps to add/remove coins from Custom Watchlist."""
    prefs = user_prefs.get_user_prefs(chat_id)
    user_coins = set([c.replace("/USDT", "").replace("/USD", "") for c in prefs.get("custom_coins", [])])

    rows = []
    current_row = []
    for coin in POPULAR_CUSTOM_COINS:
        is_sel = coin in user_coins
        label = f"✅ {coin}" if is_sel else f"+ {coin}"
        current_row.append(InlineKeyboardButton(label, callback_data=f"pick_{coin}"))
        if len(current_row) == 4:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    # If the user typed additional coins not in the 24 popular list, show interactive toggle buttons for them too!
    extra_typed_coins = sorted(list(user_coins - set(POPULAR_CUSTOM_COINS)))
    if extra_typed_coins:
        rows.append([InlineKeyboardButton("📝 User Added Coins (Tap to Remove):", callback_data="noop")])
        typed_row = []
        for coin in extra_typed_coins:
            typed_row.append(InlineKeyboardButton(f"✅ {coin}", callback_data=f"pick_{coin}"))
            if len(typed_row) == 3:
                rows.append(typed_row)
                typed_row = []
        if typed_row:
            rows.append(typed_row)

    rows.append([InlineKeyboardButton("✍️ Type Any Custom Coin in Chat", callback_data="prompt_type_coin")])
    rows.append([InlineKeyboardButton("🔙 Back to Filter Dashboard", callback_data="back_to_dashboard")])
    return InlineKeyboardMarkup(rows)

def build_dashboard_text(chat_id: str) -> str:
    prefs = user_prefs.get_user_prefs(chat_id)
    active_tfs_list = [t.upper() for t in prefs.get("timeframes", list(user_prefs.DEFAULT_TIMEFRAMES))]
    active_tfs_str = ", ".join(active_tfs_list) if active_tfs_list else "None"
    user_sigs = set(prefs.get("signal_types", list(user_prefs.DEFAULT_SIGNAL_TYPES)))
    active_sigs = []
    if "crossover" in user_sigs: active_sigs.append("Fresh Cross")
    if "retest" in user_sigs: active_sigs.append("Live Retest")
    if prefs.get("gold_scalp_5m_15m", False): active_sigs.append("🥇 Gold Scalp")
    active_groups = prefs.get("coin_groups", list(user_prefs.DEFAULT_COIN_GROUPS))
    status = "🟢 ACTIVE" if prefs.get("enabled", True) else "🔴 PAUSED"

    # Calculate exact total unique coins being monitored for this user
    from pullback_tracker import CORE_10, TOP_25_ALTS, COMMODITIES, US_EQUITIES
    total_active_coins_set = set()
    if "core_10" in active_groups:
        total_active_coins_set.update(CORE_10)
    if "alts_25" in active_groups:
        total_active_coins_set.update(TOP_25_ALTS)
    if "commodities" in active_groups:
        total_active_coins_set.update(COMMODITIES)
    if "us_stocks" in active_groups:
        total_active_coins_set.update(US_EQUITIES)
    if "custom" in active_groups:
        total_active_coins_set.update(prefs.get("custom_coins", []))

    group_labels = {
        "core_10": "Core 10 Majors",
        "alts_25": "Top 25 Alts",
        "commodities": "Commodities",
        "us_stocks": "US Equities",
        "custom": f"Custom (+{len(prefs.get('custom_coins', []))} coins)",
        "all_235": "All 235 Binance Universe (Cross Only)"
    }
    selected_groups_str = " + ".join([group_labels.get(g, g) for g in active_groups]) if active_groups else "None"

    if "all_235" in active_groups:
        coins_count_display = "<b>235+ Coins (Crossovers)</b>"
    else:
        coins_count_display = f"<b>{len(total_active_coins_set)} Coins</b>"

    custom_info = ""
    if "custom" in active_groups:
        coins = prefs.get("custom_coins", [])
        if coins:
            custom_info = f"\n<b>Custom Added:</b> <code>{', '.join(coins[:12])}</code>"
            if len(coins) > 12:
                custom_info += f" <i>(+{len(coins)-12} more)</i>"
        else:
            custom_info = "\n<i>(Custom active, list empty. Tap 'Pick Custom Coins' below to add)</i>"

    min_stars = prefs.get("min_stars", 3)
    stars_display = "⭐ 3★ & 4★ Stars" if min_stars == 3 else "🌟 4★ Stars Only"

    gold_scalp_active = prefs.get("gold_scalp_5m_15m", False)
    gold_scalp_display = "🟢 ON (5M & 15M Scalps)" if gold_scalp_active else "⚪ OFF (Standard HTF)"
    squeeze_active = prefs.get("squeeze_alerts", True)
    squeeze_display = "🟢 ON (Radar + Breakout)" if squeeze_active else "🔴 OFF (Muted)"

    import zoneinfo
    from datetime import datetime, timezone
    ist_tz = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(timezone.utc).astimezone(ist_tz)
    time_str = now_ist.strftime("%I:%M:%S %p IST")

    msg = (
        f"🎯 <b>ALPHA CRYPTO SIGNALS — CONTROL DASHBOARD</b>\n\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Active Setups:</b> <code>{', '.join(active_sigs) if active_sigs else 'None'}</code>\n"
        f"<b>Squeeze Alerts:</b> <code>{squeeze_display}</code>\n"
        f"<b>Confluence Filter:</b> <code>{stars_display}</code>\n"
        f"<b>Gold Scalp Filter:</b> <code>{gold_scalp_display}</code>\n"
        f"<b>Active Selection:</b> {selected_groups_str}\n"
        f"📊 <b>Total Coins Monitored:</b> {coins_count_display}\n"
        f"<b>Strategy:</b> 9/26 EMA Cross + Retest with RSI ✨\n"
        f"🕒 <b>Last Refreshed:</b> <code>{time_str}</code> (Live)\n"
        f"{custom_info}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Tap any button below to toggle that setting ON/OFF:</i>"
    )
    return msg

def build_coin_picker_text(chat_id: str) -> str:
    prefs = user_prefs.get_user_prefs(chat_id)
    coins = prefs.get("custom_coins", [])
    coins_str = ", ".join(coins) if coins else "None yet"
    return (
        f"🎯 <b>CUSTOM WATCHLIST BUILDER</b>\n\n"
        f"Tap any coin below to <b>Add</b> or <b>Remove</b> it instantly from your watchlist!\n\n"
        f"📋 <b>Your Active Custom Coins ({len(coins)}):</b>\n"
        f"<code>{coins_str}</code>\n\n"
        f"<i>💡 Want a coin not in the list? Simply type it in chat (e.g. <code>+LINK</code> or <code>SOL PEPE INJ</code>)!</i>"
    )

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exclusive Admin Command: View all registered users with exact 9-action management panel."""
    from config import TELEGRAM_CHAT_ID
    sender_id = str(update.effective_chat.id)
    admin_id = str(TELEGRAM_CHAT_ID).strip()

    if sender_id != admin_id:
        await update.message.reply_text(
            f"⛔ <b>Access Denied:</b> This command is restricted to the Admin.\n"
            f"Your ID: <code>{sender_id}</code> | Admin ID: <code>{admin_id}</code>",
            parse_mode="HTML"
        )
        return

    import admin_panel
    text, kb = admin_panel.build_admin_overview()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def cmd_quick_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 1-tap inline text commands like /rm_scan_12345, /ban_alpha_12345, /unban_cross_12345, etc."""
    from config import TELEGRAM_CHAT_ID
    sender_id = str(update.effective_chat.id)
    admin_id = str(TELEGRAM_CHAT_ID).strip()
    if sender_id != admin_id:
        return

    text = (update.message.text or "").strip()
    if not text.startswith("/"):
        return

    import re
    import admin_panel

    # Check unified 9-action pattern
    match = re.match(r"^/(rm|ban|unban)_(scan|scanner|cross|alpha)_(\d+)", text)
    if match:
        act_verb, platform, target_uid = match.groups()
        plat = "scan" if "scan" in platform else platform
        act_key = f"{act_verb}_{plat}"
        ok, res_msg = await admin_panel.execute_action(context.bot, act_key, target_uid)
        status_emoji = "✅" if ok else "⚠️"
        await update.message.reply_text(f"{status_emoji} <b>{res_msg}</b>", parse_mode="HTML")
        return

    # Fallback legacy /remove_12345, /ban_12345, /unban_12345
    parts = text[1:].split("_", 1)
    if len(parts) == 2:
        act, target_id = parts[0].lower(), parts[1].strip()
        if act == "remove":
            user_prefs.remove_user(target_id)
            await update.message.reply_text(f"✅ User <code>{target_id}</code> <b>REMOVED</b> from AlphaCryptoSignals.", parse_mode="HTML")
        elif act == "ban":
            user_prefs.ban_user(target_id)
            await update.message.reply_text(f"🚫 User <code>{target_id}</code> <b>PERMANENTLY BANNED</b> from Alpha.", parse_mode="HTML")
        elif act == "unban":
            user_prefs.unban_user(target_id)
            await update.message.reply_text(f"✅ User <code>{target_id}</code> <b>UNBANNED</b> in Alpha.", parse_mode="HTML")


async def cmd_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exclusive Admin Command: /removeuser <chat_id> or /ban <chat_id> or /unban <chat_id>"""
    from config import TELEGRAM_CHAT_ID
    sender_id = str(update.effective_chat.id)
    admin_id = str(TELEGRAM_CHAT_ID).strip()

    if sender_id != admin_id:
        await update.message.reply_text("⛔ <b>Access Denied:</b> This command is restricted to the Admin.", parse_mode="HTML")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "ℹ️ <b>Admin User Controls:</b>\n\n"
            "• <code>/removeuser &lt;user_id&gt;</code> &mdash; Remove from active subscribers\n"
            "• <code>/ban &lt;user_id&gt;</code> &mdash; Permanently ban user from receiving signals\n"
            "• <code>/unban &lt;user_id&gt;</code> &mdash; Unban user\n\n"
            "<i>💡 Type <code>/admin</code> to view all user IDs with 1-click remove buttons.</i>",
            parse_mode="HTML"
        )
        return

    target_id = str(args[0]).strip()
    cmd_text = (update.message.text or "").split()[0].lower()

    if "unban" in cmd_text:
        unbanned = user_prefs.unban_user(target_id)
        if unbanned:
            await update.message.reply_text(f"✅ User <code>{target_id}</code> has been <b>UNBANNED</b>.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"⚠️ User <code>{target_id}</code> was not in the ban list.", parse_mode="HTML")
        return

    is_ban = "ban" in cmd_text
    success = user_prefs.ban_user(target_id) if is_ban else user_prefs.remove_user(target_id)

    action_label = "PERMANENTLY BANNED" if is_ban else "REMOVED"
    if success:
        await update.message.reply_text(
            f"✅ <b>Action Successful!</b>\n\n"
            f"User <code>{target_id}</code> has been <b>{action_label}</b> from AlphaCryptoSignals.\n"
            f"They will no longer receive any alert dispatches.",
            parse_mode="HTML"
        )
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="⚠️ <b>Notice:</b> Your subscription to AlphaCryptoSignals has been discontinued by the administrator.",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(f"⚠️ User ID <code>{target_id}</code> was not found in active subscribers.", parse_mode="HTML")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_prefs.get_user_prefs(chat_id, user_obj=update.effective_user)
    text = (
        f"👋 <b>Welcome to AlphaCryptoSignals!</b>\n\n"
        f"Control exactly what signals you want to receive:\n"
        f"• <b>Setups:</b> Fresh Crossovers and/or Live Retests\n"
        f"• <b>Confluence Quality:</b> 3★ & 4★ Stars or 4★ Only\n"
        f"• <b>Coins:</b> Core 10, Top 25 Alts, Commodities, US Stocks, or Custom\n"
        f"• <b>Custom Coins:</b> Tap coin buttons or simply type coin names in chat!"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    await update.message.reply_text(
        build_dashboard_text(chat_id),
        reply_markup=get_filter_menu_keyboard(chat_id),
        parse_mode="HTML"
    )

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_prefs.get_user_prefs(chat_id, user_obj=update.effective_user)
    await update.message.reply_text(
        build_dashboard_text(chat_id),
        reply_markup=get_filter_menu_keyboard(chat_id),
        parse_mode="HTML"
    )

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = context.args
    if not args:
        await update.message.reply_text(
            build_coin_picker_text(chat_id),
            reply_markup=get_coin_picker_keyboard(chat_id),
            parse_mode="HTML"
        )
        return

    added = []
    removed = []
    for arg in args:
        item = arg.strip().upper()
        if item.startswith("+"):
            coin = item[1:]
            user_prefs.add_custom_coins(chat_id, [coin])
            added.append(coin)
        elif item.startswith("-"):
            coin = item[1:]
            user_prefs.remove_custom_coin(chat_id, coin)
            removed.append(coin)
        else:
            user_prefs.add_custom_coins(chat_id, [item])
            added.append(item)

    resp = "✅ <b>Watchlist Updated!</b>\n"
    if added: resp += f"➕ Added: <code>{', '.join(added)}</code>\n"
    if removed: resp += f"➖ Removed: <code>{', '.join(removed)}</code>\n"

    prefs = user_prefs.get_user_prefs(chat_id)
    resp += f"\nTotal in Custom Watchlist: <b>{len(prefs.get('custom_coins', []))} coins</b>"
    await update.message.reply_text(resp, parse_mode="HTML")

async def on_user_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows user to simply type coin names in chat to add/remove them directly."""
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if text.startswith("/"):
        return

    chat_id = str(update.effective_chat.id)
    tokens = text.replace(",", " ").split()
    added = []
    removed = []

    for t in tokens:
        clean = t.upper().replace("/USDT", "").replace("/USD", "")
        if clean.startswith("-"):
            coin = clean[1:]
            user_prefs.remove_custom_coin(chat_id, coin)
            removed.append(coin)
        elif clean.startswith("+"):
            coin = clean[1:]
            user_prefs.add_custom_coins(chat_id, [coin])
            added.append(coin)
        else:
            # If word is like a ticker (2 to 10 letters)
            if clean.isalnum() and 2 <= len(clean) <= 10:
                user_prefs.add_custom_coins(chat_id, [clean])
                added.append(clean)

    if added or removed:
        prefs = user_prefs.get_user_prefs(chat_id)
        all_custom = prefs.get("custom_coins", [])
        resp = "✅ <b>Custom Watchlist Updated!</b>\n"
        if added: resp += f"➕ <b>Added:</b> <code>{', '.join(added)}</code>\n"
        if removed: resp += f"➖ <b>Removed:</b> <code>{', '.join(removed)}</code>\n"
        resp += f"\n📋 <b>All Monitored Custom Coins ({len(all_custom)}):</b>\n"
        resp += f"<code>{', '.join(all_custom) if all_custom else 'None'}</code>\n"

        # Provide convenient inline navigation buttons
        nav_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Open Coin Picker", callback_data="open_coin_picker")],
            [InlineKeyboardButton("📊 Back to Filter Dashboard", callback_data="back_to_dashboard")]
        ])
        await msg.reply_text(resp, reply_markup=nav_keyboard, parse_mode="HTML")

async def on_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = str(query.message.chat_id)

    if data == "noop":
        return

    # ── Unified Admin Panel Action Callbacks ─────────────────────────
    if data.startswith("act_"):
        from config import TELEGRAM_CHAT_ID
        admin_id = str(TELEGRAM_CHAT_ID).strip()
        if chat_id != admin_id:
            await query.answer("Access Denied.", show_alert=True)
            return

        import admin_panel
        if data == "act_refresh" or data == "act_back_users":
            text, kb = admin_panel.build_admin_overview()
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if data == "act_view_banned":
            text, kb = admin_panel.build_banned_users_overview()
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if data.startswith("act_user_"):
            target_uid = data.replace("act_user_", "").strip()
            text, kb = admin_panel.build_user_action_menu(target_uid)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        parts = data.split("_")
        if len(parts) >= 4:
            act_key = f"{parts[1]}_{parts[2]}"
            target_uid = parts[3]
            ok, res_msg = await admin_panel.execute_action(context.bot, act_key, target_uid)
            await query.answer(res_msg, show_alert=True)
            text, kb = admin_panel.build_user_action_menu(target_uid)
            status_emoji = "✅" if ok else "⚠️"
            await query.edit_message_text(f"{status_emoji} <b>{res_msg}</b>\n\n" + text, reply_markup=kb, parse_mode="HTML")
            return


    # ── Admin User Removal Callbacks ──────────────────────────────────
    if data.startswith("adm_rm_"):
        from config import TELEGRAM_CHAT_ID
        admin_id = str(TELEGRAM_CHAT_ID).strip()
        if chat_id != admin_id:
            await query.answer("Access Denied.", show_alert=True)
            return
        target_uid = data.replace("adm_rm_", "").strip()
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚠️ Confirm Remove", callback_data=f"adm_cfm_rm_{target_uid}"),
                InlineKeyboardButton("🚫 Ban", callback_data=f"adm_cfm_ban_{target_uid}")
            ],
            [InlineKeyboardButton("🔙 Cancel", callback_data="adm_cancel_rm")]
        ])
        await query.edit_message_text(
            f"❓ <b>Confirm Action for User:</b> <code>{target_uid}</code>\n\n"
            f"Choose whether to remove them from active subscribers or permanently ban them:",
            reply_markup=confirm_kb,
            parse_mode="HTML"
        )
        return

    elif data.startswith("adm_cfm_rm_"):
        from config import TELEGRAM_CHAT_ID
        admin_id = str(TELEGRAM_CHAT_ID).strip()
        if chat_id != admin_id:
            await query.answer("Access Denied.", show_alert=True)
            return
        target_uid = data.replace("adm_cfm_rm_", "").strip()
        user_prefs.remove_user(target_uid)
        await query.edit_message_text(
            f"✅ User <code>{target_uid}</code> has been <b>REMOVED</b> from AlphaCryptoSignals.",
            parse_mode="HTML"
        )
        try:
            await query.bot.send_message(
                chat_id=target_uid,
                text="⚠️ <b>Notice:</b> Your subscription to AlphaCryptoSignals has been removed by the administrator.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    elif data.startswith("adm_cfm_ban_"):
        from config import TELEGRAM_CHAT_ID
        admin_id = str(TELEGRAM_CHAT_ID).strip()
        if chat_id != admin_id:
            await query.answer("Access Denied.", show_alert=True)
            return
        target_uid = data.replace("adm_cfm_ban_", "").strip()
        user_prefs.ban_user(target_uid)
        await query.edit_message_text(
            f"🚫 User <code>{target_uid}</code> has been <b>PERMANENTLY BANNED</b> from AlphaCryptoSignals.",
            parse_mode="HTML"
        )
        try:
            await query.bot.send_message(
                chat_id=target_uid,
                text="⚠️ <b>Notice:</b> Your account has been banned from AlphaCryptoSignals by the administrator.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    elif data == "adm_cancel_rm":
        await query.edit_message_text("🔙 User removal action cancelled.", parse_mode="HTML")
        return

    if data.startswith("tf_"):
        await query.answer("Timeframes are managed automatically (1H+ & 30M Early Cross).")
        return
    elif data.startswith("sig_"):
        sig = data.replace("sig_", "")
        user_prefs.toggle_signal_type(chat_id, sig)
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data.startswith("grp_"):
        grp = data.replace("grp_", "")
        user_prefs.toggle_coin_group(chat_id, grp)
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "set_stars_3":
        user_prefs.set_min_stars(chat_id, 3)
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "set_stars_4":
        user_prefs.set_min_stars(chat_id, 4)
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "toggle_gold_scalp":
        user_prefs.toggle_gold_scalp(chat_id)
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "toggle_squeeze_alerts":
        user_prefs.toggle_squeeze_alerts(chat_id)
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "toggle_squeeze_only":
        user_prefs.toggle_squeeze_only(chat_id)
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "toggle_enabled":
        u = user_prefs.get_user_prefs(chat_id)
        user_prefs.update_user_pref(chat_id, "enabled", not u.get("enabled", True))
        await query.edit_message_text(
            build_dashboard_text(chat_id),
            reply_markup=get_filter_menu_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "open_coin_picker":
        await query.edit_message_text(
            build_coin_picker_text(chat_id),
            reply_markup=get_coin_picker_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data.startswith("pick_"):
        coin = data.replace("pick_", "")
        sym = f"{coin}/USDT"
        u = user_prefs.get_user_prefs(chat_id)
        current = set(u.get("custom_coins", []))
        if sym in current:
            user_prefs.remove_custom_coin(chat_id, coin)
        else:
            user_prefs.add_custom_coins(chat_id, [coin])
        await query.edit_message_text(
            build_coin_picker_text(chat_id),
            reply_markup=get_coin_picker_keyboard(chat_id),
            parse_mode="HTML"
        )
    elif data == "prompt_type_coin":
        await query.message.reply_text(
            "✍️ <b>Type coin names in chat now</b> (e.g. <code>SUI PEPE RENDER NEAR</code>):\n"
            "Bot will instantly add them to your Custom Watchlist!",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True)
        )
    elif data == "back_to_dashboard" or data == "refresh_menu":
        try:
            await query.answer("✅ Dashboard Refreshed!", show_alert=False)
        except Exception:
            pass
        try:
            await query.edit_message_text(
                build_dashboard_text(chat_id),
                reply_markup=get_filter_menu_keyboard(chat_id),
                parse_mode="HTML"
            )
        except Exception as edit_e:
            if "Message is not modified" in str(edit_e):
                try:
                    await query.answer("✅ Dashboard Refreshed!", show_alert=False)
                except Exception:
                    pass
            else:
                logger.error(f"Error refreshing dashboard: {edit_e}")

async def dispatch_filtered_alert(alert_data: dict, formatted_message: str):
    """
    Dispatches alerts to all registered subscribers whose multi-selected filter settings match.
    Automatically links admin message ID with all recipient IDs for unified deletion sync.
    """
    subscribers = user_prefs.get_all_subscribers()
    if not subscribers or not _filter_app_instance:
        return

    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")
    admin_msg_id = None
    recipients = []

    # Send to Admin ONLY IF matches Admin's chosen filter preferences
    admin_allowed = user_prefs.is_alert_allowed_for_user(admin_id, alert_data)
    if admin_allowed:
        try:
            sent_admin = await _filter_app_instance.bot.send_message(
                chat_id=admin_id,
                text=formatted_message,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            admin_msg_id = sent_admin.message_id
        except Exception as e_adm:
            logger.warning(f"Failed to dispatch alert to admin {admin_id}: {e_adm}")

    # Dispatch to all other matching subscribers
    for cid in subscribers:
        if str(cid) == admin_id:
            continue
        try:
            if user_prefs.is_alert_allowed_for_user(cid, alert_data):
                m = await _filter_app_instance.bot.send_message(
                    chat_id=cid,
                    text=formatted_message,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                recipients.append({"bot_type": "filter", "chat_id": str(cid), "message_id": m.message_id})
        except Exception as e:
            logger.error(f"Failed to dispatch filtered alert to {cid}: {e}")

    if admin_msg_id and recipients:
        import message_sync
        sym = alert_data.get("symbol", "")
        tf = alert_data.get("tf_label", "")
        message_sync.record_broadcast_group("filter", admin_msg_id, recipients, desc=f"{sym} {tf}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err_str = str(context.error or "")
    if "Message is not modified" in err_str:
        logger.debug(f"Ignored Telegram Message is not modified error: {err_str}")
        return

    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "✅ <b>Dashboard Refreshed:</b> Tap /filter to view your control dashboard.",
                parse_mode="HTML"
            )
        except Exception:
            pass

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: broadcasts a message to all registered filter bot subscribers."""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")
    if user_id != admin_id:
        await update.message.reply_text("⛔ Unauthorized. Admin access only.")
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        text = (
            "📢 <b>IMPORTANT NOTIFICATION: FILTER SETTINGS UPDATE</b>\n\n"
            "Hey Traders! 🚀\n\n"
            "We have upgraded our signal engine with <b>Multi-Stage Trailing SL & CVD Absorption Filters</b>.\n\n"
            "⚠️ <b>Kindly re-select your filters</b> on your dashboard to ensure you receive all active signals:\n\n"
            "👉 Tap <b>/filter</b> to open your control panel\n"
            "1️⃣ <b>Setup Types:</b> Toggle Crossovers & Retests\n"
            "2️⃣ <b>Coin Groups:</b> Select your preferred coin baskets\n"
            "3️⃣ <b>Confluence:</b> Confirm your minimum Star rating\n\n"
            "<i>Tap /filter now to update your alerts!</i> 🎯"
        )

    subscribers = user_prefs.get_all_subscribers()
    sent_count = 0
    fail_count = 0
    for cid in subscribers:
        try:
            await context.bot.send_message(chat_id=cid, text=text, parse_mode="HTML", disable_web_page_preview=True)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Failed broadcast to {cid}: {e}")
            fail_count += 1

    await update.message.reply_text(f"✅ Broadcast complete!\nDelivered to: {sent_count} users\nFailed: {fail_count} users")

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: deletes message across all users and channels."""
    user_id = str(update.effective_user.id if update.effective_user else "")
    from config import TELEGRAM_CHAT_ID
    admin_id = str(TELEGRAM_CHAT_ID or "771305593")
    if user_id != admin_id:
        return

    import message_sync
    # 1. If replied to a message, delete that message for everyone
    if update.message and update.message.reply_to_message:
        replied_mid = update.message.reply_to_message.message_id
        try:
            await update.message.reply_to_message.delete()
        except Exception:
            pass
        try:
            await update.message.delete()
        except Exception:
            pass
        found = await message_sync.delete_by_admin_message_id(replied_mid)
        if not found:
            await message_sync.delete_last_n_broadcasts(1)
        return

    # 2. If parameter given like /del 2, delete last 2
    count = 1
    if context.args and context.args[0].isdigit():
        count = int(context.args[0])
    try:
        await update.message.delete()
    except Exception:
        pass
    await message_sync.delete_last_n_broadcasts(count)

async def start_filter_bot_service():
    """Starts the filter bot application polling asynchronously."""
    global _filter_app_instance
    if not FILTER_BOT_TOKEN:
        logger.warning("FILTER_BOT_TOKEN not provided, filter bot service skipped.")
        return

    try:
        _filter_app_instance = Application.builder().token(FILTER_BOT_TOKEN).build()
        _filter_app_instance.add_error_handler(error_handler)
        _filter_app_instance.add_handler(CommandHandler(["start", "help"], cmd_start))
        _filter_app_instance.add_handler(CommandHandler(["filter", "settings", "menu"], cmd_filter))
        _filter_app_instance.add_handler(CommandHandler(["watch", "watchlist"], cmd_watch))
        _filter_app_instance.add_handler(CommandHandler(["admin", "users", "subscribers"], cmd_admin))
        _filter_app_instance.add_handler(CommandHandler(["broadcast"], cmd_broadcast))
        _filter_app_instance.add_handler(CommandHandler(["removeuser", "deleteuser", "kick", "ban", "unban"], cmd_remove_user))
        _filter_app_instance.add_handler(CommandHandler(["del", "delete", "purge"], cmd_delete))
        _filter_app_instance.add_handler(CallbackQueryHandler(on_button_callback))
        _filter_app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_user_text_message))

        await _filter_app_instance.initialize()
        await _filter_app_instance.start()
        await _filter_app_instance.updater.start_polling(drop_pending_updates=True)
        logger.info("🚀 New Filter Bot Service (@Alpha_Crypto_SignalBot) started polling successfully!")

    except Exception as e:
        logger.error(f"Failed to start Filter Bot Service: {e}")

async def stop_filter_bot_service():
    global _filter_app_instance
    if _filter_app_instance:
        try:
            await _filter_app_instance.updater.stop()
            await _filter_app_instance.stop()
            await _filter_app_instance.shutdown()
            logger.info("Filter Bot Service stopped cleanly.")
        except Exception as e:
            logger.error(f"Error stopping Filter Bot: {e}")
