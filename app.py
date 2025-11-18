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

# Validate env vars (but allow admin ID missing detection)
missing = []
if not BOT_TOKEN_1:
    missing.append("BOT_TOKEN_1")
if not ADMIN_ID_RAW:
    missing.append("CHAT_ID_1")
if not DATA_CENTER_BOT_TOKEN:
    missing.append("BOT_TOKEN_2")
if not DATA_CENTER_BOT_CHAT_ID:
    missing.append("CHAT_ID_2")
if missing:
    raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except Exception:
    ADMIN_ID = None

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Flask App ----------
app = Flask(__name__)

# ---------- Background asyncio loop ----------
LOOP = None

def start_bg_loop():
    """Create and start an asyncio event loop in a background thread."""
    global LOOP
    if LOOP is not None:
        return

    LOOP = asyncio.new_event_loop()

    def run_loop(loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = Thread(target=run_loop, args=(LOOP,), daemon=True)
    thread.start()
    logger.info("✅ Background asyncio loop started")

# Start the loop immediately so handlers can register tasks (Gunicorn import triggers this)
start_bg_loop()

# ---------- Bot & Dispatcher (aiogram) ----------
bot = Bot(token=BOT_TOKEN_1)
dp = Dispatcher()

# ---------- Database ----------
class Database:
    def __init__(self):
        self.db_file = 'bot_database.db'
        # Ensure DB exists and column exists
        self._ensure_column_exists()
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

    def _ensure_column_exists(self):
        try:
            conn = sqlite3.connect(self.db_file, check_same_thread=False)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(users)")
            columns = [column[1] for column in cursor.fetchall()]
            if 'data_center_sent' not in columns:
                cursor.execute('ALTER TABLE users ADD COLUMN data_center_sent BOOLEAN DEFAULT FALSE')
                conn.commit()
            conn.close()
        except Exception:
            # Table may not exist yet — that's OK
            pass

    def init_db(self):
        cursor = self.conn.cursor()
        cursor.execute('''
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
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                users_reached INTEGER DEFAULT 0
            )
        ''')
        self.conn.commit()
        logger.info("✅ Database initialized with all columns")

    def add_user(self, user_id, username, first_name, last_name):
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name))
            self.conn.commit()
            logger.info(f"✅ User added to DB: {user_id}")
        except Exception as e:
            logger.error(f"Error adding user: {e}")

    def mark_data_center_sent(self, user_id):
        try:
            cursor = self.conn.cursor()
            cursor.execute('UPDATE users SET data_center_sent = TRUE WHERE user_id = ?', (user_id,))
            self.conn.commit()
            logger.info(f"✅ User {user_id} marked as sent to Data Center")
        except Exception as e:
            logger.error(f"❌ Error marking data center sent: {e}")

    def is_data_center_sent(self, user_id):
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT data_center_sent FROM users WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            result = bool(row[0]) if row else False
            return result
        except Exception as e:
            logger.error(f"❌ Error checking data center sent: {e}")
            return False

    def update_user_activity(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE users SET last_active = ? WHERE user_id = ?', (datetime.now(), user_id))
        self.conn.commit()

    def log_action(self, user_id, action):
        cursor = self.conn.cursor()
        cursor.execute('INSERT INTO user_actions (user_id, action) VALUES (?, ?)', (user_id, action))
        self.conn.commit()

    def get_user_stats(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        today = date.today().isoformat()
        cursor.execute('SELECT COUNT(*) FROM users WHERE DATE(join_date) = ?', (today,))
        new_users_today = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM users WHERE DATE(last_active) = ?', (today,))
        active_today = cursor.fetchone()[0]
        return {'total_users': total_users, 'new_users_today': new_users_today, 'active_today': active_today}

    def get_all_users(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT user_id FROM users')
        return [row[0] for row in cursor.fetchall()]

db = Database()

# ---------- Data Center sender ----------
class DataCenter:
    @staticmethod
    def send_user_data_sync(user_data: dict):
        """Send user data to data center bot - SYNC HTTP call (keeps original behavior)"""
        if not DATA_CENTER_BOT_TOKEN or not DATA_CENTER_BOT_CHAT_ID:
            logger.info("❌ Data Center disabled or tokens missing")
            return False
        try:
            if db.is_data_center_sent(user_data['chat_id']):
                logger.info(f"✅ User {user_data['chat_id']} already sent to Data Center (skipping)")
                return True

            message = (
                f"🆕 NEW USER - NAINO ACADEMY BOT\n\n"
                f"👤 User Information:\n"
                f"• Chat ID: {user_data['chat_id']}\n"
                f"• Username: @{user_data['username']}\n"
                f"• First Name: {user_data['first_name']}\n"
                f"• Last Name: {user_data['last_name']}\n"
                f"• Joined: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"• Source: Naino_Academy_Bot"
            )

            api_url = f"https://api.telegram.org/bot{DATA_CENTER_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": DATA_CENTER_BOT_CHAT_ID, "text": message}
            response = requests.post(api_url, json=payload, timeout=10)
            if response.status_code == 200:
                db.mark_data_center_sent(user_data['chat_id'])
                logger.info(f"🎉 SUCCESS: User data sent to Data Center: {user_data['chat_id']}")
                return True
            else:
                logger.error(f"❌ FAILED: Data Center Error {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error sending to data center: {e}")
            return False

data_center = DataCenter()

# ---------- Keyboards (unchanged) ----------
_main_menu = None
_admin_panel = None

def create_main_menu():
    global _main_menu
    if _main_menu is None:
        keyboard = InlineKeyboardBuilder()
        buttons = [
            ("🆘 Help", "help"),
            ("🎁 Free DPP", "free_dpp"),
            ("🎓 Buy Lectures", "buy_lecture"),
            ("▶️ YouTube", "youtube"),
            ("👥 Groups", "groups")
        ]
        for text, callback in buttons:
            keyboard.button(text=text, callback_data=callback)
        keyboard.adjust(2, 2, 1)
        _main_menu = keyboard.as_markup()
    return _main_menu

def create_admin_panel():
    global _admin_panel
    if _admin_panel is None:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="📈 View Stats", callback_data="admin_stats")
        keyboard.button(text="📢 Send Broadcast", callback_data="admin_broadcast")
        keyboard.button(text="🔙 Main Menu", callback_data="main_menu")
        keyboard.adjust(1)
        _admin_panel = keyboard.as_markup()
    return _admin_panel

# ---------- Handlers (mostly unchanged) ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    db.add_user(user.id, user.username, user.first_name, user.last_name)
    db.log_action(user.id, "start_command")
    user_data = {
        'chat_id': user.id,
        'username': user.username or "No username",
        'first_name': user.first_name or "No first name",
        'last_name': user.last_name or "No last name"
    }
    # call sync data center (keeps previous behavior)
    data_center.send_user_data_sync(user_data)
    welcome_text = """🌟 <b>Welcome to Naino Academy!</b> 🌟

🚀 Your one-stop destination for:
• 📚 Free DPP & Study Materials
• 🎓 Premium Lectures
• 📺 Educational Content
• 👥 Study Groups

👇 <b>Choose an option below to get started:</b>"""
    await message.answer(welcome_text, reply_markup=create_main_menu(), parse_mode="HTML")

@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.log_action(user_id, "main_menu")
    db.update_user_activity(user_id)
    menu_text = "🏠 <b>Main Menu</b>\n\nChoose an option:"
    await callback.message.edit_text(menu_text, reply_markup=create_main_menu(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "help")
async def help_section(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.log_action(user_id, "help_section")
    db.update_user_activity(user_id)
    await callback.answer()
    await callback.message.answer(
        "Please click the button below to contact admin:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📞 Contact Admin Now", url="https://t.me/Nainoacademy")]
            ]
        )
    )

