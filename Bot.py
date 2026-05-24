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
BOT_SHARE   = "https://t.me/kentehsharevvipbot"
BOT_TRADING = "https://t.me/evalonaitrading_bot"

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
BOT_SHARE   = "https://t.me/kentehsharevvipbot"
BOT_TRADING = "https://t.me/evalonaitrading_bot"

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
# BUTTONS — per service
# ============================================================
def make_keyboard(service: str) -> InlineKeyboardMarkup:
    """3 buttons on every post. First button label changes per service. No usernames visible."""
    labels = {
        "vip_signals":      "👑 Join VIP Now",
        "auto_trading_bot": "🤖 Start Auto Trading",
        "social_trading":   "✨ Start Social Copy",
        "manual_bot":       "🎁 Claim Free Bot",
        "indicators":       "📊 Get Indicators",
        "spin_invite":      "🎰 Spin & Save 70%",
    }
    first = labels.get(service, "👑 Access Now")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(first,                url=BOT_MAIN)],
        [InlineKeyboardButton("🎁 Claim Free Bot",  url=BOT_SHARE)],
        [InlineKeyboardButton("🤖 Start Auto Trading", url=BOT_TRADING)],
    ])

def make_broadcast_keyboard() -> InlineKeyboardMarkup:
    """Same 3 buttons for admin broadcast posts."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Join VIP Now",       url=BOT_MAIN)],
        [InlineKeyboardButton("🎁 Claim Free Bot",     url=BOT_SHARE)],
        [InlineKeyboardButton("🤖 Start Auto Trading", url=BOT_TRADING)],
    ])

# ============================================================
# POST CONTENT — 6 SERVICES (no @mention in text, button handles it)
# ============================================================
POSTS = {

    "vip_signals": [
        (
            "📊 *EVALON VIP SIGNALS*\n\n"
            "🔥 *Non-Martingale signals only*\n\n"
            "✅ 8 to 10 signals per day\n"
            "✅ Monday to Friday — consistent delivery\n"
            "✅ BUY/SELL direction with expiry time\n"
            "✅ WIN/LOSS results after every trade\n"
            "✅ High accuracy entries — no guessing\n\n"
            "💎 Trade smarter. Follow the signal."
        ),
        (
            "⚡ *TIRED OF LOSING TRADES?*\n\n"
            "Switch to *EVALON VIP SIGNALS*\n\n"
            "📈 8–10 clean signals every trading day\n"
            "🎯 Non-Martingale — no dangerous recovery trades\n"
            "📲 Signals delivered directly to your Telegram\n"
            "✅ Monday to Friday, session by session\n\n"
            "Stop guessing. Start winning."
        ),
        (
            "🏆 *EVALON VIP SIGNALS — THE DIFFERENCE*\n\n"
            "While others use Martingale and blow accounts...\n\n"
            "We use *pure strategy*:\n"
            "📊 8–10 signals daily\n"
            "🎯 Non-Martingale — protect your capital\n"
            "⏰ Mon–Fri, every session\n"
            "📲 Real results. WIN/LOSS every trade\n\n"
            "Your capital deserves better."
        ),
        (
            "📲 *EVALON VIP SIGNALS*\n\n"
            "Every weekday you get:\n\n"
            "🔔 Signal notification\n"
            "📈 Asset + direction + expiry\n"
            "✅ Result after every trade\n\n"
            "🎯 *Non-Martingale only* — clean and safe\n"
            "🗓 Monday to Friday — 8 to 10 signals per session\n\n"
            "Your edge in the market starts here."
        ),
    ],

    "auto_trading_bot": [
        (
            "🤖 *EVALON AUTO TRADING BOT*\n\n"
            "Set it. Forget it. Profit.\n\n"
            "✅ Works on *ALL brokers*\n"
            "✅ Non-Martingale strategy built in\n"
            "✅ Stop Loss & Take Profit settings\n"
            "✅ Compounding settings available\n"
            "📈 *87% to 95% accuracy*\n\n"
            "Let the bot trade while you live your life."
        ),
        (
            "⚙️ *TRADE AUTOMATICALLY WITH EVALON BOT*\n\n"
            "No screen time needed.\n\n"
            "🤖 Fully automated trading\n"
            "🔒 Stop Loss protection\n"
            "💰 Take Profit settings\n"
            "📈 Compounding to grow your account\n"
            "🌐 *All brokers supported*\n"
            "🎯 87–95% accuracy\n\n"
            "Your account works even when you sleep."
        ),
        (
            "💰 *WANT YOUR MONEY WORKING FOR YOU?*\n\n"
            "*EVALON Auto Trading Bot* does exactly that.\n\n"
            "✅ All brokers — no restrictions\n"
            "✅ Non-Martingale — capital protected\n"
            "✅ Customizable Stop & Take Profit\n"
            "✅ Compounding settings\n"
            "✅ 87–95% accuracy record\n\n"
            "Set up once. Earn consistently."
        ),
        (
            "🌐 *ALL BROKERS. ONE BOT.*\n\n"
            "EVALON Auto Trading Bot supports every major broker.\n\n"
            "📊 Non-Martingale strategy\n"
            "🔒 Built-in Stop Loss & Take Profit\n"
            "📈 87–95% accuracy\n"
            "💹 Compounding mode to scale profits\n\n"
            "Start automated trading today."
        ),
    ],

    "social_trading": [
        (
            "🔗 *EVALON SOCIAL TRADING — POCKET OPTION*\n\n"
            "Don't trade alone. Copy a proven account.\n\n"
            "✅ Copy trades directly from our Pocket Option account\n"
            "📅 *Monday to Monday* — no weekends off\n"
            "🌙 OTC trading included — 24/7 coverage\n"
            "📲 Everything automated — just connect and earn\n\n"
            "The simplest way to profit from trading."
        ),
        (
            "📋 *COPY TRADING — EVALON SOCIAL TRADING*\n\n"
            "What we trade, you trade. Automatically.\n\n"
            "🎯 Pocket Option platform\n"
            "📅 7 days a week — Monday to Monday\n"
            "🌙 OTC markets included — no downtime\n"
            "✅ No experience needed — just copy\n\n"
            "Your account mirrors our trades in real time."
        ),
        (
            "🌙 *TRADING DOESN'T STOP — NEITHER DO WE*\n\n"
            "*EVALON Social Trading on Pocket Option*\n\n"
            "📅 Active Monday to Monday\n"
            "🌙 OTC included — weekends too\n"
            "🔗 Auto-copy every trade we make\n"
            "✅ Pocket Option account required\n\n"
            "While others rest, your account keeps growing."
        ),
        (
            "💡 *NEW TO TRADING? START HERE.*\n\n"
            "*EVALON Social Trading* — copy without learning.\n\n"
            "✅ Connect your Pocket Option account\n"
            "✅ Our trades copy to yours automatically\n"
            "📅 7 days a week including OTC\n"
            "🎯 No analysis needed — we do it for you\n\n"
            "Your easiest path to consistent profits."
        ),
    ],

    "manual_bot": [
        (
            "🎁 *EVALON MANUAL BOT — FREE ACCESS*\n\n"
            "Get it simply by registering through our broker links.\n\n"
            "✅ Register via our bot using partner broker links\n"
            "✅ Manual bot access activated automatically\n"
            "🔗 Multiple brokers available\n"
            "📲 Everything handled inside the bot\n\n"
            "The easiest free tool you'll get today."
        ),
        (
            "🤝 *REGISTER. GET THE BOT. START TRADING.*\n\n"
            "*EVALON Manual Bot* — yours when you sign up.\n\n"
            "📋 Sign up through broker links inside our bot\n"
            "✅ Manual bot unlocked instantly\n"
            "🌐 Multiple supported brokers\n"
            "💰 Zero extra cost — just register\n\n"
            "Free access. Real results."
        ),
        (
            "🔓 *UNLOCK THE EVALON MANUAL BOT*\n\n"
            "No purchase needed.\n\n"
            "1️⃣ Open our bot\n"
            "2️⃣ Register via a broker link\n"
            "3️⃣ Manual bot access — activated ✅\n\n"
            "Simple. Fast. Free."
        ),
    ],

    "indicators": [
        (
            "📉 *EVALON INDICATORS*\n\n"
            "Available on *MT4, MT5 & TradingView*\n\n"
            "✅ *Non-repaint* — what you see is what you get\n"
            "✅ Get access with any Evalon service\n"
            "📊 Works on all major pairs and assets\n"
            "🎯 Precise entry signals on your chart\n\n"
            "See the market clearly. Trade with confidence."
        ),
        (
            "📊 *NON-REPAINT INDICATORS — MT4, MT5, TRADINGVIEW*\n\n"
            "No more signals that disappear after the fact.\n\n"
            "✅ Evalon Indicators never repaint\n"
            "✅ Available on all 3 platforms\n"
            "🎁 Included when you join any Evalon service\n\n"
            "Trade what you see. Every time."
        ),
        (
            "🖥️ *TRADINGVIEW • MT4 • MT5*\n\n"
            "*EVALON Indicators* — on every platform you use.\n\n"
            "📌 Non-repaint signals on your chart\n"
            "✅ No confusion — clear BUY/SELL\n"
            "🎁 Access granted with any Evalon service\n\n"
            "Your charts. Our precision."
        ),
    ],

    "spin_invite": [
        (
            "🎰 *SPIN & INVITE — SAVE UP TO 70%*\n\n"
            "Our services don't have to cost full price.\n\n"
            "🎯 Spin to win discounts on any Evalon service\n"
            "👥 Invite friends and unlock more savings\n"
            "💸 Up to *70% off* on VIP, Bots, Social Trading & more\n\n"
            "Why pay full price when you don't have to?"
        ),
        (
            "💸 *GET EVALON SERVICES FOR LESS*\n\n"
            "*Spin & Invite* — your shortcut to big discounts.\n\n"
            "🎰 Spin inside the bot for instant discounts\n"
            "📲 Invite a friend — unlock more savings\n"
            "🏷️ Up to 70% off any service\n\n"
            "VIP. Auto Bot. Social Trading. Indicators.\n"
            "All discounted — all accessible."
        ),
        (
            "👥 *INVITE FRIENDS. SAVE BIG.*\n\n"
            "*EVALON Spin & Invite Access*\n\n"
            "🎰 Spin for surprise discounts\n"
            "🤝 Refer friends and save even more\n"
            "💸 Discounts up to *70%* on all services\n\n"
            "The more you share, the less you pay."
        ),
    ],
}

# ============================================================
# SCHEDULE — 10 to 12 posts per day (08:00–23:00 EAT = 05:00–20:00 UTC)
# ============================================================
SCHEDULE_HOURS_UTC = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]

def get_todays_schedule():
    count  = random.randint(10, 12)
    chosen = sorted(random.sample(SCHEDULE_HOURS_UTC, min(count, len(SCHEDULE_HOURS_UTC))))
    return [(h, random.randint(0, 55)) for h in chosen]

# ============================================================
# DYNAMIC POSTS — day-aware and date-stamped
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
    Returns None randomly — so dynamic posts appear ~2x per day in rotation.
    """
    if random.random() > 0.25:  # 25% chance to return a dynamic post
        return None

    now     = datetime.now(timezone.utc)
    # Convert UTC to EAT (UTC+3)
    eat_hour = (now.hour + 3) % 24
    weekday  = now.weekday()  # 0=Monday, 6=Sunday
    day_name = DAY_NAMES[weekday]
    date_str = now.strftime("%d %B %Y")  # e.g. "24 May 2025"

    is_weekend  = weekday >= 5  # Saturday or Sunday
    is_friday   = weekday == 4
    is_monday   = weekday == 0

    DYNAMIC_POSTS = []

    # --- WEEKEND posts (Sat/Sun) ---
    if is_weekend:
        DYNAMIC_POSTS += [
            (
                f"📅 *{day_name} — {date_str}*\n\n"
                "💰 *VIP members are making money this weekend.*\n\n"
                "While the forex market rests...\n"
                "Our *Social Trading* runs *Monday to Monday* — OTC included.\n\n"
                "The market never fully sleeps.\n"
                "Neither do we. 🌙\n\n"
                "Are you still on the outside? Join us now 👇"
            ),
            (
                f"🗓 *Today is {day_name} — {date_str}*\n\n"
                "Weekend is here — but profits don't wait.\n\n"
                "✅ *Social Copy Trading* is LIVE — OTC markets open\n"
                "✅ *Auto Trading Bot* is running — all brokers\n"
                "✅ *VIP members* are already ahead of you\n\n"
                "You can still join today 👇"
            ),
        ]

    # --- FRIDAY posts ---
    elif is_friday:
        DYNAMIC_POSTS += [
            (
                f"🗓 *Today is Friday — {date_str}*\n\n"
                "⚡ *Weekend is starting — are you ready?*\n\n"
                "VIP members already locked in profits this week.\n"
                "Social Trading keeps running through the weekend.\n\n"
                "Don't let another week pass without taking action.\n\n"
                "Join now 👇"
            ),
            (
                f"📅 *Friday — {date_str}*\n\n"
                "🏁 End of the trading week.\n\n"
                "This week our VIP members:\n"
                "📈 Received 8–10 signals daily\n"
                "✅ Non-Martingale — capital protected\n"
                "💰 Consistent profits every session\n\n"
                "Next week starts Monday.\n"
                "Will you be ready? Join before the weekend ends 👇"
            ),
        ]

    # --- MONDAY posts ---
    elif is_monday:
        DYNAMIC_POSTS += [
            (
                f"📅 *Monday — {date_str}*\n\n"
                "🔔 *New week. New signals. New profits.*\n\n"
                "VIP signals are LIVE from today.\n"
                "8–10 signals per day, Monday to Friday.\n\n"
                "If you missed last week — don't miss this one.\n\n"
                "Join now 👇"
            ),
        ]

    # --- WEEKDAY posts (Tue/Wed/Thu) ---
    else:
        DYNAMIC_POSTS += [
            (
                f"📅 *{day_name} — {date_str}*\n\n"
                "⚡ *VIP signals are running RIGHT NOW.*\n\n"
                "While you're reading this, our members are:\n"
                "📈 Following live signals\n"
                "✅ Booking profits\n"
                "🤖 Running auto bots on all brokers\n\n"
                "You're still on the outside.\n"
                "Fix that today 👇"
            ),
            (
                f"🗓 *{day_name} — {date_str}*\n\n"
                "💎 *Another trading day. Another opportunity.*\n\n"
                "EVALON VIP members get:\n"
                "📊 8–10 clean signals today\n"
                "🎯 Non-Martingale only\n"
                "📲 Results after every trade\n\n"
                "Today's session is already running.\n"
                "Don't miss tomorrow's — join now 👇"
            ),
        ]

    if not DYNAMIC_POSTS:
        return None

    text = random.choice(DYNAMIC_POSTS)
    return "vip_signals", text  # dynamic posts use VIP keyboard


