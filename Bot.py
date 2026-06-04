#!/usr/bin/env python3
"""
EVALON AUTO-POST BOT v3
- PostgreSQL database (Render)
- No duplicate posts per day
- Post history via /history
- Admin controls: /pause /resume /schedule
- Inline buttons + watermark
"""

import os, random, asyncio, logging, threading, io, json
from datetime import datetime, timezone, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN    = os.environ.get("AUTOPOST_BOT_TOKEN")
CHANNEL_ID   = "-1003403743370"
ADMIN_ID     = 8535925646
DATABASE_URL = os.environ.get("DATABASE_URL")
DATA_DIR     = os.environ.get("DATA_DIR", "/tmp/autopost_data")
os.makedirs(DATA_DIR, exist_ok=True)

# Bot links
BOT_MAIN    = "https://t.me/evalonwinnersbot"

# ============================================================
# DATABASE
# ============================================================
def _pg_conn():
    if not DATABASE_URL or not psycopg2: return None
    try:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    except Exception as e:
        logger.warning(f"DB connect failed: {e}"); return None

def db_init():
    """Create tables if not exist."""
    conn = _pg_conn()
    if not conn: return
    try:
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS autopost_history (
                    id SERIAL PRIMARY KEY,
                    service TEXT NOT NULL,
                    text_preview TEXT,
                    posted_at TIMESTAMPTZ DEFAULT NOW(),
                    day_key TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS autopost_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
        conn.commit()
    except Exception as e:
        logger.warning(f"DB init failed: {e}")
    finally:
        conn.close()

def db_get(key: str, default=None):
    conn = _pg_conn()
    if not conn:
        # Fallback to local file
        path = os.path.join(DATA_DIR, f"{key}.json")
        if os.path.exists(path):
            with open(path) as f: return json.load(f)
        return default
    try:
        with conn.cursor() as c:
            c.execute("SELECT value FROM autopost_state WHERE key=%s", (key,))
            row = c.fetchone()
            return json.loads(row[0]) if row else default
    except: return default
    finally: conn.close()

def db_set(key: str, value):
    conn = _pg_conn()
    val  = json.dumps(value)
    if not conn:
        path = os.path.join(DATA_DIR, f"{key}.json")
        with open(path, "w") as f: f.write(val)
        return
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO autopost_state(key, value) VALUES(%s,%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
            """, (key, val))
        conn.commit()
    except Exception as e:
        logger.warning(f"DB set failed: {e}")
    finally: conn.close()

def db_log_post(service: str, text: str):
    """Log a sent post to history."""
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    preview = text[:120].replace("\n", " ")
    conn = _pg_conn()
    if not conn:
        # Local fallback
        path = os.path.join(DATA_DIR, "history.json")
        hist = []
        if os.path.exists(path):
            with open(path) as f: hist = json.load(f)
        hist.append({"service": service, "preview": preview,
                     "posted_at": datetime.now(timezone.utc).isoformat(), "day_key": day_key})
        hist = hist[-200:]  # keep last 200
        with open(path, "w") as f: json.dump(hist, f)
        return
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO autopost_history(service, text_preview, day_key)
                VALUES(%s, %s, %s)
            """, (service, preview, day_key))
        conn.commit()
    except Exception as e:
        logger.warning(f"DB log failed: {e}")
    finally: conn.close()

def db_get_todays_posts() -> list:
    """Get list of services already posted today."""
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _pg_conn()
    if not conn:
        path = os.path.join(DATA_DIR, "history.json")
        if not os.path.exists(path): return []
        with open(path) as f: hist = json.load(f)
        return [h["service"] for h in hist if h.get("day_key") == day_key]
    try:
        with conn.cursor() as c:
            c.execute("SELECT service FROM autopost_history WHERE day_key=%s", (day_key,))
            return [r[0] for r in c.fetchall()]
    except: return []
    finally: conn.close()

def db_get_history(limit=20) -> list:
    """Get recent post history."""
    conn = _pg_conn()
    if not conn:
        path = os.path.join(DATA_DIR, "history.json")
        if not os.path.exists(path): return []
        with open(path) as f: hist = json.load(f)
        return hist[-limit:]
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as c:
            c.execute("""
                SELECT service, text_preview, posted_at, day_key
                FROM autopost_history
                ORDER BY posted_at DESC LIMIT %s
            """, (limit,))
            return [dict(r) for r in c.fetchall()]
    except: return []
    finally: conn.close()

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN  = os.environ.get("AUTOPOST_BOT_TOKEN")
CHANNEL_ID = "-1003403743370"
ADMIN_ID   = 8535925646

# Bot links
BOT_MAIN    = "https://t.me/evalonwinnersbot"

WATERMARK_TEXT = "EVALON WINNERS BOT"

def add_watermark(image_bytes: bytes) -> bytes:
    """Add diagonal tiled watermark to image."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font_size = max(18, w // 16)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size
            )
        except:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
        tw = bbox[2] - bbox[0]; th = bbox[3] - bbox[1]
        tile = Image.new("RGBA", (tw + 20, th + 20), (0, 0, 0, 0))
        td   = ImageDraw.Draw(tile)
        td.text((3, 3), WATERMARK_TEXT, font=font, fill=(0, 0, 0, 110))
        td.text((1, 1), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 170))
        rot = tile.rotate(330, expand=True)
        rw, rh = rot.size
        for y in range(-rh, h + rh, rh + 50):
            for x in range(-rw, w + rw, rw + 30):
                overlay.paste(rot, (x, y), rot)

        out = Image.alpha_composite(img, overlay).convert("RGB")
        buf = io.BytesIO()
        out.save(buf, format="JPEG", quality=90)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.warning(f"Watermark failed: {e}")
        return image_bytes

async def download_photo(bot: Bot, file_id: str) -> bytes:
    """Download a Telegram photo as bytes."""
    file = await bot.get_file(file_id)
    return bytes(await file.download_as_bytearray())

# ============================================================
# BUTTONS â€” per service
# ============================================================
def make_keyboard(service: str) -> InlineKeyboardMarkup:
    """1 button per post. Label and deep-link param change per service."""
    SERVICE_BUTTONS = {
        "vip_signals":      ("ðŸ‘‘ Join VIP Now",        "vip"),
        "auto_trading_bot": ("ðŸ¤– Start Auto Trading",  "auto"),
        "social_trading":   ("âœ¨ Start Social Copy",   "copy"),
        "manual_bot":       ("ðŸŽ Claim Free Bot",      "freebooters"),
        "indicators":       ("ðŸ“Š Get Indicators",      "indicator"),
        "spin_invite":      ("ðŸŽ° Spin & Save 70%",     "spin"),
    }
    label, param = SERVICE_BUTTONS.get(service, ("ðŸ‘‘ Access Now", "vip"))
    url = f"{BOT_MAIN}?start={param}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, url=url)],
    ])

def make_broadcast_keyboard() -> InlineKeyboardMarkup:
    """Single button for admin broadcast posts."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ðŸ‘‘ Join VIP Now", url=f"{BOT_MAIN}?start=vip")],
    ])

