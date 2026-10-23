import os
import urllib.request
import urllib.error
# Aggressive memory & storage trim for Linux/Docker (bot-hosting.net)
os.environ["MALLOC_TRIM_THRESHOLD_"] = "65536"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
import sys
import json
import shutil
import tempfile
import asyncio
import datetime
import time
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor

# Safe standard output configuration (supports pythonw and UTF-8 on Windows)
_WAS_STARTED_AS_PYTHONW = (sys.stdout is None)
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr and sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Fix curl_cffi / certifi bug with non-ASCII paths on Windows
try:
    import certifi
    ca_path = certifi.where()
    temp_ca = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(temp_ca) or os.path.getsize(temp_ca) != os.path.getsize(ca_path):
        shutil.copyfile(ca_path, temp_ca)
    os.environ["CURL_CA_BUNDLE"] = temp_ca
    os.environ["SSL_CERT_FILE"] = temp_ca
except Exception:
    pass

# Built-in .env parser (Dependency-free fallback)
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env()

import discord
from discord.ext import commands, tasks
from discord import app_commands
import warnings
warnings.filterwarnings("ignore", message=".*inplace.*")
import yfinance as yf
try:
    yf.set_tz_cache_location(os.path.join(tempfile.gettempdir(), "yf_tz_cache"))
except Exception:
    pass

from scan_stocks import (
    MACRO_TICKERS,
    calculate_technical, analyze_macro,
    get_session
)

# Comprehensive US Market Sector Mapping (11 Official GICS Sectors + ETF)
SECTOR_MAP = {
    'Technology': 'เทคโนโลยี (Technology)',
    'Healthcare': 'สุขภาพ & การแพทย์ (Healthcare)',
    'Financial Services': 'การเงิน & การธนาคาร (Financials)',
    'Consumer Cyclical': 'สินค้าฟุ่มเฟือย (Consumer Cyclical)',
    'Consumer Defensive': 'สินค้าจำเป็น (Consumer Defensive)',
    'Communication Services': 'สื่อสาร & บริการ (Communication)',
    'Industrials': 'อุตสาหกรรม (Industrials)',
    'Energy': 'พลังงาน (Energy)',
    'Utilities': 'สาธารณูปโภค (Utilities)',
    'Real Estate': 'อสังหาริมทรัพย์ (Real Estate)',
    'Basic Materials': 'วัสดุพื้นฐาน (Basic Materials)'
}

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("CRITICAL ERROR: 'DISCORD_BOT_TOKEN' is not set! Please define it in your .env file.")

_guild_id_env = os.getenv("DISCORD_GUILD_ID")
MY_GUILD_ID = int(_guild_id_env) if _guild_id_env and _guild_id_env.isdigit() else None
_owner_id_env = os.getenv("DISCORD_OWNER_ID")
DISCORD_OWNER_ID = int(_owner_id_env) if _owner_id_env and _owner_id_env.isdigit() else None
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

def _resolve_data_path(filename: str) -> str:
    subpath = os.path.join(DATA_DIR, filename)
    rootpath = os.path.join(os.path.dirname(__file__), filename)
    if os.path.exists(subpath) or not os.path.exists(rootpath):
        return subpath
    return rootpath

USER_WATCHLIST_FILE = _resolve_data_path("user_watchlists.json")
USER_ALERT_FILE = _resolve_data_path("user_alerts.json")
USER_PORTFOLIO_FILE = _resolve_data_path("user_portfolios.json")
PORTFOLIO_FILE = _resolve_data_path("portfolio_holdings.json")
_file_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="stock_worker")
_json_mem_cache = {}

def _read_json_cached(filepath, default_factory=dict):
    with _file_lock:
        if not os.path.exists(filepath):
            _json_mem_cache.pop(filepath, None)
            return default_factory() if callable(default_factory) else default_factory
        try:
            mtime = os.path.getmtime(filepath)
            cached = _json_mem_cache.get(filepath)
            if cached is not None and cached['mtime'] == mtime:
                return json.loads(cached['raw'])
            with open(filepath, "r", encoding="utf-8") as f:
                raw = f.read()
            parsed = json.loads(raw)
            _json_mem_cache[filepath] = {'mtime': mtime, 'raw': raw}
            return parsed
        except Exception:
            return default_factory() if callable(default_factory) else default_factory

# ==================== CLOUD PERSISTENCE (SUPABASE) ====================
# โครงสร้างตาราง Supabase (public.user_profiles): แยกแถวตาม user_id เพื่อความยืดหยุ่นสูงสุดและต่อยอด Dashboard ได้ทันที
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_KEY = (
    os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_ANON_KEY")
    or ""
).strip()
SUPABASE_TABLE = (os.getenv("SUPABASE_TABLE") or "user_profiles").strip()
_SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

USER_DATA_FILES = {
    "user_watchlists": USER_WATCHLIST_FILE,
    "user_alerts": USER_ALERT_FILE,
    "user_portfolios": USER_PORTFOLIO_FILE,
}
_suppress_cloud_sync = False

def _write_json_local_only(filepath, data):
    with _file_lock:
        try:
            raw = json.dumps(data, ensure_ascii=False, indent=2)
            temp_path = filepath + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(raw)
            os.replace(temp_path, filepath)
            mtime = os.path.getmtime(filepath)
            _json_mem_cache[filepath] = {'mtime': mtime, 'raw': raw}
        except Exception as e:
            print(f"Error saving {filepath}: {e}")

def _supabase_upsert_all_users():
    """ซิงค์ข้อมูลผู้ใช้ทั้งหมดขึ้นตาราง user_profiles บน Supabase แบบแยกแถวรายคน"""
    if not _SUPABASE_ENABLED:
        return False
    try:
        watchlists = _read_json_cached(USER_WATCHLIST_FILE, dict)
        portfolios = _read_json_cached(USER_PORTFOLIO_FILE, dict)
        alerts = _read_json_cached(USER_ALERT_FILE, dict)
        owner_port = _read_json_cached(PORTFOLIO_FILE, dict)
        owner_uid = str(DISCORD_OWNER_ID or owner_port.get("owner_id") or "").strip()
        if owner_uid and owner_port.get("holdings") and not portfolios.get(owner_uid):
            portfolios = dict(portfolios)
            portfolios[owner_uid] = owner_port
        all_uids = (set(watchlists.keys()) | set(portfolios.keys()) | set(alerts.keys())) - {"unit_test_user_888", "test_user_999", "friend_123"}
        if not all_uids:
            return True
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows = [
            {
                "user_id": str(uid),
                "watchlist": watchlists.get(uid, []),
                "portfolio": portfolios.get(uid, {}),
                "alerts": alerts.get(uid, []),
                "updated_at": now_iso,
            }
            for uid in all_uids
        ]
        url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=user_id"
        body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"⚠️ Supabase upsert warning: {e}", flush=True)
        return False

