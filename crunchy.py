import asyncio
import json
import sqlite3  # Use sqlite3 for sync init, aiosqlite for async
import aiosqlite
import aiohttp
import random
import string
import logging
import sys
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, asdict
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

# CONFIG - CHANGE THESE
BOT_TOKEN = "8237272070:AAG-jMJFqpS5H8BRhPWt7uQB8omQJeMbBdc"  # MUST CHANGE
ADMIN_IDS = [7935621079]  # CHANGE TO YOUR TG ID

# Setup logging to file for debugging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_debug.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Constants
DB_PATH = "crunchyroll_checker.db"
DEFAULT_WORKERS = 3
DEFAULT_TIMEOUT = 15

# Initialize bot and dp
try:
    bot = Bot(token=BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    router = Router()
    dp.include_router(router)
    logger.info("Bot initialized successfully")
except Exception as e:
    logger.error(f"Failed to init bot: {e}")
    sys.exit(1)

# Data Models
@dataclass
class CheckResult:
    email: str
    password: str
    status: str
    subscription: Optional[str] = None
    plan_type: Optional[str] = None
    expiry: Optional[str] = None
    proxy_used: Optional[str] = None
    checked_at: Optional[str] = None

# Database Manager
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    def init_sync(self):
        """Synchronous initialization for startup"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS combos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                password TEXT,
                status TEXT DEFAULT 'unchecked',
                subscription TEXT,
                plan_type TEXT,
                expiry TEXT,
                checked_at TIMESTAMP,
                proxy_used TEXT)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY,
                total_checked INTEGER DEFAULT 0,
                hits INTEGER DEFAULT 0,
                frees INTEGER DEFAULT 0,
                fails INTEGER DEFAULT 0)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                type TEXT DEFAULT 'http',
                uses INTEGER DEFAULT 0,
                fails INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT 1)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT)''')
            
            c.execute("INSERT OR IGNORE INTO stats (id) VALUES (1)")
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('workers', ?)", (str(DEFAULT_WORKERS),))
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('timeout', ?)", (str(DEFAULT_TIMEOUT),))
            
            conn.commit()
            conn.close()
            logger.info("Database initialized")
        except Exception as e:
            logger.error(f"DB Init error: {e}")
            raise
    
    async def get_setting(self, key: str, default=None):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
                row = await cursor.fetchone()
                return row[0] if row else default
        except Exception as e:
            logger.error(f"DB get_setting error: {e}")
            return default
    
    async def get_unchecked_combos(self, limit: int = 100):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT id, email, password FROM combos WHERE status='unchecked' LIMIT ?", (limit,))
                return await cursor.fetchall()
        except Exception as e:
            logger.error(f"DB get_unchecked error: {e}")
            return []
    
    async def update_combo(self, combo_id: int, result: CheckResult):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute('''UPDATE combos SET 
                    status=?, subscription=?, plan_type=?, expiry=?, 
                    checked_at=CURRENT_TIMESTAMP, proxy_used=?
                    WHERE id=?''', (
                    result.status, result.subscription, result.plan_type, result.expiry,
                    result.proxy_used, combo_id
                ))
                await db.commit()
        except Exception as e:
            logger.error(f"DB update_combo error: {e}")
    
    async def add_combos(self, combos: List[Tuple[str, str]]):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.executemany("INSERT OR IGNORE INTO combos (email, password) VALUES (?, ?)", combos)
                await db.commit()
        except Exception as e:
            logger.error(f"DB add_combos error: {e}")
    
    async def add_proxies(self, proxies: List[str]):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for proxy in proxies:
                    proxy = proxy.strip()
                    if proxy and '://' in proxy:
                        ptype = proxy.split('://')[0]
                        await db.execute("INSERT OR IGNORE INTO proxies (url, type) VALUES (?, ?)", (proxy, ptype))
                await db.commit()
        except Exception as e:
            logger.error(f"DB add_proxies error: {e}")
    
    async def get_active_proxies(self):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT * FROM proxies WHERE is_active=1 ORDER BY fails ASC")
                return await cursor.fetchall()
        except Exception as e:
            logger.error(f"DB get_proxies error: {e}")
            return []

# Global instances
db = Database(DB_PATH)
proxy_list = []
current_proxy_idx = 0
running_tasks = {}

# States
class Form(StatesGroup):
    upload = State()
    add_proxy = State()
    set_workers = State()

# Crunchyroll Checker
async def check_account(email: str, password: str, proxy: Optional[str] = None) -> CheckResult:
    """Simplified robust checker"""
    try:
        # Simulate checking logic here - replace with actual implementation
        await asyncio.sleep(0.5)  # Simulate network delay
        
        # Random result for demo (replace with real logic)
        rand = random.random()
        if rand > 0.7:
            return CheckResult(email, password, "HIT", subscription="Mega Fan", plan_type="Premium", proxy_used=proxy)
        elif rand > 0.4:
            return CheckResult(email, password, "FREE", proxy_used=proxy)
        else:
            return CheckResult(email, password, "FAIL", proxy_used=proxy)
            
    except Exception as e:
        logger.error(f"Check error: {e}")
        return CheckResult(email, password, "RETRY", proxy_used=proxy)

# UI Builders
def main_menu(is_admin: bool = False):
    kb = [
        [InlineKeyboardButton(text="🚀 Start Check", callback_data="start")],
        [InlineKeyboardButton(text="📁 Combos", callback_data="combos"),
         InlineKeyboardButton(text="🌐 Proxies", callback_data="proxy")],
        [InlineKeyboardButton(text="⚙️ Settings", callback_data="settings"),
         InlineKeyboardButton(text="📊 Stats", callback_data="stats")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="🔧 Admin", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# Handlers
@router.message(Command("start"))
async def cmd_start(message: Message):
    try:
        is_admin = message.from_user.id in ADMIN_IDS
        await message.answer(
            "🎌 <b>CrunchyRoll Checker</b>\n\nReady to use!",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(is_admin)
        )
    except Exception as e:
        logger.error(f"Start error: {e}")
        await message.answer("❌ Error starting bot. Check logs.")

@router.callback_query(F.data == "main_menu")
async def back_menu(callback: CallbackQuery):
    try:
        await callback.answer()
        is_admin = callback.from_user.id in ADMIN_IDS
        await callback.message.edit_text(
            "🎌 <b>Main Menu</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(is_admin)
        )
    except Exception as e:
        logger.error(f"Menu error: {e}")

@router.callback_query(F.data == "proxy")
async def proxy_menu(callback: CallbackQuery):
    try:
        await callback.answer("Loading...")
        proxies = await db.get_active_proxies()
        count = len(proxies)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📊 Active: {count}", callback_data="dummy")],
            [InlineKeyboardButton(text="➕ Add Proxy", callback_data="add_proxy")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(
            "🌐 <b>Proxy Manager</b>\n\nFormat: http://ip:port or socks5://ip:port",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Proxy menu error: {e}")
        await callback.answer("Error loading proxies")

@router.callback_query(F.data == "add_proxy")
async def add_proxy_start(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(Form.add_proxy)
        await callback.answer("Send proxies")
        await callback.message.edit_text(
            "🌐 Send proxies (one per line):\n<code>http://ip:port</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Cancel", callback_data="proxy")
            ]])
        )
    except Exception as e:
        logger.error(f"Add proxy error: {e}")

@router.message(Form.add_proxy)
async def process_proxy(message: Message, state: FSMContext):
    try:
        lines = message.text.strip().split('\n')
        valid = [l.strip() for l in lines if '://' in l]
        
        await db.add_proxies(valid)
        await state.clear()
        
        await message.answer(
            f"✅ Added {len(valid)} proxies",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🌐 View", callback_data="proxy")
            ]])
        )
    except Exception as e:
        logger.error(f"Process proxy error: {e}")
        await message.answer("❌ Error saving proxies")

@router.callback_query(F.data == "combos")
async def combo_menu(callback: CallbackQuery):
    try:
        await callback.answer()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Upload", callback_data="upload")],
            [InlineKeyboardButton(text="▶️ Start Check", callback_data="start")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            "📁 <b>Combo Manager</b>\n\nUpload .txt file (email:pass)",
            parse_mode=ParseMode.HTML,
            reply_markup=kb
        )
    except Exception as e:
        logger.error(f"Combo menu error: {e}")

@router.callback_query(F.data == "upload")
async def upload_start(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(Form.upload)
        await callback.answer("Send file")
        await callback.message.edit_text(
            "📤 Send .txt file with format:\n<code>email:password</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Cancel", callback_data="combos")
            ]])
        )
    except Exception as e:
        logger.error(f"Upload start error: {e}")

@router.message(Form.upload, F.document)
async def process_upload(message: Message, state: FSMContext):
    try:
        if not message.document.file_name.endswith('.txt'):
            await message.answer("❌ Only .txt files")
            return
        
        await message.answer("⏳ Processing...")
        
        file = await bot.get_file(message.document.file_id)
        content = await bot.download_file(file.file_path)
        text = content.read().decode('utf-8', errors='ignore')
        
        combos = []
        for line in text.split('\n'):
            if ':' in line:
                email, pwd = line.split(':', 1)
                combos.append((email.strip(), pwd.strip()))
        
        await db.add_combos(combos)
        await state.clear()
        
        await message.answer(
            f"✅ Loaded {len(combos)} combos",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Start", callback_data="start"),
                InlineKeyboardButton(text="📁 Menu", callback_data="combos")
            ]])
        )
    except Exception as e:
        logger.error(f"Process upload error: {e}")
        await message.answer("❌ Error processing file")

@router.callback_query(F.data == "start")
async def start_check(callback: CallbackQuery):
    try:
        await callback.answer("Starting...")
        combos = await db.get_unchecked_combos(100)
        
        if not combos:
            await callback.answer("❌ No combos!", show_alert=True)
            return
        
        workers = int(await db.get_setting('workers', DEFAULT_WORKERS))
        
        msg = await callback.message.edit_text(
            f"🚀 <b>Checking...</b>\nWorkers: {workers}\nQueue: {len(combos)}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛑 Stop", callback_data="stop")
            ]])
        )
        
        # Start background task
        task = asyncio.create_task(run_bulk_check(combos, workers, msg))
        running_tasks[callback.from_user.id] = task
        
    except Exception as e:
        logger.error(f"Start check error: {e}")
        await callback.answer("Error starting checker")

async def run_bulk_check(combos: List, workers: int, message: Message):
    """Threaded checking with semaphore"""
    semaphore = asyncio.Semaphore(workers)
    total = len(combos)
    completed = 0
    hits = 0
    
    async def check_one(combo):
        nonlocal completed, hits
        async with semaphore:
            try:
                cid, email, pwd = combo
                result = await check_account(email, pwd)
                await db.update_combo(cid, result)
                
                completed += 1
                if result.status == "HIT":
                    hits += 1
                
                # Update every 5
                if completed % 5 == 0:
                    try:
                        await message.edit_text(
                            f"🚀 <b>Checking...</b>\n"
                            f"Progress: {completed}/{total}\n"
                            f"Hits: {hits}",
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="🛑 Stop", callback_data="stop")
                            ]])
                        )
                    except:
                        pass
                        
            except Exception as e:
                logger.error(f"Check combo error: {e}")
    
    try:
        await asyncio.gather(*[check_one(c) for c in combos])
        await message.edit_text(
            f"✅ <b>Complete!</b>\nChecked: {completed}\nHits: {hits}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu()
        )
    except asyncio.CancelledError:
        await message.edit_text("⏸ <b>Stopped</b>", reply_markup=main_menu())
    finally:
        if message.chat.id in running_tasks:
            del running_tasks[message.chat.id]

@router.callback_query(F.data == "stop")
async def stop_check(callback: CallbackQuery):
    try:
        user_id = callback.from_user.id
        if user_id in running_tasks:
            running_tasks[user_id].cancel()
            await callback.answer("🛑 Stopping...")
        else:
            await callback.answer("Not running")
    except Exception as e:
        logger.error(f"Stop error: {e}")

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    try:
        await callback.answer()
        await callback.message.edit_text(
            "📊 <b>Statistics</b>\n\nUse /start to refresh",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
            ]])
        )
    except Exception as e:
        logger.error(f"Stats error: {e}")

@router.callback_query(F.data == "settings")
async def settings(callback: CallbackQuery):
    try:
        await callback.answer()
        await callback.message.edit_text(
            "⚙️ <b>Settings</b>\n\nWorkers: Use buttons below",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Set Workers", callback_data="set_workers")],
                [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
            ])
        )
    except Exception as e:
        logger.error(f"Settings error: {e}")

@router.callback_query(F.data == "set_workers")
async def set_workers_start(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(Form.set_workers)
        await callback.answer("Send number")
        await callback.message.edit_text(
            "👥 Send number of workers (1-10):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔙 Cancel", callback_data="settings")
            ]])
        )
    except Exception as e:
        logger.error(f"Set workers error: {e}")

@router.message(Form.set_workers)
async def process_workers(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if 1 <= val <= 20:
            async with aiosqlite.connect(DB_PATH) as db_conn:
                await db_conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('workers', ?)", (str(val),))
                await db_conn.commit()
            await state.clear()
            await message.answer(f"✅ Workers set to {val}")
        else:
            await message.answer("❌ Must be 1-20")
    except Exception as e:
        logger.error(f"Process workers error: {e}")
        await message.answer("❌ Invalid number")

async def main():
    try:
        # Initialize DB
        db.init_sync()
        logger.info("Starting bot...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Main error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
