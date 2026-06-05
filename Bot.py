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
from datetime import datetime, timezone, timedelta, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
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

async def download_video(bot: Bot, file_id: str) -> bytes:
    """Download a Telegram video as bytes."""
    file = await bot.get_file(file_id)
    return bytes(await file.download_as_bytearray())

def add_video_watermark(video_bytes: bytes) -> bytes:
    """Burn EVALON WINNERS BOT watermark onto video using ffmpeg."""
    import subprocess, tempfile, os
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as inp:
            inp.write(video_bytes)
            inp_path = inp.name
        out_path = inp_path.replace(".mp4", "_wm.mp4")
        text = WATERMARK_TEXT
        # ffmpeg drawtext filter - diagonal watermark bottom-right
        cmd = [
            "ffmpeg", "-y", "-i", inp_path,
            "-vf", (
                f"drawtext=text='{text}':fontsize=24:fontcolor=white@0.6:"
                f"x=w-tw-20:y=h-th-20:shadowcolor=black@0.5:shadowx=2:shadowy=2"
            ),
            "-c:a", "copy",
            "-preset", "ultrafast",
            out_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and os.path.exists(out_path):
            with open(out_path, "rb") as f:
                wm_bytes = f.read()
            os.unlink(inp_path)
            os.unlink(out_path)
            return wm_bytes
        else:
            logger.warning(f"ffmpeg watermark failed: {result.stderr.decode()}")
    except Exception as e:
        logger.warning(f"Video watermark error: {e}")
    finally:
        try: os.unlink(inp_path)
        except: pass
        try: os.unlink(out_path)
        except: pass
    return video_bytes  # fallback: original video

# ============================================================
# BUTTONS \u2014 per service
# ============================================================
def make_keyboard(service: str) -> InlineKeyboardMarkup:
    """1 button per post. Label and deep-link param change per service."""
    SERVICE_BUTTONS = {
        "vip_signals":      ("\U0001f451 Join VIP Now",        "vip"),
        "auto_trading_bot": ("\U0001f916 Start Auto Trading",  "auto"),
        "social_trading":   ("\u2728 Start Social Copy",   "copy"),
        "manual_bot":       ("\U0001f381 Claim Free Bot",      "freebooters"),
        "indicators":       ("\U0001f4ca Get Indicators",      "indicator"),
        "spin_invite":      ("\U0001f3b0 Spin & Save 70%",     "spin"),
    }
    label, param = SERVICE_BUTTONS.get(service, ("\U0001f451 Access Now", "vip"))
    url = f"{BOT_MAIN}?start={param}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, url=url)],
    ])