def restore_user_data_from_supabase() -> bool:
    """ดึงข้อมูลผู้ใช้ทุกคนจากตาราง user_profiles บน Supabase มาลงไฟล์แคชในเครื่องตอนเปิดบอท"""
    global _suppress_cloud_sync
    if not _SUPABASE_ENABLED:
        return False
    try:
        url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?select=user_id,watchlist,portfolio,alerts"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if not isinstance(rows, list) or not rows:
            return False

        watchlists = _read_json_cached(USER_WATCHLIST_FILE, dict)
        portfolios = _read_json_cached(USER_PORTFOLIO_FILE, dict)
        alerts = _read_json_cached(USER_ALERT_FILE, dict)
        watchlists.pop("unit_test_user_888", None)
        portfolios.pop("unit_test_user_888", None)
        alerts.pop("unit_test_user_888", None)

        owner_portfolio = None
        for r in rows:
            uid = str(r.get("user_id") or "").strip()
            if not uid or uid == "unit_test_user_888":
                continue
            if isinstance(r.get("watchlist"), list):
                watchlists[uid] = r["watchlist"]
            if isinstance(r.get("portfolio"), dict) and r["portfolio"]:
                portfolios[uid] = r["portfolio"]
                if (DISCORD_OWNER_ID and uid == str(DISCORD_OWNER_ID)) or str(r["portfolio"].get("owner_id", "")) == uid:
                    owner_portfolio = r["portfolio"]
            if isinstance(r.get("alerts"), list):
                alerts[uid] = r["alerts"]

        _suppress_cloud_sync = True
        try:
            _write_json_local_only(USER_WATCHLIST_FILE, watchlists)
            _write_json_local_only(USER_PORTFOLIO_FILE, portfolios)
            _write_json_local_only(USER_ALERT_FILE, alerts)
            if owner_portfolio:
                _write_json_local_only(PORTFOLIO_FILE, owner_portfolio)
        finally:
            _suppress_cloud_sync = False
        print(f"☁️ กู้คืนข้อมูลผู้ใช้จาก Supabase สำเร็จ ({len(rows)} ผู้ใช้)", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ ดึงข้อมูลจาก Supabase ไม่สำเร็จ: {e}", flush=True)
        return False

def _write_json_cached(filepath, data):
    _write_json_local_only(filepath, data)
    if not _suppress_cloud_sync and _SUPABASE_ENABLED and filepath in USER_DATA_FILES.values():
        _executor.submit(_supabase_upsert_all_users)

# Multi-sector popular US Tickers for smart autocomplete
POPULAR_TICKERS = [
    # Core & Broad ETFs
    "VOO", "QQQM", "SPY", "QQQ", "SCHD", "VTI", "SMH", "XLK", "XLF", "XLV",
    # Technology & AI Leaders
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "TSM", "AVGO", "AMD", "PLTR",
    # Healthcare & Pharma
    "LLY", "UNH", "JNJ", "ABBV",
    # Financials & Payments
    "JPM", "V", "MA", "BRK-B",
    # Consumer & Retail
    "COST", "WMT", "KO", "PG",
    # Industrials & Energy
    "CAT", "GE", "XOM"
]
POPULAR_TICKERS_SET = set(POPULAR_TICKERS)
ETF_SYMBOLS = {'VOO', 'QQQM', 'SPY', 'QQQ', 'IVV', 'VTI', 'SCHD', 'SMH', 'XLK', 'XLF', 'XLV', 'XLE', 'IWM', 'DIA', 'GLD', 'SLV', 'USO'}

# Common ticker aliases & index mappings for user-friendly input resolution
TICKER_ALIASES = {
    'S&P500': 'SPY',
    'S&P 500': 'SPY',
    'SP500': 'SPY',
    'S&P': 'SPY',
    'SPX': 'SPY',
    'NASDAQ': 'QQQ',
    'NASDAQ100': 'QQQ',
    'NDX': 'QQQ',
    'DOW': 'DIA',
    'DJI': 'DIA',
    'DOWJONES': 'DIA',
    'RUSSELL': 'IWM',
    'RUSSELL2000': 'IWM',
    'GOLD': 'GLD',
    'SILVER': 'SLV',
    'OIL': 'USO',
}

_RE_LEADING_SIGNS = re.compile(r'^[$#]+')
_RE_PREFIX_TYPO = re.compile(r'^(ticker|ticket|sym|symbol|stock)\s*[:=]?\s*', re.IGNORECASE)
_RE_CLASS_SHARE = re.compile(r'^[A-Za-z]{1,5}\.[A-Za-z]$')

def clean_ticker(ticker_str: str) -> str:
    if not ticker_str:
        return ""
    s = str(ticker_str).strip().strip('"\'`')
    s = _RE_LEADING_SIGNS.sub('', s).strip()
    s = _RE_PREFIX_TYPO.sub('', s).strip()
    if _RE_CLASS_SHARE.match(s):
        s = s.replace('.', '-')
    s_upper = s.upper().strip()
    return TICKER_ALIASES.get(s_upper, s_upper)

# In-memory Macro Cache (3 minutes TTL)
_macro_cache = {
    'data': None,
    'timestamp': 0
}
_macro_lock = threading.Lock()

def get_cached_macro(ttl_seconds=180):
    now = time.time()
    if _macro_cache['data'] and (now - _macro_cache['timestamp'] < ttl_seconds):
        return _macro_cache['data']
    with _macro_lock:
        now = time.time()
        if _macro_cache['data'] and (now - _macro_cache['timestamp'] < ttl_seconds):
            return _macro_cache['data']
        try:
            data = analyze_macro()
            _macro_cache['data'] = data
            _macro_cache['timestamp'] = now
            return data
        except Exception as e:
            print(f"Error fetching macro: {e}", flush=True)
            if _macro_cache['data']:
                return _macro_cache['data']
            fallback = {
                'regime': 'Neutral (Safe Fallback)',
                'vix': 18.0,
                '10y_yield': 4.25,
                'spx': 5500.0,
                'spx_ema50': 5400.0,
                'spx_above_ema50': True,
                'hurdle': 80,
                'macro_score': 10
            }
            _macro_cache['data'] = fallback
            _macro_cache['timestamp'] = now
            return fallback

# Helper: Visual Progress Bar
def make_progress_bar(val, max_val=100, length=10):
    ratio = max(0.0, min(1.0, val / max_val))
    filled = int(round(ratio * length))
    empty = length - filled
    return "█" * filled + "░" * empty

# Helper: Per-User Watchlist persistence (Thread-safe & Atomic write with in-memory mtime cache)
def load_user_watchlists():
    return _read_json_cached(USER_WATCHLIST_FILE, dict)

def save_user_watchlists(data):
    _write_json_cached(USER_WATCHLIST_FILE, data)

def get_user_symbols(user_id_str):
    watchlists = load_user_watchlists()
    return list(watchlists.get(str(user_id_str), []))

# ==================== PORTFOLIO DATA & EMBED ====================

def load_portfolio_data():
    return _read_json_cached(PORTFOLIO_FILE, lambda: None)

def load_user_portfolios():
    return _read_json_cached(USER_PORTFOLIO_FILE, dict)

def save_user_portfolios(data):
    _write_json_cached(USER_PORTFOLIO_FILE, data)

def get_user_portfolio_data(user_id_str):
    """ดึงข้อมูลพอร์ตของ User หากไม่มีและเป็นเจ้าของ ให้ fallback ไปที่ portfolio_holdings.json"""
    user_id_str = str(user_id_str) if user_id_str else ""
    portfolios = load_user_portfolios()
    
    if user_id_str and user_id_str in portfolios:
        return portfolios[user_id_str]
    
    # ถ้าไม่มี user_id หรือเป็นเจ้าของ ให้ fallback ไปอ่าน portfolio_holdings.json
    if not user_id_str or (DISCORD_OWNER_ID and user_id_str == str(DISCORD_OWNER_ID)):
        return load_portfolio_data()
    
    p_data = load_portfolio_data()
    if p_data:
        json_owner = p_data.get("owner_id") or p_data.get("discord_user_id")
        if json_owner and str(user_id_str) == str(json_owner):
            return p_data

    return None

def add_user_portfolio_holding(user_id_str, ticker, shares, avg_cost, category="General"):
    user_id_str = str(user_id_str)
    ticker = clean_ticker(ticker)
    portfolios = load_user_portfolios()
    
    user_port = portfolios.get(user_id_str)
    if not user_port:
        base = get_user_portfolio_data(user_id_str)
        if base and "holdings" in base:
            user_port = json.loads(json.dumps(base))
        else:
            user_port = {
                "updated_at": datetime.datetime.now().isoformat(),
                "source": "Dime! App Portfolio",
                "holdings": {}
            }
    
    if "holdings" not in user_port:
        user_port["holdings"] = {}
        
    user_port["updated_at"] = datetime.datetime.now().isoformat()
    user_port["holdings"][ticker] = {
        "shares": float(shares),
        "avg_cost_usd": float(avg_cost),
        "total_cost_usd": round(float(shares) * float(avg_cost), 2),
        "category": str(category) if category else "General"
    }
    portfolios[user_id_str] = user_port
    save_user_portfolios(portfolios)
    return True

def remove_user_portfolio_holding(user_id_str, ticker):
    user_id_str = str(user_id_str)
    ticker = clean_ticker(ticker)
    portfolios = load_user_portfolios()
    
    user_port = portfolios.get(user_id_str)
    if not user_port:
        base = get_user_portfolio_data(user_id_str)
        if base and "holdings" in base:
            user_port = json.loads(json.dumps(base))
        else:
            return False
            
    holdings = user_port.get("holdings", {})
    if ticker in holdings:
        del holdings[ticker]
        user_port["updated_at"] = datetime.datetime.now().isoformat()
        portfolios[user_id_str] = user_port
        save_user_portfolios(portfolios)
        return True
    return False

def clear_user_portfolio(user_id_str):
    user_id_str = str(user_id_str)
    portfolios = load_user_portfolios()
    portfolios[user_id_str] = {
        "updated_at": datetime.datetime.now().isoformat(),
        "source": "Dime! App Portfolio",
        "holdings": {}
    }
    save_user_portfolios(portfolios)
    return True

async def is_portfolio_owner(user) -> bool:
    """ตรวจสอบว่าเป็นเจ้าของพอร์ตหรือไม่"""
    if user is None:
        return False
    # 1. เช็คจาก DISCORD_OWNER_ID ใน .env
    if DISCORD_OWNER_ID and user.id == DISCORD_OWNER_ID:
        return True
    
    # 2. เช็คจาก owner_id ในไฟล์ portfolio_holdings.json
    try:
        p_data = load_portfolio_data()
        if p_data:
            json_owner = p_data.get("owner_id") or p_data.get("discord_user_id")
            if json_owner and str(user.id) == str(json_owner):
                return True
    except Exception:
        pass

    # 3. Fallback: เช็คว่าเป็นเจ้าของ Discord Application (ผู้สร้างบอท) หรือไม่
    try:
        if await bot.is_owner(user):
            return True
    except Exception:
        pass

    return False

def build_portfolio_embed(user_id=None, user_name="", refresh_live_prices=True):
    data = get_user_portfolio_data(user_id) if user_id else load_portfolio_data()
    display_title_name = f"{user_name}'s Portfolio" if user_name else "Dime! Portfolio"
    if not data or not data.get("holdings"):
        if user_id:
            embed = discord.Embed(
                title=f"💼 พอร์ตโฟลิโอของคุณ ({display_title_name})",
                description=(
                    "ยังไม่มีข้อมูลหุ้นในพอร์ตโฟลิโอของคุณในระบบ\n\n"
                    "**วิธีเริ่มต้นเพิ่มหุ้นในพอร์ต:**\n"
                    "• กดปุ่ม **`[➕ เพิ่ม/แก้หุ้น]`** ด้านล่าง เพื่อกรอกผ่านฟอร์ม\n"
                    "• หรือพิมพ์คำสั่ง: `/port_add <ชื่อย่อหุ้น> <จำนวนหุ้น> <ต้นทุนเฉลี่ย>`\n"
                    "  *(เช่น `/port_add NVDA 1.5 210.0`)*"
                ),
                color=0x3498DB
            )
            return embed
        embed = discord.Embed(
            title="💼 ไม่พบข้อมูลพอร์ตโฟลิโอ (portfolio_holdings.json)",
            description="กรุณาตรวจสอบว่ามีไฟล์ `portfolio_holdings.json` อยู่ในโฟลเดอร์บอทเรียบร้อยแล้ว",
            color=0xE74C3C
        )
        return embed

    summary = data.get("portfolio_summary", {})
    holdings = data.get("holdings", {})
    source_name = f"{user_name}'s Portfolio" if user_name else data.get("source", "Dime! Portfolio")

    symbols = list(holdings.keys())
    fetched_map = {}
    if refresh_live_prices and symbols:
        try:
            fetched_items = list(_executor.map(fetch_ticker_data, symbols))
            for s, item in zip(symbols, fetched_items):
                if item:
                    fetched_map[s] = item
        except Exception as e:
            print(f"Error fetching live prices for portfolio: {e}")

    total_cost = 0.0
    total_value = 0.0
    updated_holdings = []

    for sym, h in holdings.items():
        shares = float(h.get("shares", 0))
        avg_cost = float(h.get("avg_cost_usd", 0))
        cost = float(h.get("total_cost_usd", shares * avg_cost))
        category = h.get("category", "General")

        live_item = fetched_map.get(sym)
        if live_item and live_item.get('tech'):
            curr_price = float(live_item['tech'].get('price', h.get("current_price_usd", avg_cost)))
        else:
            curr_price = float(h.get("current_price_usd", avg_cost))

        curr_val = shares * curr_price
        pnl_val = curr_val - cost
        pnl_pct = (pnl_val / cost * 100) if cost > 0 else 0.0

        total_cost += cost
        total_value += curr_val

        updated_holdings.append({
            "ticker": sym,
            "shares": shares,
            "avg_cost": avg_cost,
            "curr_price": curr_price,
            "cost": cost,
            "curr_val": curr_val,
            "pnl_val": pnl_val,
            "pnl_pct": pnl_pct,
            "category": category
        })

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

    updated_holdings.sort(key=lambda x: x["curr_val"], reverse=True)
    for item in updated_holdings:
        item["weight_pct"] = (item["curr_val"] / total_value * 100) if total_value > 0 else 0.0

    color = 0x2ECC71 if total_pnl >= 0 else 0xE74C3C
    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

    now_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    embed = discord.Embed(
        title=f"💼 สรุปภาพรวมพอร์ตการลงทุน ({source_name})",
        description=f"อัปเดตราคาตลาดสหรัฐฯ ล่าสุด • {now_str}",
        color=color
    )

    embed.add_field(
        name="💰 มูลค่าพอร์ตรวม (Total Value)",
        value=f"**${total_value:,.2f}**\n(ต้นทุน: `${total_cost:,.2f}`)",
        inline=True
    )
    embed.add_field(
        name=f"{pnl_emoji} กำไร/ขาดทุนรวม (Total PnL)",
        value=f"**{pnl_sign}${total_pnl:,.2f}**\n(`{pnl_sign}{total_pnl_pct:.2f}%`)",
        inline=True
    )
    embed.add_field(
        name="📦 สินทรัพย์ทั้งหมด",
        value=f"**{len(updated_holdings)}** ตัว",
        inline=True
    )

    # Category Breakdown
    cats = {}
    for item in updated_holdings:
        c = item["category"]
        cats[c] = cats.get(c, 0.0) + item["curr_val"]
    cat_strs = [f"• **{k}**: {(v/total_value*100):.1f}% (`${v:,.1f}`)" for k, v in cats.items()]
    embed.add_field(
        name="📊 สัดส่วนตามหมวดหมู่ (Allocation)",
        value="\n".join(cat_strs) if cat_strs else "-",
        inline=False
    )

    # Holdings details
    lines = []
    for item in updated_holdings:
        sym = item["ticker"]
        sign = "+" if item["pnl_val"] >= 0 else ""
        sym_emoji = "🟢" if item["pnl_val"] >= 0 else "🔴"
        lines.append(
            f"{sym_emoji} **`{sym:<5}`** | `${item['curr_price']:.2f}` (ทุน `${item['avg_cost']:.2f}`) | "
            f"**{sign}{item['pnl_pct']:.2f}%** ({sign}${item['pnl_val']:.2f}) | น้ำหนัก {item['weight_pct']:.1f}%"
        )

    embed.add_field(
        name="📋 รายละเอียดสินทรัพย์ในพอร์ต",
        value="\n".join(lines[:15]) if lines else "ไม่มีรายการ",
        inline=False
    )

    embed.set_footer(text="Dime! App Portfolio • ข้อมูลเพื่อประกอบการตัดสินใจส่วนบุคคล")
    return embed

# ==================== PRICE ALERT STORAGE & HELPERS ====================

def load_user_alerts():
    return _read_json_cached(USER_ALERT_FILE, dict)

def save_user_alerts(data):
    _write_json_cached(USER_ALERT_FILE, data)

def add_user_alert(user_id_str, channel_id, ticker, target_price, current_price=None):
    ticker = clean_ticker(ticker)
    if not ticker or target_price <= 0:
        return False, "⚠️ ชื่อหุ้นหรือราคาเป้าหมายไม่ถูกต้อง"

    if current_price is None:
        cached_p = get_cached_price(ticker)
        if cached_p > 0:
            current_price = cached_p
        else:
            data = fetch_ticker_data(ticker)
            if data and data.get('tech'):
                current_price = data['tech'].get('price', target_price)
            else:
                current_price = target_price

    condition = "gte" if target_price >= current_price else "lte"
    cond_desc = f"ขึ้นแตะหรือทะลุ ${target_price:.2f}" if condition == "gte" else f"ย่อลงแตะหรือต่ำกว่า ${target_price:.2f}"

    alerts = load_user_alerts()
    user_list = alerts.get(str(user_id_str), [])

    if len(user_list) >= 20:
        return False, "⚠️ คุณมีรายการแจ้งเตือนครบโควตา 20 รายการแล้ว กรุณาลบรายการเก่าออกก่อน"

    alert_id = f"{ticker}_{target_price:.2f}_{condition}"
    for a in user_list:
        if a.get('symbol') == ticker and abs(a.get('target_price', 0) - target_price) < 0.001 and not a.get('triggered', False):
            return False, f"ℹ️ มีรายการแจ้งเตือน `{ticker}` ที่ราคา `${target_price:.2f}` อยู่แล้ว"

    new_alert = {
        "id": alert_id,
        "symbol": ticker,
        "target_price": round(float(target_price), 2),
        "condition": condition,
        "channel_id": int(channel_id) if channel_id else None,
        "created_at": datetime.datetime.now().isoformat(),
        "initial_price": round(float(current_price), 2),
        "triggered": False
    }
    user_list.append(new_alert)
    alerts[str(user_id_str)] = user_list
    save_user_alerts(alerts)
    return True, f"✅ ตั้งแจ้งเตือน `{ticker}` เมื่อราคา{cond_desc} สำเร็จ! (ราคาปัจจุบัน: `${current_price:.2f}`)"

def remove_user_alert(user_id_str, ticker_or_id):
    ticker_or_id = ticker_or_id.strip()
    alerts = load_user_alerts()
    user_list = alerts.get(str(user_id_str), [])
    if not user_list:
        return 0
    clean_sym = clean_ticker(ticker_or_id)
    rem = [a for a in user_list if a.get('id') == ticker_or_id or a.get('symbol') == clean_sym]
    if not rem:
        return 0
    new_list = [a for a in user_list if a not in rem]
    alerts[str(user_id_str)] = new_list
    save_user_alerts(alerts)
    return len(rem)

def build_alerts_embed(user_name, user_id_str):
    alerts = load_user_alerts()
    user_list = [a for a in alerts.get(str(user_id_str), []) if not a.get('triggered', False)]

    embed = discord.Embed(
        title=f"🔔 รายการแจ้งเตือนราคาของคุณ ({user_name})",
        color=0x3498DB
    )
    if not user_list:
        embed.description = (
            "ขณะนี้คุณยังไม่มีการตั้งเตือนราคาหุ้นครับ\n\n"
            "💡 **วิธีตั้งเตือนราคา:**\n"
            "• `/alert_set <ticker> <target_price>` เช่น `/alert_set NVDA 250`\n"
            "• หรือกดปุ่ม `[🔔 ตั้งเตือนราคา]` ในหน้า `/price` ได้ทันที"
        )
        return embed

    lines = []
    for a in user_list:
        sym = a.get('symbol')
        target = a.get('target_price')
        init_p = a.get('initial_price', target)
        cond = "📈 ขึ้นแตะ ≥" if a.get('condition') == "gte" else "📉 ย่อลงแตะ ≤"
        lines.append(f"• **`{sym}`**: {cond} **${target:.2f}** (ตั้งไว้ตอน `${init_p:.2f}`)")

    embed.description = f"รายการแจ้งเตือนที่กำลังเปิดใช้งาน ({len(user_list)} รายการ):\n\n" + "\n".join(lines)
    embed.set_footer(text="ระบบจะตรวจสอบราคาอัตโนมัติและส่งข้อความเตือนเมื่อราคาแตะเป้า")
    return embed

# Ticker validation via Yahoo Finance (with fast-path lookup)
def is_valid_ticker(ticker_sym):
    ticker_sym = clean_ticker(ticker_sym)
    if not ticker_sym:
        return False
    if ticker_sym in POPULAR_TICKERS_SET or ticker_sym in ETF_SYMBOLS:
        return True
    with _ticker_cache_lock:
        if ticker_sym in _ticker_cache:
            return True
    try:
        s = get_session()
        t = yf.Ticker(ticker_sym, session=s) if s else yf.Ticker(ticker_sym)
        hist = t.history(period="5d")
        return not hist.empty
    except Exception:
        return False

# Sector-Adaptive Fundamental & Valuation Evaluator (All-Around US Equities & ETFs)
def analyze_all_around_fundamentals(ticker_obj, is_etf=False, info=None):
    if is_etf:
        category = (info or {}).get('category', 'Index') if info else 'Index'
        return {
            'is_etf': True,
            'sector_raw': 'ETF',
            'sector_display': f"กองทุนดัชนี (ETF: {category})",
            'industry': category,
            'fund_score': 25,
            'val_score': 16,
            'risk_score': 9,
            'portfolio_fit': 5,
            'fwd_pe': 'N/A',
            'peg': 'N/A',
            'rev_growth': 'N/A',
            'market_cap': 0
        }

    if info is None:
        try:
            info = ticker_obj.info or {}
        except Exception:
            info = {}
            
    q_type = str(info.get('quoteType', '')).upper()
    if q_type == 'ETF':
        category = info.get('category', 'Index')
        return {
            'is_etf': True,
            'sector_raw': 'ETF',
            'sector_display': f"กองทุนดัชนี (ETF: {category})",
            'industry': category,
            'fund_score': 25,
            'val_score': 16,
            'risk_score': 9,
            'portfolio_fit': 5,
            'fwd_pe': 'N/A',
            'peg': 'N/A',
            'rev_growth': 'N/A',
            'market_cap': 0
        }
        
    sec_raw = info.get('sector', 'US Equities')
    industry = info.get('industry', 'Equities')
    sec_display = SECTOR_MAP.get(sec_raw, sec_raw if sec_raw else "หุ้นสหรัฐฯ (US Equities)")
    
    rev_g = info.get('revenueGrowth', 0) or 0
    earn_g = info.get('earningsGrowth', 0) or 0
    fwd_pe = info.get('forwardPE', None)
    peg = info.get('pegRatio', None)
    mcap = info.get('marketCap', 0) or 0
    pm = info.get('profitMargins', 0) or 0
    roe = info.get('returnOnEquity', 0) or 0
    
    # 1. Fundamental Score (Max 30) - Sector Adaptive
    fund = 15
    if sec_raw == 'Financial Services':
        # Banks & Financials: evaluate by ROE and Profit Margins
        if roe > 0.15: fund += 8
        elif roe > 0.10: fund += 4
        if pm > 0.20: fund += 7
        elif pm > 0.10: fund += 4
    elif sec_raw in ['Consumer Defensive', 'Utilities', 'Healthcare']:
        # Defensive / Quality Dividend: steady cash cows
        if rev_g > 0.10: fund += 6
        elif rev_g > 0.04: fund += 4
        if earn_g > 0.10: fund += 6
        elif earn_g > 0.05: fund += 3
        if roe > 0.15: fund += 3
    else:
        # Tech, Consumer Cyclical, Industrials, Energy, Materials
        if rev_g > 0.20: fund += 7
        elif rev_g > 0.10: fund += 4
        elif rev_g > 0: fund += 2
        if earn_g > 0.20: fund += 8
        elif earn_g > 0.10: fund += 4
    fund = max(0, min(30, fund))
    
    # 2. Valuation Score (Max 20) - Sector Adaptive Benchmarks
    val = 10
    if peg is not None and peg > 0:
        if peg < 1.5: val += 8
        elif peg <= 2.2: val += 5
        elif peg > 3.0: val -= 3
    elif fwd_pe is not None and fwd_pe > 0:
        pe_bench = 18 if sec_raw in ['Financial Services', 'Energy'] else (28 if sec_raw in ['Consumer Defensive', 'Utilities'] else 35)
        if fwd_pe < pe_bench * 0.8: val += 7
        elif fwd_pe <= pe_bench * 1.1: val += 4
        elif fwd_pe > pe_bench * 1.5: val -= 3
    val = max(0, min(20, val))
    
    # 3. Balance Sheet & Risk Score (Max 10)
    risk = 7
    if sec_raw == 'Financial Services':
        risk = 9 if roe > 0.12 else 7
    else:
        tot_debt = info.get('totalDebt', 0) or 0
        fcf = info.get('freeCashflow', 0) or 0
        if fcf > 0:
            df = tot_debt / fcf
            if df < 2.5: risk += 3
            elif df > 6.0: risk -= 3
    risk = max(0, min(10, risk))
    
    # 4. Portfolio Fit (Max 5) - Scale and Moat Stability
    if mcap > 200e9:
        fit = 5 # Mega-Cap Global Anchor
    elif mcap > 50e9:
        fit = 4 # Large-Cap Quality Pillar
    elif mcap > 10e9:
        fit = 3 # Mid-Cap Solid Performer
    else:
        fit = 3
        
    return {
        'is_etf': False,
        'sector_raw': sec_raw,
        'sector_display': sec_display,
        'industry': industry,
        'fund_score': fund,
        'val_score': val,
        'risk_score': risk,
        'portfolio_fit': fit,
        'fwd_pe': round(fwd_pe, 1) if fwd_pe else 'N/A',
        'peg': round(peg, 2) if peg else 'N/A',
        'rev_growth': f"{round(rev_g*100, 1)}%" if rev_g else 'N/A',
        'market_cap': mcap
    }

def trim_memory():
    """Forces garbage collection and releases glibc heap memory to OS kernel."""
    import gc
    gc.collect()
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass

# In-memory Ticker Cache (180s TTL, bounded to 20 items) & Fundamental Cache (1800s TTL, bounded to 30 items)
_ticker_cache = {}
_ticker_cache_lock = threading.Lock()
_fund_cache = {}
_MAX_TICKER_CACHE = 20
_MAX_FUND_CACHE = 30

def _evict_oldest(cache_dict: dict, max_items: int):
    while len(cache_dict) > max_items:
        oldest_key = min(cache_dict, key=lambda k: cache_dict[k].get('timestamp', 0))
        cache_dict.pop(oldest_key, None)

def get_cached_price(ticker_sym: str) -> float:
    """Fast 0ms non-blocking lookup of last known price in memory"""
    ticker_sym = clean_ticker(ticker_sym)
    with _ticker_cache_lock:
        cached = _ticker_cache.get(ticker_sym)
        if cached and cached.get('data') and cached['data'].get('tech'):
            return float(cached['data']['tech'].get('price', 0.0))
    return 0.0

# Parallel data fetcher for single ticker with Two-Tier TTL caching
def fetch_ticker_data(ticker_sym, force_refresh=False, ttl_seconds=180):
    ticker_sym = clean_ticker(ticker_sym)
    if not ticker_sym:
        return None
    now = time.time()
    if not force_refresh:
        with _ticker_cache_lock:
            cached = _ticker_cache.get(ticker_sym)
            if cached and (now - cached['timestamp'] < ttl_seconds):
                return cached['data']

    is_etf_static = ticker_sym in ETF_SYMBOLS
    try:
        s = get_session()
        t = yf.Ticker(ticker_sym, session=s) if s else yf.Ticker(ticker_sym)
        hist = t.history(period="1y")
        if hist.empty:
            return None
        tech = calculate_technical(hist)

        # Keep only recent compact OHLCV float32 slice (35 rows) to drastically cut RAM
        cols = [c for c in ('Open', 'High', 'Low', 'Close', 'Volume') if c in hist.columns]
        compact_hist = hist[cols].tail(35).astype('float32')

        fund_val = None
        with _ticker_cache_lock:
            f_cached = _fund_cache.get(ticker_sym)
            if f_cached and (now - f_cached['timestamp'] < 3600):
                fund_val = f_cached['data']

        if fund_val is None:
            if is_etf_static:
                fund_val = analyze_all_around_fundamentals(t, is_etf=True, info={})
            else:
                info = {}
                try:
                    info = t.info or {}
                except Exception:
                    pass
                fund_val = analyze_all_around_fundamentals(t, is_etf=False, info=info)
            with _ticker_cache_lock:
                _fund_cache[ticker_sym] = {'data': fund_val, 'timestamp': now}
                if len(_fund_cache) > _MAX_FUND_CACHE:
                    _evict_oldest(_fund_cache, _MAX_FUND_CACHE)

        res = {
            'ticker': ticker_sym,
            'is_etf': fund_val['is_etf'],
            'tech': tech,
            'fund_val': fund_val,
            'hist': compact_hist
        }
        with _ticker_cache_lock:
            _ticker_cache[ticker_sym] = {
                'data': res,
                'timestamp': now
            }
            if len(_ticker_cache) > _MAX_TICKER_CACHE:
                _evict_oldest(_ticker_cache, _MAX_TICKER_CACHE)
        return res
    except Exception as e:
        print(f"Error fetching {ticker_sym}: {e}")
        return None

# Helper: Quantitative Entry Zone Calculator (Pullback Sweetspot, Multi-Tier Support & Stop Loss)
def compute_entry_zone(tech):
    price = tech.get('price', 0)
    ema20 = tech.get('ema20', price)
    ema50 = tech.get('ema50', price)
    ema200 = tech.get('ema200', price)
    ema_dist = tech.get('ema_dist', 0)
    
    if -2.0 <= ema_dist <= 2.5:
        # Tier 1: In Sweetspot (near EMA20)
        buy_low = ema20 * 0.985
        buy_high = ema20 * 1.02
        stop_loss = ema50 * 0.97
        zone_type = "ใกล้เส้น EMA20"
        entry_status = "🟢 อยู่ในโซนซื้อเหมาะสม (In Sweetspot)"
        entry_advice = "ราคาอยู่ในโซนย่อตัวที่ได้เปรียบต้นทุน เข้าซื้อสะสมได้ทันที"
        playbook_note = "จังหวะย่อตัวปกติในแนวโน้มขาขึ้น ทยอยสะสมตามแผนได้"
        buy_zone_str = f"${buy_low:.2f} - ${buy_high:.2f}"
    elif ema_dist > 2.5:
        # Tier 1: Extended
        buy_low = ema20 * 0.985
        buy_high = ema20 * 1.02
        stop_loss = ema50 * 0.97
        zone_type = "เป้าหมายย่อตัว EMA20"
        entry_status = f"🟡 วิ่งเกินจุดซื้อ (+{ema_dist:.1f}%) รอจังหวะย่อ"
        entry_advice = f"ราคาไล่ขึ้นสูง แนะนำรอราคาย่อตัวกลับมาที่ ${buy_low:.2f} - ${buy_high:.2f}"
        playbook_note = "ราคาขึ้นมาไกล ไม่ควรไล่ราคา รอให้ย่อตัวกลับเข้าหากรอบ EMA20"
        buy_zone_str = f"${buy_low:.2f} - ${buy_high:.2f}"
    else:
        # Deep Pullback / Breakdown (ema_dist < -2.0%)
        if price >= ema50 * 0.97:
            # Tier 2: Testing EMA50 Intermediate Support
            buy_low = ema50 * 0.985
            buy_high = ema50 * 1.015
            stop_loss = ema50 * 0.95
            zone_type = "แนวรับ EMA50"
            entry_status = f"🟠 พักฐานลึก ({ema_dist:.1f}%) รอตั้งรับ EMA50"
            entry_advice = f"ราคาหลุด EMA20 กลายเป็นแนวต้าน แนะนำชะลอซื้อและรอดูแรงรับที่โซน EMA50 (${buy_low:.2f} - ${buy_high:.2f})"
            playbook_note = "[ถือต่อ] มีของห้ามตกใจขาย | [ช้อนซื้อ] รอราคายืน EMA50 ได้แล้วแบ่งไม้ซื้อ | [พัก] ถือเงินสดรอความนิ่ง"
            buy_zone_str = f"${buy_low:.2f} - ${buy_high:.2f}"
        elif ema200 and ema200 > 0 and price >= ema200 * 0.97:
            # Tier 3: Testing EMA200 Long-term Support
            buy_low = ema200 * 0.985
            buy_high = ema200 * 1.015
            stop_loss = ema200 * 0.95
            zone_type = "แนวรับใหญ่ EMA200"
            entry_status = f"🔴 หลุดแนวรับ EMA50 ({ema_dist:.1f}%) รอรับ EMA200"
            entry_advice = f"ราคาหลุดทั้ง EMA20 และ EMA50 แนวโน้มระยะกลางเสียทรง แนะนำชะลอซื้อ รอดูแรงรับที่แนวรับใหญ่ EMA200 (${buy_low:.2f} - ${buy_high:.2f})"
            playbook_note = "[ถือต่อ] หุ้น Core ระยะยาวถือได้ | [ช้อนซื้อ] ชะลอรับมีด รอสร้างฐานที่ EMA200 | [พัก] ถือเงินสดปลอดภัยสุด"
            buy_zone_str = f"${buy_low:.2f} - ${buy_high:.2f}"
        else:
            # Tier 4: Major breakdown below EMA200
            buy_low = price * 0.92
            buy_high = price * 0.96
            stop_loss = price * 0.88
            zone_type = "ชะลอซื้อ รอสร้างฐาน"
            entry_status = "🔴 เสียทรงขาลง (Breakdown)"
            entry_advice = "ราคาหลุดแนวรับสำคัญทุกเส้น แนะนำพักการลงทุน (Wait) รอราคาหยุดลงและสร้างฐานใหม่"
            playbook_note = "พักการเข้าซื้อทันที ห้ามรับมีด ถือเงินสดรอดูสถานการณ์"
            buy_zone_str = "ชะลอซื้อ (รอฐาน)"

    # BSL Sweep Trap Warning Override
    if tech.get('bsl_sweep'):
        entry_status = "⚠️ ระวังติดดอย (เกิด BSL Sweep ชนยอดดอย ห้ามไล่ราคา)"
        entry_advice = "ตรวจพบแรงเทขายกดทับหลังแทงทะลุ High 5 วัน (Liquidity Sweep) เสี่ยงพักฐานทันที ห้ามไล่ราคาเด็ดขาด"
        
    return {
        'buy_low': buy_low,
        'buy_high': buy_high,
        'stop_loss': stop_loss,
        'zone_type': zone_type,
        'entry_status': entry_status,
        'entry_advice': entry_advice,
        'playbook_note': playbook_note,
        'buy_zone_str': buy_zone_str
    }

# Core Quant Evaluator (All-Around US Equities)
def evaluate_stock(data_dict, macro, user_symbols=None):
    ticker_sym = data_dict['ticker']
    tech = data_dict['tech']
    fund_val = data_dict['fund_val']
    
    portfolio_fit = fund_val['portfolio_fit']
    sector_display = fund_val['sector_display']
    entry_info = compute_entry_zone(tech)
    
    total_score = (
        fund_val['fund_score'] + 
        fund_val['val_score'] + 
        tech.get('score', 10) + 
        macro['macro_score'] + 
        fund_val['risk_score'] + 
        portfolio_fit
    )
    
    hurdle = macro['hurdle']
    if total_score >= hurdle:
        raw_action = "BUY"
    elif total_score >= 65:
        raw_action = "HOLD"
    else:
        raw_action = "WAIT"

    final_action = raw_action
    override_applied = False
    override_reason = ""
    if tech.get('bsl_sweep', False) and raw_action == "BUY":
        final_action = "WAIT"
        override_applied = True
        override_reason = "เกิด BSL Sweep (ติดกับดักราคายอดดอย ห้ามไล่ราคา)"
    elif tech.get('ema_dist', 0) > 8.0 and raw_action == "BUY":
        final_action = "WAIT"
        override_applied = True
        override_reason = f"ราคาฉีกห่าง EMA20 เกิน +{tech.get('ema_dist', 0):.1f}% (เสี่ยงไล่ราคาดอย)"
    elif tech.get('rsi', 0) >= 70 and raw_action == "BUY":
        final_action = "HOLD"
        override_applied = True
        override_reason = f"RSI Overbought ({tech.get('rsi', 0):.1f} >= 70 เสี่ยงย่อพักฐาน)"
    elif tech.get('price', 0) < tech.get('ema50', 0) and tech.get('price', 0) < tech.get('ema200', 0) and raw_action == "BUY":
        final_action = "WAIT"
        override_applied = True
        override_reason = "แนวโน้มขาลง (ราคาหลุดต่ำกว่าทั้ง EMA50 และ EMA200)"
        
    return {
        'ticker': ticker_sym,
        'price': tech.get('price', 0),
        'rsi': tech.get('rsi', 0),
        'ema_dist': tech.get('ema_dist', 0),
        'score': total_score,
        'raw_action': raw_action,
        'final_action': final_action,
        'override': override_applied,
        'override_reason': override_reason,
        'sector_display': sector_display,
        'sector_raw': fund_val['sector_raw'],
        'peg': fund_val['peg'],
        'fwd_pe': fund_val['fwd_pe'],
        'fund_val': fund_val,
        'tech': tech,
        'portfolio_fit': portfolio_fit,
        'entry_info': entry_info
    }

# ==================== EMBED BUILDERS ====================

def build_scan_embed(symbols, user_name=""):
    if not symbols:
        embed = discord.Embed(
            title=f"⚠️ Watchlist ว่างเปล่า: {user_name}",
            description=(
                "ขณะนี้คุณยังไม่มีหุ้นใน Watchlist ส่วนตัวครับ (เริ่มต้นว่างเปล่า)\n\n"
                "📌 **วิธีเพิ่มหุ้นเข้า Watchlist:**\n"
                "• ใช้คำสั่ง `/add <ชื่อหุ้น>` เช่น `/add NVDA, AAPL, MSFT`\n"
                "• หรือพิมพ์ `!add TSLA`\n"
                "• หรือกดปุ่ม `[⭐ เพิ่มลง Watchlist]` เมื่อใช้คำสั่ง `/check` หรือ `/price`"
            ),
            color=0xF1C40F
        )
        embed.set_footer(text="Watchlist ถูกแยกเก็บเป็นรายบุคคลตาม Discord User ID")
        return embed

    macro_fut = _executor.submit(get_cached_macro)
    fetched_items = list(_executor.map(fetch_ticker_data, symbols))
    macro = macro_fut.result()
    results = []
        
    for item in fetched_items:
        if item is not None:
            evaluated = evaluate_stock(item, macro)
            results.append(evaluated)
            
    results.sort(key=lambda x: x['score'], reverse=True)
    
    buys = [r for r in results if r['final_action'] == 'BUY']
    holds = [r for r in results if r['final_action'] == 'HOLD']
    
    # Theme color: Green if BUY present, Yellow if HOLD, Red if Risk-off
    if buys:
        color = 0x2ECC71  # Emerald Green
    elif holds:
        color = 0xF1C40F  # Golden Yellow
    else:
        color = 0x95A5A6  # Gray
        
    now_str = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    embed = discord.Embed(
        title=f"📊 รายงานสแกนพอร์ตและแจ้งเตือนหุ้น",
        description=f"วิเคราะห์เฉพาะ Watchlist ของคุณ **{user_name}** • {now_str}",
        color=color
    )
    
    # Macro Status Field
    regime_emoji = "🟢" if "Bull" in macro['regime'] else ("🟡" if "Neutral" in macro['regime'] else "🔴")
    macro_info = (
        f"{regime_emoji} **Regime:** `{macro['regime']}` | **VIX:** `{macro['vix']:.1f}` | **10Y:** `{macro['10y_yield']:.2f}%`\n"
        f"🎯 **เกณฑ์คะแนนซื้อ (Hurdle):** `{macro['hurdle']} คะแนน` | สภาวะ: `{'ความเสี่ยงต่ำ เข้าซื้อได้มั่นใจ' if 'Bull' in macro['regime'] else ('แกว่งตัวในกรอบ เน้นหุ้นคุณภาพ' if 'Neutral' in macro['regime'] else 'เน้นระมัดระวัง ถือเงินสด')}`"
    )
    embed.add_field(name="🌐 สภาวะตลาดรวม (Macro)", value=macro_info, inline=False)
    
    # Buys Section (Focus on Top Pick)
    if buys:
        top_b = buys[0]
        ent = top_b['entry_info']
        top_line = (
            f"⭐ **{top_b['ticker']}** (อันดับ 1): คะแนน **{top_b['score']}/100** | ราคา `${top_b['price']:.2f}`\n"
            f"   🎯 **ราคาควรเข้าซื้อ:** `{ent['buy_zone_str']}`\n"
            f"   ⚡ สถานะ: {ent['entry_status']} | 🛑 Stop: `${ent['stop_loss']:.2f}`"
        )
        embed.add_field(name="🟢 หุ้นแนะนำซื้อที่ดีที่สุด (Top Pick)", value=top_line, inline=False)
        if len(buys) > 1:
            other_lines = [f"• **{b['ticker']}**: คะแนน {b['score']}/100 | ราคา `${b['price']:.2f}`" for b in buys[1:]]
            embed.add_field(name="📋 หุ้นอื่นที่ผ่านเกณฑ์ BUY เช่นกัน", value="\n".join(other_lines), inline=False)
    else:
        embed.add_field(
            name="🟢 หุ้นที่ผ่านเกณฑ์ 'BUY' วันนี้",
            value="🟡 *ใน Watchlist ของคุณยังไม่มีหุ้นที่คะแนนถึงเกณฑ์ BUY*",
            inline=False
        )
        
    # Dynamic Sector Diversity Insight (All-Around US Market)
    if len(results) >= 3:
        from collections import Counter
        sec_counts = Counter([r['sector_display'] for r in results])
        top_sec, top_cnt = sec_counts.most_common(1)[0]
        if (top_cnt / len(results)) > 0.50 and not top_sec.startswith('กองทุนดัชนี'):
            top_pct = int((top_cnt / len(results)) * 100)
            embed.add_field(
                name="💡 ข้อสังเกตการกระจายความเสี่ยง (Sector Diversification)",
                value=(
                    f"• ลิสต์ของคุณเน้นหนักในกลุ่ม **{top_sec}** อยู่ที่ `{top_cnt}/{len(results)}` ตัว ({top_pct}%)\n"
                    f"• แนะนำพิจารณากระจายสู่หมวดอื่น (เช่น Healthcare, Financials, Consumer หรือ Core ETF) เพื่อบาลานซ์พอร์ตในทุกสภาวะตลาด"
                ),
                inline=False
            )
            
    # Market Drawdown / Risk-Off Playbook Guidance
    deep_pullbacks = [r for r in results if r.get('tech', {}).get('ema_dist', 0) < -2.0]
    is_choppy = ("Risk-Off" in macro.get('regime', '')) or (macro.get('vix', 0) >= 19.5) or (len(deep_pullbacks) >= 2)
    if is_choppy:
        embed.add_field(
            name="🛡️ กลยุทธ์รับมือตลาดพักฐาน (Market Drawdown Playbook)",
            value=(
                "• 🧘 **ถือหุ้นต่อ (Hold):** หุ้น Core ETF (VOO, QQQM) และบิ๊กเทคพื้นฐานแกร่ง **ห้าม Panic Sell ที่ก้นหลุม** หากเป็นเงินเย็นให้ถือต่อ (HOLD) รอแรงขายสะเด็ดน้ำ\n"
                "• 🎯 **การลงทุนต่อ (Dip Buy):** **อย่าเพิ่งรีบรับมีด (No Falling Knives)** ในวันแรกที่ดิ่งแรง ให้รอราคาชะลอตัวทดสอบแนวรับ (EMA50/EMA200) และ **แบ่งไม้ซื้อ (DCA 2-3 ไม้)** เสมอ\n"
                "• 🛑 **การพัก / ถือเงินสด (Cash):** สภาวะ Risk-Off และ Yield สูง **การถือเงินสดรอความชัดเจนคือแต้มต่อ** พักการเทรดระยะสั้น รอจนกว่าระบบจะเริ่มกลับมาส่งสัญญาณ BUY ชัดเจน"
            ),
            inline=False
        )
        
    # Ranking Table with Buy Zones (Chunked to respect Discord 1024-char limit)
    table_rows = []
    for r in results:
        ent = r['entry_info']
        table_rows.append(
            f"{r['ticker']:<7} | {r['final_action']:<5} | ${r['price']:<7.2f}| {ent['buy_zone_str']:<17} | {r['score']:>3}/100"
        )

    header = f"{'Ticker':<7} | {'Action':<5} | {'Price':<8} | {'Buy Zone':<17} | {'Score'}\n" + ("-" * 49)
    row_chunks = []
    curr_rows = []
    curr_len = len(header) + 10
    for row in table_rows:
        if curr_len + len(row) + 1 > 950 and curr_rows:
            row_chunks.append(curr_rows)
            curr_rows = [row]
            curr_len = len(header) + 10 + len(row) + 1
        else:
            curr_rows.append(row)
            curr_len += len(row) + 1
    if curr_rows:
        row_chunks.append(curr_rows)

    for idx, c_rows in enumerate(row_chunks):
        t_title = f"📋 ตารางจัดอันดับ & โซนราคาเข้าซื้อ ({len(results)} ตัว)" if idx == 0 else f"📋 ตารางจัดอันดับ (หน้า {idx + 1})"
        t_body = "```text\n" + header + "\n" + "\n".join(c_rows) + "\n```"
        embed.add_field(name=t_title, value=t_body, inline=False)
    
    embed.set_footer(text="ประเมินตามเกณฑ์ปัจจัยพื้นฐานและเทคนิค • แนะนำราคาเข้าซื้อตามแนวรับย่อตัว (EMA20/50/200)")
    return embed

def build_check_embed(ticker_sym):
    ticker_sym = clean_ticker(ticker_sym)
    data_fut = _executor.submit(fetch_ticker_data, ticker_sym)
    macro_fut = _executor.submit(get_cached_macro)
    data_dict = data_fut.result()
    if not data_dict:
        embed = discord.Embed(
            title="❌ ไม่พบข้อมูลหุ้น",
            description=f"ไม่สามารถดึงข้อมูลสำหรับสัญลักษณ์ `{ticker_sym}` ได้ กรุณาตรวจสอบชื่อย่อหุ้น",
            color=0xE74C3C
        )
        return embed, None
        
    macro = macro_fut.result()
    ev = evaluate_stock(data_dict, macro)
    
    fund_val = ev['fund_val']
    tech = ev['tech']
    total_score = ev['score']
    hurdle = macro['hurdle']
    action = ev['final_action']
    
    if action == "BUY":
        color = 0x2ECC71
        action_emoji = "🟢"
    elif action == "HOLD":
        color = 0xF1C40F
        action_emoji = "🟡"
    else:
        color = 0x95A5A6
        action_emoji = "⚪"
        
    progress_bar = make_progress_bar(total_score, 100, 12)
    
    ent = ev['entry_info']
    embed = discord.Embed(
        title=f"📊 ผลการประเมินหุ้น: {ticker_sym}",
        description=(
            f"คำแนะนำ: {action_emoji} **{action}**\n"
            f"`[{progress_bar}]` **{total_score}/100** (เกณฑ์ซื้อ: {hurdle} คะแนน)\n"
            f"🎯 **ราคาควรเข้าซื้อ:** `{ent['buy_zone_str']}`"
        ),
        color=color
    )
    
    # Highlight Target Buy Zone & Stop Loss
    embed.add_field(
        name="🎯 โซนราคาที่ควรเข้าซื้อ & จุดตัดขาดทุน (Target Buy Zone)",
        value=(
            f"• **กรอบราคาเข้าซื้อ:** `{ent['buy_zone_str']}` ({ent.get('zone_type', 'ใกล้เส้น EMA20')})\n"
            f"• **สถานะปัจจุบัน:** {ent['entry_status']}\n"
            f"• **คำแนะนำจังหวะ:** {ent['entry_advice']}\n"
            f"• **จุดตัดขาดทุนแนะนำ (Stop Loss):** `${ent['stop_loss']:.2f}`\n"
            f"• **กลยุทธ์ตามสภาวะ:** {ent.get('playbook_note', '-')}"
        ),
        inline=False
    )
    
    # 1. Fundamental
    f_bar = make_progress_bar(fund_val['fund_score'], 30, 6)
    embed.add_field(
        name=f"🏢 ปัจจัยพื้นฐาน ({fund_val['fund_score']}/30)",
        value=(
            f"`[{f_bar}]`\n"
            f"• Revenue Growth: `{fund_val['rev_growth']}`\n"
            f"• OCF / ROIC: ผ่านเกณฑ์คุณภาพ"
        ),
        inline=True
    )
    
    # 2. Valuation
    v_bar = make_progress_bar(fund_val['val_score'], 20, 6)
    embed.add_field(
        name=f"🏷️ การประเมินมูลค่า ({fund_val['val_score']}/20)",
        value=(
            f"`[{v_bar}]`\n"
            f"• Forward P/E: `{fund_val['fwd_pe']}`\n"
            f"• Dynamic PEG: `{fund_val['peg']}`"
        ),
        inline=True
    )
    
    # 3. Technical
    t_bar = make_progress_bar(tech.get('score', 0), 20, 6)
    trend_txt = "EMA20 > EMA50" if tech.get('price', 0) > tech.get('ema20', 0) > tech.get('ema50', 0) else "Consolidation"
    amd_note = "\n• 🔥 AMD Sweep: `+3 โบนัสสับขาหลอก`" if tech.get('amd_sweep') else ""
    bsl_note = "\n• ⚠️ BSL Sweep: `-3 ระวังติดดอย ห้ามไล่ราคา`" if tech.get('bsl_sweep') else ""
    embed.add_field(
        name=f"📈 สัญญาณเทคนิค ({tech.get('score', 0)}/20)",
        value=(
            f"`[{t_bar}]`\n"
            f"• ราคา: `${tech.get('price', 0):.2f}`\n"
            f"• RSI (14): `{tech.get('rsi', 0):.1f}`\n"
            f"• EMA Dist: `{tech.get('ema_dist', 0):+.1f}%`\n"
            f"• Trend: `{trend_txt}`{amd_note}{bsl_note}"
        ),
        inline=True
    )
    
    # 4. Macro & Risk
    risk_score = macro['macro_score'] + fund_val['risk_score']
    m_bar = make_progress_bar(risk_score, 25, 6)
    embed.add_field(
        name=f"🛡️ ตลาด & ความเสี่ยง ({risk_score}/25)",
        value=(
            f"`[{m_bar}]`\n"
            f"• Macro: `{macro['regime']}`\n"
            f"• VIX: `{macro['vix']:.1f}` | 10Y: `{macro['10y_yield']:.2f}%`"
        ),
        inline=True
    )
    
    # 5. Portfolio Fit & Sector Allocation
    fit_score = ev['portfolio_fit']
    sector_display = ev.get('sector_display', 'US Equities')
    embed.add_field(
        name=f"⚖️ บทบาทในพอร์ต & หมวดหมู่ ({fit_score}/5)",
        value=(
            f"• กลุ่มอุตสาหกรรม: `{sector_display}`\n"
            f"• ข้อแนะนำ: สัดส่วนรายกลุ่มอุตสาหกรรมไม่ควรเกิน 20-30% ของพอร์ต"
        ),
        inline=True
    )
        
    embed.set_footer(text=f"Stock Advisor • อัปเดตล่าสุด {datetime.datetime.now().strftime('%H:%M:%S')}")
    return embed, ev

def build_price_embed(ticker_sym):
    ticker_sym = clean_ticker(ticker_sym)
    try:
        data_dict = fetch_ticker_data(ticker_sym)
        hist = None
        if data_dict and hasattr(data_dict.get('hist'), 'tail'):
            full_hist = data_dict['hist']
            if not full_hist.empty:
                hist = full_hist.tail(22)
        if hist is None or hist.empty:
            s = get_session()
            t = yf.Ticker(ticker_sym, session=s) if s else yf.Ticker(ticker_sym)
            hist = t.history(period="1mo")
        if hist.empty:
            return discord.Embed(
                title="❌ ไม่พบข้อมูลราคา",
                description=f"ไม่พบข้อมูลการซื้อขายสำหรับ `{ticker_sym}`",
                color=0xE74C3C
            )
            
        close = hist['Close']
        curr = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) > 1 else curr
        chg = curr - prev
        pct = (chg / prev) * 100 if prev else 0
        
        day_high = float(hist['High'].iloc[-1])
        day_low = float(hist['Low'].iloc[-1])
        open_p = float(hist['Open'].iloc[-1])
        
        m_high = float(hist['High'].max())
        m_low = float(hist['Low'].min())
        
        vol = float(hist['Volume'].iloc[-1])
        avg_vol = float(hist['Volume'].mean())
        rvol = vol / (avg_vol + 1e-9)
        
        color = 0x2ECC71 if chg >= 0 else 0xE74C3C
        sign = "+" if chg >= 0 else ""
        
        embed = discord.Embed(
            title=f"📈 ข้อมูลราคาตลาด: {ticker_sym}",
            description=f"**ราคาล่าสุด:** `${curr:.2f}` ({sign}{chg:.2f}, `{sign}{pct:.2f}%`)",
            color=color
        )
        
        embed.add_field(
            name="📅 กรอบราคาประจำวัน (Day Range)",
            value=f"• ต่ำสุด: `${day_low:.2f}`\n• สูงสุด: `${day_high:.2f}`\n• ราคาเปิด: `${open_p:.2f}`",
            inline=True
        )
        
        embed.add_field(
            name="📊 กรอบรอบ 1 เดือน (Month Range)",
            value=f"• ต่ำสุด: `${m_low:.2f}`\n• สูงสุด: `${m_high:.2f}`",
            inline=True
        )
        
        embed.add_field(
            name="🔊 ปริมาณซื้อขาย (Volume)",
            value=f"• Volume วันนี้: `{vol:,.0f}`\n• Relative Volume: `{rvol:.2f}x`",
            inline=True
        )
        
        # Target Buy Zone
        if data_dict and data_dict.get('tech'):
            ent = compute_entry_zone(data_dict['tech'])
            embed.add_field(
                name="🎯 โซนราคาที่ควรเข้าซื้อ (Target Buy Zone)",
                value=f"• **กรอบราคาเข้าซื้อ:** `{ent['buy_zone_str']}` ({ent['zone_type']})\n• **สถานะราคา:** {ent['entry_status']}",
                inline=False
            )
        elif len(close) >= 20:
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            buy_low = ema20 * 0.985
            buy_high = ema20 * 1.02
            dist = ((curr - ema20) / ema20) * 100
            status_txt = "🟢 อยู่ในโซนซื้อเหมาะสม" if -2.0 <= dist <= 2.5 else (f"🟡 วิ่งเกินจุดซื้อ (+{dist:.1f}%)" if dist > 2.5 else f"🟠 ย่อตัว ({dist:.1f}%)")
            embed.add_field(
                name="🎯 โซนราคาที่ควรเข้าซื้อ (Target Buy Zone)",
                value=f"• **กรอบราคาเข้าซื้อ:** `${buy_low:.2f} - ${buy_high:.2f}` (ใกล้ EMA20)\n• **สถานะราคา:** {status_txt}",
                inline=False
            )
        
        embed.set_footer(text=f"Yahoo Finance Realtime • อัปเดต {datetime.datetime.now().strftime('%H:%M:%S')}")
        return embed
    except Exception as e:
        return discord.Embed(title="❌ เกิดข้อผิดพลาด", description=str(e), color=0xE74C3C)