# ============================================================
# POST CONTENT â€” 6 SERVICES (no @mention in text, button handles it)
# ============================================================
POSTS = {

    "vip_signals": [
        (
            "ðŸ“Š <b>EVALON VIP SIGNALS</b>\n\n"
            "ðŸ”¥ <b>Non-Martingale signals only</b>\n\n"
            "âœ… 8 to 10 signals per day\n"
            "âœ… Monday to Friday â€” consistent delivery\n"
            "âœ… BUY/SELL direction with expiry time\n"
            "âœ… WIN/LOSS results after every trade\n"
            "âœ… High accuracy entries â€” no guessing\n\n"
            "ðŸ’Ž Trade smarter. Follow the signal."
        ),
        (
            "âš¡ <b>TIRED OF LOSING TRADES?</b>\n\n"
            "Switch to <b>EVALON VIP SIGNALS</b>\n\n"
            "ðŸ“ˆ 8â€“10 clean signals every trading day\n"
            "ðŸŽ¯ Non-Martingale â€” no dangerous recovery trades\n"
            "ðŸ“² Signals delivered directly to your Telegram\n"
            "âœ… Monday to Friday, session by session\n\n"
            "Stop guessing. Start winning."
        ),
        (
            "ðŸ† <b>EVALON VIP SIGNALS â€” THE DIFFERENCE</b>\n\n"
            "While others use Martingale and blow accounts...\n\n"
            "We use <b>pure strategy</b>:\n"
            "ðŸ“Š 8â€“10 signals daily\n"
            "ðŸŽ¯ Non-Martingale â€” protect your capital\n"
            "â° Monâ€“Fri, every session\n"
            "ðŸ“² Real results. WIN/LOSS every trade\n\n"
            "Your capital deserves better."
        ),
        (
            "ðŸ“² <b>EVALON VIP SIGNALS</b>\n\n"
            "Every weekday you get:\n\n"
            "ðŸ”” Signal notification\n"
            "ðŸ“ˆ Asset + direction + expiry\n"
            "âœ… Result after every trade\n\n"
            "ðŸŽ¯ <b>Non-Martingale only</b> â€” clean and safe\n"
            "ðŸ—“ Monday to Friday â€” 8 to 10 signals per session\n\n"
            "Your edge in the market starts here."
        ),
        (
            "ðŸ’¬ <b>Quick question.</b>\n"
            "\n"
            "How many trades did you lose this week because you had no plan?\n"
            "\n"
            "Our VIP members don't guess.\n"
            "They follow a signal â€” entry, direction, expiry.\n"
            "Then wait for the result.\n"
            "\n"
            "That's it. No stress. No confusion.\n"
            "\n"
            "Ready to trade with a plan? ðŸ‘‡"
        ),
        (
            "ðŸŒ… <b>Morning check-in.</b>\n"
            "\n"
            "The market is open.\n"
            "Signals are being prepared.\n"
            "\n"
            "VIP members already know what to trade today.\n"
            "Do you?\n"
            "\n"
            "8â€“10 signals. Non-Martingale. Monâ€“Fri.\n"
            "Your edge starts here ðŸ‘‡"
        ),
        (
            "ðŸŒ™ <b>End of session.</b>\n"
            "\n"
            "Another trading day is closing.\n"
            "\n"
            "VIP members followed their signals.\n"
            "Logged their results.\n"
            "Closed their charts.\n"
            "\n"
            "No stress. No revenge trading. No blown accounts.\n"
            "\n"
            "That's what a system does for you.\n"
            "Join before tomorrow's session ðŸ‘‡"
        ),
        (
            "ðŸ“Œ <b>One thing separates profitable traders from the rest.</b>\n"
            "\n"
            "A consistent entry strategy.\n"
            "\n"
            "Not luck.\n"
            "Not more screen time.\n"
            "Not a bigger deposit.\n"
            "\n"
            "Just clean, consistent signals â€” followed with discipline.\n"
            "\n"
            "EVALON VIP gives you exactly that ðŸ‘‡"
        ),
        (
            "ðŸŽ¯ <b>What does a VIP signal look like?</b>\n"
            "\n"
            "ðŸ“Š Asset: EUR/USD OTC\n"
            "ðŸ“ˆ Direction: CALL â¬†ï¸\n"
            "â± Expiry: 5 minutes\n"
            "\n"
            "That's all you need.\n"
            "No analysis. No confusion.\n"
            "Just follow and wait.\n"
            "\n"
            "8â€“10 of these every trading day ðŸ‘‡"
        ),
    ],

    "auto_trading_bot": [
        (
            "ðŸ¤– <b>EVALON AUTO TRADING BOT</b>\n\n"
            "Set it. Forget it. Profit.\n\n"
            "âœ… Works on <b>ALL brokers</b>\n"
            "âœ… Non-Martingale strategy built in\n"
            "âœ… Stop Loss & Take Profit settings\n"
            "âœ… Compounding settings available\n"
            "ðŸ“ˆ <b>87% to 95% accuracy</b>\n\n"
            "Let the bot trade while you live your life."
        ),
        (
            "âš™ï¸ <b>TRADE AUTOMATICALLY WITH EVALON BOT</b>\n\n"
            "No screen time needed.\n\n"
            "ðŸ¤– Fully automated trading\n"
            "ðŸ”’ Stop Loss protection\n"
            "ðŸ’° Take Profit settings\n"
            "ðŸ“ˆ Compounding to grow your account\n"
            "ðŸŒ <b>All brokers supported</b>\n"
            "ðŸŽ¯ 87â€“95% accuracy\n\n"
            "Your account works even when you sleep."
        ),
        (
            "ðŸ’° <b>WANT YOUR MONEY WORKING FOR YOU?</b>\n\n"
            "<b>EVALON Auto Trading Bot</b> does exactly that.\n\n"
            "âœ… All brokers â€” no restrictions\n"
            "âœ… Non-Martingale â€” capital protected\n"
            "âœ… Customizable Stop & Take Profit\n"
            "âœ… Compounding settings\n"
            "âœ… 87â€“95% accuracy record\n\n"
            "Set up once. Earn consistently."
        ),
        (
            "ðŸŒ <b>ALL BROKERS. ONE BOT.</b>\n\n"
            "EVALON Auto Trading Bot supports every major broker.\n\n"
            "ðŸ“Š Non-Martingale strategy\n"
            "ðŸ”’ Built-in Stop Loss & Take Profit\n"
            "ðŸ“ˆ 87â€“95% accuracy\n"
            "ðŸ’¹ Compounding mode to scale profits\n\n"
            "Start automated trading today."
        ),
        (
            "ðŸ’¬ <b>Be honest.</b>\n"
            "\n"
            "How much time do you spend watching charts every day?\n"
            "\n"
            "2 hours? 4 hours? More?\n"
            "\n"
            "EVALON Auto Bot handles it all.\n"
            "You set it up once â€” it runs, trades, and manages risk.\n"
            "\n"
            "Your time is worth more than a screen ðŸ‘‡"
        ),
        (
            "ðŸŒ… <b>While you were sleeping last night...</b>\n"
            "\n"
            "Our Auto Trading Bot was running.\n"
            "\n"
            "âœ… Scanning the market\n"
            "âœ… Placing trades\n"
            "âœ… Managing Stop Loss\n"
            "âœ… Protecting your capital\n"
            "\n"
            "Automated. Consistent. Safe.\n"
            "Set it up today ðŸ‘‡"
        ),
        (
            "ðŸ”’ <b>The biggest fear in trading?</b>\n"
            "\n"
            "Losing more than you planned.\n"
            "\n"
            "That's why EVALON Auto Bot has:\n"
            "ðŸ›‘ Stop Loss â€” cuts losses automatically\n"
            "ðŸ’° Take Profit â€” locks in gains\n"
            "ðŸ“ˆ Compounding â€” grows your account steadily\n"
            "\n"
            "Risk managed. Always ðŸ‘‡"
        ),
        (
            "ðŸ“Š <b>87â€“95% accuracy.</b>\n"
            "\n"
            "That's the track record of EVALON Auto Trading Bot.\n"
            "\n"
            "Not a promise.\n"
            "Not a guess.\n"
            "A result â€” built on Non-Martingale strategy and consistent execution.\n"
            "\n"
            "All brokers supported. Start today ðŸ‘‡"
        ),
        (
            "âš™ï¸ <b>Setup takes less than 5 minutes.</b>\n"
            "\n"
            "1ï¸âƒ£ Open the bot\n"
            "2ï¸âƒ£ Connect your broker\n"
            "3ï¸âƒ£ Set your Stop Loss & Take Profit\n"
            "4ï¸âƒ£ Start\n"
            "\n"
            "That's it.\n"
            "The bot does the rest â€” 24/7.\n"
            "\n"
            "Works on ALL brokers ðŸ‘‡"
        ),
    ],

    "social_trading": [
        (
            "ðŸ”— <b>EVALON SOCIAL TRADING â€” POCKET OPTION</b>\n\n"
            "Don't trade alone. Copy a proven account.\n\n"
            "âœ… Copy trades directly from our Pocket Option account\n"
            "ðŸ“… <b>Monday to Monday</b> â€” no weekends off\n"
            "ðŸŒ™ OTC trading included â€” 24/7 coverage\n"
            "ðŸ“² Everything automated â€” just connect and earn\n\n"
            "The simplest way to profit from trading."
        ),
        (
            "ðŸ“‹ <b>COPY TRADING â€” EVALON SOCIAL TRADING</b>\n\n"
            "What we trade, you trade. Automatically.\n\n"
            "ðŸŽ¯ Pocket Option platform\n"
            "ðŸ“… 7 days a week â€” Monday to Monday\n"
            "ðŸŒ™ OTC markets included â€” no downtime\n"
            "âœ… No experience needed â€” just copy\n\n"
            "Your account mirrors our trades in real time."
        ),
        (
            "ðŸŒ™ <b>TRADING DOESN'T STOP â€” NEITHER DO WE</b>\n\n"
            "<b>EVALON Social Trading on Pocket Option</b>\n\n"
            "ðŸ“… Active Monday to Monday\n"
            "ðŸŒ™ OTC included â€” weekends too\n"
            "ðŸ”— Auto-copy every trade we make\n"
            "âœ… Pocket Option account required\n\n"
            "While others rest, your account keeps growing."
        ),
        (
            "ðŸ’¡ <b>NEW TO TRADING? START HERE.</b>\n\n"
            "<b>EVALON Social Trading</b> â€” copy without learning.\n\n"
            "âœ… Connect your Pocket Option account\n"
            "âœ… Our trades copy to yours automatically\n"
            "ðŸ“… 7 days a week including OTC\n"
            "ðŸŽ¯ No analysis needed â€” we do it for you\n\n"
            "Your easiest path to consistent profits."
        ),
        (
            "ðŸ’¬ <b>What if you could profit from trading...</b>\n"
            "\n"
            "Without knowing how to trade?\n"
            "\n"
            "That's exactly what EVALON Social Trading does.\n"
            "\n"
            "Our Pocket Option account trades.\n"
            "Your account copies â€” automatically.\n"
            "\n"
            "No experience needed. No charts. No stress ðŸ‘‡"
        ),
        (
            "ðŸŒ™ <b>It's the weekend.</b>\n"
            "\n"
            "Most traders are offline.\n"
            "\n"
            "But EVALON Social Trading?\n"
            "Still running.\n"
            "\n"
            "OTC markets are open.\n"
            "Your account is still copying trades.\n"
            "Monday to Monday â€” no breaks.\n"
            "\n"
            "Set it and forget it ðŸ‘‡"
        ),
        (
            "ðŸ“‹ <b>Copy trading â€” simplified.</b>\n"
            "\n"
            "You don't need to:\n"
            "âŒ Analyze charts\n"
            "âŒ Read indicators\n"
            "âŒ Know entry strategies\n"
            "\n"
            "You just need to:\n"
            "âœ… Connect your Pocket Option account\n"
            "âœ… Let EVALON do the rest\n"
            "\n"
            "That's the whole process ðŸ‘‡"
        ),
        (
            "ðŸ• <b>How much time does copy trading take?</b>\n"
            "\n"
            "Setup: 5 minutes.\n"
            "Daily management: 0 minutes.\n"
            "\n"
            "EVALON Social Trading runs itself.\n"
            "OTC included â€” active 7 days a week.\n"
            "\n"
            "Your easiest trading decision ðŸ‘‡"
        ),
        (
            "ðŸ”— <b>One connection. Endless trades.</b>\n"
            "\n"
            "Link your Pocket Option account to EVALON Social Trading.\n"
            "\n"
            "Every trade we place â€” you get it too.\n"
            "Same entry. Same direction. Same result.\n"
            "\n"
            "Monday to Monday. OTC included.\n"
            "No screen time required ðŸ‘‡"
        ),
    ],

    "manual_bot": [
        (
            "ðŸŽ <b>EVALON MANUAL BOT â€” FREE ACCESS</b>\n\n"
            "Get it simply by registering through our broker links.\n\n"
            "âœ… Register via our bot using partner broker links\n"
            "âœ… Manual bot access activated automatically\n"
            "ðŸ”— Multiple brokers available\n"
            "ðŸ“² Everything handled inside the bot\n\n"
            "The easiest free tool you'll get today."
        ),
        (
            "ðŸ¤ <b>REGISTER. GET THE BOT. START TRADING.</b>\n\n"
            "<b>EVALON Manual Bot</b> â€” yours when you sign up.\n\n"
            "ðŸ“‹ Sign up through broker links inside our bot\n"
            "âœ… Manual bot unlocked instantly\n"
            "ðŸŒ Multiple supported brokers\n"
            "ðŸ’° Zero extra cost â€” just register\n\n"
            "Free access. Real results."
        ),
        (
            "ðŸ”“ <b>UNLOCK THE EVALON MANUAL BOT</b>\n\n"
            "No purchase needed.\n\n"
            "1ï¸âƒ£ Open our bot\n"
            "2ï¸âƒ£ Register via a broker link\n"
            "3ï¸âƒ£ Manual bot access â€” activated âœ…\n\n"
            "Simple. Fast. Free."
        ),
        (
            "ðŸ’¬ <b>Did you know?</b>\n"
            "\n"
            "You can get the EVALON Manual Bot completely free.\n"
            "\n"
            "No payment needed.\n"
            "Just register with a broker through our bot.\n"
            "\n"
            "Takes 3 minutes.\n"
            "Access unlocks instantly.\n"
            "\n"
            "The free tool most traders don't know about ðŸ‘‡"
        ),
        (
            "ðŸ¤” <b>Why pay for a bot when you can get one free?</b>\n"
            "\n"
            "EVALON Manual Bot is unlocked the moment you:\n"
            "\n"
            "1ï¸âƒ£ Open our bot\n"
            "2ï¸âƒ£ Register via any partner broker link\n"
            "3ï¸âƒ£ Done â€” bot activated âœ…\n"
            "\n"
            "Multiple brokers available.\n"
            "Zero cost. Real access ðŸ‘‡"
        ),
        (
            "â± <b>3 minutes from now...</b>\n"
            "\n"
            "You could have access to the EVALON Manual Bot.\n"
            "\n"
            "Register via a broker link inside our bot.\n"
            "Access activates automatically.\n"
            "No waiting. No payment.\n"
            "\n"
            "Simplest free tool in trading ðŸ‘‡"
        ),
        (
            "ðŸŒ <b>Multiple brokers. One bot.</b>\n"
            "\n"
            "EVALON Manual Bot works across our partner brokers.\n"
            "\n"
            "Register through any of them â€” inside our bot.\n"
            "Manual bot access is yours immediately.\n"
            "\n"
            "Pick your broker. Start trading.\n"
            "It's completely free ðŸ‘‡"
        ),
        (
            "ðŸŽ <b>Free doesn't mean basic.</b>\n"
            "\n"
            "EVALON Manual Bot gives you:\n"
            "\n"
            "âœ… Manual trading signals\n"
            "âœ… Entry guidance\n"
            "âœ… Broker access through one place\n"
            "\n"
            "All for registering through our partner link.\n"
            "Start here ðŸ‘‡"
        ),
    ],

    "indicators": [
        (
            "ðŸ“‰ <b>EVALON INDICATORS</b>\n\n"
            "Available on <b>MT4, MT5 & TradingView</b>\n\n"
            "âœ… <b>Non-repaint</b> â€” what you see is what you get\n"
            "âœ… Get access with any Evalon service\n"
            "ðŸ“Š Works on all major pairs and assets\n"
            "ðŸŽ¯ Precise entry signals on your chart\n\n"
            "See the market clearly. Trade with confidence."
        ),
        (
            "ðŸ“Š <b>NON-REPAINT INDICATORS â€” MT4, MT5, TRADINGVIEW</b>\n\n"
            "No more signals that disappear after the fact.\n\n"
            "âœ… Evalon Indicators never repaint\n"
            "âœ… Available on all 3 platforms\n"
            "ðŸŽ Included when you join any Evalon service\n\n"
            "Trade what you see. Every time."
        ),
        (
            "ðŸ–¥ï¸ <b>TRADINGVIEW â€¢ MT4 â€¢ MT5</b>\n\n"
            "<b>EVALON Indicators</b> â€” on every platform you use.\n\n"
            "ðŸ“Œ Non-repaint signals on your chart\n"
            "âœ… No confusion â€” clear BUY/SELL\n"
            "ðŸŽ Access granted with any Evalon service\n\n"
            "Your charts. Our precision."
        ),
        (
            "ðŸ’¬ <b>Ever placed a trade...</b>\n"
            "\n"
            "Then watched the signal disappear from your chart?\n"
            "\n"
            "That's a repainting indicator.\n"
            "It changes history â€” so it always looks right after the fact.\n"
            "\n"
            "EVALON Indicators never repaint.\n"
            "What you see is exactly what happened ðŸ‘‡"
        ),
        (
            "ðŸ–¥ï¸ <b>Which platform do you use?</b>\n"
            "\n"
            "MT4 âœ…\n"
            "MT5 âœ…\n"
            "TradingView âœ…\n"
            "\n"
            "EVALON Indicators work on all three.\n"
            "Non-repaint. Clear BUY/SELL signals.\n"
            "Included with any Evalon service ðŸ‘‡"
        ),
        (
            "ðŸ“Œ <b>A good indicator does one thing well.</b>\n"
            "\n"
            "It tells you when to enter.\n"
            "\n"
            "Not maybe.\n"
            "Not 'it depends'.\n"
            "A clear signal â€” on your chart â€” right when you need it.\n"
            "\n"
            "EVALON Indicators are built for exactly that.\n"
            "MT4, MT5 & TradingView ðŸ‘‡"
        ),
        (
            "ðŸŽ¯ <b>Precision matters in trading.</b>\n"
            "\n"
            "A signal that repaints is worse than no signal.\n"
            "It gives you false confidence.\n"
            "\n"
            "EVALON Indicators are built different:\n"
            "âœ… Non-repaint â€” locked when candle closes\n"
            "âœ… Works across all major assets\n"
            "âœ… Available on 3 platforms\n"
            "\n"
            "See the market clearly ðŸ‘‡"
        ),
        (
            "ðŸ“Š <b>Indicators that work with you â€” not against you.</b>\n"
            "\n"
            "No clutter. No confusion.\n"
            "\n"
            "Just clean entry signals on your chart.\n"
            "Non-repaint. Multi-platform.\n"
            "Included free with any Evalon service.\n"
            "\n"
            "MT4 â€¢ MT5 â€¢ TradingView ðŸ‘‡"
        ),
    ],

    "spin_invite": [
        (
            "ðŸŽ° <b>SPIN & INVITE â€” SAVE UP TO 70%</b>\n\n"
            "Our services don't have to cost full price.\n\n"
            "ðŸŽ¯ Spin to win discounts on any Evalon service\n"
            "ðŸ‘¥ Invite friends and unlock more savings\n"
            "ðŸ’¸ Up to <b>70% off</b> on VIP, Bots, Social Trading & more\n\n"
            "Why pay full price when you don't have to?"
        ),
        (
            "ðŸ’¸ <b>GET EVALON SERVICES FOR LESS</b>\n\n"
            "<b>Spin & Invite</b> â€” your shortcut to big discounts.\n\n"
            "ðŸŽ° Spin inside the bot for instant discounts\n"
            "ðŸ“² Invite a friend â€” unlock more savings\n"
            "ðŸ·ï¸ Up to 70% off any service\n\n"
            "VIP. Auto Bot. Social Trading. Indicators.\n"
            "All discounted â€” all accessible."
        ),
        (
            "ðŸ‘¥ <b>INVITE FRIENDS. SAVE BIG.</b>\n\n"
            "<b>EVALON Spin & Invite Access</b>\n\n"
            "ðŸŽ° Spin for surprise discounts\n"
            "ðŸ¤ Refer friends and save even more\n"
            "ðŸ’¸ Discounts up to <b>70%</b> on all services\n\n"
            "The more you share, the less you pay."
        ),
        (
            "ðŸ’¬ <b>Quick tip.</b>\n"
            "\n"
            "Before you pay full price for any EVALON service â€”\n"
            "open the bot and spin first.\n"
            "\n"
            "You might get 20%, 40%, even 70% off.\n"
            "Takes 10 seconds.\n"
            "\n"
            "Why pay more than you have to? ðŸ‘‡"
        ),
        (
            "ðŸ‘¥ <b>Know someone who wants to start trading?</b>\n"
            "\n"
            "Invite them through EVALON.\n"
            "\n"
            "They get access to our services.\n"
            "You unlock deeper discounts â€” up to 70% off.\n"
            "\n"
            "Share the opportunity.\n"
            "Save together ðŸ‘‡"
        ),
        (
            "ðŸŽ° <b>Not ready to pay full price yet?</b>\n"
            "\n"
            "That's fine.\n"
            "\n"
            "Spin inside the bot â€” you might not have to.\n"
            "\n"
            "Discounts on:\n"
            "ðŸ‘‘ VIP Signals\n"
            "ðŸ¤– Auto Trading Bot\n"
            "âœ¨ Social Copy Trading\n"
            "ðŸ“Š Indicators\n"
            "\n"
            "One spin. Real savings ðŸ‘‡"
        ),
        (
            "ðŸ’¸ <b>The math is simple.</b>\n"
            "\n"
            "Invite 1 friend â†’ unlock a discount.\n"
            "Invite more â†’ save more.\n"
            "Spin the wheel â†’ instant discount.\n"
            "\n"
            "Up to 70% off any EVALON service.\n"
            "\n"
            "Most people never use this.\n"
            "You should ðŸ‘‡"
        ),
        (
            "ðŸ·ï¸ <b>Discounts don't last forever.</b>\n"
            "\n"
            "EVALON services are available at full price anytime.\n"
            "But discounts â€” those come from spinning and inviting.\n"
            "\n"
            "Once your discount expires, it resets.\n"
            "\n"
            "Spin now. Save now ðŸ‘‡"
        ),
    ],
}

