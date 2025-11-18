# app.py
import os
import logging
import sqlite3
import asyncio
from datetime import datetime, date
from threading import Thread

from flask import Flask, request, jsonify
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import requests

# ---------- Environment ----------
BOT_TOKEN_1 = os.getenv("BOT_TOKEN_1")
ADMIN_ID_RAW = os.getenv("CHAT_ID_1")
DATA_CENTER_BOT_TOKEN = os.getenv("BOT_TOKEN_2")
DATA_CENTER_BOT_CHAT_ID = os.getenv("CHAT_ID_2")

missing = []
for k, v in {
    "BOT_TOKEN_1": BOT_TOKEN_1,
    "CHAT_ID_1": ADMIN_ID_RAW,
    "BOT_TOKEN_2": DATA_CENTER_BOT_TOKEN,
    "CHAT_ID_2": DATA_CENTER_BOT_CHAT_ID,
}.items():
    if not v:
        missing.append(k)

if missing:
    raise ValueError(f"Missing environment variables: {', '.join(missing)}")

ADMIN_ID = int(ADMIN_ID_RAW)

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------- Flask App ----------
app = Flask(__name__)

# ---------- Background Async Loop ----------
LOOP = asyncio.new_event_loop()

def run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()

Thread(target=run_loop, daemon=True).start()
logger.info("✅ Background asyncio loop started")

# ---------- Telegram Bot ----------
bot = Bot(token=BOT_TOKEN_1)
dp = Dispatcher()

# ---------- Database ----------
class Database:
    def __init__(self):
        self.db_file = "bot_database.db"
        self._ensure_column()
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init()

    def _ensure_column(self):
        try:
            conn = sqlite3.connect(self.db_file, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(users)")
            cols = [c[1] for c in cursor.fetchall()]
            if "data_center_sent" not in cols:
                cursor.execute("ALTER TABLE users ADD COLUMN data_center_sent BOOLEAN DEFAULT FALSE")
                conn.commit()
            conn.close()
        except:
            pass

    def init(self):
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_center_sent BOOLEAN DEFAULT FALSE
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                users_reached INTEGER DEFAULT 0
            )
        """)
        self.conn.commit()
        logger.info("✅ Database initialized")

    def add_user(self, uid, username, first, last):
        try:
            c = self.conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
                (uid, username, first, last),
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"DB error: {e}")

    def mark_data(self, uid):
        c = self.conn.cursor()
        c.execute("UPDATE users SET data_center_sent = TRUE WHERE user_id = ?", (uid,))
        self.conn.commit()

    def sent(self, uid):
        c = self.conn.cursor()
        c.execute("SELECT data_center_sent FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        return bool(row[0]) if row else False

    def update_activity(self, uid):
        c = self.conn.cursor()
        c.execute("UPDATE users SET last_active=? WHERE user_id=?", (datetime.now(), uid))
        self.conn.commit()

    def log(self, uid, action):
        c = self.conn.cursor()
        c.execute("INSERT INTO user_actions (user_id, action) VALUES (?, ?)", (uid, action))
        self.conn.commit()

    def stats(self):
        c = self.conn.cursor()
        today = date.today().isoformat()
        c.execute("SELECT COUNT(*) FROM users")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE DATE(join_date)=?", (today,))
        new = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE DATE(last_active)=?", (today,))
        active = c.fetchone()[0]
        return {"total_users": total, "new_users_today": new, "active_today": active}

    def users(self):
        c = self.conn.cursor()
        c.execute("SELECT user_id FROM users")
        return [r[0] for r in c.fetchall()]

db = Database()

# ---------- Data Center ----------
class DataCenter:
    @staticmethod
    def send(user):
        if db.sent(user["chat_id"]):
            return True
        try:
            msg = (
                f"🆕 NEW USER\n\n"
                f"Chat ID: {user['chat_id']}\n"
                f"Username: @{user['username']}\n"
                f"First: {user['first_name']}\n"
                f"Last: {user['last_name']}\n"
            )
            url = f"https://api.telegram.org/bot{DATA_CENTER_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": DATA_CENTER_BOT_CHAT_ID, "text": msg}
            r = requests.post(url, json=payload)
            if r.status_code == 200:
                db.mark_data(user["chat_id"])
                return True
        except Exception as e:
            logger.error(e)
        return False

# ---------- Keyboards ----------
_main, _panel = None, None

def main_menu():
    global _main
    if not _main:
        kb = InlineKeyboardBuilder()
        for t, c in [
            ("🆘 Help", "help"),
            ("🎁 Free DPP", "free_dpp"),
            ("🎓 Buy Lectures", "buy_lecture"),
            ("▶️ YouTube", "youtube"),
            ("👥 Groups", "groups"),
        ]:
            kb.button(text=t, callback_data=c)
        kb.adjust(2, 2, 1)
        _main = kb.as_markup()
    return _main

def admin_panel():
    global _panel
    if not _panel:
        kb = InlineKeyboardBuilder()
        kb.button(text="📈 View Stats", callback_data="admin_stats")
        kb.button(text="📢 Send Broadcast", callback_data="admin_broadcast")
        kb.button(text="🔙 Main Menu", callback_data="main_menu")
        kb.adjust(1)
        _panel = kb.as_markup()
    return _panel

# ---------- Handlers ----------
@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    u = msg.from_user
    db.add_user(u.id, u.username, u.first_name, u.last_name)
    db.log(u.id, "start")

    DataCenter.send({
        "chat_id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
    })

    await msg.answer(
        "🌟 <b>Welcome to Naino Academy!</b>\nChoose an option:",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "help")
async def help_cb(cb):
    await cb.message.answer(
        "Click below to contact admin:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📞 Contact Admin", url="https://t.me/Nainoacademy")]
            ]
        ),
    )
    await cb.answer()

@dp.callback_query(F.data == "free_dpp")
async def dpp_cb(cb):
    await cb.message.answer(
        "Open Free DPP Bot:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Free DPP", url="https://t.me/FreeDPPBot")]
            ]
        ),
    )
    await cb.answer()

# ---------- Scheduler ----------
scheduler = BackgroundScheduler()

def schedule_jobs():
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(send_daily(), LOOP),
                      CronTrigger(hour=9))
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(send_weekly(), LOOP),
                      CronTrigger(day_of_week="mon", hour=10))
    scheduler.start()
    logger.info("Scheduler started")

async def send_daily():
    msg = "🗓 Daily Update!\nCheck new content in the menu."
    for uid in db.users():
        try:
            await bot.send_message(uid, msg)
        except:
            pass

async def send_weekly():
    msg = "📚 Weekly Tip!\nKeep learning!"
    for uid in db.users():
        try:
            await bot.send_message(uid, msg)
        except:
            pass

schedule_jobs()

# ---------- Webhook ----------
@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "running"})

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        upd = types.Update(**request.get_json())
        asyncio.run_coroutine_threadsafe(dp.feed_update(bot, upd), LOOP)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error(e)
        return jsonify({"ok": False}), 500

@app.route("/set_webhook", methods=["GET"])
async def set_webhook():
    try:
        url = "https://nainobot-1.onrender.com/webhook"
        await bot.set_webhook(url)
        return jsonify({"status": "success", "webhook_url": url})
    except Exception as e:
        logger.error(e)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "healthy", "time": datetime.now().isoformat()})

logger.info("App loaded — background loop + scheduler ready")