def make_broadcast_keyboard() -> InlineKeyboardMarkup:
    """Single button for admin broadcast posts."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f451 Join VIP Now", url=f"{BOT_MAIN}?start=vip")],
    ])

# ============================================================
# POST CONTENT \u2014 6 SERVICES (no @mention in text, button handles it)
# ============================================================
POSTS = {

    "vip_signals": [
        (
            "\U0001f4ca EVALON VIP SIGNALS\n\n"
            "\U0001f525 Non-Martingale signals only\n\n"
            "\u2705 8 to 10 signals per day\n"
            "\u2705 Monday to Friday \u2014 consistent delivery\n"
            "\u2705 BUY/SELL direction with expiry time\n"
            "\u2705 WIN/LOSS results after every trade\n"
            "\u2705 High accuracy entries \u2014 no guessing\n\n"
            "\U0001f48e Trade smarter. Follow the signal."
        ),
        (
            "\u26a1 TIRED OF LOSING TRADES?\n\n"
            "Switch to EVALON VIP SIGNALS\n\n"
            "\U0001f4c8 8\u201310 clean signals every trading day\n"
            "\U0001f3af Non-Martingale \u2014 no dangerous recovery trades\n"
            "\U0001f4f2 Signals delivered directly to your Telegram\n"
            "\u2705 Monday to Friday, session by session\n\n"
            "Stop guessing. Start winning."
        ),
        (
            "\U0001f3c6 EVALON VIP SIGNALS \u2014 THE DIFFERENCE\n\n"
            "While others use Martingale and blow accounts...\n\n"
            "We use pure strategy:\n"
            "\U0001f4ca 8\u201310 signals daily\n"
            "\U0001f3af Non-Martingale \u2014 protect your capital\n"
            "\u23f0 Mon\u2013Fri, every session\n"
            "\U0001f4f2 Real results. WIN/LOSS every trade\n\n"
            "Your capital deserves better."
        ),
        (
            "\U0001f4f2 EVALON VIP SIGNALS\n\n"
            "Every weekday you get:\n\n"
            "\U0001f514 Signal notification\n"
            "\U0001f4c8 Asset + direction + expiry\n"
            "\u2705 Result after every trade\n\n"
            "\U0001f3af Non-Martingale only \u2014 clean and safe\n"
            "\U0001f5d3 Monday to Friday \u2014 8 to 10 signals per session\n\n"
            "Your edge in the market starts here."
        ),
        (
            "\U0001f4ac Quick question.\n"
            "\n"
            "How many trades did you lose this week because you had no plan?\n"
            "\n"
            "Our VIP members don't guess.\n"
            "They follow a signal \u2014 entry, direction, expiry.\n"
            "Then wait for the result.\n"
            "\n"
            "That's it. No stress. No confusion.\n"
            "\n"
            "Ready to trade with a plan? \U0001f447"
        ),
        (
            "\U0001f305 Morning check-in.\n"
            "\n"
            "The market is open.\n"
            "Signals are being prepared.\n"
            "\n"
            "VIP members already know what to trade today.\n"
            "Do you?\n"
            "\n"
            "8\u201310 signals. Non-Martingale. Mon\u2013Fri.\n"
            "Your edge starts here \U0001f447"
        ),
        (
            "\U0001f319 End of session.\n"
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
            "Join before tomorrow's session \U0001f447"
        ),
        (
            "\U0001f4cc One thing separates profitable traders from the rest.\n"
            "\n"
            "A consistent entry strategy.\n"
            "\n"
            "Not luck.\n"
            "Not more screen time.\n"
            "Not a bigger deposit.\n"
            "\n"
            "Just clean, consistent signals \u2014 followed with discipline.\n"
            "\n"
            "EVALON VIP gives you exactly that \U0001f447"
        ),
        (
            "\U0001f3af What does a VIP signal look like?\n"
            "\n"
            "\U0001f4ca Asset: EUR/USD OTC\n"
            "\U0001f4c8 Direction: CALL ⬆️\n"
            "\u23f1 Expiry: 5 minutes\n"
            "\n"
            "That's all you need.\n"
            "No analysis. No confusion.\n"
            "Just follow and wait.\n"
            "\n"
            "8\u201310 of these every trading day \U0001f447"
        ),
    ],

    "auto_trading_bot": [
        (
            "\U0001f916 EVALON AUTO TRADING BOT\n\n"
            "Set it. Forget it. Profit.\n\n"
            "\u2705 Works on ALL brokers\n"
            "\u2705 Non-Martingale strategy built in\n"
            "\u2705 Stop Loss & Take Profit settings\n"
            "\u2705 Compounding settings available\n"
            "\U0001f4c8 87% to 95% accuracy\n\n"
            "Let the bot trade while you live your life."
        ),
        (
            "\U0001f527 TRADE AUTOMATICALLY WITH EVALON BOT\n\n"
            "No screen time needed.\n\n"
            "\U0001f916 Fully automated trading\n"
            "\U0001f512 Stop Loss protection\n"
            "\U0001f4b0 Take Profit settings\n"
            "\U0001f4c8 Compounding to grow your account\n"
            "\U0001f310 All brokers supported\n"
            "\U0001f3af 87\u201395% accuracy\n\n"
            "Your account works even when you sleep."
        ),
        (
            "\U0001f4b0 WANT YOUR MONEY WORKING FOR YOU?\n\n"
            "EVALON Auto Trading Bot does exactly that.\n\n"
            "\u2705 All brokers \u2014 no restrictions\n"
            "\u2705 Non-Martingale \u2014 capital protected\n"
            "\u2705 Customizable Stop & Take Profit\n"
            "\u2705 Compounding settings\n"
            "\u2705 87\u201395% accuracy record\n\n"
            "Set up once. Earn consistently."
        ),
        (
            "\U0001f310 ALL BROKERS. ONE BOT.\n\n"
            "EVALON Auto Trading Bot supports every major broker.\n\n"
            "\U0001f4ca Non-Martingale strategy\n"
            "\U0001f512 Built-in Stop Loss & Take Profit\n"
            "\U0001f4c8 87\u201395% accuracy\n"
            "\U0001f4b9 Compounding mode to scale profits\n\n"
            "Start automated trading today."
        ),
        (
            "\U0001f4ac Be honest.\n"
            "\n"
            "How much time do you spend watching charts every day?\n"
            "\n"
            "2 hours? 4 hours? More?\n"
            "\n"
            "EVALON Auto Bot handles it all.\n"
            "You set it up once \u2014 it runs, trades, and manages risk.\n"
            "\n"
            "Your time is worth more than a screen \U0001f447"
        ),
        (
            "\U0001f305 While you were sleeping last night...\n"
            "\n"
            "Our Auto Trading Bot was running.\n"
            "\n"
            "\u2705 Scanning the market\n"
            "\u2705 Placing trades\n"
            "\u2705 Managing Stop Loss\n"
            "\u2705 Protecting your capital\n"
            "\n"
            "Automated. Consistent. Safe.\n"
            "Set it up today \U0001f447"
        ),
        (
            "\U0001f512 The biggest fear in trading?\n"
            "\n"
            "Losing more than you planned.\n"
            "\n"
            "That's why EVALON Auto Bot has:\n"
            "\U0001f6d1 Stop Loss \u2014 cuts losses automatically\n"
            "\U0001f4b0 Take Profit \u2014 locks in gains\n"
            "\U0001f4c8 Compounding \u2014 grows your account steadily\n"
            "\n"
            "Risk managed. Always \U0001f447"
        ),
        (
            "\U0001f4ca 87\u201395% accuracy.\n"
            "\n"
            "That's the track record of EVALON Auto Trading Bot.\n"
            "\n"
            "Not a promise.\n"
            "Not a guess.\n"
            "A result \u2014 built on Non-Martingale strategy and consistent execution.\n"
            "\n"
            "All brokers supported. Start today \U0001f447"
        ),
        (
            "\U0001f527 Setup takes less than 5 minutes.\n"
            "\n"
            "1️⃣ Open the bot\n"
            "2️⃣ Connect your broker\n"
            "3️⃣ Set your Stop Loss & Take Profit\n"
            "4️⃣ Start\n"
            "\n"
            "That's it.\n"
            "The bot does the rest \u2014 24/7.\n"
            "\n"
            "Works on ALL brokers \U0001f447"
        ),
    ],

    "social_trading": [
        (
            "\U0001f517 EVALON SOCIAL TRADING \u2014 POCKET OPTION\n\n"
            "Don't trade alone. Copy a proven account.\n\n"
            "\u2705 Copy trades directly from our Pocket Option account\n"
            "\U0001f4c5 Monday to Monday \u2014 no weekends off\n"
            "\U0001f319 OTC trading included \u2014 24/7 coverage\n"
            "\U0001f4f2 Everything automated \u2014 just connect and earn\n\n"
            "The simplest way to profit from trading."
        ),
        (
            "\U0001f4cb COPY TRADING \u2014 EVALON SOCIAL TRADING\n\n"
            "What we trade, you trade. Automatically.\n\n"
            "\U0001f3af Pocket Option platform\n"
            "\U0001f4c5 7 days a week \u2014 Monday to Monday\n"
            "\U0001f319 OTC markets included \u2014 no downtime\n"
            "\u2705 No experience needed \u2014 just copy\n\n"
            "Your account mirrors our trades in real time."
        ),
        (
            "\U0001f319 TRADING DOESN'T STOP \u2014 NEITHER DO WE\n\n"
            "EVALON Social Trading on Pocket Option\n\n"
            "\U0001f4c5 Active Monday to Monday\n"
            "\U0001f319 OTC included \u2014 weekends too\n"
            "\U0001f517 Auto-copy every trade we make\n"
            "\u2705 Pocket Option account required\n\n"
            "While others rest, your account keeps growing."
        ),
        (
            "\U0001f4a1 NEW TO TRADING? START HERE.\n\n"
            "EVALON Social Trading \u2014 copy without learning.\n\n"
            "\u2705 Connect your Pocket Option account\n"
            "\u2705 Our trades copy to yours automatically\n"
            "\U0001f4c5 7 days a week including OTC\n"
            "\U0001f3af No analysis needed \u2014 we do it for you\n\n"
            "Your easiest path to consistent profits."
        ),
        (
            "\U0001f4ac What if you could profit from trading...\n"
            "\n"
            "Without knowing how to trade?\n"
            "\n"
            "That's exactly what EVALON Social Trading does.\n"
            "\n"
            "Our Pocket Option account trades.\n"
            "Your account copies \u2014 automatically.\n"
            "\n"
            "No experience needed. No charts. No stress \U0001f447"
        ),
        (
            "\U0001f319 It's the weekend.\n"
            "\n"
            "Most traders are offline.\n"
            "\n"
            "But EVALON Social Trading?\n"
            "Still running.\n"
            "\n"
            "OTC markets are open.\n"
            "Your account is still copying trades.\n"
            "Monday to Monday \u2014 no breaks.\n"
            "\n"
            "Set it and forget it \U0001f447"
        ),
        (
            "\U0001f4cb Copy trading \u2014 simplified.\n"
            "\n"
            "You don't need to:\n"
            "\u274c Analyze charts\n"
            "\u274c Read indicators\n"
            "\u274c Know entry strategies\n"
            "\n"
            "You just need to:\n"
            "\u2705 Connect your Pocket Option account\n"
            "\u2705 Let EVALON do the rest\n"
            "\n"
            "That's the whole process \U0001f447"
        ),
        (
            "\U0001f550 How much time does copy trading take?\n"
            "\n"
            "Setup: 5 minutes.\n"
            "Daily management: 0 minutes.\n"
            "\n"
            "EVALON Social Trading runs itself.\n"
            "OTC included \u2014 active 7 days a week.\n"
            "\n"
            "Your easiest trading decision \U0001f447"
        ),
        (
            "\U0001f517 One connection. Endless trades.\n"
            "\n"
            "Link your Pocket Option account to EVALON Social Trading.\n"
            "\n"
            "Every trade we place \u2014 you get it too.\n"
            "Same entry. Same direction. Same result.\n"
            "\n"
            "Monday to Monday. OTC included.\n"
            "No screen time required \U0001f447"
        ),
    ],

    "manual_bot": [
        (
            "\U0001f381 EVALON MANUAL BOT \u2014 FREE ACCESS\n\n"
            "Get it simply by registering through our broker links.\n\n"
            "\u2705 Register via our bot using partner broker links\n"
            "\u2705 Manual bot access activated automatically\n"
            "\U0001f517 Multiple brokers available\n"
            "\U0001f4f2 Everything handled inside the bot\n\n"
            "The easiest free tool you'll get today."
        ),
        (
            "\U0001f91d REGISTER. GET THE BOT. START TRADING.\n\n"
            "EVALON Manual Bot \u2014 yours when you sign up.\n\n"
            "\U0001f4cb Sign up through broker links inside our bot\n"
            "\u2705 Manual bot unlocked instantly\n"
            "\U0001f310 Multiple supported brokers\n"
            "\U0001f4b0 Zero extra cost \u2014 just register\n\n"
            "Free access. Real results."
        ),
        (
            "\U0001f513 UNLOCK THE EVALON MANUAL BOT\n\n"
            "No purchase needed.\n\n"
            "1️⃣ Open our bot\n"
            "2️⃣ Register via a broker link\n"
            "3️⃣ Manual bot access \u2014 activated \u2705\n\n"
            "Simple. Fast. Free."
        ),
        (
            "\U0001f4ac Did you know?\n"
            "\n"
            "You can get the EVALON Manual Bot completely free.\n"
            "\n"
            "No payment needed.\n"
            "Just register with a broker through our bot.\n"
            "\n"
            "Takes 3 minutes.\n"
            "Access unlocks instantly.\n"
            "\n"
            "The free tool most traders don't know about \U0001f447"
        ),
        (
            "\U0001f914 Why pay for a bot when you can get one free?\n"
            "\n"
            "EVALON Manual Bot is unlocked the moment you:\n"
            "\n"
            "1️⃣ Open our bot\n"
            "2️⃣ Register via any partner broker link\n"
            "3️⃣ Done \u2014 bot activated \u2705\n"
            "\n"
            "Multiple brokers available.\n"
            "Zero cost. Real access \U0001f447"
        ),
        (
            "\u23f1 3 minutes from now...\n"
            "\n"
            "You could have access to the EVALON Manual Bot.\n"
            "\n"
            "Register via a broker link inside our bot.\n"
            "Access activates automatically.\n"
            "No waiting. No payment.\n"
            "\n"
            "Simplest free tool in trading \U0001f447"
        ),
        (
            "\U0001f310 Multiple brokers. One bot.\n"
            "\n"
            "EVALON Manual Bot works across our partner brokers.\n"
            "\n"
            "Register through any of them \u2014 inside our bot.\n"
            "Manual bot access is yours immediately.\n"
            "\n"
            "Pick your broker. Start trading.\n"
            "It's completely free \U0001f447"
        ),
        (
            "\U0001f381 Free doesn't mean basic.\n"
            "\n"
            "EVALON Manual Bot gives you:\n"
            "\n"
            "\u2705 Manual trading signals\n"
            "\u2705 Entry guidance\n"
            "\u2705 Broker access through one place\n"
            "\n"
            "All for registering through our partner link.\n"
            "Start here \U0001f447"
        ),
    ],

    "indicators": [
        (
            "\U0001f4c9 EVALON INDICATORS\n\n"
            "Available on MT4, MT5 & TradingView\n\n"
            "\u2705 Non-repaint \u2014 what you see is what you get\n"
            "\u2705 Get access with any Evalon service\n"
            "\U0001f4ca Works on all major pairs and assets\n"
            "\U0001f3af Precise entry signals on your chart\n\n"
            "See the market clearly. Trade with confidence."
        ),
        (
            "\U0001f4ca NON-REPAINT INDICATORS \u2014 MT4, MT5, TRADINGVIEW\n\n"
            "No more signals that disappear after the fact.\n\n"
            "\u2705 Evalon Indicators never repaint\n"
            "\u2705 Available on all 3 platforms\n"
            "\U0001f381 Included when you join any Evalon service\n\n"
            "Trade what you see. Every time."
        ),
        (
            "\U0001f5a5️ TRADINGVIEW • MT4 • MT5\n\n"
            "EVALON Indicators \u2014 on every platform you use.\n\n"
            "\U0001f4cc Non-repaint signals on your chart\n"
            "\u2705 No confusion \u2014 clear BUY/SELL\n"
            "\U0001f381 Access granted with any Evalon service\n\n"
            "Your charts. Our precision."
        ),
        (
            "\U0001f4ac Ever placed a trade...\n"
            "\n"
            "Then watched the signal disappear from your chart?\n"
            "\n"
            "That's a repainting indicator.\n"
            "It changes history \u2014 so it always looks right after the fact.\n"
            "\n"
            "EVALON Indicators never repaint.\n"
            "What you see is exactly what happened \U0001f447"
        ),
        (
            "\U0001f5a5️ Which platform do you use?\n"
            "\n"
            "MT4 \u2705\n"
            "MT5 \u2705\n"
            "TradingView \u2705\n"
            "\n"
            "EVALON Indicators work on all three.\n"
            "Non-repaint. Clear BUY/SELL signals.\n"
            "Included with any Evalon service \U0001f447"
        ),
        (
            "\U0001f4cc A good indicator does one thing well.\n"
            "\n"
            "It tells you when to enter.\n"
            "\n"
            "Not maybe.\n"
            "Not 'it depends'.\n"
            "A clear signal \u2014 on your chart \u2014 right when you need it.\n"
            "\n"
            "EVALON Indicators are built for exactly that.\n"
            "MT4, MT5 & TradingView \U0001f447"
        ),
        (
            "\U0001f3af Precision matters in trading.\n"
            "\n"
            "A signal that repaints is worse than no signal.\n"
            "It gives you false confidence.\n"
            "\n"
            "EVALON Indicators are built different:\n"
            "\u2705 Non-repaint \u2014 locked when candle closes\n"
            "\u2705 Works across all major assets\n"
            "\u2705 Available on 3 platforms\n"
            "\n"
            "See the market clearly \U0001f447"
        ),
        (
            "\U0001f4ca Indicators that work with you \u2014 not against you.\n"
            "\n"
            "No clutter. No confusion.\n"
            "\n"
            "Just clean entry signals on your chart.\n"
            "Non-repaint. Multi-platform.\n"
            "Included free with any Evalon service.\n"
            "\n"
            "MT4 • MT5 • TradingView \U0001f447"
        ),
    ],

    "spin_invite": [
        (
            "\U0001f3b0 SPIN & INVITE \u2014 SAVE UP TO 70%\n\n"
            "Our services don't have to cost full price.\n\n"
            "\U0001f3af Spin to win discounts on any Evalon service\n"
            "\U0001f465 Invite friends and unlock more savings\n"
            "\U0001f4b8 Up to 70% off on VIP, Bots, Social Trading & more\n\n"
            "Why pay full price when you don't have to?"
        ),
        (
            "\U0001f4b8 GET EVALON SERVICES FOR LESS\n\n"
            "Spin & Invite \u2014 your shortcut to big discounts.\n\n"
            "\U0001f3b0 Spin inside the bot for instant discounts\n"
            "\U0001f4f2 Invite a friend \u2014 unlock more savings\n"
            "\U0001f3f7️ Up to 70% off any service\n\n"
            "VIP. Auto Bot. Social Trading. Indicators.\n"
            "All discounted \u2014 all accessible."
        ),
        (
            "\U0001f465 INVITE FRIENDS. SAVE BIG.\n\n"
            "EVALON Spin & Invite Access\n\n"
            "\U0001f3b0 Spin for surprise discounts\n"
            "\U0001f91d Refer friends and save even more\n"
            "\U0001f4b8 Discounts up to 70% on all services\n\n"
            "The more you share, the less you pay."
        ),
        (
            "\U0001f4ac Quick tip.\n"
            "\n"
            "Before you pay full price for any EVALON service \u2014\n"
            "open the bot and spin first.\n"
            "\n"
            "You might get 20%, 40%, even 70% off.\n"
            "Takes 10 seconds.\n"
            "\n"
            "Why pay more than you have to? \U0001f447"
        ),
        (
            "\U0001f465 Know someone who wants to start trading?\n"
            "\n"
            "Invite them through EVALON.\n"
            "\n"
            "They get access to our services.\n"
            "You unlock deeper discounts \u2014 up to 70% off.\n"
            "\n"
            "Share the opportunity.\n"
            "Save together \U0001f447"
        ),
        (
            "\U0001f3b0 Not ready to pay full price yet?\n"
            "\n"
            "That's fine.\n"
            "\n"
            "Spin inside the bot \u2014 you might not have to.\n"
            "\n"
            "Discounts on:\n"
            "\U0001f451 VIP Signals\n"
            "\U0001f916 Auto Trading Bot\n"
            "\u2728 Social Copy Trading\n"
            "\U0001f4ca Indicators\n"
            "\n"
            "One spin. Real savings \U0001f447"
        ),
        (
            "\U0001f4b8 The math is simple.\n"
            "\n"
            "Invite 1 friend → unlock a discount.\n"
            "Invite more → save more.\n"
            "Spin the wheel → instant discount.\n"
            "\n"
            "Up to 70% off any EVALON service.\n"
            "\n"
            "Most people never use this.\n"
            "You should \U0001f447"
        ),
        (
            "\U0001f3f7️ Discounts don't last forever.\n"
            "\n"
            "EVALON services are available at full price anytime.\n"
            "But discounts \u2014 those come from spinning and inviting.\n"
            "\n"
            "Once your discount expires, it resets.\n"
            "\n"
            "Spin now. Save now \U0001f447"
        ),
    ],
}

# ============================================================
# SCHEDULE \u2014 10 to 12 posts per day (08:00\u201323:00 EAT = 05:00\u201320:00 UTC)
# ============================================================
# 02:00-22:00 EAT = 23:00-19:00 UTC (masaa 20)
SCHEDULE_HOURS_UTC = [23, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]

def get_todays_schedule():
    """10-12 posts spread randomly — ONE post per chosen hour (02:00-22:00 EAT)."""
    count  = random.randint(10, 12)
    # Pick 10-12 distinct hours from the 20-hour window
    chosen = sorted(random.sample(SCHEDULE_HOURS_UTC, min(count, len(SCHEDULE_HOURS_UTC))))
    # One post per hour at a random minute — guarantees spacing
    return [(h, random.randint(5, 55)) for h in chosen]

# ============================================================
# DYNAMIC POSTS \u2014 day-aware and date-stamped
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
    Returns None randomly \u2014 so dynamic posts appear ~2x per day in rotation.
    """
    if random.random() > 0.25:  # 25% chance to return a dynamic post
        return None

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
                f"\U0001f4c5 {day_name} \u2014 {date_str}\n\n"
                "\U0001f4b0 VIP members are making money this weekend.\n\n"
                "While the forex market rests...\n"
                "Our Social Trading runs Monday to Monday \u2014 OTC included.\n\n"
                "The market never fully sleeps.\n"
                "Neither do we. \U0001f319\n\n"
                "Are you still on the outside? Join us now \U0001f447"
            ),
            (
                f"\U0001f5d3 Today is {day_name} \u2014 {date_str}\n\n"
                "Weekend is here \u2014 but profits don't wait.\n\n"
                "\u2705 Social Copy Trading is LIVE \u2014 OTC markets open\n"
                "\u2705 Auto Trading Bot is running \u2014 all brokers\n"
                "\u2705 VIP members are already ahead of you\n\n"
                "You can still join today \U0001f447"
            ),
        ]

    # --- FRIDAY posts ---
    elif is_friday:
        DYNAMIC_POSTS += [
            (
                f"\U0001f5d3 Today is Friday \u2014 {date_str}\n\n"
                "\u26a1 Weekend is starting \u2014 are you ready?\n\n"
                "VIP members already locked in profits this week.\n"
                "Social Trading keeps running through the weekend.\n\n"
                "Don't let another week pass without taking action.\n\n"
                "Join now \U0001f447"
            ),
            (
                f"\U0001f4c5 Friday \u2014 {date_str}\n\n"
                "\U0001f3c1 End of the trading week.\n\n"
                "This week our VIP members:\n"
                "\U0001f4c8 Received 8\u201310 signals daily\n"
                "\u2705 Non-Martingale \u2014 capital protected\n"
                "\U0001f4b0 Consistent profits every session\n\n"
                "Next week starts Monday.\n"
                "Will you be ready? Join before the weekend ends \U0001f447"
            ),
        ]

    # --- MONDAY posts ---
    elif is_monday:
        DYNAMIC_POSTS += [
            (
                f"\U0001f4c5 Monday \u2014 {date_str}\n\n"
                "\U0001f514 New week. New signals. New profits.\n\n"
                "VIP signals are LIVE from today.\n"
                "8\u201310 signals per day, Monday to Friday.\n\n"
                "If you missed last week \u2014 don't miss this one.\n\n"
                "Join now \U0001f447"
            ),
        ]

    # --- WEEKDAY posts (Tue/Wed/Thu) ---
    else:
        DYNAMIC_POSTS += [
            (
                f"\U0001f4c5 {day_name} \u2014 {date_str}\n\n"
                "\u26a1 VIP signals are running RIGHT NOW.\n\n"
                "While you're reading this, our members are:\n"
                "\U0001f4c8 Following live signals\n"
                "\u2705 Booking profits\n"
                "\U0001f916 Running auto bots on all brokers\n\n"
                "You're still on the outside.\n"
                "Fix that today \U0001f447"
            ),
            (
                f"\U0001f5d3 {day_name} \u2014 {date_str}\n\n"
                "\U0001f48e Another trading day. Another opportunity.\n\n"
                "EVALON VIP members get:\n"
                "\U0001f4ca 8\u201310 clean signals today\n"
                "\U0001f3af Non-Martingale only\n"
                "\U0001f4f2 Results after every trade\n\n"
                "Today's session is already running.\n"
                "Don't miss tomorrow's \u2014 join now \U0001f447"
            ),
        ]

    if not DYNAMIC_POSTS:
        return None

    text = random.choice(DYNAMIC_POSTS)
    return "vip_signals", text  # dynamic posts use VIP keyboard