# ============================================================
# SCHEDULE â€” 10 to 12 posts per day (08:00â€“23:00 EAT = 05:00â€“20:00 UTC)
# ============================================================
SCHEDULE_HOURS_UTC = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]

def get_todays_schedule():
    count  = random.randint(10, 12)
    chosen = sorted(random.sample(SCHEDULE_HOURS_UTC, min(count, len(SCHEDULE_HOURS_UTC))))
    return [(h, random.randint(0, 55)) for h in chosen]

# ============================================================
# DYNAMIC POSTS â€” day-aware and date-stamped
# ============================================================
DAY_NAMES = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}

def get_dynamic_post() -> tuple[str, str] | None:
    """
    Returns a (service_key, text) dynamic post based on current day/date.
    Returns None randomly â€” so dynamic posts appear ~2x per day in rotation.
    """
    if random.random() > 0.25:  # 25% chance to return a dynamic post
        return None

    from datetime import timedelta
    now     = datetime.now(timezone.utc)
    eat     = now + timedelta(hours=3)  # EAT = UTC+3
    weekday  = eat.weekday()  # 0=Monday, 6=Sunday
    eat_hour = eat.hour
    day_name = DAY_NAMES[weekday]
    date_str = eat.strftime("%d %B %Y")  # e.g. "24 May 2025"

    is_weekend  = weekday >= 5  # Saturday or Sunday
    is_friday   = weekday == 4
    is_monday   = weekday == 0

    DYNAMIC_POSTS = []

    # --- WEEKEND posts (Sat/Sun) ---
    if is_weekend:
        DYNAMIC_POSTS += [
            (
                f"ðŸ“… <b>{day_name} â€” {date_str}</b>\n\n"
                "ðŸ’° <b>VIP members are making money this weekend.</b>\n\n"
                "While the forex market rests...\n"
                "Our <b>Social Trading</b> runs <b>Monday to Monday</b> â€” OTC included.\n\n"
                "The market never fully sleeps.\n"
                "Neither do we. ðŸŒ™\n\n"
                "Are you still on the outside? Join us now ðŸ‘‡"
            ),
            (
                f"ðŸ—“ <b>Today is {day_name} â€” {date_str}</b>\n\n"
                "Weekend is here â€” but profits don't wait.\n\n"
                "âœ… <b>Social Copy Trading</b> is LIVE â€” OTC markets open\n"
                "âœ… <b>Auto Trading Bot</b> is running â€” all brokers\n"
                "âœ… <b>VIP members</b> are already ahead of you\n\n"
                "You can still join today ðŸ‘‡"
            ),
        ]

    # --- FRIDAY posts ---
    elif is_friday:
        DYNAMIC_POSTS += [
            (
                f"ðŸ—“ <b>Today is Friday â€” {date_str}</b>\n\n"
                "âš¡ <b>Weekend is starting â€” are you ready?</b>\n\n"
                "VIP members already locked in profits this week.\n"
                "Social Trading keeps running through the weekend.\n\n"
                "Don't let another week pass without taking action.\n\n"
                "Join now ðŸ‘‡"
            ),
            (
                f"ðŸ“… <b>Friday â€” {date_str}</b>\n\n"
                "ðŸ End of the trading week.\n\n"
                "This week our VIP members:\n"
                "ðŸ“ˆ Received 8â€“10 signals daily\n"
                "âœ… Non-Martingale â€” capital protected\n"
                "ðŸ’° Consistent profits every session\n\n"
                "Next week starts Monday.\n"
                "Will you be ready? Join before the weekend ends ðŸ‘‡"
            ),
        ]

    # --- MONDAY posts ---
    elif is_monday:
        DYNAMIC_POSTS += [
            (
                f"ðŸ“… <b>Monday â€” {date_str}</b>\n\n"
                "ðŸ”” <b>New week. New signals. New profits.</b>\n\n"
                "VIP signals are LIVE from today.\n"
                "8â€“10 signals per day, Monday to Friday.\n\n"
                "If you missed last week â€” don't miss this one.\n\n"
                "Join now ðŸ‘‡"
            ),
        ]

    # --- WEEKDAY posts (Tue/Wed/Thu) ---
    else:
        DYNAMIC_POSTS += [
            (
                f"ðŸ“… <b>{day_name} â€” {date_str}</b>\n\n"
                "âš¡ <b>VIP signals are running RIGHT NOW.</b>\n\n"
                "While you're reading this, our members are:\n"
                "ðŸ“ˆ Following live signals\n"
                "âœ… Booking profits\n"
                "ðŸ¤– Running auto bots on all brokers\n\n"
                "You're still on the outside.\n"
                "Fix that today ðŸ‘‡"
            ),
            (
                f"ðŸ—“ <b>{day_name} â€” {date_str}</b>\n\n"
                "ðŸ’Ž <b>Another trading day. Another opportunity.</b>\n\n"
                "EVALON VIP members get:\n"
                "ðŸ“Š 8â€“10 clean signals today\n"
                "ðŸŽ¯ Non-Martingale only\n"
                "ðŸ“² Results after every trade\n\n"
                "Today's session is already running.\n"
                "Don't miss tomorrow's â€” join now ðŸ‘‡"
            ),
        ]

    if not DYNAMIC_POSTS:
        return None

    text = random.choice(DYNAMIC_POSTS)
    return "vip_signals", text  # dynamic posts use VIP keyboard