def pick_post(avoid_services: list = None):
    """Pick a post — avoid services already posted today."""
    avoid = set(avoid_services or [])

    # Try dynamic post first (~25% chance)
    dynamic = get_dynamic_post()
    if dynamic and dynamic[0] not in avoid:
        return dynamic

    # Pick from static posts — avoid already-used services if possible
    available = [s for s in POSTS.keys() if s not in avoid]
    if not available:
        available = list(POSTS.keys())  # all services used today — reset

    service = random.choice(available)
    return service, random.choice(POSTS[service])

# ============================================================
# MEDIA STORAGE — DB-backed (replaces static VIDEO_FILE_IDS)
# ============================================================
SERVICES = ["vip_signals", "auto_trading_bot", "social_trading", "manual_bot", "indicators", "spin_invite"]
SERVICE_LABELS = {
    "vip_signals":      "👑 VIP Signals",
    "auto_trading_bot": "🤖 Auto Trading Bot",
    "social_trading":   "✨ Social Copy Trading",
    "manual_bot":       "🎁 Manual Bot",
    "indicators":       "📊 Indicators",
    "spin_invite":      "🎰 Spin & Invite",
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
                cap = text + f"\n\n📹 {WATERMARK_TEXT}"
                await bot.send_video(
                    chat_id=CHANNEL_ID,
                    video=item["file_id"],
                    caption=cap.strip(),
                    parse_mode="Markdown",
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
                    parse_mode="Markdown",
                    reply_markup=kb
                )
            return
        except Exception as e:
            logger.warning(f"Media send failed, falling back to text: {e}")

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=kb,
        disable_web_page_preview=True
    )