@dp.callback_query(F.data == "free_dpp")
async def free_dpp(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.log_action(user_id, "free_dpp")
    db.update_user_activity(user_id)
    await callback.answer()
    await callback.message.answer(
        "Please click the button below to open Free DPP bot:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🤖 Open Free DPP Bot", url="https://t.me/FreeDPPBot")]
            ]
        )
    )

@dp.callback_query(F.data == "buy_lecture")
async def buy_lecture(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.log_action(user_id, "buy_lecture")
    db.update_user_activity(user_id)
    lecture_text = """🎓 <b>Premium Lectures</b>

Enhance your learning with our premium features:
• 🎥 HD Video Lectures
• 📝 Detailed PDF Notes
• ❓ Doubt Solving Sessions
• 📱 Access Anywhere, Anytime

👇 <b>Watch the guide and contact for purchase:</b>"""
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="▶️ Watch Guide", url="https://youtube.com/shorts/_yw9tPqkSuo?si=wUcQ9ZLbUS8svCiY")
    keyboard.button(text="💬 Contact Option 1", url="https://t.me/Nainoacademy")
    keyboard.button(text="💬 Contact Option 2", url="https://t.me/Pankajmourrya")
    keyboard.button(text="🔙 Back to Main", callback_data="main_menu")
    keyboard.adjust(1, 2)
    await callback.message.edit_text(lecture_text, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "youtube")