_PERIOD_ROW_MAP = {'5d': 5, '1mo': 22, '3mo': 63, '6mo': 126, '1y': 252}

def build_history_embed(ticker_sym, period="1mo"):
    ticker_sym = clean_ticker(ticker_sym)
    period = period.lower().strip()
    if period not in _PERIOD_ROW_MAP:
        period = '1mo'
        
    try:
        data_dict = fetch_ticker_data(ticker_sym)
        hist = None
        if data_dict and hasattr(data_dict.get('hist'), 'tail'):
            full_hist = data_dict['hist']
            if not full_hist.empty:
                hist = full_hist.tail(_PERIOD_ROW_MAP[period])
        if hist is None or hist.empty:
            s = get_session()
            t = yf.Ticker(ticker_sym, session=s) if s else yf.Ticker(ticker_sym)
            hist = t.history(period=period)
        if hist.empty:
            return discord.Embed(
                title="❌ ไม่พบข้อมูลย้อนหลัง",
                description=f"ไม่สามารถดึงประวัติราคาของ `{ticker_sym}` ได้",
                color=0xE74C3C
            )
            
        recent_hist = hist.tail(8)
        first_close = float(hist['Close'].iloc[0])
        last_close = float(hist['Close'].iloc[-1])
        period_return = ((last_close - first_close) / first_close) * 100
        sign = "+" if period_return >= 0 else ""
        color = 0x2ECC71 if period_return >= 0 else 0xE74C3C
        
        embed = discord.Embed(
            title=f"📅 ตารางราคาย้อนหลัง: {ticker_sym} ({period.upper()})",
            description=f"ผลตอบแทนรอบช่วงเวลานี้: **{sign}{period_return:.2f}%** (จาก ${first_close:.2f} สู่ ${last_close:.2f})",
            color=color
        )
        
        lines = ["```text"]
        lines.append(f"{'วันที่':<10} | {'เปิด':<7} | {'สูงสุด':<7} | {'ต่ำสุด':<7} | {'ปิด':<7} | {'เปลี่ยน'}")
        lines.append("-" * 52)
        
        for i in range(len(recent_hist)):
            row = recent_hist.iloc[i]
            d_str = recent_hist.index[i].strftime('%Y-%m-%d')
            op = float(row['Open'])
            hi = float(row['High'])
            lo = float(row['Low'])
            cl = float(row['Close'])
            
            if i > 0:
                prev_cl = float(recent_hist.iloc[i-1]['Close'])
                diff_pct = ((cl - prev_cl) / prev_cl) * 100
                diff_str = f"{diff_pct:+.2f}%"
            else:
                diff_str = "N/A"
                
            lines.append(f"{d_str:<10} | ${op:<6.2f}| ${hi:<6.2f}| ${lo:<6.2f}| ${cl:<6.2f}| {diff_str}")
        lines.append("```")
        
        embed.add_field(name="ข้อมูลการซื้อขาย 8 วันล่าสุด", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"คลิกปุ่มด้านล่างเพื่อเปลี่ยนช่วงเวลาได้อย่างรวดเร็ว")
        return embed
    except Exception as e:
        return discord.Embed(title="❌ เกิดข้อผิดพลาด", description=str(e), color=0xE74C3C)