# ============================================================
# /addmedia — admin sends video/photo + selects service
# ============================================================
async def cmd_addmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    # Show service selection buttons
    buttons = [[InlineKeyboardButton(label, callback_data=f"addmedia_wait_{svc}")]
               for svc, label in SERVICE_LABELS.items()]
    await update.message.reply_text(
        "📎 *Add Media*\n\nWhich service is this video/photo for?\n\n"
        "1️⃣ Select the service below\n"
        "2️⃣ Then send the video or photo",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def cmd_listmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    store = media_load()
    if not any(store.values()):
        await update.message.reply_text("📭 No media saved yet.\nUse /addmedia to add."); return

    lines = ["🗂 *SAVED MEDIA*\n"]
    for svc, items in store.items():
        if not items: continue
        label = SERVICE_LABELS.get(svc, svc)
        lines.append(f"{label} — *{len(items)} file(s)*")
        for i, m in enumerate(items):
            lines.append(f"  `{i}` — {m['type']}")
    lines.append("\n_Use /removemedia to delete_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_removemedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    store = media_load()
    buttons = []
    for svc, items in store.items():
        for i, m in enumerate(items):
            label = f"{SERVICE_LABELS.get(svc, svc)} — {m['type']} #{i}"
            buttons.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"removemedia_{svc}_{i}")])
    if not buttons:
        await update.message.reply_text("📭 No media to remove."); return
    await update.message.reply_text(
        "🗑 *Remove Media*\n\nSelect item to remove:",
        parse_mode="Markdown",
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
            f"✅ *{label}* selected.\n\n"
            "Now send me the *video or photo* to attach to this service.\n\n"
            "_It will be saved and used automatically in future auto-posts._",
            parse_mode="Markdown"
        )

    elif data.startswith("removemedia_"):
        parts   = data.split("_", 2)
        # removemedia_{service}_{index}
        _, svc, idx_str = data.split("_", 2)
        # svc might have underscores — handle carefully
        # format: removemedia_{svc}_{i}
        last_underscore = data.rfind("_")
        idx_str = data[last_underscore+1:]
        svc     = data[len("removemedia_"):last_underscore]
        try:
            idx = int(idx_str)
            if media_remove(svc, idx):
                label = SERVICE_LABELS.get(svc, svc)
                await q.edit_message_text(f"🗑 Removed media #{idx} from *{label}*", parse_mode="Markdown")
            else:
                await q.edit_message_text("⚠️ Item not found.")
        except: await q.edit_message_text("⚠️ Error removing media.")


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
            f"✅ *{media_type.capitalize()} saved for {label}!*\n\n"
            f"It will now be attached to auto-posts for this service.\n"
            f"Use /listmedia to see all saved media.",
            parse_mode="Markdown"
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
                caption=msg.caption or "", parse_mode="Markdown", reply_markup=kb
            )
        elif msg.video:
            cap = (msg.caption or "") + f"\n\n📹 {WATERMARK_TEXT}"
            await context.bot.send_video(
                chat_id=CHANNEL_ID, video=msg.video.file_id,
                caption=cap.strip(), parse_mode="Markdown", reply_markup=kb
            )
        elif msg.animation:
            await context.bot.send_animation(
                chat_id=CHANNEL_ID, animation=msg.animation.file_id,
                caption=msg.caption or "", parse_mode="Markdown", reply_markup=kb
            )
        elif msg.text:
            await context.bot.send_message(
                chat_id=CHANNEL_ID, text=msg.text,
                parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True
            )
        else:
            await msg.reply_text("⚠️ Unsupported message type."); return

        await msg.reply_text("✅ *Sent to channel!*", parse_mode="Markdown")
        logger.info("Admin broadcast sent to channel")

    except Exception as e:
        await msg.reply_text(f"❌ Failed: {e}")
        logger.error(f"Broadcast failed: {e}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(
        "📡 *EVALON AUTOPOST BOT v3*\n"
        "━━━━━━━━━━━━━━\n\n"
        "🤖 *AUTO-POSTING*\n"
        "Posts 10–12x daily (08:00–23:00 EAT) automatically.\n"
        "Rotates across 6 services. No duplicates per day.\n\n"
        "📣 *BROADCAST (Manual Post)*\n"
        "Send any message here → goes to channel with buttons.\n"
        "Supports: Text, Photo, Video, GIF\n"
        "Photos get watermark automatically.\n\n"
        "━━━━━━━━━━━━━━\n"
        "⚙️ *BOT CONTROLS*\n"
        "`/pause` — Stop auto-posting\n"
        "`/resume` — Resume auto-posting\n"
        "`/status` — Current bot status\n"
        "`/schedule` — Today's post times\n"
        "`/history` — Last 10 posts sent\n\n"
        "━━━━━━━━━━━━━━\n"
        "📎 *MEDIA (Video/Photo per service)*\n"
        "`/addmedia`\n"
        "  1️⃣ Tap the service name\n"
        "  2️⃣ Send the video or photo\n"
        "  ✅ Saved — used in auto-posts automatically\n\n"
        "`/listmedia` — See all saved media\n"
        "`/removemedia` — Delete a saved media file\n\n"
        "━━━━━━━━━━━━━━\n"
        "🖼 *WATERMARK*\n"
        "All photos → `EVALON WINNERS BOT` diagonal\n"
        "All videos → watermark in caption\n\n"
        "━━━━━━━━━━━━━━\n"
        "💬 *BUTTONS ON EVERY POST*\n"
        "Each post has 3 buttons linking to your 3 bots.",
        parse_mode="Markdown"
    )

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    db_set("paused", True)
    await update.message.reply_text("⏸ *Auto-posting paused.*\nUse /resume to restart.", parse_mode="Markdown")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    db_set("paused", False)
    await update.message.reply_text("▶️ *Auto-posting resumed!*", parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    paused      = db_get("paused", False)
    todays      = db_get_todays_posts()
    now         = datetime.now(timezone.utc)
    eat_time    = f"{(now.hour+3)%24:02d}:{now.minute:02d} EAT"
    await update.message.reply_text(
        f"📊 *AUTOPOST BOT STATUS*\n\n"
        f"{'⏸ PAUSED' if paused else '▶️ RUNNING'}\n"
        f"🕐 Time: {eat_time}\n"
        f"📬 Posts today: *{len(todays)}*\n"
        f"🗂 Services posted: {', '.join(set(todays)) or 'none'}\n"
        f"💾 DB: {'✅ PostgreSQL' if DATABASE_URL else '⚠️ Local'}",
        parse_mode="Markdown"
    )

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    hist = db_get_history(limit=10)
    if not hist:
        await update.message.reply_text("📭 No post history yet."); return

    SERVICE_EMOJI = {
        "vip_signals": "👑", "auto_trading_bot": "🤖", "social_trading": "✨",
        "manual_bot": "🎁", "indicators": "📊", "spin_invite": "🎰",
    }
    lines = ["📋 *LAST 10 POSTS*\n"]
    for h in hist:
        svc     = h.get("service", "?")
        emoji   = SERVICE_EMOJI.get(svc, "📌")
        preview = h.get("preview") or h.get("text_preview", "")[:60]
        posted  = h.get("posted_at", "")
        if hasattr(posted, "strftime"):
            t = f"{(posted.hour+3)%24:02d}:{posted.minute:02d} EAT"
        else:
            try:
                dt = datetime.fromisoformat(str(posted).replace("Z",""))
                t  = f"{(dt.hour+3)%24:02d}:{dt.minute:02d} EAT"
            except: t = str(posted)[:16]
        lines.append(f"{emoji} *{svc}* — {t}\n_{preview[:70]}..._\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    now      = datetime.now(timezone.utc)
    sched    = get_todays_schedule()
    todays   = db_get_todays_posts()
    eat_now  = (now.hour + 3) % 24

    lines = [f"🗓 *TODAY'S SCHEDULE — {now.strftime('%d %b %Y')}*\n"]
    for i, (h, m) in enumerate(sched):
        eat_h  = (h + 3) % 24
        status = "✅ Done" if i < len(todays) else ("🔄 Next" if eat_h == eat_now else "⏳ Pending")
        lines.append(f"`{eat_h:02d}:{m:02d} EAT` — {status}")

    lines.append(f"\n📬 Sent: *{len(todays)}/{len(sched)}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ============================================================
# AUTO-POST LOOP
# ============================================================
async def autopost_loop(bot: Bot):
    logger.info("AutoPost loop started.")
    today_date     = None
    today_schedule = None
    slot_index     = 0

    while True:
        now = datetime.now(timezone.utc)

        # Check if paused
        if db_get("paused", False):
            logger.info("Bot is paused — sleeping 5 min")
            await asyncio.sleep(300)
            continue

        if today_date != now.date():
            today_date     = now.date()
            today_schedule = get_todays_schedule()
            slot_index     = 0
            logger.info(f"Schedule {today_date}: {today_schedule}")

        if slot_index >= len(today_schedule):
            await asyncio.sleep(3600)
            continue

        next_h, next_m = today_schedule[slot_index]
        target = datetime(now.year, now.month, now.day, next_h, next_m, 0, tzinfo=timezone.utc)

        if now >= target:
            # Get today's already-posted services to avoid duplicates
            todays_posts = db_get_todays_posts()
            service, text = pick_post(avoid_services=todays_posts)

            try:
                await send_post(bot, service, text)
                db_log_post(service, text)
                logger.info(f"✅ Auto-posted [{service}] {now.strftime('%H:%M UTC')}")
            except Exception as e:
                logger.error(f"❌ Auto-post failed: {e}")

            slot_index += 1
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