def get_date_header() -> str:
    """Varied date/time header — 8 rotating styles including time greetings."""
    eat        = datetime.now(timezone.utc) + timedelta(hours=3)
    hour       = eat.hour
    day_name   = eat.strftime("%A")
    full_date  = eat.strftime("%d %B %Y")
    short_date = eat.strftime("%d %b")

    if 5 <= hour < 12:
        greeting = "Good morning"
    elif 12 <= hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    style = eat.timetuple().tm_yday % 8

    if style == 0:
        return f"\U0001f4c5 {day_name}, {full_date}\n\n"
    elif style == 1:
        return f"\U0001f4c5 {day_name}\n\n"
    elif style == 2:
        return f"\U0001f4c6 {short_date}\n\n"
    elif style == 3:
        return ""
    elif style == 4:
        return "This week \u2014\n\n"
    elif style == 5:
        return f"Today, {day_name} \u2014\n\n"
    elif style == 6:
        return f"{greeting} \U0001f305\n\n"
    else:
        return f"{greeting}, it's {day_name} \u2014\n\n"


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
    """Pick a post \u2014 avoid services already posted today, no repeated posts."""
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
# MEDIA STORAGE \u2014 DB-backed (replaces static VIDEO_FILE_IDS)
# ============================================================
SERVICES = ["vip_signals", "auto_trading_bot", "social_trading", "manual_bot", "indicators", "spin_invite"]
SERVICE_LABELS = {
    "vip_signals":      "\U0001f451 VIP Signals",
    "auto_trading_bot": "\U0001f916 Auto Trading Bot",
    "social_trading":   "\u2728 Social Copy Trading",
    "manual_bot":       "\U0001f381 Manual Bot",
    "indicators":       "\U0001f4ca Indicators",
    "spin_invite":      "\U0001f3b0 Spin & Invite",
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
                # Try to burn watermark on video
                try:
                    raw_v = await download_video(bot, item["file_id"])
                    wm_v  = add_video_watermark(raw_v)
                    bio_v = io.BytesIO(wm_v); bio_v.name = "post.mp4"
                    await bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=bio_v,
                        caption=text,
                        reply_markup=kb
                    )
                except Exception as ve:
                    logger.warning(f"Video wm upload failed: {ve}, using file_id")
                    cap = text + f"\n\n- {WATERMARK_TEXT}"
                    await bot.send_video(
                        chat_id=CHANNEL_ID,
                        video=item["file_id"],
                        caption=cap.strip(),
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
                    
                    reply_markup=kb
                )
            return
        except Exception as e:
            logger.warning(f"Media send failed, falling back to text: {e}")

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        
        reply_markup=kb,
        disable_web_page_preview=True
    )