def build_macro_embed():
    macro = get_cached_macro()
    regime = macro['regime']
    vix = macro['vix']
    tnx = macro['10y_yield']
    spx_bull = macro['spx_above_ema50']
    hurdle = macro['hurdle']
    
    if "Bull" in regime:
        color = 0x2ECC71
        regime_badge = "🟢 Risk-On (Bull Market)"
        advice = "ตลาดอยู่ในภาวะขาขึ้นชัดเจน อารมณ์ตลาดปลอดภัย สามารถเข้าซื้อหุ้นชั้นยอดที่ Pullback ใกล้ EMA20 ได้อย่างมั่นใจ"
    elif "Neutral" in regime:
        color = 0xF1C40F
        regime_badge = "🟡 Neutral (Consolidation)"
        advice = "ตลาดพักฐาน แกว่งตัวในกรอบ ให้เน้นหุ้นคุณภาพสูงที่งบแข็งแกร่ง และรอจังหวะย่อตัวลงมาใกล้แนวรับ"
    else:
        color = 0xE74C3C
        regime_badge = "🔴 Risk-Off (Correction / High Volatility)"
        advice = "ตลาดมีความเสี่ยงสูง VIX สูงหรือหลุด EMA50 แนะนำ WAIT ชะลอการซื้อ ถือเงินสดรอจนกว่าความผันผวนจะลดลง"
        
    embed = discord.Embed(
        title="🌐 สภาวะตลาดมหภาค (Macro Regime Monitor)",
        description=f"สถานะปัจจุบัน: **{regime_badge}**",
        color=color
    )
    
    embed.add_field(
        name="⚡ VIX Volatility Index",
        value=f"• ระดับปัจจุบัน: `{vix:.1f}`\n• สถานะ: {'🟢 สงบ (<20)' if vix < 20 else ('🟡 ปานกลาง (20-25)' if vix <= 25 else '🔴 วิตกกังวล (>25)')}",
        inline=True
    )
    
    embed.add_field(
        name="💵 US 10-Year Yield",
        value=f"• ผลตอบแทน: `{tnx:.2f}%`\n• ผลกระทบ: {'บวกต่อ Valuation หุ้น Tech' if tnx < 4.2 else 'จับตาแรงกดดันจากดอกเบี้ย'}",
        inline=True
    )
    
    embed.add_field(
        name="📈 S&P 500 vs EMA50",
        value=f"• สถานะ: {'✅ อยู่เหนือ EMA50 (Bullish)' if spx_bull else '❌ หลุดต่ำกว่า EMA50 (Bearish)'}",
        inline=True
    )
    
    embed.add_field(
        name="🎯 เกณฑ์คะแนนซื้อขั้นต่ำ (Hurdle Score)",
        value=f"**{hurdle} คะแนน** (หุ้นต้องได้คะแนนรวม 5 มิติถึงเกณฑ์นี้จึงจะออกคำแนะนำ BUY)",
        inline=False
    )
    
    embed.add_field(
        name="💡 คำแนะนำเชิงกลยุทธ์",
        value=advice,
        inline=False
    )
    
    embed.set_footer(text=f"ดึงข้อมูลสดจากดัชนี ^GSPC, ^NDX, ^VIX, ^TNX")
    return embed

def build_watchlist_embed(user_name, symbols):
    if not symbols:
        embed = discord.Embed(
            title=f"📋 Watchlist ของคุณ: {user_name}",
            description=(
                "📂 **ขณะนี้ยังไม่มีหุ้นใน Watchlist ของคุณ (เริ่มต้นว่างเปล่า)**\n\n"
                "ระบบแยกพื้นที่จัดเก็บสำหรับสมาชิกแต่ละคน คุณสามารถเพิ่มหุ้นที่ต้องการติดตามและวิเคราะห์ได้เองอย่างอิสระครับ"
            ),
            color=0x3498DB
        )
        embed.add_field(
            name="💡 วิธีเพิ่มหุ้นเข้า Watchlist",
            value=(
                "• พิมพ์ `/add <ชื่อหุ้น>` เช่น `/add NVDA, AAPL, VOO`\n"
                "• หรือพิมพ์ `!add TSLA, AMD`\n"
                "• หรือกดปุ่ม `[⭐ เพิ่มลง Watchlist]` จากหน้าจอ `/check` หรือ `/price`"
            ),
            inline=False
        )
        embed.set_footer(text="ระบบแยก Watchlist ตาม Discord User ID 100%")
        return embed

    etf_count = sum(1 for s in symbols if s in ETF_SYMBOLS)
    stock_count = len(symbols) - etf_count
    
    embed = discord.Embed(
        title=f"📋 Watchlist ของคุณ: {user_name}",
        description=f"มีหุ้นทั้งหมด **{len(symbols)}** ตัวในลิสต์การติดตามของคุณ",
        color=0x3498DB
    )
    
    formatted_tags = [f"`{s}`" for s in symbols]
    embed.add_field(
        name="📌 รายชื่อหุ้นที่คุณติดตาม",
        value="  ".join(formatted_tags),
        inline=False
    )
    
    embed.add_field(
        name="📊 สรุปโครงสร้างลิสต์",
        value=f"• หุ้นรายตัว: `{stock_count}` ตัว | กองทุน ETF: `{etf_count}` ตัว",
        inline=False
    )
    
    embed.set_footer(text="คลิกปุ่ม '🚀 สแกนทันที' ด้านล่างเพื่อประเมินหุ้นทั้งหมดในลิสต์ของคุณ")
    return embed

def build_compare_embed(sym1, sym2):
    sym1 = clean_ticker(sym1)
    sym2 = clean_ticker(sym2)
    
    f_macro = _executor.submit(get_cached_macro)
    f_d1 = _executor.submit(fetch_ticker_data, sym1)
    f_d2 = _executor.submit(fetch_ticker_data, sym2)
    macro = f_macro.result()
    d1 = f_d1.result()
    d2 = f_d2.result()
    
    if not d1 or not d2:
        return discord.Embed(
            title="❌ ข้อมูลไม่ครบถ้วน",
            description=f"ไม่สามารถเปรียบเทียบได้เนื่องจากไม่พบข้อมูลของ `{sym1 if not d1 else sym2}`",
            color=0xE74C3C
        )
        
    ev1 = evaluate_stock(d1, macro)
    ev2 = evaluate_stock(d2, macro)
    
    if ev1['score'] > ev2['score']:
        desc = f"ผลคะแนนรวม: **{sym1}** ได้คะแนนสูงกว่า ({ev1['score']} เทียบกับ {ev2['score']})"
    elif ev2['score'] > ev1['score']:
        desc = f"ผลคะแนนรวม: **{sym2}** ได้คะแนนสูงกว่า ({ev2['score']} เทียบกับ {ev1['score']})"
    else:
        desc = f"ผลคะแนนรวม: ทั้งสองตัวมีคะแนนเท่ากัน ({ev1['score']} คะแนน)"
    
    embed = discord.Embed(
        title=f"📊 เปรียบเทียบหุ้น: {sym1} กับ {sym2}",
        description=desc,
        color=0x9B59B6
    )
    
    table = ["```text"]
    table.append(f"{'หัวข้อ (Metrics)':<18} | {sym1:<10} | {sym2:<10}")
    table.append("-" * 44)
    table.append(f"{'คะแนนรวม (Score)':<18} | {ev1['score']:>4}/100    | {ev2['score']:>4}/100")
    table.append(f"{'คำแนะนำ (Verdict)':<18} | {ev1['final_action']:<10} | {ev2['final_action']:<10}")
    table.append(f"{'ราคา (Price)':<18} | ${ev1['price']:<9.2f}| ${ev2['price']:<9.2f}")
    table.append(f"{'RSI (14)':<18} | {ev1['rsi']:<10.1f}| {ev2['rsi']:<10.1f}")
    table.append(f"{'EMA Distance':<18} | {ev1['ema_dist']:<+9.1f}%| {ev2['ema_dist']:<+9.1f}%")
    table.append(f"{'Forward P/E':<18} | {str(ev1['fwd_pe']):<10} | {str(ev2['fwd_pe']):<10}")
    table.append(f"{'Dynamic PEG':<18} | {str(ev1['peg']):<10} | {str(ev2['peg']):<10}")
    table.append(f"{'Revenue Growth':<18} | {str(ev1['fund_val']['rev_growth']):<10} | {str(ev2['fund_val']['rev_growth']):<10}")
    table.append("```")
    
    embed.add_field(name="ตารางประเมินเปรียบเทียบ", value="\n".join(table), inline=False)
    embed.set_footer(text="เกณฑ์ซื้อปัจจุบัน: " + str(macro['hurdle']) + " คะแนน")
    return embed

