"""
signal_analyser.py — AlphaSniper Auto Signal Analyser
======================================================
Automatically evaluates every new signal and generates a confidence
score (0-100) + human-readable AI analysis block.

Scoring Criteria:
  1. RSI Zone             (0-20 pts)
  2. ADX Trend Strength   (0-15 pts)
  3. Volume Surge         (0-15 pts)
  4. HTF Confluence       (0-20 pts)
  5. MACD Alignment       (0-10 pts)
  6. Market Session       (0-10 pts)
  7. Candle Body Quality  (0-10 pts)
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("signal_analyser")

# Session windows (UTC hours)
_SESSIONS = {
    "🇬🇧 London Open":    (7, 10),
    "🇺🇸 New York Open":  (13, 16),
    "🇯🇵 Asian Session":  (0, 4),
    "🌙 Dead Zone":        (3, 6),
}

def _get_market_session() -> tuple[str, bool]:
    """Returns (session_label, is_good_session)."""
    h = datetime.now(timezone.utc).hour
    if 7 <= h < 10:
        return "🇬🇧 London Open", True
    if 13 <= h < 16:
        return "🇺🇸 New York Open", True
    if 0 <= h < 4:
        return "🇯🇵 Asian Session", True
    if 10 <= h < 13:
        return "🌍 London-NY Overlap", True
    if 3 <= h < 7:
        return "🌙 Dead Zone (Low Liq)", False
    return "🕐 Off-Hours", False


def _rsi_score(rsi: float, signal: str) -> tuple[int, str]:
    """Score RSI zone (0-20). Returns (score, label)."""
    if rsi is None or rsi == 0:
        return 10, "RSI: N/A"
    if signal == "BUY":
        if 40 <= rsi <= 60:
            return 20, f"RSI {rsi:.0f} ✅ Healthy zone"
        elif 30 <= rsi < 40:
            return 18, f"RSI {rsi:.0f} ✅ Oversold bounce"
        elif 60 < rsi <= 70:
            return 12, f"RSI {rsi:.0f} ⚠️ Slightly hot"
        elif rsi < 30:
            return 15, f"RSI {rsi:.0f} 🔥 Extreme oversold"
        else:
            return 4, f"RSI {rsi:.0f} ❌ Overbought"
    else:  # SELL
        if 40 <= rsi <= 60:
            return 20, f"RSI {rsi:.0f} ✅ Healthy zone"
        elif 60 < rsi <= 70:
            return 18, f"RSI {rsi:.0f} ✅ Overbought fade"
        elif 30 <= rsi < 40:
            return 12, f"RSI {rsi:.0f} ⚠️ Slightly cold"
        elif rsi > 70:
            return 15, f"RSI {rsi:.0f} 🔥 Extreme overbought"
        else:
            return 4, f"RSI {rsi:.0f} ❌ Oversold"


def _adx_score(adx: float) -> tuple[int, str]:
    """Score ADX trend strength (0-15)."""
    if adx is None or adx == 0:
        return 7, "ADX: N/A"
    if adx >= 35:
        return 15, f"ADX {adx:.0f} 💪 Strong trend"
    elif adx >= 25:
        return 12, f"ADX {adx:.0f} ✅ Good trend"
    elif adx >= 20:
        return 8, f"ADX {adx:.0f} ⚠️ Weak trend"
    else:
        return 3, f"ADX {adx:.0f} ❌ Ranging market"


def _volume_score(vol: float, vol_sma: float) -> tuple[int, str]:
    """Score volume surge (0-15)."""
    if vol is None or vol_sma is None or vol_sma == 0:
        return 7, "Volume: N/A"
    ratio = vol / vol_sma
    if ratio >= 2.5:
        return 15, f"Vol {ratio:.1f}x avg 🚀 Huge spike"
    elif ratio >= 2.0:
        return 13, f"Vol {ratio:.1f}x avg ✅ Strong surge"
    elif ratio >= 1.5:
        return 10, f"Vol {ratio:.1f}x avg ✅ Above avg"
    elif ratio >= 1.0:
        return 6, f"Vol {ratio:.1f}x avg ⚠️ Normal"
    else:
        return 2, f"Vol {ratio:.1f}x avg ❌ Below avg"


def _htf_score(aligned_count: int, signal: str) -> tuple[int, str]:
    """Score higher timeframe confluence (0-20)."""
    if aligned_count >= 4:
        return 20, f"HTF {aligned_count}/4 ✅ All aligned"
    elif aligned_count == 3:
        return 16, f"HTF {aligned_count}/4 ✅ Strong"
    elif aligned_count == 2:
        return 10, f"HTF {aligned_count}/4 ⚠️ Moderate"
    elif aligned_count == 1:
        return 5, f"HTF {aligned_count}/4 ⚠️ Weak"
    else:
        return 0, "HTF 0/4 ❌ No alignment"


def _macd_score(macd_line: float, macd_signal: float, signal: str) -> tuple[int, str]:
    """Score MACD alignment (0-10)."""
    if macd_line is None or macd_signal is None:
        return 5, "MACD: N/A"
    if signal == "BUY":
        if macd_line > macd_signal and macd_line > 0:
            return 10, "MACD ✅ Bullish above zero"
        elif macd_line > macd_signal:
            return 7, "MACD ✅ Bullish cross"
        else:
            return 2, "MACD ❌ Not aligned"
    else:
        if macd_line < macd_signal and macd_line < 0:
            return 10, "MACD ✅ Bearish below zero"
        elif macd_line < macd_signal:
            return 7, "MACD ✅ Bearish cross"
        else:
            return 2, "MACD ❌ Not aligned"


def _session_score(is_good: bool) -> tuple[int, str]:
    """Score market session timing (0-10)."""
    return (10, "Session: Active ✅") if is_good else (3, "Session: Low Liq ⚠️")


def _candle_body_score(body_ratio: float | None) -> tuple[int, str]:
    """Score candle body quality (0-10)."""
    if body_ratio is None:
        return 5, "Body: N/A"
    pct = body_ratio * 100
    if pct >= 70:
        return 10, f"Body {pct:.0f}% 💪 Conviction candle"
    elif pct >= 50:
        return 8, f"Body {pct:.0f}% ✅ Solid"
    elif pct >= 30:
        return 5, f"Body {pct:.0f}% ⚠️ Medium"
    else:
        return 2, f"Body {pct:.0f}% ❌ Weak/Wick-heavy"


def _verdict(score: int) -> str:
    if score >= 80:
        return "🟢 HIGH CONFIDENCE — Take Trade"
    elif score >= 65:
        return "🟡 MODERATE — Proceed Carefully"
    elif score >= 50:
        return "🟠 WEAK — Wait for Better Entry"
    else:
        return "🔴 SKIP — Low Quality Signal"


def _star_rating(score: int) -> str:
    stars = round(score / 20)
    return "⭐" * stars + "☆" * (5 - stars)


def analyse_signal(
    symbol: str,
    signal: str,            # "BUY" or "SELL"
    rsi: float = None,
    adx: float = None,
    volume: float = None,
    volume_sma: float = None,
    macd_line: float = None,
    macd_signal_val: float = None,
    body_ratio: float = None,
    aligned_count: int = 0,
    tf_label: str = "1H",
    cross_type: str = "",
) -> dict:
    """
    Main entry point. Returns analysis dict with score, verdict, and
    a pre-formatted Telegram HTML block.
    """
    try:
        session_label, is_good_session = _get_market_session()

        # --- Individual component scores ---
        s_rsi,     l_rsi     = _rsi_score(rsi, signal)
        s_adx,     l_adx     = _adx_score(adx)
        s_vol,     l_vol     = _volume_score(volume, volume_sma)
        s_htf,     l_htf     = _htf_score(aligned_count, signal)
        s_macd,    l_macd    = _macd_score(macd_line, macd_signal_val, signal)
        s_session, l_session = _session_score(is_good_session)
        s_body,    l_body    = _candle_body_score(body_ratio)

        total = s_rsi + s_adx + s_vol + s_htf + s_macd + s_session + s_body
        total = min(total, 100)

        verdict = _verdict(total)
        stars   = _star_rating(total)

        # --- Build Telegram-formatted analysis block ---
        direction_arrow = "📈" if signal == "BUY" else "📉"

        analysis_block = (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 <b>AI SIGNAL ANALYSIS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Score:</b> {stars} <b>({total}/100)</b>\n"
            f"<b>Session:</b> {session_label}\n"
            f"\n"
            f"├ {l_rsi}\n"
            f"├ {l_adx}\n"
            f"├ {l_vol}\n"
            f"├ {l_htf}\n"
            f"├ {l_macd}\n"
            f"├ {l_body}\n"
            f"└ {l_session}\n"
            f"\n"
            f"<b>VERDICT: {verdict}</b>"
        )

        return {
            "score":          total,
            "verdict":        verdict,
            "stars":          stars,
            "session":        session_label,
            "analysis_block": analysis_block,
            "components": {
                "rsi":     s_rsi,
                "adx":     s_adx,
                "volume":  s_vol,
                "htf":     s_htf,
                "macd":    s_macd,
                "session": s_session,
                "body":    s_body,
            }
        }

    except Exception as e:
        logger.warning(f"signal_analyser error for {symbol}: {e}")
        return {
            "score":          50,
            "verdict":        "⚠️ Analysis Failed",
            "stars":          "⭐⭐⭐☆☆",
            "session":        "Unknown",
            "analysis_block": "\n\n🧠 <b>AI Analysis:</b> Temporarily unavailable.",
            "components":     {}
        }