# ============================================================
# /addmedia \u2014 admin sends video/photo + selects service
# ============================================================
async def cmd_addmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    # Show service selection buttons
    buttons = [[InlineKeyboardButton(label, callback_data=f"addmedia_wait_{svc}")]
               for svc, label in SERVICE_LABELS.items()]
    await update.message.reply_text(
        "\U0001f4ce Add Media\n\nWhich service is this video/photo for?\n\n"
        "1️⃣ Select the service below\n"
        "2️⃣ Then send the video or photo",
        
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def cmd_listmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    store = media_load()
    if not any(store.values()):
        await update.message.reply_text("\U0001f4ed No media saved yet.\nUse /addmedia to add."); return

    lines = ["\U0001f5c2 SAVED MEDIA\n"]
    for svc, items in store.items():
        if not items: continue
        label = SERVICE_LABELS.get(svc, svc)
        lines.append(f"{label} \u2014 {len(items)} file(s)")
        for i, m in enumerate(items):
            lines.append(f"  `{i}` \u2014 {m['type']}")
    lines.append("\n_Use /removemedia to delete_")
    await update.message.reply_text("\n".join(lines))

async def cmd_removemedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    store = media_load()
    buttons = []
    for svc, items in store.items():
        for i, m in enumerate(items):
            label = f"{SERVICE_LABELS.get(svc, svc)} \u2014 {m['type']} #{i}"
            buttons.append([InlineKeyboardButton(f"\U0001f5d1 {label}", callback_data=f"removemedia_{svc}_{i}")])
    if not buttons:
        await update.message.reply_text("\U0001f4ed No media to remove."); return
    await update.message.reply_text(
        "\U0001f5d1 Remove Media\n\nSelect item to remove:",
        
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
            f"\u2705 {label} selected.\n\n"
            "Now send me the video or photo to attach to this service.\n\n"
            "_It will be saved and used automatically in future auto-posts._",

        )

    elif data.startswith("removemedia_"):
        parts   = data.split("_", 2)
        # removemedia_{service}_{index}
        _, svc, idx_str = data.split("_", 2)
        # svc might have underscores \u2014 handle carefully
        # format: removemedia_{svc}_{i}
        last_underscore = data.rfind("_")
        idx_str = data[last_underscore+1:]
        svc     = data[len("removemedia_"):last_underscore]
        try:
            idx = int(idx_str)
            if media_remove(svc, idx):
                label = SERVICE_LABELS.get(svc, svc)
                await q.edit_message_text(f"\U0001f5d1 Removed media #{idx} from {label}")
            else:
                await q.edit_message_text("\u26a0️ Item not found.")
        except: await q.edit_message_text("\u26a0️ Error removing media.")


async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin sends message → channel with buttons. Also handles addmedia flow."""
    if update.effective_user.id != ADMIN_ID: return

    msg = update.message

    # ── ADDMEDIA FLOW ──
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
            f"\u2705 {media_type.capitalize()} saved for {label}!\n\n"
            f"It will now be attached to auto-posts for this service.\n"
            f"Use /listmedia to see all saved media.",

        )
        return

    # ── BROADCAST FLOW ──
    kb = make_broadcast_keyboard()
    try:
        if msg.photo:
            raw = await download_photo(context.bot, msg.photo[-1].file_id)
            wm  = add_watermark(raw)
            bio = io.BytesIO(wm); bio.name = "post.jpg"
            await context.bot.send_photo(
                chat_id=CHANNEL_ID, photo=bio,
                caption=msg.caption or "", reply_markup=kb
            )
        elif msg.video:
            try:
                raw_v = await download_video(context.bot, msg.video.file_id)
                wm_v  = add_video_watermark(raw_v)
                bio_v = io.BytesIO(wm_v); bio_v.name = "post.mp4"
                cap   = msg.caption or ""
                await context.bot.send_video(
                    chat_id=CHANNEL_ID, video=bio_v,
                    caption=cap, reply_markup=kb
                )
            except Exception as ve:
                logger.warning(f"Broadcast video wm failed: {ve}")
                cap = (msg.caption or "") + f"\n\n- {WATERMARK_TEXT}"
                await context.bot.send_video(
                    chat_id=CHANNEL_ID, video=msg.video.file_id,
                    caption=cap.strip(), reply_markup=kb
                )
        elif msg.animation:
            await context.bot.send_animation(
                chat_id=CHANNEL_ID, animation=msg.animation.file_id,
                caption=msg.caption or "", reply_markup=kb
            )
        elif msg.text:
            import re as _re
            url_pattern = r'https?://\S+'
            has_url = bool(_re.search(url_pattern, msg.text))
            await context.bot.send_message(
                chat_id=CHANNEL_ID, text=msg.text,
                reply_markup=kb,
                disable_web_page_preview=not has_url  # show preview for links
            )
        else:
            await msg.reply_text("\u26a0️ Unsupported message type."); return

        await msg.reply_text("\u2705 Sent to channel!")
        logger.info("Admin broadcast sent to channel")

    except Exception as e:
        await msg.reply_text(f"\u274c Failed: {e}")
        logger.error(f"Broadcast failed: {e}")

async def cmd_addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin sends /addlink — bot asks which service this is about."""
    if update.effective_user.id != ADMIN_ID: return
    msg = update.message
    args_text = msg.text.replace("/addlink", "").strip()

    if not args_text:
        await msg.reply_text(
            "How to use /addlink:\n\n"
            "Send:\n"
            "/addlink\n"
            "Your title or description here\n"
            "https://youtube.com/yourlink\n\n"
            "Bot will ask which service button to add."
        )
        return

    import re as _re
    url_match = _re.search(r'https?://\S+', args_text)
    if not url_match:
        await msg.reply_text("\u274c No link found. Include a URL starting with https://")
        return

    # Save the post text for step 2
    context.user_data["addlink_text"] = args_text
    context.user_data["addlink_url"]  = url_match.group()

    # Ask which service this post is about
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f451 VIP Signals",        callback_data="addlink_vip_signals")],
        [InlineKeyboardButton("\U0001f916 Auto Trading Bot",   callback_data="addlink_auto_trading_bot")],
        [InlineKeyboardButton("\u2728 Social Copy Trading",    callback_data="addlink_social_trading")],
        [InlineKeyboardButton("\U0001f381 Manual Bot",         callback_data="addlink_manual_bot")],
        [InlineKeyboardButton("\U0001f4ca Indicators",         callback_data="addlink_indicators")],
        [InlineKeyboardButton("\U0001f3b0 Spin & Invite",      callback_data="addlink_spin_invite")],
    ])
    await msg.reply_text("Which service is this post about?", reply_markup=kb)