# ==================== INTERACTIVE DISCORD UI VIEWS (PERSISTENT) ====================

def _resolve_ticker_from_interaction(self_ticker: str, interaction: discord.Interaction) -> str:
    """ดึงชื่อหุ้นจาก View หรือจากหัวข้อ Embed (กรณีปุ่มถูกกดหลังจากบอทเพิ่งรีสตาร์ต/อัปเดต)"""
    if self_ticker:
        return clean_ticker(self_ticker)
    try:
        if interaction.message and interaction.message.embeds:
            title = interaction.message.embeds[0].title or ""
            if ":" in title:
                raw = title.split(":")[-1].split("(")[0].strip()
                return clean_ticker(raw)
    except Exception:
        pass
    return ""

class WatchlistView(discord.ui.View):
    def __init__(self, user_id: str = "", user_name: str = ""):
        super().__init__(timeout=None)
        self.user_id = str(user_id) if user_id else ""
        self.user_name = user_name

    @discord.ui.button(label="สแกนทันที", style=discord.ButtonStyle.success, emoji="🚀", custom_id="wl:scan")
    async def scan_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        clicker_id = str(interaction.user.id)
        symbols = get_user_symbols(clicker_id)
        if not symbols:
            await interaction.followup.send(
                "⚠️ **Watchlist ของคุณยังว่างเปล่า**\nกรุณาเพิ่มหุ้นด้วยคำสั่ง `/add <ชื่อหุ้น>` เช่น `/add NVDA, AAPL` ก่อนกดสแกนนะครับ",
                ephemeral=True
            )
            return
            
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_scan_embed, symbols, interaction.user.display_name)
        view = ScanView(clicker_id, interaction.user.display_name)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="ล้างลิสต์ให้ว่าง", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="wl:reset")
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        clicker_id = str(interaction.user.id)
        target_uid = self.user_id or clicker_id
        if self.user_id and clicker_id != self.user_id:
            await interaction.response.send_message(
                f"❌ คุณไม่ใช่เจ้าของ Watchlist นี้ (เฉพาะคุณ **{self.user_name}** เท่านั้นที่สามารถกดล้างลิสต์นี้ได้ครับ)",
                ephemeral=True
            )
            return

        watchlists = load_user_watchlists()
        watchlists[target_uid] = []
        save_user_watchlists(watchlists)
        embed = build_watchlist_embed(self.user_name or interaction.user.display_name, [])
        try:
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send("🗑️ ล้าง Watchlist ส่วนตัวของคุณเป็นค่าว่างเปล่าเรียบร้อยแล้ว", ephemeral=True)
        except Exception:
            if not interaction.response.is_done():
                await interaction.response.send_message("🗑️ ล้าง Watchlist ส่วนตัวของคุณเป็นค่าว่างเปล่าเรียบร้อยแล้ว", embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("🗑️ ล้าง Watchlist ส่วนตัวของคุณเป็นค่าว่างเปล่าเรียบร้อยแล้ว", embed=embed, ephemeral=True)

class ScanView(discord.ui.View):
    def __init__(self, user_id: str = "", user_name: str = ""):
        super().__init__(timeout=None)
        self.user_id = str(user_id) if user_id else ""
        self.user_name = user_name

    @discord.ui.button(label="รีเฟรชสแกน", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="scan:refresh")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        clicker_id = str(interaction.user.id)
        symbols = get_user_symbols(clicker_id)
        if not symbols:
            await interaction.followup.send(
                "⚠️ **Watchlist ของคุณยังว่างเปล่า**\nกรุณาเพิ่มหุ้นด้วยคำสั่ง `/add <ชื่อหุ้น>` ก่อนนะครับ",
                ephemeral=True
            )
            return
            
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_scan_embed, symbols, interaction.user.display_name)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="ตรวจ Macro", style=discord.ButtonStyle.secondary, emoji="🌐", custom_id="scan:macro")
    async def macro_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_macro_embed)
        await interaction.followup.send(embed=embed, ephemeral=True)

class QuickAlertModal(discord.ui.Modal, title="🔔 ตั้งแจ้งเตือนราคาหุ้น"):
    def __init__(self, ticker: str, default_target: float = 0.0):
        super().__init__()
        self.ticker = clean_ticker(ticker)
        self.target_input = discord.ui.TextInput(
            label=f"ราคาเป้าหมายสำหรับ {self.ticker} ($USD)",
            placeholder=f"เช่น {default_target:.2f}" if default_target > 0 else "เช่น 250.00",
            default=f"{default_target:.2f}" if default_target > 0 else "",
            required=True
        )
        self.add_item(self.target_input)

    async def on_submit(self, interaction: discord.Interaction):
        val_str = self.target_input.value.strip().replace("$", "")
        try:
            target_val = float(val_str)
            await interaction.response.defer(ephemeral=True)
            loop = asyncio.get_running_loop()
            ok, msg = await loop.run_in_executor(
                _executor, add_user_alert, str(interaction.user.id), interaction.channel_id, self.ticker, target_val
            )
            await interaction.followup.send(msg, ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ กรุณาระบุราคาเป้าหมายเป็นตัวเลข เช่น `250.50`", ephemeral=True)

class PortfolioAddModal(discord.ui.Modal, title="➕ บันทึกหุ้นในพอร์ต"):
    def __init__(self, user_id: str, default_ticker: str = ""):
        super().__init__()
        self.user_id = str(user_id)
        self.ticker_input = discord.ui.TextInput(
            label="ชื่อย่อหุ้น (Ticker)",
            placeholder="เช่น NVDA, AAPL, VOO",
            default=default_ticker,
            min_length=1,
            max_length=10,
            required=True
        )
        self.shares_input = discord.ui.TextInput(
            label="จำนวนหุ้น (Shares)",
            placeholder="เช่น 1.5 หรือ 0.55989",
            min_length=1,
            max_length=20,
            required=True
        )
        self.cost_input = discord.ui.TextInput(
            label="ต้นทุนเฉลี่ย USD (Avg Cost)",
            placeholder="เช่น 210.50",
            min_length=1,
            max_length=20,
            required=True
        )
        self.cat_input = discord.ui.TextInput(
            label="หมวดหมู่ (Category - ไม่บังคับ)",
            placeholder="เช่น Semiconductor, Core ETF, Big Tech",
            required=False,
            max_length=30
        )
        self.add_item(self.ticker_input)
        self.add_item(self.shares_input)
        self.add_item(self.cost_input)
        self.add_item(self.cat_input)

    async def on_submit(self, interaction: discord.Interaction):
        sym = clean_ticker(self.ticker_input.value.strip())
        try:
            shares = float(self.shares_input.value.strip().replace(",", ""))
            avg_cost = float(self.cost_input.value.strip().replace("$", "").replace(",", ""))
            if shares <= 0 or avg_cost < 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ กรุณาระบุจำนวนหุ้นและราคาต้นทุนเป็นตัวเลขที่ถูกต้อง (มากกว่า 0)", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        cat = self.cat_input.value.strip() or "General"
        add_user_portfolio_holding(self.user_id, sym, shares, avg_cost, cat)
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_portfolio_embed, self.user_id, interaction.user.display_name, True)
        view = PortfolioView(self.user_id, interaction.user.display_name)
        await interaction.followup.send(f"✅ บันทึกหุ้น `{sym}` (จำนวน {shares:g} หุ้น @ ${avg_cost:.2f}) เข้าพอร์ตเรียบร้อย!", embed=embed, view=view, ephemeral=True)

class PortfolioRemoveModal(discord.ui.Modal, title="🗑️ ลบหุ้นออกจากพอร์ต"):
    def __init__(self, user_id: str):
        super().__init__()
        self.user_id = str(user_id)
        self.ticker_input = discord.ui.TextInput(
            label="ชื่อย่อหุ้นที่ต้องการลบ (Ticker)",
            placeholder="เช่น NVDA หรือ VOO",
            min_length=1,
            max_length=10,
            required=True
        )
        self.add_item(self.ticker_input)

    async def on_submit(self, interaction: discord.Interaction):
        sym = clean_ticker(self.ticker_input.value.strip())
        ok = remove_user_portfolio_holding(self.user_id, sym)
        if not ok:
            await interaction.response.send_message(f"⚠️ ไม่พบหุ้น `{sym}` ในพอร์ตของคุณ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_portfolio_embed, self.user_id, interaction.user.display_name, True)
        view = PortfolioView(self.user_id, interaction.user.display_name)
        await interaction.followup.send(f"🗑️ ลบหุ้น `{sym}` ออกจากพอร์ตแล้ว", embed=embed, view=view, ephemeral=True)

class PortfolioView(discord.ui.View):
    def __init__(self, user_id: str = "", user_name: str = ""):
        super().__init__(timeout=None)
        self.user_id = str(user_id) if user_id else ""
        self.user_name = user_name

    @discord.ui.button(label="รีเฟรชราคา", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="port:refresh")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        clicker_id = str(interaction.user.id)
        if self.user_id and clicker_id != self.user_id:
            await interaction.response.send_message("⛔ ปุ่มนี้เป็นของพอร์ตผู้ใช้อื่น กรุณาใช้คำสั่ง `/portfolio` เพื่อดูพอร์ตของคุณ", ephemeral=True)
            return
        target_uid = self.user_id or clicker_id
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_portfolio_embed, target_uid, self.user_name or interaction.user.display_name, True)
        await interaction.edit_original_response(embed=embed, view=PortfolioView(target_uid, interaction.user.display_name))

    @discord.ui.button(label="เพิ่ม/แก้หุ้น", style=discord.ButtonStyle.secondary, emoji="➕", custom_id="port:add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        clicker_id = str(interaction.user.id)
        if self.user_id and clicker_id != self.user_id:
            await interaction.response.send_message("⛔ กรุณาใช้คำสั่ง `/portfolio` เพื่อจัดการพอร์ตของคุณเอง", ephemeral=True)
            return
        await interaction.response.send_modal(PortfolioAddModal(self.user_id or clicker_id))

    @discord.ui.button(label="ลบหุ้น", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="port:remove")
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        clicker_id = str(interaction.user.id)
        if self.user_id and clicker_id != self.user_id:
            await interaction.response.send_message("⛔ กรุณาใช้คำสั่ง `/portfolio` เพื่อจัดการพอร์ตของคุณเอง", ephemeral=True)
            return
        await interaction.response.send_modal(PortfolioRemoveModal(self.user_id or clicker_id))

    @discord.ui.button(label="สแกนหุ้นในพอร์ต", style=discord.ButtonStyle.success, emoji="📊", custom_id="port:scan")
    async def scan_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        clicker_id = str(interaction.user.id)
        if self.user_id and clicker_id != self.user_id:
            await interaction.response.send_message("⛔ กรุณาใช้คำสั่ง `/portfolio` เพื่อสแกนพอร์ตของคุณเอง", ephemeral=True)
            return
        target_uid = self.user_id or clicker_id
        await interaction.response.defer(ephemeral=True)
        try:
            p_data = get_user_portfolio_data(target_uid)
            symbols = list(p_data.get("holdings", {}).keys()) if p_data else []
            if not symbols:
                await interaction.followup.send("⚠️ ไม่พบรายชื่อหุ้นในพอร์ตโฟลิโอของคุณ กรุณากดปุ่ม `[➕ เพิ่ม/แก้หุ้น]` ก่อน", ephemeral=True)
                return
            loop = asyncio.get_running_loop()
            embed = await loop.run_in_executor(_executor, build_scan_embed, symbols, f"Portfolio ({interaction.user.display_name})")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"Error in scan_btn: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการสแกนหุ้น: {e}", ephemeral=True)

class AlertsView(discord.ui.View):
    def __init__(self, user_id: str = "", user_name: str = ""):
        super().__init__(timeout=None)
        self.user_id = str(user_id) if user_id else ""
        self.user_name = user_name

    @discord.ui.button(label="รีเฟรชรายการ", style=discord.ButtonStyle.secondary, emoji="🔄", custom_id="alerts:refresh")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_uid = self.user_id or str(interaction.user.id)
        embed = build_alerts_embed(self.user_name or interaction.user.display_name, target_uid)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="ล้างการแจ้งเตือนทั้งหมด", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="alerts:clear")
    async def clear_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_uid = self.user_id or str(interaction.user.id)
        alerts = load_user_alerts()
        alerts[target_uid] = []
        save_user_alerts(alerts)
        embed = build_alerts_embed(self.user_name or interaction.user.display_name, target_uid)
        await interaction.response.edit_message(embed=embed, view=self)

class CheckView(discord.ui.View):
    def __init__(self, ticker: str = "", user_id: str = ""):
        super().__init__(timeout=None)
        self.ticker = clean_ticker(ticker) if ticker else ""
        self.user_id = str(user_id) if user_id else ""
        
        if self.ticker and self.user_id:
            symbols = get_user_symbols(self.user_id)
            in_wl = self.ticker in symbols
            self.wl_btn.label = "ลบจาก Watchlist" if in_wl else "เพิ่มลง Watchlist"
            self.wl_btn.style = discord.ButtonStyle.danger if in_wl else discord.ButtonStyle.success

    @discord.ui.button(label="ดูราคาละเอียด", style=discord.ButtonStyle.primary, emoji="📈", custom_id="check:price")
    async def price_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_price_embed, sym)
        view = PriceView(sym, str(interaction.user.id))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="ประวัติราคา", style=discord.ButtonStyle.secondary, emoji="📅", custom_id="check:hist")
    async def hist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_history_embed, sym, "1mo")
        view = HistoryView(sym, "1mo")
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="ตั้งเตือนราคา", style=discord.ButtonStyle.secondary, emoji="🔔", custom_id="check:alert")
    async def alert_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        curr_p = get_cached_price(sym)
        modal = QuickAlertModal(sym, curr_p)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Watchlist", style=discord.ButtonStyle.secondary, emoji="⭐", custom_id="check:wl")
    async def wl_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        await interaction.response.defer(ephemeral=True)
        clicker_id = str(interaction.user.id)
        watchlists = load_user_watchlists()
        current = watchlists.get(clicker_id, [])
        if sym in current:
            current.remove(sym)
            msg = f"🗑️ ลบ `{sym}` ออกจาก Watchlist ส่วนตัวของคุณเรียบร้อยแล้ว (คงเหลือ {len(current)} ตัว)"
            button.label = "เพิ่มลง Watchlist"
            button.style = discord.ButtonStyle.success
        else:
            current.append(sym)
            msg = f"⭐ เพิ่ม `{sym}` เข้า Watchlist ส่วนตัวของคุณเรียบร้อยแล้ว (ปัจจุบันมี {len(current)} ตัว)"
            button.label = "ลบจาก Watchlist"
            button.style = discord.ButtonStyle.danger
            
        watchlists[clicker_id] = current
        save_user_watchlists(watchlists)
        await interaction.followup.send(msg, ephemeral=True)

class PriceView(discord.ui.View):
    def __init__(self, ticker: str = "", user_id: str = ""):
        super().__init__(timeout=None)
        self.ticker = clean_ticker(ticker) if ticker else ""
        self.user_id = str(user_id) if user_id else ""
        
        if self.ticker and self.user_id:
            symbols = get_user_symbols(self.user_id)
            in_wl = self.ticker in symbols
            self.wl_btn.label = "ลบจาก Watchlist" if in_wl else "เพิ่มลง Watchlist"
            self.wl_btn.style = discord.ButtonStyle.danger if in_wl else discord.ButtonStyle.success

    @discord.ui.button(label="วิเคราะห์ 5 มิติ", style=discord.ButtonStyle.primary, emoji="🎯", custom_id="price:check")
    async def check_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        embed, _ = await loop.run_in_executor(_executor, build_check_embed, sym)
        view = CheckView(sym, str(interaction.user.id))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="ประวัติราคา", style=discord.ButtonStyle.secondary, emoji="📅", custom_id="price:hist")
    async def hist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_history_embed, sym, "1mo")
        view = HistoryView(sym, "1mo")
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="ตั้งเตือนราคา", style=discord.ButtonStyle.secondary, emoji="🔔", custom_id="price:alert")
    async def alert_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        curr_p = get_cached_price(sym)
        modal = QuickAlertModal(sym, curr_p)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Watchlist", style=discord.ButtonStyle.secondary, emoji="⭐", custom_id="price:wl")
    async def wl_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        await interaction.response.defer(ephemeral=True)
        clicker_id = str(interaction.user.id)
        watchlists = load_user_watchlists()
        current = watchlists.get(clicker_id, [])
        if sym in current:
            current.remove(sym)
            msg = f"🗑️ ลบ `{sym}` ออกจาก Watchlist ส่วนตัวของคุณเรียบร้อยแล้ว (คงเหลือ {len(current)} ตัว)"
            button.label = "เพิ่มลง Watchlist"
            button.style = discord.ButtonStyle.success
        else:
            current.append(sym)
            msg = f"⭐ เพิ่ม `{sym}` เข้า Watchlist ส่วนตัวของคุณเรียบร้อยแล้ว (ปัจจุบันมี {len(current)} ตัว)"
            button.label = "ลบจาก Watchlist"
            button.style = discord.ButtonStyle.danger
            
        watchlists[clicker_id] = current
        save_user_watchlists(watchlists)
        await interaction.followup.send(msg, ephemeral=True)