def get_date_header() -> str:
    """Returns date header in EAT timezone (UTC+3)."""
    from datetime import timedelta
    eat = datetime.now(timezone.utc) + timedelta(hours=3)
    return f"ðŸ“… <b>{eat.strftime('%A, %d %B %Y')}</b>\n\n"


def post_used_key(service: str) -> str:
    return f"used_posts_{service}"


def pick_unused_post(service: str) -> str:
    """Cycle through all posts before repeating any."""
    posts = POSTS[service]
    key   = post_used_key(service)
    used  = db_get(key, [])

    all_indexes = list(range(len(posts)))
    remaining   = [i for i in all_indexes if i not in used]

    if not remaining:
        used      = []
        remaining = all_indexes

    chosen = random.choice(remaining)
    used.append(chosen)
    db_set(key, used)
    return posts[chosen]


def pick_post(avoid_services: list = None):
    """Pick a post â€” avoid services already posted today, no repeated posts."""
    avoid = set(avoid_services or [])

    dynamic = get_dynamic_post()
    if dynamic and dynamic[0] not in avoid:
        service, text = dynamic
        return service, get_date_header() + text

    available = [s for s in POSTS.keys() if s not in avoid]
    if not available:
        available = list(POSTS.keys())

    service = random.choice(available)
    text    = pick_unused_post(service)
    return service, get_date_header() + text