async def cb_addlink_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Admin picks service — bot posts to channel with correct button."""
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return
    await q.answer()

    service   = q.data.replace("addlink_", "")
    post_text = context.user_data.pop("addlink_text", "")
    url       = context.user_data.pop("addlink_url", "")

    if not post_text or not url:
        await q.edit_message_text("\u274c Session expired. Please /addlink again.")
        return

    # Detect platform for first button label
    import re as _re
    if "youtube" in url or "youtu.be" in url:
        btn_label = "\U0001f534 Watch on YouTube"
    elif "tiktok" in url:
        btn_label = "\U0001f3b5 Watch on TikTok"
    elif "instagram" in url:
        btn_label = "\U0001f4f8 View on Instagram"
    elif "twitter" in url or "x.com" in url:
        btn_label = "\U0001f426 View on Twitter"
    elif "facebook" in url or "fb." in url:
        btn_label = "\U0001f464 View on Facebook"
    elif "t.me" in url:
        btn_label = "\U0001f4e2 Open in Telegram"
    else:
        btn_label = "\U0001f517 Open Link"

    # Second button matches the service
    SERVICE_BUTTONS = {
        "vip_signals":      ("\U0001f451 Join VIP Now",        "vip"),
        "auto_trading_bot": ("\U0001f916 Start Auto Trading",  "auto"),
        "social_trading":   ("\u2728 Start Social Copy",       "copy"),
        "manual_bot":       ("\U0001f381 Claim Free Bot",      "freebooters"),
        "indicators":       ("\U0001f4ca Get Indicators",      "indicator"),
        "spin_invite":      ("\U0001f3b0 Spin & Save 70%",     "spin"),
    }
    svc_label, svc_param = SERVICE_BUTTONS.get(service, ("\U0001f451 Join VIP Now", "vip"))

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(btn_label, url=url)],
        [InlineKeyboardButton(svc_label, url=f"{BOT_MAIN}?start={svc_param}")],
    ])

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            reply_markup=kb,
            disable_web_page_preview=False
        )
        await q.edit_message_text(f"\u2705 Posted to channel with {svc_label} button!")
    except Exception as e:
        await q.edit_message_text(f"\u274c Failed: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(
        "\U0001f4e1 EVALON AUTOPOST BOT v3\n"
        "- - - - - - - - -\n\n"
        "\U0001f916 AUTO-POSTING\n"
        "Posts 10-12x daily (02:00-22:00 EAT) automatically.\n"
        "Rotates across 6 services. No duplicates per day.\n\n"
        "\U0001f4e3 BROADCAST (Manual Post)\n"
        "Send any message here - goes to channel with buttons.\n"
        "Supports: Text, Photo, Video, GIF\n"
        "Photos get watermark automatically.\n\n"
        "- - - - - - - - -\n"
        "\U0001f527 BOT CONTROLS\n"
        "/pause - Stop auto-posting\n"
        "/resume - Resume auto-posting\n"
        "/status - Current bot status\n"
        "/schedule - Today's post times\n"
        "/history - Last 10 posts sent\n\n"
        "- - - - - - - - -\n"
        "\U0001f4ce MEDIA (Video/Photo per service)\n"
        "/addmedia\n"
        "  1 - Tap the service name\n"
        "  2 - Send the video or photo\n"
        "  Saved - used in auto-posts automatically\n\n"
        "/listmedia - See all saved media\n"
        "/removemedia - Delete a saved media file\n\n"
        "- - - - - - - - -\n"
        "\U0001f5bc WATERMARK\n"
        "All photos: EVALON WINNERS BOT diagonal\n"
        "All videos: watermark burned on video\n\n"
        "- - - - - - - - -\n"
        "\U0001f4ac BUTTONS ON EVERY POST\n"
        "Each post has 1 button per service linking to @evalonwinnersbot.\n\n""- - - - - - - - -\n""LINK POSTS\n""/addlink\n""  Write title/description + URL (YouTube, TikTok, etc)\n""  Bot posts with link preview + open button",
    )

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    db_set("paused", True)
    await update.message.reply_text("\u23f8 Auto-posting paused.\nUse /resume to restart.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    db_set("paused", False)
    await update.message.reply_text("\u25b6 Auto-posting resumed!")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    paused      = db_get("paused", False)
    todays      = db_get_todays_posts()
    now         = datetime.now(timezone.utc)
    eat_time    = f"{(now.hour+3)%24:02d}:{now.minute:02d} EAT"
    await update.message.reply_text(
        "\U0001f4ca AUTOPOST BOT STATUS\n\n"
        + ("\u23f8 PAUSED" if paused else "\u25b6 RUNNING") + "\n"
        + f"\U0001f550 Time: {eat_time}\n"
        + f"\U0001f4ec Posts today: {len(todays)}\n"
        + f"\U0001f5c2 Services posted: {', '.join(set(todays)) or 'none'}\n"
        + ("\U0001f4be DB: \u2705 PostgreSQL" if DATABASE_URL else "\U0001f4be DB: \u26a0 Local"),
    )

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    hist = db_get_history(limit=10)
    if not hist:
        await update.message.reply_text("\U0001f4ed No post history yet."); return

    SERVICE_EMOJI = {
        "vip_signals": "\U0001f451", "auto_trading_bot": "\U0001f916", "social_trading": "\u2728",
        "manual_bot": "\U0001f381", "indicators": "\U0001f4ca", "spin_invite": "\U0001f3b0",
    }
    lines = ["\U0001f4cb LAST 10 POSTS\n"]
    for h in hist:
        svc     = h.get("service", "?")
        emoji   = SERVICE_EMOJI.get(svc, "\U0001f4cc")
        preview = h.get("preview") or h.get("text_preview", "")[:60]
        posted  = h.get("posted_at", "")
        if hasattr(posted, "strftime"):
            t = f"{(posted.hour+3)%24:02d}:{posted.minute:02d} EAT"
        else:
            try:
                dt = datetime.fromisoformat(str(posted).replace("Z",""))
                t  = f"{(dt.hour+3)%24:02d}:{dt.minute:02d} EAT"
            except: t = str(posted)[:16]
        lines.append(f"{emoji} {svc} \u2014 {t}\n_{preview[:70]}..._\n")

    await update.message.reply_text("\n".join(lines))

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    now      = datetime.now(timezone.utc)
    sched    = get_todays_schedule()
    todays   = db_get_todays_posts()
    eat_now  = (now.hour + 3) % 24

    lines = [f"\U0001f5d3 TODAY'S SCHEDULE \u2014 {now.strftime('%d %b %Y')}\n"]
    for i, (h, m) in enumerate(sched):
        eat_h  = (h + 3) % 24
        status = "\u2705 Done" if i < len(todays) else ("\U0001f504 Next" if eat_h == eat_now else "\u23f3 Pending")
        lines.append(f"`{eat_h:02d}:{m:02d} EAT` \u2014 {status}")

    lines.append(f"\n\U0001f4ec Sent: {len(todays)}/{len(sched)}")
    await update.message.reply_text("\n".join(lines))

# ============================================================
# AUTO-POST LOOP
# ============================================================
async def autopost_loop(bot: Bot):
    logger.info("AutoPost loop started.")

    while True:
        now = datetime.now(timezone.utc)

        # Check if paused
        if db_get("paused", False):
            logger.info("Bot is paused \u2014 sleeping 5 min")
            await asyncio.sleep(300)
            continue

        today_str = now.strftime("%Y-%m-%d")

        # Load or create today's schedule from DB
        saved = db_get("schedule_today", {})
        if saved.get("date") != today_str:
            schedule = get_todays_schedule()
            # Mark already-passed slots as done on fresh start
            done = []
            for h, m in schedule:
                slot_dt = datetime(now.year, now.month, now.day, h, m, 0, tzinfo=timezone.utc)
                if now > slot_dt + timedelta(minutes=2):
                    done.append([h, m])
            db_set("schedule_today", {"date": today_str, "slots": schedule, "done": done})
            logger.info(f"New schedule {today_str}: {schedule}, skipping {len(done)} passed slots")
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
            total_slots  = len(schedule)
            slot_number  = len(done) + 1  # 1-based: which post is this today

            service, text = pick_post(avoid_services=todays_posts)

            # First post of the night: prepend morning/opening greeting
            if slot_number == 1:
                eat_now  = now + timedelta(hours=3)
                day_name = eat_now.strftime("%A")
                date_str = eat_now.strftime("%d %B %Y")
                opening  = (
                    f"Good evening! \U0001f319\n"
                    f"Today is {day_name}, {date_str}.\n"
                    f"Trading signals are live \u2014 let's make it count tonight.\n"
                    f"\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n\n"
                )
                text = opening + text

            # Last post of the night: append good night message
            elif slot_number == total_slots:
                closing = (
                    f"\n\n\u2014\u2014\u2014\u2014\u2014\u2014\u2014\u2014\n"
                    f"Good night! \U0001f303\n"
                    f"That's all from EVALON for tonight.\n"
                    f"Rest well \u2014 we'll be back tomorrow with more signals. \U0001f451"
                )
                text = text + closing

            try:
                await send_post(bot, service, text)
                db_log_post(service, text)
                logger.info(f"\u2705 Auto-posted [{service}] slot {slot_number}/{total_slots} {now.strftime('%H:%M UTC')}")
            except Exception as e:
                logger.error(f"\u274c Auto-post failed: {e}")

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
    app.add_handler(CommandHandler("addlink",      cmd_addlink))
    app.add_handler(CallbackQueryHandler(cb_addlink_service, pattern="^addlink_"))
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