class HistoryView(discord.ui.View):
    def __init__(self, ticker: str = "", current_period: str = "1mo"):
        super().__init__(timeout=None)
        self.ticker = clean_ticker(ticker) if ticker else ""
        self.current_period = current_period
        
        # Highlight active timeframe
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                if child.custom_id == f"p_{current_period}":
                    child.style = discord.ButtonStyle.primary
                else:
                    child.style = discord.ButtonStyle.secondary

    async def change_period(self, interaction: discord.Interaction, period: str):
        sym = _resolve_ticker_from_interaction(self.ticker, interaction)
        await interaction.response.defer()
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_history_embed, sym, period)
        new_view = HistoryView(sym, period)
        await interaction.edit_original_response(embed=embed, view=new_view)

    @discord.ui.button(label="5d", custom_id="p_5d")
    async def btn_5d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_period(interaction, "5d")

    @discord.ui.button(label="1mo", custom_id="p_1mo")
    async def btn_1mo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_period(interaction, "1mo")

    @discord.ui.button(label="3mo", custom_id="p_3mo")
    async def btn_3mo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_period(interaction, "3mo")

    @discord.ui.button(label="6mo", custom_id="p_6mo")
    async def btn_6mo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_period(interaction, "6mo")

    @discord.ui.button(label="1y", custom_id="p_1y")
    async def btn_1y(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_period(interaction, "1y")

# ==================== AUTOCOMPLETE HELPERS ====================

async def ticker_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    user_id = str(interaction.user.id)
    watchlist = get_user_symbols(user_id)
    watchlist_set = set(watchlist)
    combined = list(dict.fromkeys(watchlist + POPULAR_TICKERS))
    
    current = clean_ticker(current)
    if not current:
        matches = combined[:25]
    else:
        matches = [s for s in combined if current in s]
        if current not in matches and len(current) <= 5:
            matches.insert(0, current)
            
    return [
        app_commands.Choice(
            name=f"{sym} {'(⭐ ใน Watchlist)' if sym in watchlist_set else ''}".strip(),
            value=sym
        )
        for sym in matches[:25]
    ]

async def watchlist_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    user_id = str(interaction.user.id)
    watchlist = get_user_symbols(user_id)
    current = current.upper().strip()
    matches = [s for s in watchlist if current in s] if current else watchlist
    return [app_commands.Choice(name=sym, value=sym) for sym in matches[:25]]

async def period_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    options = [
        ("5 วัน (5d)", "5d"),
        ("1 เดือน (1mo)", "1mo"),
        ("3 เดือน (3mo)", "3mo"),
        ("6 เดือน (6mo)", "6mo"),
        ("1 ปี (1y)", "1y"),
    ]
    current = current.lower().strip()
    return [
        app_commands.Choice(name=label, value=val)
        for label, val in options if current in val or current in label.lower()
    ]

from aiohttp import web

BOT_START_TIME = time.time()
_restart_in_progress = False

def get_process_memory_mb():
    # 1. Linux / Docker / Koyeb container (reads VmRSS from /proc)
    if os.path.exists("/proc/self/status"):
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
        except Exception:
            pass

    # 2. Windows fallback
    try:
        import ctypes
        from ctypes import wintypes
        class PMC_EX(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
                ('PrivateUsage', ctypes.c_size_t)
            ]
        fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC_EX), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        pmc = PMC_EX()
        pmc.cb = ctypes.sizeof(PMC_EX)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if fn(handle, ctypes.byref(pmc), pmc.cb):
            return float(pmc.WorkingSetSize) / (1024.0 * 1024.0)
    except Exception:
        pass
    return 0.0

_health_runner = None

async def perform_graceful_restart(reason: str):
    global _restart_in_progress, _health_runner
    if _restart_in_progress:
        return
    _restart_in_progress = True
    print(f"\n🔄 [AUTO-RESTART TRIGGERED] Reason: {reason}", flush=True)

    # 1. Clean up HTTP health server to free port 8080 immediately
    if _health_runner:
        try:
            await _health_runner.cleanup()
        except Exception:
            pass

    # 2. Close Discord connection
    try:
        await bot.close()
    except Exception:
        pass

    await asyncio.sleep(2.0)

    # In Docker/Koyeb/Railway or under supervisor, exiting cleanly allows host to restart with 0 MB
    if os.environ.get("RUNNING_UNDER_SUPERVISOR") == "1" or os.name != 'nt':
        print("🔄 [RECYCLE] Clean process exit: container/supervisor will restart fresh...", flush=True)
        os._exit(0)

    script_path = os.path.abspath(__file__)
    working_dir = os.path.dirname(script_path)

    # Windows standalone fallback
    python_exe = sys.executable
    is_pythonw = _WAS_STARTED_AS_PYTHONW or ("pythonw" in os.path.basename(sys.executable).lower())
    target_bin = "pythonw.exe" if is_pythonw else "python.exe"

    candidates = [
        os.path.join(working_dir, ".venv", "Scripts", target_bin),
        os.path.join(sys.prefix, "Scripts", target_bin),
        os.path.join(working_dir, ".venv", "Scripts", "pythonw.exe"),
        os.path.join(working_dir, ".venv", "Scripts", "python.exe"),
        os.path.join(sys.prefix, "Scripts", "python.exe"),
        sys.executable,
    ]
    for c in candidates:
        if os.path.exists(c):
            python_exe = c
            break

    import subprocess
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        [python_exe, script_path],
        cwd=working_dir,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os._exit(0)

async def start_health_check_server():
    global _health_runner
    port = int(os.getenv("PORT", 8080))
    async def handle_ping(request):
        return web.Response(
            text="Bot is running OK",
            content_type="text/plain",
            headers={"Connection": "close"}
        )
        
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_head("/", handle_ping)
    
    try:
        runner = web.AppRunner(app)
        await runner.setup()
        _health_runner = runner
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"🌐 Health check HTTP server listening on port {port}")
    except Exception as e:
        print(f"Health check server note: {e}")

# ==================== BOT CLIENT & SETUP ====================

class StockAdvisorBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            max_messages=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none()
        )

    async def setup_hook(self):
        # ลงทะเบียน Persistent Views ทั้งหมด เพื่อให้ปุ่มกดในแชททำงานได้ตลอดแม้บอทเพิ่งรีสตาร์ตหรืออัปเดต
        for persistent_view in (
            WatchlistView(),
            ScanView(),
            PortfolioView(),
            AlertsView(),
            CheckView(),
            PriceView(),
            HistoryView(),
            StatusView(),
        ):
            self.add_view(persistent_view)

        # ป้องกันปัญหาคำสั่งซ้อนกัน (Duplicate Slash Commands)
        # หากใช้ copy_global_to ไปยัง Guild พร้อมกับ sync() แบบ Global
        # Discord จะแสดงผลคำสั่งซ้ำกัน 2 ชุดในเซิร์ฟเวอร์
        # เคลียร์ Guild commands ให้เป็น 0 และซิงค์เฉพาะ Global commands ชุดเดียว
        if MY_GUILD_ID:
            try:
                guild_obj = discord.Object(id=MY_GUILD_ID)
                self.tree.clear_commands(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
                print(f"🧹 ล้าง Guild Commands ในเซิร์ฟเวอร์ {MY_GUILD_ID} เรียบร้อย (ป้องกันเมนูซ้ำ)", flush=True)
            except Exception as e:
                print(f"⚠️ Note on clearing guild commands: {e}", flush=True)

        synced_global = await self.tree.sync()
        print(f"✅ ซิงค์ Global Slash Commands สำเร็จ ({len(synced_global)} คำสั่ง, ปราศจากคำสั่งซ้ำ)", flush=True)

bot = StockAdvisorBot()

# ==================== BACKGROUND TASKS ====================

@tasks.loop(minutes=3)
async def check_price_alerts_task():
    try:
        alerts = load_user_alerts()
        all_symbols = set()
        for uid, u_alerts in alerts.items():
            for a in u_alerts:
                if not a.get('triggered', False):
                    all_symbols.add(a.get('symbol'))
        if not all_symbols:
            return

        loop = asyncio.get_running_loop()
        def _fetch_all(sym_list):
            items = list(_executor.map(fetch_ticker_data, sym_list))
            return {s: it for s, it in zip(sym_list, items) if it}

        fetched_dict = await loop.run_in_executor(_executor, _fetch_all, list(all_symbols))

        updated_any = False
        for uid, u_alerts in alerts.items():
            for a in u_alerts:
                if a.get('triggered', False):
                    continue
                sym = a.get('symbol')
                data = fetched_dict.get(sym)
                if not data or not data.get('tech'):
                    continue
                curr_price = float(data['tech'].get('price', 0))
                if curr_price <= 0:
                    continue
                target = float(a.get('target_price', 0))
                cond = a.get('condition', 'gte')

                is_hit = (curr_price >= target) if cond == 'gte' else (curr_price <= target)
                if is_hit:
                    a['triggered'] = True
                    updated_any = True
                    channel_id = a.get('channel_id')
                    try:
                        channel = bot.get_channel(channel_id) if channel_id else None
                        if not channel and MY_GUILD_ID:
                            guild = bot.get_guild(MY_GUILD_ID)
                            if guild and guild.text_channels:
                                channel = guild.text_channels[0]

                        embed = discord.Embed(
                            title=f"🚨 แจ้งเตือนราคาหุ้นแตะเป้าหมาย: {sym}",
                            description=f"<@{uid}> หุ้น **{sym}** แตะราคาเป้าหมายที่คุณตั้งไว้แล้วครับ!",
                            color=0x2ECC71 if cond == 'gte' else 0xE74C3C
                        )
                        embed.add_field(name="🎯 ราคาเป้าหมาย", value=f"**${target:.2f}**", inline=True)
                        embed.add_field(name="💲 ราคาล่าสุด", value=f"**${curr_price:.2f}**", inline=True)
                        cond_text = "ขึ้นแตะหรือทะลุเป้า" if cond == 'gte' else "ย่อลงแตะหรือต่ำกว่าเป้า"
                        embed.add_field(name="📌 เงื่อนไข", value=f"{cond_text}", inline=True)
                        embed.set_footer(text="ระบบปิดการแจ้งเตือนรายการนี้แล้ว (เพื่อป้องกันการแจ้งซ้ำ) คุณสามารถตั้งใหม่ได้ตลอดเวลา")

                        sent_dm = False
                        try:
                            user = await bot.fetch_user(int(uid))
                            if user:
                                await user.send(content=f"<@{uid}> 🔔 **แจ้งเตือนราคาหุ้นแตะเป้าหมาย!**", embed=embed)
                                sent_dm = True
                        except Exception as dm_err:
                            print(f"Could not send DM to {uid}: {dm_err}")

                        if not sent_dm and channel:
                            await channel.send(content=f"<@{uid}> 🔔 **แจ้งเตือนราคาหุ้นแตะเป้าหมาย!**", embed=embed)
                    except Exception as ex:
                        print(f"Error sending alert notification for {sym} to {uid}: {ex}")

        if updated_any:
            save_user_alerts(alerts)
        trim_memory()
    except Exception as e:
        print(f"Error in check_price_alerts_task: {e}")

@check_price_alerts_task.before_loop
async def before_check_price_alerts_task():
    await bot.wait_until_ready()

_high_latency_count = 0

@tasks.loop(minutes=2)
async def auto_health_watchdog_task():
    global _high_latency_count
    try:
        trim_memory()

        now = time.time()
        with _ticker_cache_lock:
            expired = [k for k, v in _ticker_cache.items() if now - v.get('timestamp', 0) > 180]
            for k in expired:
                _ticker_cache.pop(k, None)
            expired_fund = [k for k, v in _fund_cache.items() if now - v.get('timestamp', 0) > 1800]
            for k in expired_fund:
                _fund_cache.pop(k, None)

        # 1. Check Container Memory Safety (> 205 MB on 256 MB instances prevents OOM Kill)
        mem_mb = get_process_memory_mb()
        # Default ceiling is 205MB on Linux/container, 320MB on local Windows
        mem_limit = 205.0 if os.name != 'nt' or os.path.exists("/proc/self/status") else 320.0
        if mem_mb > mem_limit:
            await perform_graceful_restart(f"หน่วยความจำใกล้เต็มขีดจำกัด ({mem_mb:.1f} MB > {mem_limit:.0f} MB)")
            return

        # 2. Check 6-Hour Scheduled Recycle
        uptime_sec = now - BOT_START_TIME
        if uptime_sec >= 6 * 3600:
            await perform_graceful_restart(f"ครบกำหนดรอบ 6 ชั่วโมง (Uptime: {uptime_sec/3600:.1f} ชม.)")
            return

        # 3. Check Latency / Heartbeat Sluggishness (> 3.0s consecutively 3 times)
        lat = bot.latency
        import math
        if lat is not None and not math.isnan(lat) and lat > 3.0:
            _high_latency_count += 1
            if _high_latency_count >= 3:
                await perform_graceful_restart(f"การตอบสนองหน่วงสะสม (Ping {lat*1000:.0f}ms ต่อเนื่อง)")
                return
        else:
            _high_latency_count = 0

    except Exception as e:
        print(f"Watchdog check note: {e}", flush=True)

@auto_health_watchdog_task.before_loop
async def before_auto_health_watchdog_task():
    await bot.wait_until_ready()

@bot.check
async def globally_block_unauthorized_guilds(ctx):
    if ctx.guild is None or ctx.guild.id != MY_GUILD_ID:
        return False
    return True

_data_restore_done = False

@bot.event
async def on_ready():
    global _data_restore_done
    print(f"🤖 บอทออนไลน์แล้วในชื่อ: {bot.user} (ID: {bot.user.id})")
    if not _data_restore_done and _SUPABASE_ENABLED:
        _data_restore_done = True
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(_executor, restore_user_data_from_supabase):
            await loop.run_in_executor(_executor, _supabase_upsert_all_users)
    if not check_price_alerts_task.is_running():
        try:
            check_price_alerts_task.start()
            print("🔔 ระบบ Background Task ตรวจสอบราคาหุ้น (Price Alert) เริ่มทำงานแล้ว")
        except Exception as e:
            print(f"Note on starting alerts task: {e}")
    if not auto_health_watchdog_task.is_running():
        try:
            auto_health_watchdog_task.start()
            print("🛡️ ระบบ Watchdog รีสตาร์ตอัตโนมัติ (ทุก 6 ชม. / เมื่อเริ่มหน่วง) เริ่มทำงานแล้ว")
        except Exception as e:
            print(f"Note on starting watchdog task: {e}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="US Stocks 📈 • /help"
        )
    )

# ==================== SLASH COMMANDS ====================

@bot.tree.command(name="watchlist", description="ดูรายการหุ้นใน Watchlist ส่วนตัวของคุณ (เริ่มต้นว่างเปล่า)")
async def slash_watchlist(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    symbols = get_user_symbols(user_id)
    embed = build_watchlist_embed(interaction.user.display_name, symbols)
    view = WatchlistView(user_id, interaction.user.display_name)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="add", description="เพิ่มหุ้นเข้า Watchlist ส่วนตัว (ใส่ได้หลายตัวคั่นด้วยลูกน้ำ เช่น NVDA, AAPL, PLTR)")