# ============================================================
# MEDIA STORAGE â€” DB-backed (replaces static VIDEO_FILE_IDS)
# ============================================================
SERVICES = ["vip_signals", "auto_trading_bot", "social_trading", "manual_bot", "indicators", "spin_invite"]
SERVICE_LABELS = {
    "vip_signals":      "ðŸ‘‘ VIP Signals",
    "auto_trading_bot": "ðŸ¤– Auto Trading Bot",
    "social_trading":   "âœ¨ Social Copy Trading",
    "manual_bot":       "ðŸŽ Manual Bot",
    "indicators":       "ðŸ“Š Indicators",
    "spin_invite":      "ðŸŽ° Spin & Invite",
}

def media_load() -> dict:
    """Load media file_ids from DB. Returns {service: [{type, file_id}, ...]}"""
    return db_get("media_store", {})

def media_save(data: dict):
    db_set("media_store", data)

def media_get_for_service(service: str) -> list:
    """Get list of {type, file_id} for a service."""
    return media_load().get(service, [])

def media_add(service: str, file_id: str, media_type: str):
    """Add a file_id to a service. media_type = 'video' or 'photo'"""
    store = media_load()
    if service not in store:
        store[service] = []
    # Avoid duplicates
    existing_ids = [m["file_id"] for m in store[service]]
    if file_id not in existing_ids:
        store[service].append({"type": media_type, "file_id": file_id})
    media_save(store)