async def youtube_channels(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.log_action(user_id, "youtube_channels")
    db.update_user_activity(user_id)
    youtube_text = """📺 <b>Our YouTube Channels</b>

Subscribe to our channels for:
• 🎓 Free educational content
• 📚 Subject-wise tutorials
• 💡 Study tips & strategies
• 🏆 Success stories

👇 <b>Choose a channel to explore:</b>"""
    youtube_links = {
        "🔴 Wisdom NEET": "https://www.youtube.com/@wisdomneet",
        "🔴 LearnX NEET": "https://www.youtube.com/@Learnxneet",
        "🔴 Naino NEET": "https://www.youtube.com/@NainoNeet"
    }
    keyboard = InlineKeyboardBuilder()
    for name, url in youtube_links.items():
        keyboard.button(text=name, url=url)
    keyboard.button(text="🔙 Back to Main", callback_data="main_menu")
    keyboard.adjust(1)
    await callback.message.edit_text(youtube_text, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "groups")
async def public_groups(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.log_action(user_id, "public_groups")
    db.update_user_activity(user_id)
    groups_text = """👥 <b>Join Our Community</b>

Connect with fellow students in our active groups:
• 💬 Discussion & Doubts
• 📚 Study Material Sharing
• 🎯 Exam Updates
• 🤝 Peer Support

👇 <b>Choose a group to join:</b>"""
    group_links = {
        "💬 GROUP 1": "https://t.me/nainoneet",
        "💬 GROUP 2": "https://t.me/+kYDy07SlxeI5ZDZl",
        "💬 GROUP 3": "https://t.me/Nainoacademy_bot"
    }
    keyboard = InlineKeyboardBuilder()
    for name, url in group_links.items():
        keyboard.button(text=name, url=url)
    keyboard.button(text="🔙 Back to Main", callback_data="main_menu")
    keyboard.adjust(1)
    await callback.message.edit_text(groups_text, reply_markup=keyboard.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.message(Command("panel"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ You don't have permission to use this command.")
        return
    db.log_action(message.from_user.id, "admin_panel")
    admin_text = """🛠️ <b>Admin Panel</b>

Manage your bot efficiently with these tools:"""
    await message.answer(admin_text, reply_markup=create_admin_panel(), parse_mode="HTML")

@dp.message(Command("stats"))
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ You don't have permission to use this command.")
        return
    stats = db.get_user_stats()
    stats_text = f"""📊 <b>Bot Statistics</b>

👤 Total Users: <b>{stats['total_users']}</b>
🆕 New Users Today: <b>{stats['new_users_today']}</b>
📈 Active Today: <b>{stats['active_today']}</b>"""
    await message.answer(stats_text, parse_mode="HTML")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Access denied", show_alert=True)
        return
    stats = db.get_user_stats()
    stats_text = f"""📊 <b>Bot Statistics</b>

👤 Total Users: <b>{stats['total_users']}</b>
🆕 New Users Today: <b>{stats['new_users_today']}</b>
📈 Active Today: <b>{stats['active_today']}</b>"""
    await callback.message.edit_text(stats_text, reply_markup=create_admin_panel(), parse_mode="HTML")
    await callback.answer()

@dp.message(F.text & ~F.text.startswith('/'))
async def handle_broadcast_message(message: types.Message):
    if message.from_user.id != ADMIN_ID or message.reply_to_message:
        return
    if len(message.text) > 10:
        users = db.get_all_users()
        success_count = 0
        batch_size = 30
        for i in range(0, len(users), batch_size):
            batch = users[i:i + batch_size]
            tasks = []
            for user_id in batch:
                try:
                    tasks.append(bot.send_message(user_id, f"📢 <b>Announcement:</b>\n\n{message.text}", parse_mode="HTML"))
                except Exception as e:
                    logger.error(f"Failed to queue send to {user_id}: {e}")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count += sum(1 for r in results if not isinstance(r, Exception))
            if i + batch_size < len(users):
                await asyncio.sleep(0.5)
        await message.answer(f"✅ Broadcast completed!\n📨 Sent to: {success_count}/{len(users)} users")

# ---------- Scheduler tasks ----------
async def send_daily_update():
    try:
        message = """🗓️ <b>Today's Update!</b>

🎁 New DPP added in Free DPP section!
📚 Study tips and tricks available
💪 Stay motivated and keep learning!

Check the main menu for latest content 👇"""
        users = db.get_all_users()
        batch_size = 25
        for i in range(0, len(users), batch_size):
            batch = users[i:i + batch_size]
            tasks = []
            for user_id in batch:
                try:
                    tasks.append(bot.send_message(user_id, message, parse_mode="HTML"))
                except Exception as e:
                    logger.error(f"Failed daily update to {user_id}: {e}")
            await asyncio.gather(*tasks, return_exceptions=True)
            if i + batch_size < len(users):
                await asyncio.sleep(0.3)
    except Exception as e:
        logger.error(f"Error in send_daily_update: {e}")

async def send_weekly_tip():
    try:
        message = """📚 <b>Weekly Study Tip</b>

🎯 Plan your week ahead and set daily goals!
📖 Small consistent efforts lead to big results.
⏰ Manage time effectively for better productivity.

Check out our YouTube channels for more tips! 👇"""
        users = db.get_all_users()
        batch_size = 25
        for i in range(0, len(users), batch_size):
            batch = users[i:i + batch_size]
            tasks = []
            for user_id in batch:
                try:
                    tasks.append(bot.send_message(user_id, message, parse_mode="HTML"))
                except Exception as e:
                    logger.error(f"Failed weekly tip to {user_id}: {e}")
            await asyncio.gather(*tasks, return_exceptions=True)
            if i + batch_size < len(users):
                await asyncio.sleep(0.3)
    except Exception as e:
        logger.error(f"Error in send_weekly_tip: {e}")

# ---------- Scheduler setup (BackgroundScheduler) ----------
scheduler = BackgroundScheduler()

def schedule_jobs():
    # Schedule jobs that dispatch coroutines to the background LOOP
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(send_daily_update(), LOOP),
                      CronTrigger(hour=9, minute=0))
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(send_weekly_tip(), LOOP),
                      CronTrigger(day_of_week='mon', hour=10, minute=0))
    scheduler.start()
    logger.info("✅ BackgroundScheduler started and jobs scheduled")

# Start scheduler once event loop exists
schedule_jobs()

# ---------- Flask routes (sync) ----------
@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "Naino Academy Bot is running!", "webhook": True})

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive Telegram updates (sync WSGI route). We schedule dp.feed_update on background LOOP."""
    try:
        data = request.get_json(force=True)
        update = types.Update(**data)
        asyncio.run_coroutine_threadsafe(dp.feed_update(bot, update), LOOP)
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    """Set webhook using bot.set_webhook (runs on background loop synchronously via future.result)."""
    try:
        webhook_url = f"https://{request.host}/webhook"
        future = asyncio.run_coroutine_threadsafe(bot.set_webhook(webhook_url), LOOP)
        future.result(timeout=15)  # wait for result or raise
        return jsonify({"status": "success", "webhook_url": webhook_url})
    except Exception as e:
        logger.error(f"Set webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

# ---------- When Gunicorn imports app.py it will have already started LOOP and scheduler ----------
logger.info("App module loaded — background loop and scheduler configured.")

# Note: Do not run Flask built-in server under Gunicorn path. Only use gunicorn app:app in Render.