@app_commands.describe(tickers="ชื่อย่อหุ้นที่ต้องการเพิ่ม (คั่นด้วยลูกน้ำหรือเว้นวรรค)")
async def slash_add(interaction: discord.Interaction, tickers: str):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    raw_list = [clean_ticker(s) for s in re.split(r'[,;\s]+', tickers) if clean_ticker(s)]
    
    if not raw_list:
        await interaction.followup.send("⚠️ กรุณาระบุชื่อย่อหุ้น เช่น `/add AAPL` หรือ `/add NVDA, AMD, PLTR`", ephemeral=True)
        return
        
    if len(raw_list) > 15:
        await interaction.followup.send("⚠️ เพื่อความเสถียรของระบบ กรุณาเพิ่มหุ้นไม่เกินครั้งละ 15 ตัวครับ", ephemeral=True)
        return

    watchlists = load_user_watchlists()
    current = watchlists.get(user_id, [])
    
    if len(current) >= 50:
        await interaction.followup.send("⚠️ Watchlist ของคุณมีหุ้นครบ 50 ตัวแล้ว (จำกัดสูงสุด 50 ตัว) กรุณาลบตัวที่ไม่ใช้ออกก่อนครับ", ephemeral=True)
        return
    
    loop = asyncio.get_running_loop()
    added = []
    already_in = []
    invalid = []
    to_validate = []

    for sym in raw_list:
        if len(current) + len(added) + len(to_validate) >= 50:
            break
        if sym in current:
            already_in.append(sym)
        else:
            to_validate.append(sym)

    if to_validate:
        def _check_batch(batch):
            return list(_executor.map(is_valid_ticker, batch))
        val_flags = await loop.run_in_executor(_executor, _check_batch, to_validate)
        for sym, is_val in zip(to_validate, val_flags):
            if is_val:
                current.append(sym)
                added.append(sym)
            else:
                invalid.append(sym)
            
    if added:
        watchlists[user_id] = current
        save_user_watchlists(watchlists)
        
    embed = discord.Embed(
        title=f"📝 อัปเดต Watchlist ของคุณ ({interaction.user.display_name})",
        color=0x2ECC71 if added else 0xF1C40F
    )
    if added:
        embed.add_field(name="✅ เพิ่มสำเร็จ", value=", ".join([f"`{s}`" for s in added]), inline=False)
    if already_in:
        embed.add_field(name="ℹ️ มีอยู่ในลิสต์อยู่แล้ว", value=", ".join([f"`{s}`" for s in already_in]), inline=False)
    if invalid:
        embed.add_field(name="❌ ไม่พบข้อมูลหุ้น (โปรดตรวจตัวสะกด)", value=", ".join([f"`{s}`" for s in invalid]), inline=False)
        
    embed.add_field(
        name=f"📌 ลิสต์ปัจจุบันของคุณ ({len(current)} ตัว)",
        value=", ".join([f"`{s}`" for s in current]) if current else "*ไม่มีหุ้นในลิสต์*",
        inline=False
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="remove", description="ลบหุ้นออกจาก Watchlist ส่วนตัวของคุณ")
@app_commands.autocomplete(ticker=watchlist_autocomplete)
@app_commands.describe(ticker="ชื่อหุ้นที่ต้องการลบ (มีแนะนำเฉพาะหุ้นใน Watchlist ของคุณ)")
async def slash_remove(interaction: discord.Interaction, ticker: str):
    user_id = str(interaction.user.id)
    raw_list = [clean_ticker(s) for s in re.split(r'[,;\s]+', ticker) if clean_ticker(s)]
    
    watchlists = load_user_watchlists()
    current = watchlists.get(user_id, [])
    
    removed = []
    not_found = []
    
    for sym in raw_list:
        if sym in current:
            current.remove(sym)
            removed.append(sym)
        else:
            not_found.append(sym)
            
    if removed:
        watchlists[user_id] = current
        save_user_watchlists(watchlists)
        
    embed = discord.Embed(
        title=f"🗑️ ผลการลบหุ้นออกจาก Watchlist ({interaction.user.display_name})",
        color=0xE74C3C if removed else 0xF1C40F
    )
    if removed:
        embed.add_field(name="🗑️ ลบสำเร็จ", value=", ".join([f"`{s}`" for s in removed]), inline=False)
    if not_found:
        embed.add_field(name="⚠️ ไม่พบใน Watchlist", value=", ".join([f"`{s}`" for s in not_found]), inline=False)
        
    embed.add_field(
        name=f"📌 ลิสต์คงเหลือ ({len(current)} ตัว)",
        value=", ".join([f"`{s}`" for s in current]) if current else "*ไม่มีหุ้นในลิสต์ (ว่างเปล่า)*",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear_watchlist", description="ล้างหุ้นทั้งหมดใน Watchlist ส่วนตัวของคุณให้ว่างเปล่า")
async def slash_clear_watchlist(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    watchlists = load_user_watchlists()
    watchlists[user_id] = []
    save_user_watchlists(watchlists)
    embed = build_watchlist_embed(interaction.user.display_name, [])
    view = WatchlistView(user_id, interaction.user.display_name)
    await interaction.response.send_message(content="🗑️ **ล้าง Watchlist ส่วนตัวของคุณให้ว่างเปล่าเรียบร้อยแล้ว**", embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="reset_watchlist", description="ล้างหุ้นใน Watchlist ส่วนตัวกลับเป็นค่าว่างเริ่มต้น")
async def slash_reset_watchlist(interaction: discord.Interaction):
    await slash_clear_watchlist(interaction)

@bot.tree.command(name="scan", description="สแกนและจัดอันดับหุ้นตามระบบ Quant ใน Watchlist ส่วนตัวของคุณ")
async def slash_scan(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    symbols = get_user_symbols(user_id)
    if not symbols:
        embed = build_scan_embed([], interaction.user.display_name)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
        
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_scan_embed, symbols, interaction.user.display_name)
    view = ScanView(user_id, interaction.user.display_name)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="check", description="วิเคราะห์คะแนนเจาะลึก 5 มิติของหุ้นรายตัว (Fundamental, Tech, Valuation)")
@app_commands.autocomplete(ticker=ticker_autocomplete)
@app_commands.describe(ticker="ชื่อย่อหุ้น เช่น NVDA, TSLA, AAPL")
async def slash_check(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    ticker = clean_ticker(ticker)
    loop = asyncio.get_running_loop()
    embed, _ = await loop.run_in_executor(_executor, build_check_embed, ticker)
    view = CheckView(ticker, user_id)
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="price", description="เช็คข้อมูลราคาตลาดแบบละเอียด (Day Range, Volume, RVOL)")
@app_commands.autocomplete(ticker=ticker_autocomplete)
@app_commands.describe(ticker="ชื่อย่อหุ้น เช่น NVDA, AAPL")
async def slash_price(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    user_id = str(interaction.user.id)
    ticker = clean_ticker(ticker)
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_price_embed, ticker)
    view = PriceView(ticker, user_id)
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="history", description="ดูตารางราคาย้อนหลัง พร้อมผลตอบแทนรอบช่วงเวลา")
@app_commands.autocomplete(ticker=ticker_autocomplete, period=period_autocomplete)
@app_commands.describe(ticker="ชื่อย่อหุ้น", period="ช่วงเวลา (5d, 1mo, 3mo, 6mo, 1y)")
async def slash_history(interaction: discord.Interaction, ticker: str, period: str = "1mo"):
    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    ticker = clean_ticker(ticker)
    embed = await loop.run_in_executor(_executor, build_history_embed, ticker, period)
    view = HistoryView(ticker, period)
    await interaction.followup.send(embed=embed, view=view)

@bot.tree.command(name="compare", description="เปรียบเทียบหุ้น 2 ตัวแบบตัวต่อตัว (คะแนน 5 มิติ, P/E, PEG, RSI)")
@app_commands.autocomplete(ticker1=ticker_autocomplete, ticker2=ticker_autocomplete)
@app_commands.describe(ticker1="หุ้นตัวที่ 1 (เช่น NVDA)", ticker2="หุ้นตัวที่ 2 (เช่น TSM)")
async def slash_compare(interaction: discord.Interaction, ticker1: str, ticker2: str):
    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    ticker1 = clean_ticker(ticker1)
    ticker2 = clean_ticker(ticker2)
    embed = await loop.run_in_executor(_executor, build_compare_embed, ticker1, ticker2)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="macro", description="ตรวจสอบสภาวะตลาดรวม (Macro Regime, VIX, 10Y Yield, SPX)")
async def slash_macro(interaction: discord.Interaction):
    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_macro_embed)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="portfolio", description="ดูภาพรวมพอร์ตโฟลิโอของคุณ (มูลค่ารวม, PnL, สัดส่วนสินทรัพย์)")
async def slash_portfolio(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_portfolio_embed, user_id, interaction.user.display_name, True)
    view = PortfolioView(user_id, interaction.user.display_name)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="port_add", description="เพิ่มหรืออัปเดตหุ้นในพอร์ตโฟลิโอส่วนตัวของคุณ")
@app_commands.autocomplete(ticker=ticker_autocomplete)
@app_commands.describe(
    ticker="ชื่อย่อหุ้น เช่น NVDA, AAPL, VOO",
    shares="จำนวนหุ้น เช่น 1.5 หรือ 0.55989",
    avg_cost="ราคาต้นทุนเฉลี่ย USD เช่น 210.50",
    category="หมวดหมู่ (ไม่บังคับ) เช่น Semiconductor, Core ETF, Big Tech"
)
async def slash_port_add(interaction: discord.Interaction, ticker: str, shares: float, avg_cost: float, category: str = "General"):
    if shares <= 0 or avg_cost < 0:
        await interaction.response.send_message("❌ กรุณาระบุจำนวนหุ้นมากกว่า 0 และต้นทุนไม่ติดลบ", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    sym = clean_ticker(ticker)
    add_user_portfolio_holding(user_id, sym, shares, avg_cost, category)
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_portfolio_embed, user_id, interaction.user.display_name, True)
    view = PortfolioView(user_id, interaction.user.display_name)
    await interaction.followup.send(f"✅ บันทึกหุ้น `{sym}` (จำนวน {shares:g} หุ้น @ ${avg_cost:.2f}) เข้าพอร์ตของคุณเรียบร้อยแล้ว!", embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="port_remove", description="ลบหุ้นออกจากพอร์ตโฟลิโอส่วนตัวของคุณ")
@app_commands.describe(ticker="ชื่อย่อหุ้นที่ต้องการลบ เช่น NVDA")
async def slash_port_remove(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    sym = clean_ticker(ticker)
    ok = remove_user_portfolio_holding(user_id, sym)
    if not ok:
        await interaction.followup.send(f"⚠️ ไม่พบหุ้น `{sym}` ในพอร์ตโฟลิโอของคุณ", ephemeral=True)
        return
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_portfolio_embed, user_id, interaction.user.display_name, True)
    view = PortfolioView(user_id, interaction.user.display_name)
    await interaction.followup.send(f"🗑️ ลบหุ้น `{sym}` ออกจากพอร์ตเรียบร้อยแล้ว", embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="port_clear", description="ล้างข้อมูลหุ้นทั้งหมดในพอร์ตโฟลิโอส่วนตัวของคุณ")
async def slash_port_clear(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    clear_user_portfolio(user_id)
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_portfolio_embed, user_id, interaction.user.display_name, False)
    view = PortfolioView(user_id, interaction.user.display_name)
    await interaction.followup.send("🗑️ ล้างข้อมูลพอร์ตโฟลิโอของคุณเรียบร้อยแล้ว", embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="alert_set", description="ตั้งแจ้งเตือนราคาหุ้นเป้าหมาย (แจ้งเตือนผ่าน Discord เมื่อราคาแตะเป้า)")
@app_commands.autocomplete(ticker=ticker_autocomplete)
@app_commands.describe(ticker="ชื่อย่อหุ้น เช่น NVDA, AAPL", target_price="ราคาเป้าหมายในสกุล USD เช่น 250.0")
async def slash_alert_set(interaction: discord.Interaction, ticker: str, target_price: float):
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(_executor, add_user_alert, user_id, interaction.channel_id, ticker, target_price)
    await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="alert_list", description="ดูรายการราคาหุ้นที่ตั้งแจ้งเตือนไว้ทั้งหมดของคุณ")
async def slash_alert_list(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    embed = build_alerts_embed(interaction.user.display_name, user_id)
    view = AlertsView(user_id, interaction.user.display_name)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="alert_remove", description="ยกเลิกการแจ้งเตือนราคาหุ้น")
@app_commands.autocomplete(ticker=ticker_autocomplete)
@app_commands.describe(ticker="ชื่อย่อหุ้นที่ต้องการยกเลิกการแจ้งเตือน เช่น NVDA")
async def slash_alert_remove(interaction: discord.Interaction, ticker: str):
    user_id = str(interaction.user.id)
    cnt = remove_user_alert(user_id, ticker)
    if cnt > 0:
        await interaction.response.send_message(f"🗑️ ลบรายการแจ้งเตือนสำหรับ `{clean_ticker(ticker)}` จำนวน {cnt} รายการเรียบร้อยแล้ว", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ ไม่พบรายการแจ้งเตือนที่ตรงกับ `{ticker}` ของคุณ", ephemeral=True)

def build_help_embed():
    embed = discord.Embed(
        title="📖 คู่มือการใช้งานบอทวิเคราะห์หุ้น",
        description="สรุปคำสั่งและวิธีการใช้งานระบบวิเคราะห์ พอร์ต และแจ้งเตือนราคา",
        color=0x3498DB
    )
    
    embed.add_field(
        name="📁 จัดการ Watchlist ส่วนตัว (Private - เห็นเฉพาะคุณ)",
        value=(
            "• `/watchlist` - ดูรายการหุ้นในลิสต์ (เฉพาะคุณ) พร้อมปุ่มสแกนหรือล้างลิสต์\n"
            "• `/add <tickers>` - เพิ่มหุ้นเข้าลิสต์ (ใส่หลายตัวคั่นด้วย `,` ได้ เช่น `NVDA, AAPL, VOO`)\n"
            "• `/remove <ticker>` - ลบหุ้นออกจากลิสต์\n"
            "• `/clear_watchlist` - ล้างหุ้นทั้งหมดในลิสต์ส่วนตัว"
        ),
        inline=False
    )

    embed.add_field(
        name="💼 พอร์ตการลงทุน (Per-User Portfolio)",
        value=(
            "• `/portfolio` - ดูภาพรวมพอร์ตของคุณ (PnL, มูลค่า, สัดส่วน - เป็นส่วนตัว)\n"
            "• `/port_add <ticker> <shares> <avg_cost> [cat]` - เพิ่ม/อัปเดตหุ้นในพอร์ต\n"
            "• `/port_remove <ticker>` - ลบหุ้นออกจากพอร์ต\n"
            "• `/port_clear` - ล้างข้อมูลพอร์ตทั้งหมด\n"
            "• *(หรือกดปุ่ม `[➕ เพิ่ม/แก้หุ้น]` จากเมนูพอร์ต เพื่อกรอกผ่านฟอร์ม)*"
        ),
        inline=False
    )

    embed.add_field(
        name="🔔 แจ้งเตือนราคาเป้าหมาย (Price Alerts)",
        value=(
            "• `/alert_set <ticker> <target_price>` - ตั้งเตือนเมื่อราคาแตะเป้าหมาย\n"
            "• `/alert_list` - ดูรายการที่ตั้งเตือนไว้ทั้งหมด พร้อมปุ่มรีเฟรชหรือล้าง\n"
            "• `/alert_remove <ticker>` - ยกเลิกการแจ้งเตือนราคาหุ้น"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📊 สแกนและวิเคราะห์ราคา",
        value=(
            "• `/scan` - สแกนและประเมินหุ้นใน Watchlist ของคุณ (รวดเร็วด้วย Parallel Cache)\n"
            "• `/check <ticker>` - ตรวจสอบคะแนนอย่างละเอียด 5 มิติ พร้อมกรอบราคาเข้าซื้อ\n"
            "• `/playbook` - คู่มือกลยุทธ์รับมือตลาดปรับฐานแรง (ถือต่อ vs ช้อนซื้อ vs พัก)\n"
            "• `/price <ticker>` - ดูราคาล่าสุด กรอบราคาประจำวัน และปริมาณซื้อขาย\n"
            "• `/history <ticker> [period]` - ดูประวัติราคา พร้อมปุ่มเลือกช่วงเวลา\n"
            "• `/compare <t1> <t2>` - เปรียบเทียบคะแนนและตัวเลขสำคัญของหุ้น 2 ตัว\n"
            "• `/macro` - ตรวจสอบสภาวะตลาดรวมและดัชนี VIX\n"
            "• `/bot_status` - ดูสถานะความเร็ว RAM และรอบรีสตาร์ตอัตโนมัติ\n"
            "• `/restart` - สั่งรีสตาร์ตบอททันที"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💡 ข้อมูลเพิ่มเติม",
        value=(
            "• **แยกรายบุคคล:** ข้อมูล Watchlist และ Alert ถูกแยกตาม Discord User ID อย่างปลอดภัย\n"
            "• **ปุ่มลัด (Quick Actions):** กดปุ่ม `[🔔 ตั้งเตือนราคา]` หรือ `[⭐ Watchlist]` ได้ทันทีจากผลลัพธ์\n"
            "• **รองรับ Prefix:** ใช้งานผ่าน `!wl`, `!port`, `!alert list`, `!scan`, `!check`, `!playbook`, `!status`, `!restart` ได้เช่นกัน"
        ),
        inline=False
    )
    
    embed.set_footer(text="Stock Advisor • ข้อมูลเพื่อประกอบการวิเคราะห์ทางสถิติเท่านั้น")
    return embed

def build_playbook_embed():
    macro = get_cached_macro()
    embed = discord.Embed(
        title="🛡️ คู่มือและกลยุทธ์รับมือตลาดปรับฐานแรง (Market Drawdown Playbook)",
        description=(
            f"**สภาวะตลาดปัจจุบัน:** `{macro.get('regime', 'N/A')}` | **VIX:** `{macro.get('vix', 0):.1f}` | **10Y Yield:** `{macro.get('10y_yield', 0):.2f}%`\n"
            "เมื่อเกิดเหตุการณ์หุ้นร่วงทั้งกระดาน หรือพอร์ตลดลงพร้อมกัน ให้ปฏิบัติตามแนวทาง 3 เสาหลักนี้:"
        ),
        color=0xE67E22
    )
    embed.add_field(
        name="1. 🧘 มีหุ้นในพอร์ตอยู่แล้ว (ถือต่อ vs ขาย?)",
        value=(
            "• **หุ้น Core Index (VOO, QQQM) & Big Tech คุณภาพสูง (NVDA, TSM, MSFT, GOOGL, AMZN, AVGO):**\n"
            "  - **ห้าม Panic Sell ที่ก้นหลุมเด็ดขาด:** การขายตามอารมณ์ตอนตลาดดิ่งแรงมักเป็นการขายที่จุดต่ำสุด\n"
            "  - ตรวจสอบพื้นฐาน: ถ้าปัจจัยพื้นฐานและกระแสเงินสดของบริษัทยังโตตามปกติ การย่อตัวเกิดจากแรงเทขาย Macro/Liquidity ให้ **HOLD (ถือต่อ)**\n"
            "  - ปิดจอ ไม่เช็คราคารายนาที ปล่อยให้ระบบชะล้างความร้อนแรง (De-leveraging)"
        ),
        inline=False
    )
    embed.add_field(
        name="2. 🎯 อยากลงทุนต่อ / ช้อนซื้อ (Dip Buying & DCA)",
        value=(
            "• **อย่าเพิ่งรีบรับมีด (No Falling Knives):** อย่าซื้อในวันแรกที่ตลาดเทขายแท่งยาวสีแดง\n"
            "• **ดูแนวรับตามระบบ Quant:**\n"
            "  - หากราคาหลุด **EMA20** -> แนวรับแรกกลายเป็นแนวต้าน\n"
            "  - รอสังเกตแรงรับที่ **EMA50** หรือแนวรับใหญ่ **EMA200**\n"
            "  - รอให้เกิดแท่งเทียนหยุดลง หรือ RSI Oversold เริ่มยกตัว\n"
            "• **กฎการแบ่งไม้:** แบ่งเงินเป็น 2-3 ไม้เสมอ (เช่น 30% / 30% / 40%) ห้ามทุ่มไม้เดียวเด็ดขาด"
        ),
        inline=False
    )
    embed.add_field(
        name="3. 🛑 การพัก / ถือเงินสด (Cash Preservation)",
        value=(
            "• **ถือเงินสด = ได้เปรียบ (Cash is King):** ในช่วง Risk-Off และ Bond Yield สูง เงินสดคือสินทรัพย์ที่ปลอดภัยที่สุด\n"
            "• **พักการเทรดระยะสั้น:** ตลาดผันผวนสูงมักจะเหวี่ยงหลอก (Whipsaw) รอให้ VIX ต่ำกว่า 20 และดัชนียืนเหนือเส้น EMA50 ได้ก่อน\n"
            "• **ความอดทนคือแต้มต่อ:** รอให้บอทคำนวณคะแนนรวมผ่านเกณฑ์ซื้อ (Hurdle Score 85 คะแนน) แล้วค่อยเข้าซื้อตามสัญญาณอย่างปลอดภัย"
        ),
        inline=False
    )
    embed.set_footer(text="Stock Quant System • กลยุทธ์การบริหารความเสี่ยงระยะยาว")
    return embed

@bot.tree.command(name="playbook", description="คู่มือและกลยุทธ์รับมือตลาดปรับฐานแรง / พอร์ตติดลบ (ถือต่อ vs ช้อนซื้อ vs พัก)")
async def slash_playbook(interaction: discord.Interaction):
    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_playbook_embed)
    await interaction.followup.send(embed=embed)

def build_status_embed():
    now = time.time()
    uptime_sec = int(now - BOT_START_TIME)
    hrs = uptime_sec // 3600
    mins = (uptime_sec % 3600) // 60
    secs = uptime_sec % 60

    mem_mb = get_process_memory_mb()
    latency_ms = int(bot.latency * 1000) if (bot.latency is not None and not math.isnan(bot.latency)) else 0
    next_restart_sec = max(0, int((6 * 3600) - uptime_sec))
    next_hrs = next_restart_sec // 3600
    next_mins = (next_restart_sec % 3600) // 60

    is_container = os.name != 'nt' or os.path.exists("/proc/self/status")
    mem_limit = 205.0 if is_container else 320.0
    mem_ceil_display = 256 if is_container else 350
    is_laggy = mem_mb > mem_limit or latency_ms > 2000
    status_text = "🟡 เริ่มหน่วง (ใกล้รอบรีสตาร์ต)" if is_laggy else "🟢 เสถียรและตอบสนองเร็ว"
    color = 0xF1C40F if is_laggy else 0x2ECC71

    embed = discord.Embed(
        title="⚙️ สถานะระบบและการทำงานของบอท (System Status)",
        description=f"ความสมบูรณ์ของระบบ: **{status_text}**",
        color=color
    )
    embed.add_field(
        name="⏱️ ระยะเวลาเปิดทำงาน (Uptime)",
        value=f"`{hrs} ชม. {mins} นาที {secs} วินาที`\n*(รอบรีสตาร์ตอัตโนมัติ: ทุก 6 ชม.)*",
        inline=True
    )
    embed.add_field(
        name="⏳ รอบรีสตาร์ตอัตโนมัติถัดไป",
        value=f"`อีก {next_hrs} ชม. {next_mins} นาที`\n*(หรือเมื่อแตะ >{mem_limit:.0f}MB / Ping สูง)*",
        inline=True
    )
    embed.add_field(
        name="📊 หน่วยความจำ (RAM Usage)",
        value=f"`{mem_mb:.1f} MB` / เพดาน {mem_ceil_display} MB",
        inline=True
    )
    embed.add_field(
        name="⚡ ความเร็วการตอบสนอง (Ping)",
        value=f"`{latency_ms} ms`",
        inline=True
    )
    embed.set_footer(text="Watchdog Active • ระบบเฝ้าระวังความหน่วงและรีสตาร์ตอัตโนมัติ")
    return embed

class StatusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="รีสตาร์ตบอททันที", style=discord.ButtonStyle.danger, emoji="🔄", custom_id="status:restart")
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔄 กำลังรีสตาร์ตบอททันที กรุณารอสักครู่ (ประมาณ 3-5 วินาที)...", ephemeral=True)
        await perform_graceful_restart(f"สั่งรีสตาร์ตผ่านปุ่ม StatusView โดย {interaction.user.display_name}")

@bot.tree.command(name="bot_status", description="ตรวจสอบสถานะ ความเร็ว RAM และรอบรีสตาร์ตอัตโนมัติของบอท")
async def slash_bot_status(interaction: discord.Interaction):
    embed = build_status_embed()
    view = StatusView()
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="restart", description="คำสั่งแอดมิน: สั่งรีสตาร์ตบอท Discord ทันที")
async def slash_restart(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 ได้รับคำสั่งรีสตาร์ตบอท กำลังเริ่มการทำงานใหม่ใน 2 วินาที...", ephemeral=True)
    await perform_graceful_restart(f"สั่งรีสตาร์ตผ่าน /restart โดย {interaction.user.display_name}")

@bot.tree.command(name="help", description="ดูคู่มือคำสั่งและวิธีใช้งานบอทวิเคราะห์หุ้น")
async def slash_help(interaction: discord.Interaction):
    embed = build_help_embed()
    await interaction.response.send_message(embed=embed)

# ==================== PREFIX COMMANDS FALLBACK ====================

@bot.command(name="status", aliases=["bot_status"])
async def cmd_status(ctx):
    embed = build_status_embed()
    view = StatusView()
    await ctx.send(embed=embed, view=view)

@bot.command(name="restart")
async def cmd_restart(ctx):
    await ctx.send("🔄 กำลังรีสตาร์ตบอท กรุณารอสักครู่...")
    await perform_graceful_restart(f"สั่งรีสตาร์ตผ่าน !restart โดย {ctx.author.display_name}")

@bot.command(name="playbook", aliases=["advice", "crash", "crisis"])
async def cmd_playbook(ctx):
    loop = asyncio.get_running_loop()
    embed = await loop.run_in_executor(_executor, build_playbook_embed)
    await ctx.send(embed=embed)

@bot.command(name="watchlist", aliases=["wl"])
async def cmd_wl(ctx):
    user_id = str(ctx.author.id)
    symbols = get_user_symbols(user_id)
    embed = build_watchlist_embed(ctx.author.display_name, symbols)
    view = WatchlistView(user_id, ctx.author.display_name)
    await ctx.send(embed=embed, view=view)

@bot.command(name="add")
async def cmd_add(ctx, *, tickers: str = ""):
    if not tickers:
        await ctx.send("⚠️ กรุณาระบุชื่อหุ้น เช่น `!add TSLA` หรือ `!add AAPL, AMD, PLTR`")
        return
    user_id = str(ctx.author.id)
    raw_list = [clean_ticker(s) for s in re.split(r'[,;\s]+', tickers) if clean_ticker(s)]
    if len(raw_list) > 15:
        await ctx.send("⚠️ เพื่อความเสถียรของระบบ กรุณาเพิ่มหุ้นไม่เกินครั้งละ 15 ตัวครับ")
        return

    watchlists = load_user_watchlists()
    current = watchlists.get(user_id, [])
    if len(current) >= 50:
        await ctx.send("⚠️ Watchlist ของคุณมีหุ้นครบ 50 ตัวแล้ว (จำกัดสูงสุด 50 ตัว) กรุณาลบตัวที่ไม่ใช้ออกก่อนครับ")
        return

    loop = asyncio.get_running_loop()
    added = []
    already_in = []
    invalid = []
    to_validate = []
    for sym in raw_list:
        if len(current) + len(added) + len(to_validate) >= 50:
            break
        if sym in current:
            already_in.append(sym)
        else:
            to_validate.append(sym)

    if to_validate:
        def _check_batch(batch):
            return list(_executor.map(is_valid_ticker, batch))
        val_flags = await loop.run_in_executor(_executor, _check_batch, to_validate)
        for sym, is_val in zip(to_validate, val_flags):
            if is_val:
                current.append(sym)
                added.append(sym)
            else:
                invalid.append(sym)
            
    if added:
        watchlists[user_id] = current
        save_user_watchlists(watchlists)
        
    embed = discord.Embed(title=f"📝 อัปเดต Watchlist ของ {ctx.author.display_name}", color=0x2ECC71 if added else 0xF1C40F)
    if added:
        embed.add_field(name="✅ เพิ่มสำเร็จ", value=", ".join([f"`{s}`" for s in added]), inline=False)
    if already_in:
        embed.add_field(name="ℹ️ มีอยู่แล้ว", value=", ".join([f"`{s}`" for s in already_in]), inline=False)
    if invalid:
        embed.add_field(name="❌ ไม่พบข้อมูล", value=", ".join([f"`{s}`" for s in invalid]), inline=False)
    embed.add_field(name=f"📌 ลิสต์ปัจจุบัน ({len(current)} ตัว)", value=", ".join([f"`{s}`" for s in current]) if current else "*ไม่มีหุ้นในลิสต์*", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="remove", aliases=["rm", "del"])
async def cmd_remove(ctx, *, tickers: str = ""):
    if not tickers:
        await ctx.send("⚠️ กรุณาระบุชื่อหุ้น เช่น `!remove TSLA`")
        return
    user_id = str(ctx.author.id)
    raw_list = [clean_ticker(s) for s in re.split(r'[,;\s]+', tickers) if clean_ticker(s)]
    watchlists = load_user_watchlists()
    current = watchlists.get(user_id, [])
    
    removed = [s for s in raw_list if s in current]
    for s in removed:
        current.remove(s)
        
    if removed:
        watchlists[user_id] = current
        save_user_watchlists(watchlists)
        await ctx.send(f"🗑️ ลบ `{', '.join(removed)}` ออกจาก Watchlist ส่วนตัวของคุณแล้ว! (คงเหลือ: `{', '.join(current)}`)" if current else f"🗑️ ลบ `{', '.join(removed)}` เรียบร้อย (ขณะนี้ Watchlist ของคุณว่างเปล่า)")
    else:
        await ctx.send(f"⚠️ ไม่พบหุ้นดังกล่าวใน Watchlist ของคุณ")

@bot.command(name="clear", aliases=["reset", "clear_watchlist", "reset_watchlist"])
async def cmd_clear(ctx):
    user_id = str(ctx.author.id)
    watchlists = load_user_watchlists()
    watchlists[user_id] = []
    save_user_watchlists(watchlists)
    await ctx.send(f"🗑️ ล้าง Watchlist ส่วนตัวของคุณ ({ctx.author.display_name}) เป็นค่าว่างเปล่าเรียบร้อยแล้ว")

@bot.command(name="scan")
async def cmd_scan(ctx):
    user_id = str(ctx.author.id)
    symbols = get_user_symbols(user_id)
    if not symbols:
        embed = build_scan_embed([], ctx.author.display_name)
        await ctx.send(embed=embed)
        return
        
    async with ctx.typing():
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_scan_embed, symbols, ctx.author.display_name)
        view = ScanView(user_id, ctx.author.display_name)
        await ctx.send(embed=embed, view=view)