def media_remove(service: str, index: int) -> bool:
    """Remove media at index for a service. Returns True if removed."""
    store = media_load()
    items = store.get(service, [])
    if 0 <= index < len(items):
        items.pop(index)
        store[service] = items
        media_save(store)
        return True
    return False

# ============================================================
# SEND POST (text or video/photo + buttons + watermark)
# ============================================================
async def send_post(bot: Bot, service: str, text: str, keyboard: InlineKeyboardMarkup = None):
    kb    = keyboard or make_keyboard(service)
    media = media_get_for_service(service)

    if media:
        item = random.choice(media)
        try:
            if item["type"] == "video":
                # Watermark on video caption
                cap = text + f"\n\nðŸ“¹ {WATERMARK_TEXT}"
                await bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=item["file_id"],
                    caption=cap.strip(),
                    parse_mode="HTML",
                    reply_markup=kb
                )
            else:
                # Download photo, apply watermark, re-upload
                raw = await download_photo(bot, item["file_id"])
                wm  = add_watermark(raw)
                bio = io.BytesIO(wm); bio.name = "post.jpg"
                await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=bio,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb
                )
            return
        except Exception as e:
            logger.warning(f"Media send failed, falling back to text: {e}")

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True
    )

# ============================================================
# /addmedia â€” admin sends video/photo + selects service
# ============================================================
async def cmd_addmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    # Show service selection buttons
    buttons = [[InlineKeyboardButton(label, callback_data=f"addmedia_wait_{svc}")]
               for svc, label in SERVICE_LABELS.items()]
    await update.message.reply_text(
        "ðŸ“Ž <b>Add Media</b>\n\nWhich service is this video/photo for?\n\n"
        "1ï¸âƒ£ Select the service below\n"
        "2ï¸âƒ£ Then send the video or photo",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def cmd_listmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    store = media_load()
    if not any(store.values()):
        await update.message.reply_text("ðŸ“­ No media saved yet.\nUse /addmedia to add."); return

    lines = ["ðŸ—‚ <b>SAVED MEDIA</b>\n"]
    for svc, items in store.items():
        if not items: continue
        label = SERVICE_LABELS.get(svc, svc)
        lines.append(f"{label} â€” <b>{len(items)} file(s)</b>")
        for i, m in enumerate(items):
            lines.append(f"  `{i}` â€” {m['type']}")
    lines.append("\n_Use /removemedia to delete_")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_removemedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    store = media_load()
    buttons = []
    for svc, items in store.items():
        for i, m in enumerate(items):
            label = f"{SERVICE_LABELS.get(svc, svc)} â€” {m['type']} #{i}"
            buttons.append([InlineKeyboardButton(f"ðŸ—‘ {label}", callback_data=f"removemedia_{svc}_{i}")])
    if not buttons:
        await update.message.reply_text("ðŸ“­ No media to remove."); return
    await update.message.reply_text(
        "ðŸ—‘ <b>Remove Media</b>\n\nSelect item to remove:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ============================================================
# ADMIN BROADCAST HANDLER
# ============================================================
async def handle_addmedia_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle service selection for /addmedia."""
    q   = update.callback_query
    uid = q.from_user.id
    if uid != ADMIN_ID: return
    await q.answer()
    data = q.data

    if data.startswith("addmedia_wait_"):
        service = data.replace("addmedia_wait_", "")
        context.user_data["addmedia_service"] = service
        label = SERVICE_LABELS.get(service, service)
        await q.edit_message_text(
            f"âœ… <b>{label}</b> selected.\n\n"
            "Now send me the <b>video or photo</b> to attach to this service.\n\n"
            "_It will be saved and used automatically in future auto-posts._",
            parse_mode="HTML"
        )

    elif data.startswith("removemedia_"):
        parts   = data.split("_", 2)
        # removemedia_{service}_{index}
        _, svc, idx_str = data.split("_", 2)
        # svc might have underscores â€” handle carefully
        # format: removemedia_{svc}_{i}
        last_underscore = data.rfind("_")
        idx_str = data[last_underscore+1:]
        svc     = data[len("removemedia_"):last_underscore]
        try:
            idx = int(idx_str)
            if media_remove(svc, idx):
                label = SERVICE_LABELS.get(svc, svc)
                await q.edit_message_text(f"ðŸ—‘ Removed media #{idx} from <b>{label}</b>", parse_mode="HTML")
            else:
                await q.edit_message_text("âš ï¸ Item not found.")
        except: await q.edit_message_text("âš ï¸ Error removing media.")


async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends message â†’ channel with buttons. Also handles addmedia flow."""
    if update.effective_user.id != ADMIN_ID: return

    msg = update.message

    # â”€â”€ ADDMEDIA FLOW â”€â”€
    if context.user_data.get("addmedia_service") and (msg.photo or msg.video):
        service    = context.user_data.pop("addmedia_service")
        label      = SERVICE_LABELS.get(service, service)
        if msg.video:
            file_id    = msg.video.file_id
            media_type = "video"
        else:
            file_id    = msg.photo[-1].file_id
            media_type = "photo"
        media_add(service, file_id, media_type)
        await msg.reply_text(
            f"âœ… <b>{media_type.capitalize()} saved for {label}!</b>\n\n"
            f"It will now be attached to auto-posts for this service.\n"
            f"Use /listmedia to see all saved media.",
            parse_mode="HTML"
        )
        return

    # â”€â”€ BROADCAST FLOW â”€â”€
    kb = make_broadcast_keyboard()
    try:
        if msg.photo:
            raw = await download_photo(context.bot, msg.photo[-1].file_id)
            wm  = add_watermark(raw)
            bio = io.BytesIO(wm); bio.name = "post.jpg"
            await context.bot.send_photo(
                chat_id=CHANNEL_ID, photo=bio,
                caption=msg.caption or "", parse_mode="HTML", reply_markup=kb
            )
        elif msg.video:
            cap = (msg.caption or "") + f"\n\nðŸ“¹ {WATERMARK_TEXT}"
            await context.bot.send_video(
                chat_id=CHANNEL_ID, video=msg.video.file_id,
                caption=cap.strip(), parse_mode="HTML", reply_markup=kb
            )
        elif msg.animation:
            await context.bot.send_animation(
                chat_id=CHANNEL_ID, animation=msg.animation.file_id,
                caption=msg.caption or "", parse_mode="HTML", reply_markup=kb
            )
        elif msg.text:
            await context.bot.send_message(
                chat_id=CHANNEL_ID, text=msg.text,
                parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
            )
        else:
            await msg.reply_text("âš ï¸ Unsupported message type."); return

        await msg.reply_text("âœ… <b>Sent to channel!</b>", parse_mode="HTML")
        logger.info("Admin broadcast sent to channel")

    except Exception as e:
        await msg.reply_text(f"âŒ Failed: {e}")
        logger.error(f"Broadcast failed: {e}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(
        "ðŸ“¡ <b>EVALON AUTOPOST BOT v3</b>\n"
        "â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€\n\n"
        "ðŸ¤– <b>AUTO-POSTING</b>\n"
        "Posts 10-12x daily (08:00-23:00 EAT) automatically.\n"
        "Rotates across 6 services. No duplicates per day.\n\n"
        "ðŸ“£ <b>BROADCAST (Manual Post)</b>\n"
        "Send any message here - goes to channel with buttons.\n"
        "Supports: Text, Photo, Video, GIF\n"
        "Photos get watermark automatically.\n\n"
        "â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€\n"
        "âš™ï¸ <b>BOT CONTROLS</b>\n"
        "/pause - Stop auto-posting\n"
        "/resume - Resume auto-posting\n"
        "/status - Current bot status\n"
        "/schedule - Today's post times\n"
        "/history - Last 10 posts sent\n\n"
        "â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€\n"
        "ðŸ“Ž <b>MEDIA (Video/Photo per service)</b>\n"
        "/addmedia\n"
        "  1 - Tap the service name\n"
        "  2 - Send the video or photo\n"
        "  Saved - used in auto-posts automatically\n\n"
        "/listmedia - See all saved media\n"
        "/removemedia - Delete a saved media file\n\n"
        "â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€\n"
        "ðŸ–¼ <b>WATERMARK</b>\n"
        "All photos: EVALON WINNERS BOT diagonal\n"
        "All videos: watermark in caption\n\n"
        "â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€\n"
        "ðŸ’¬ <b>BUTTONS ON EVERY POST</b>\n"
        "Each post has 1 button per service linking to @evalonwinnersbot.",
        parse_mode="HTML"
    )

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    db_set("paused", True)
    await update.message.reply_text("â¸ <b>Auto-posting paused.</b>\nUse /resume to restart.", parse_mode="HTML")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    db_set("paused", False)
    await update.message.reply_text("â–¶ï¸ <b>Auto-posting resumed!</b>", parse_mode="HTML")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    paused      = db_get("paused", False)
    todays      = db_get_todays_posts()
    now         = datetime.now(timezone.utc)
    eat_time    = f"{(now.hour+3)%24:02d}:{now.minute:02d} EAT"
    await update.message.reply_text(
        f"ðŸ“Š <b>AUTOPOST BOT STATUS</b>\n\n"
        f"{'â¸ PAUSED' if paused else 'â–¶ï¸ RUNNING'}\n"
        f"ðŸ• Time: {eat_time}\n"
        f"ðŸ“¬ Posts today: <b>{len(todays)}</b>\n"
        f"ðŸ—‚ Services posted: {', '.join(set(todays)) or 'none'}\n"
        f"ðŸ’¾ DB: {'âœ… PostgreSQL' if DATABASE_URL else 'âš ï¸ Local'}",
        parse_mode="HTML"
    )

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    hist = db_get_history(limit=10)
    if not hist:
        await update.message.reply_text("ðŸ“­ No post history yet."); return

    SERVICE_EMOJI = {
        "vip_signals": "ðŸ‘‘", "auto_trading_bot": "ðŸ¤–", "social_trading": "âœ¨",
        "manual_bot": "ðŸŽ", "indicators": "ðŸ“Š", "spin_invite": "ðŸŽ°",
    }
    lines = ["ðŸ“‹ <b>LAST 10 POSTS</b>\n"]
    for h in hist:
        svc     = h.get("service", "?")
        emoji   = SERVICE_EMOJI.get(svc, "ðŸ“Œ")
        preview = h.get("preview") or h.get("text_preview", "")[:60]
        posted  = h.get("posted_at", "")
        if hasattr(posted, "strftime"):
            t = f"{(posted.hour+3)%24:02d}:{posted.minute:02d} EAT"
        else:
            try:
                dt = datetime.fromisoformat(str(posted).replace("Z",""))
                t  = f"{(dt.hour+3)%24:02d}:{dt.minute:02d} EAT"
            except: t = str(posted)[:16]
        lines.append(f"{emoji} <b>{svc}</b> â€” {t}\n_{preview[:70]}..._\n")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    now      = datetime.now(timezone.utc)
    sched    = get_todays_schedule()
    todays   = db_get_todays_posts()
    eat_now  = (now.hour + 3) % 24

    lines = [f"ðŸ—“ <b>TODAY'S SCHEDULE â€” {now.strftime('%d %b %Y')}</b>\n"]
    for i, (h, m) in enumerate(sched):
        eat_h  = (h + 3) % 24
        status = "âœ… Done" if i < len(todays) else ("ðŸ”„ Next" if eat_h == eat_now else "â³ Pending")
        lines.append(f"`{eat_h:02d}:{m:02d} EAT` â€” {status}")

    lines.append(f"\nðŸ“¬ Sent: <b>{len(todays)}/{len(sched)}</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ============================================================
# AUTO-POST LOOP
# ============================================================
async def autopost_loop(bot: Bot):
    logger.info("AutoPost loop started.")

    while True:
        now = datetime.now(timezone.utc)

        # Check if paused
        if db_get("paused", False):
            logger.info("Bot is paused â€” sleeping 5 min")
            await asyncio.sleep(300)
            continue

        today_str = now.strftime("%Y-%m-%d")

        # Load or create today's schedule from DB
        saved = db_get("schedule_today", {})
        if saved.get("date") != today_str:
            schedule = get_todays_schedule()
            db_set("schedule_today", {"date": today_str, "slots": schedule, "done": []})
            logger.info(f"New schedule {today_str}: {schedule}")
        else:
            schedule = saved["slots"]

        saved = db_get("schedule_today", {})
        done  = saved.get("done", [])

        # Find next pending slot
        pending = [s for s in schedule if s not in done]

        if not pending:
            await asyncio.sleep(3600)
            continue

        next_h, next_m = pending[0]
        target = datetime(now.year, now.month, now.day, next_h, next_m, 0, tzinfo=timezone.utc)

        if now >= target:
            todays_posts = db_get_todays_posts()
            service, text = pick_post(avoid_services=todays_posts)

            try:
                await send_post(bot, service, text)
                db_log_post(service, text)
                logger.info(f"âœ… Auto-posted [{service}] {now.strftime('%H:%M UTC')}")
            except Exception as e:
                logger.error(f"âŒ Auto-post failed: {e}")

            # Mark slot as done in DB
            done.append([next_h, next_m])
            db_set("schedule_today", {"date": today_str, "slots": schedule, "done": done})
            await asyncio.sleep(60)
        else:
            wait = max(30, int((target - now).total_seconds()) - 60)
            await asyncio.sleep(min(wait, 3600))

# ============================================================
# KEEP-ALIVE
# ============================================================
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"EVALON AutoPost Bot v2 - Running")
    def log_message(self, *args): pass

def start_keepalive():
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), KeepAlive).serve_forever(),
        daemon=True
    ).start()
    logger.info(f"Keep-alive on port {port}")

# ============================================================
# MAIN
# ============================================================
async def main_async():
    if not BOT_TOKEN:
        raise ValueError("AUTOPOST_BOT_TOKEN not set!")

    db_init()
    start_keepalive()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_help))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("pause",       cmd_pause))
    app.add_handler(CommandHandler("resume",      cmd_resume))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("history",     cmd_history))
    app.add_handler(CommandHandler("schedule",    cmd_schedule))
    app.add_handler(CommandHandler("addmedia",    cmd_addmedia))
    app.add_handler(CommandHandler("listmedia",   cmd_listmedia))
    app.add_handler(CommandHandler("removemedia", cmd_removemedia))
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(handle_addmedia_callback,
                                         pattern="^(addmedia_wait_|removemedia_)"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION),
        handle_broadcast
    ))

    print("=" * 42)
    print("  EVALON AUTOPOST BOT v3")
    print(f"  Channel : {CHANNEL_ID}")
    print(f"  Admin   : {ADMIN_ID}")
    print(f"  DB      : {'PostgreSQL' if DATABASE_URL else 'Local'}")
    print("  Posts   : 10-12/day auto")
    print("=" * 42)

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await autopost_loop(app.bot)
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main_async())