@bot.command(name="check")
async def cmd_check(ctx, ticker: str):
    ticker = clean_ticker(ticker)
    async with ctx.typing():
        loop = asyncio.get_running_loop()
        embed, _ = await loop.run_in_executor(_executor, build_check_embed, ticker)
        view = CheckView(ticker, str(ctx.author.id))
        await ctx.send(embed=embed, view=view)

@bot.command(name="price")
async def cmd_price(ctx, ticker: str):
    ticker = clean_ticker(ticker)
    async with ctx.typing():
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_price_embed, ticker)
        view = PriceView(ticker, str(ctx.author.id))
        await ctx.send(embed=embed, view=view)

@bot.command(name="history", aliases=["hist"])
async def cmd_history(ctx, ticker: str, period: str = "1mo"):
    ticker = clean_ticker(ticker)
    async with ctx.typing():
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_history_embed, ticker, period)
        view = HistoryView(ticker, period)
        await ctx.send(embed=embed, view=view)

@bot.command(name="compare", aliases=["vs"])
async def cmd_compare(ctx, ticker1: str, ticker2: str):
    ticker1 = clean_ticker(ticker1)
    ticker2 = clean_ticker(ticker2)
    async with ctx.typing():
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_compare_embed, ticker1, ticker2)
        await ctx.send(embed=embed)

@bot.command(name="macro")
async def cmd_macro(ctx):
    async with ctx.typing():
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_macro_embed)
        await ctx.send(embed=embed)

@bot.command(name="portfolio", aliases=["port"])
async def cmd_portfolio(ctx, action: str = "", ticker: str = "", shares: str = "", avg_cost: str = "", category: str = "General"):
    user_id = str(ctx.author.id)
    action = action.lower().strip()
    
    # 1. Subcommands: !port add <ticker> <shares> <avg_cost> [cat]
    if action in ["add", "set"]:
        if not ticker or not shares or not avg_cost:
            await ctx.send("⚠️ วิธีใช้: `!port add <ชื่อย่อหุ้น> <จำนวนหุ้น> <ต้นทุนเฉลี่ย>` เช่น `!port add NVDA 1.5 210.0`")
            return
        try:
            sh_val = float(shares.replace(",", ""))
            c_val = float(avg_cost.replace("$", "").replace(",", ""))
            if sh_val <= 0 or c_val < 0:
                raise ValueError()
        except ValueError:
            await ctx.send("❌ กรุณาระบุจำนวนหุ้นและต้นทุนเป็นตัวเลขที่ถูกต้อง เช่น `!port add NVDA 1.5 210.0`")
            return
        sym = clean_ticker(ticker)
        add_user_portfolio_holding(user_id, sym, sh_val, c_val, category)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        msg = await ctx.send(f"✅ บันทึกหุ้น `{sym}` ในพอร์ตของคุณ ({ctx.author.display_name}) สำเร็จแล้ว!")
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # 2. Subcommands: !port remove <ticker>
    if action in ["remove", "rm", "del", "delete"]:
        if not ticker:
            await ctx.send("⚠️ วิธีใช้: `!port remove <ชื่อย่อหุ้น>` เช่น `!port remove NVDA`")
            return
        sym = clean_ticker(ticker)
        ok = remove_user_portfolio_holding(user_id, sym)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        if ok:
            msg = await ctx.send(f"🗑️ ลบหุ้น `{sym}` ออกจากพอร์ตของคุณแล้ว")
        else:
            msg = await ctx.send(f"⚠️ ไม่พบหุ้น `{sym}` ในพอร์ตของคุณ")
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # 3. Subcommands: !port clear
    if action in ["clear", "reset"]:
        clear_user_portfolio(user_id)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        msg = await ctx.send(f"🗑️ ล้างข้อมูลพอร์ตของคุณ ({ctx.author.display_name}) เรียบร้อยแล้ว")
        await asyncio.sleep(5)
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # 4. View portfolio: ส่งเข้า DM ถ้าพิมพ์ในเซิร์ฟเวอร์ เพื่อความเป็นส่วนตัว
    if ctx.guild:
        try:
            await ctx.message.delete()
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        embed = await loop.run_in_executor(_executor, build_portfolio_embed, user_id, ctx.author.display_name, True)
        view = PortfolioView(user_id, ctx.author.display_name)
        try:
            await ctx.author.send(embed=embed, view=view)
            msg = await ctx.send(f"🔒 {ctx.author.mention} ส่งสรุปพอร์ตโฟลิโอของคุณให้ทาง DM ส่วนตัวแล้วครับ เพื่อความเป็นส่วนตัว")
            await asyncio.sleep(5)
            try:
                await msg.delete()
            except Exception:
                pass
        except Exception:
            await ctx.send(embed=embed, view=view)
    else:
        async with ctx.typing():
            loop = asyncio.get_running_loop()
            embed = await loop.run_in_executor(_executor, build_portfolio_embed, user_id, ctx.author.display_name, True)
            view = PortfolioView(user_id, ctx.author.display_name)
            await ctx.send(embed=embed, view=view)

@bot.command(name="alert", aliases=["alerts"])
async def cmd_alert(ctx, action: str = "list", ticker: str = "", target_price: str = ""):
    action = action.lower().strip()
    user_id = str(ctx.author.id)
    if action in ["list", "show"]:
        embed = build_alerts_embed(ctx.author.display_name, user_id)
        view = AlertsView(user_id, ctx.author.display_name)
        await ctx.send(embed=embed, view=view)
    elif action in ["set", "add"]:
        if not ticker or not target_price:
            await ctx.send("⚠️ วิธีใช้: `!alert set <ticker> <target_price>` เช่น `!alert set NVDA 250`")
            return
        try:
            val = float(target_price.replace("$", ""))
            loop = asyncio.get_running_loop()
            ok, msg = await loop.run_in_executor(_executor, add_user_alert, user_id, ctx.channel.id, ticker, val)
            await ctx.send(msg)
        except ValueError:
            await ctx.send("❌ ราคาเป้าหมายต้องเป็นตัวเลข เช่น `250.50`")
    elif action in ["remove", "rm", "del", "delete"]:
        if not ticker:
            await ctx.send("⚠️ วิธีใช้: `!alert remove <ticker>` เช่น `!alert remove NVDA`")
            return
        cnt = remove_user_alert(user_id, ticker)
        if cnt > 0:
            await ctx.send(f"🗑️ ลบรายการแจ้งเตือนสำหรับ `{clean_ticker(ticker)}` จำนวน {cnt} รายการเรียบร้อยแล้ว")
        else:
            await ctx.send(f"⚠️ ไม่พบรายการแจ้งเตือนที่ตรงกับ `{ticker}`")
    elif action in ["clear", "reset"]:
        alerts = load_user_alerts()
        alerts[user_id] = []
        save_user_alerts(alerts)
        await ctx.send(f"🗑️ ล้างรายการแจ้งเตือนทั้งหมดของคุณ ({ctx.author.display_name}) เรียบร้อยแล้ว")
    else:
        await ctx.send("⚠️ คำสั่งย่อยไม่ถูกต้อง: ใช้ `!alert list`, `!alert set <ticker> <price>`, หรือ `!alert rm <ticker>`")

@bot.command(name="help")
async def cmd_help(ctx):
    embed = build_help_embed()
    await ctx.send(embed=embed)

@bot.command(name="sync")
async def cmd_sync(ctx):
    """คำสั่งแอดมิน: ซิงค์และล้างคำสั่ง Slash Commands ที่ซ้ำซ้อน"""
    is_admin = ctx.channel.permissions_for(ctx.author).administrator if ctx.guild else False
    is_owner = await ctx.bot.is_owner(ctx.author)
    if not (is_admin or is_owner):
        await ctx.send("⛔ คำสั่งนี้สงวนไว้สำหรับ Admin หรือเจ้าของบอทเท่านั้น")
        return

    msg = await ctx.send("🔄 กำลังตรวจสอบและจัดระเบียบ Slash Commands...")
    try:
        if ctx.guild:
            ctx.bot.tree.clear_commands(guild=ctx.guild)
            await ctx.bot.tree.sync(guild=ctx.guild)
        synced = await ctx.bot.tree.sync()
        await msg.edit(content=f"✅ จัดการเรียบร้อย! ขณะนี้มี Global Commands `{len(synced)}` คำสั่ง (ล้างคำสั่งซ้ำในเซิร์ฟเวอร์สำเร็จ)")
    except Exception as e:
        await msg.edit(content=f"❌ เกิดข้อผิดพลาดในการซิงค์: {e}")


async def main():
    # 1. Start HTTP health check server immediately so Render probes succeed in 0.1s
    await start_health_check_server()
    # 2. Run Discord Bot with retry + full error logging (avoids crash-loop restarts on cloud)
    import traceback
    attempt = 0
    while True:
        attempt += 1
        try:
            print(f"🔌 Connecting to Discord (attempt {attempt})...", flush=True)
            await bot.start(BOT_TOKEN)
            break
        except Exception as e:
            print(f"❌ Discord connection failed: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            try:
                await bot.close()
            except Exception:
                pass
            await asyncio.sleep(min(60 * attempt, 600))
            bot.clear()

if __name__ == "__main__":
    asyncio.run(main())
