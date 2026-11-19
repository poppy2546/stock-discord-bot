import os
import sys
import shutil
import tempfile

# Safe standard output configuration (supports pythonw and UTF-8 on Windows)
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
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

import json
import datetime
import warnings
warnings.filterwarnings("ignore", message=".*inplace.*")
import yfinance as yf
import pandas as pd
import numpy as np

# Config
WATCHLIST = ['VOO', 'QQQM', 'NVDA', 'TSM', 'AVGO', 'AMZN', 'GOOGL', 'MSFT']
MACRO_TICKERS = ['^GSPC', '^NDX', '^VIX', '^TNX', 'THB=X'] # SP500, Nasdaq, VIX, 10Y Yield, USD/THB

# Portfolio Constraints & Holdings
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HOLDINGS_FILE = os.path.join(DATA_DIR, "portfolio_holdings.json") if os.path.exists(os.path.join(DATA_DIR, "portfolio_holdings.json")) else os.path.join(os.path.dirname(__file__), "portfolio_holdings.json")
USER_PORTFOLIO_FILE = os.path.join(DATA_DIR, "user_portfolios.json")
PORTFOLIO_HOLDINGS = {}
PORTFOLIO_CASH_THB = 1800.28  # งบเงินสดคงเหลือ 1,800.28 THB (หักซื้อ QQQM 1,999.99 + GOOGL 1,199.73 THB)
_PORTFOLIO_RAW_DATA = {}

import threading

def _parse_iso_ts(ts_str):
    if not ts_str:
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(str(ts_str).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=7)))
        return dt.timestamp()
    except Exception:
        return 0.0

def _atomic_write_json(filepath, data):
    tmp = None
    try:
        target_dir = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(target_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_dir, prefix=".tmp_", suffix=".json", delete=False
        ) as f:
            tmp = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filepath)
    except Exception:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

def load_latest_portfolio():
    """โหลดพอร์ตล่าสุดโดยเปรียบเทียบระหว่าง portfolio_holdings.json และ user_portfolios.json เพื่อป้องกันข้อมูลเหลื่อมกัน"""
    global PORTFOLIO_HOLDINGS, PORTFOLIO_CASH_THB, _PORTFOLIO_RAW_DATA
    h_data = {}
    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                h_data = json.load(f) or {}
        except Exception:
            h_data = {}

    owner_id = str(h_data.get("owner_id") or os.getenv("DISCORD_OWNER_ID") or "").strip()
    if os.path.exists(USER_PORTFOLIO_FILE):
        try:
            with open(USER_PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                u_ports = json.load(f) or {}
            cand = u_ports.get(owner_id) if owner_id else None
            if not cand and len(u_ports) == 1:
                cand = next(iter(u_ports.values()))
            if isinstance(cand, dict) and cand.get("holdings"):
                h_ts = _parse_iso_ts(h_data.get("updated_at"))
                c_ts = _parse_iso_ts(cand.get("updated_at"))
                if not h_data.get("holdings") or c_ts > h_ts:
                    h_data = cand
        except Exception:
            pass

    if h_data:
        _PORTFOLIO_RAW_DATA = h_data
        PORTFOLIO_HOLDINGS = h_data.get("holdings", {})
        _sum_cash = h_data.get("portfolio_summary", {}).get("cash_thb")
        if _sum_cash is not None:
            PORTFOLIO_CASH_THB = float(_sum_cash)
    return PORTFOLIO_HOLDINGS

def save_refreshed_portfolio(holdings, total_cost_usd, total_value_usd, total_pnl_usd, total_pnl_pct, fx_rate_thb=None):
    """บันทึกมูลค่าตลาดและกำไรขาดทุนล่าสุดลงทั้ง portfolio_holdings.json และ user_portfolios.json โดยคงต้นทุนเดิมไว้เสมอ"""
    global _PORTFOLIO_RAW_DATA
    if not holdings:
        return
    now_iso = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).isoformat(timespec="seconds")
    summary = dict(_PORTFOLIO_RAW_DATA.get("portfolio_summary") or {})
    fx_rate = float(fx_rate_thb if fx_rate_thb and fx_rate_thb > 0 else (summary.get("fx_rate_thb") or 33.67))
    summary.update({
        "cash_thb": PORTFOLIO_CASH_THB,
        "currency": "USD",
        "fx_rate_thb": fx_rate,
        "total_assets": len(holdings),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "total_pnl_usd": round(total_pnl_usd, 2),
        "total_cost_usd": round(total_cost_usd, 2),
        "total_value_thb": round(total_value_usd * fx_rate, 2),
        "total_value_usd": round(total_value_usd, 2),
    })
    owner_id = str(_PORTFOLIO_RAW_DATA.get("owner_id") or os.getenv("DISCORD_OWNER_ID") or "").strip()
    updated_doc = {
        "source": _PORTFOLIO_RAW_DATA.get("source", "Dime! App Portfolio"),
        "holdings": holdings,
        **({"owner_id": owner_id} if owner_id else {}),
        "updated_at": _PORTFOLIO_RAW_DATA.get("updated_at") or now_iso,
        "price_updated_at": now_iso,
        "portfolio_summary": summary,
    }
    _PORTFOLIO_RAW_DATA = updated_doc
    _atomic_write_json(HOLDINGS_FILE, updated_doc)
    if os.path.exists(USER_PORTFOLIO_FILE) or owner_id:
        try:
            u_ports = {}
            if os.path.exists(USER_PORTFOLIO_FILE):
                with open(USER_PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                    u_ports = json.load(f) or {}
            if owner_id:
                u_ports[owner_id] = updated_doc
                _atomic_write_json(USER_PORTFOLIO_FILE, u_ports)
        except Exception:
            pass

load_latest_portfolio()

# สามารถ Override ผ่าน Environment Variable ได้
if os.getenv("PORTFOLIO_CASH_THB"):
    try:
        PORTFOLIO_CASH_THB = float(os.getenv("PORTFOLIO_CASH_THB"))
    except ValueError:
        pass
SEMI_TICKERS = {'NVDA', 'TSM', 'AVGO'}
TRUE_SEMI_CAP = 0.45

# Calculate exact Semi Exposure from verified Dime! holdings
if PORTFOLIO_HOLDINGS:
    _tot_v = sum(h.get('current_value_usd', 0) for h in PORTFOLIO_HOLDINGS.values())
    _semi_v = sum(h.get('current_value_usd', 0) for s, h in PORTFOLIO_HOLDINGS.items() if s in SEMI_TICKERS)
    DIRECT_SEMI_EXPOSURE = (_semi_v / _tot_v) if _tot_v > 0 else 0.4394
else:
    DIRECT_SEMI_EXPOSURE = 0.4394

def calculate_technical(df):
    if df is None or df.empty:
        return {}
    df = df.dropna(subset=['Close'])
    if len(df) < 50:
        return {}
    close = df['Close']
    volume = df['Volume'].fillna(0) if 'Volume' in df.columns else pd.Series(0, index=df.index)
    
    # EMAs
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    # EMA200 ต้องมีข้อมูลอย่างน้อย 200 แท่ง ถ้าไม่ถึงให้เป็น 0.0 (= ไม่มีข้อมูล) แทนการยืมค่า EMA50
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1] if len(df) >= 200 else 0.0
    curr_price = close.iloc[-1]
    
    # EMA Distance
    ema_dist = ((curr_price - ema20) / ema20) * 100
    
    # RSI (14) - Wilder's smoothing (ตรงกับ TradingView / Yahoo)
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain.iloc[-1] / (loss.iloc[-1] + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_val = macd_line.iloc[-1]
    sig_val = signal_line.iloc[-1]
    
    # RVOL (20)
    avg_vol20 = volume.rolling(window=20).mean().iloc[-1]
    rvol = volume.iloc[-1] / (avg_vol20 + 1e-9)
    
    # Candle geometry & wicks
    candle_range = 0.0
    lower_wick = 0.0
    upper_wick = 0.0
    wick_ratio = 0.0
    upper_wick_ratio = 0.0
    if len(df) >= 6 and 'Low' in df.columns and 'High' in df.columns:
        curr_low = float(df['Low'].iloc[-1])
        curr_high = float(df['High'].iloc[-1])
        candle_range = curr_high - curr_low
        open_col = df['Open'] if 'Open' in df.columns else close
        curr_open = float(open_col.iloc[-1])
        lower_wick = min(curr_open, float(curr_price)) - curr_low
        wick_ratio = lower_wick / (candle_range + 1e-9)
        upper_wick = curr_high - max(curr_open, float(curr_price))
        upper_wick_ratio = upper_wick / (candle_range + 1e-9)

    # ATR (14) - Average True Range สำหรับคำนวณ Volatility, Stop Loss และ Profit Target
    high_col = df['High'] if 'High' in df.columns else close
    low_col = df['Low'] if 'Low' in df.columns else close
    prev_c = close.shift(1)
    tr = pd.concat([
        high_col - low_col,
        (high_col - prev_c).abs(),
        (low_col - prev_c).abs()
    ], axis=1).max(axis=1)
    atr = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])

    # Bollinger Bands (20, 2)
    rolling_mean20 = close.rolling(window=20).mean()
    rolling_std20 = close.rolling(window=20).std()
    upper_bb = rolling_mean20.iloc[-1] + (2 * rolling_std20.iloc[-1])
    lower_bb = rolling_mean20.iloc[-1] - (2 * rolling_std20.iloc[-1])
    percent_b = (curr_price - lower_bb) / ((upper_bb - lower_bb) + 1e-9)

    # Volume Climax / Absorption Wick (ดักจับแรงซื้อสถาบันเมื่อมีแรงเทขายสะเด็ดน้ำ)
    absorption_climax = bool(rvol >= 1.5 and wick_ratio >= 0.35)

    # 1. Volume Dry-Up (Minervini VDU): วอลุ่มแห้งขณะย่อตัวในโซน Sweetspot สะท้อนแรงขายแห้ง
    volume_dryup = bool(-2.5 <= ema_dist <= 2.5 and rvol <= 0.70)

    # 2. Minervini Stage 2 Uptrend: เรียงตัว EMA20 > EMA50 > EMA200, EMA50 ชันขึ้น, เกาะใกล้ High 52w
    ema50_s = close.ewm(span=50, adjust=False).mean()
    ema50_slope_up = bool(ema50 >= float(ema50_s.iloc[-10])) if len(df) >= 60 else True
    high_52w = float(high_col.rolling(window=min(len(df), 252)).max().iloc[-1])
    near_52w_high = bool((high_52w - curr_price) / (high_52w + 1e-9) <= 0.25)
    stage2_uptrend = bool(curr_price > ema20 > ema50 and (ema200 == 0.0 or ema50 > ema200) and ema50_slope_up and near_52w_high)

    # 3. Bullish / Bearish RSI Divergence: ตรวจเทียบราคา (Low/High + Close) และจุด Peak/Trough ของ RSI รอบ 14 วัน
    bullish_divergence = False
    bearish_divergence = False
    if len(df) >= 20:
        rsi_series = 100 - (100 / (1 + (gain / (loss + 1e-9))))
        prior_close = close.iloc[-14:-3]
        prior_low_s = low_col.iloc[-14:-3]
        prior_high_s = high_col.iloc[-14:-3]
        prior_rsi_s = rsi_series.iloc[-14:-3]
        curr_low_val = float(low_col.iloc[-1])
        curr_high_val = float(high_col.iloc[-1])
        if rsi < 45:
            min_prior_p = float(prior_close.min())
            min_prior_lo = float(prior_low_s.min())
            idx_prior = prior_low_s.idxmin()
            loc_idx = rsi_series.index.get_loc(idx_prior)
            win_lo = max(0, loc_idx - 1)
            win_hi = min(len(rsi_series), loc_idx + 2)
            prior_rsi = float(rsi_series.iloc[win_lo:win_hi].min())
            if (float(curr_price) < min_prior_p or curr_low_val < min_prior_lo) and rsi > prior_rsi + 1.5:
                bullish_divergence = True
        elif rsi > 60:
            max_prior_p = float(prior_close.max())
            max_prior_hi = float(prior_high_s.max())
            idx_prior_hi = prior_high_s.idxmax()
            loc_idx_hi = rsi_series.index.get_loc(idx_prior_hi)
            win_lo_hi = max(0, loc_idx_hi - 1)
            win_hi_hi = min(len(rsi_series), loc_idx_hi + 2)
            prior_hi_rsi = float(rsi_series.iloc[win_lo_hi:win_hi_hi].max())
            if (float(curr_price) > max_prior_p or curr_high_val > max_prior_hi) and rsi < prior_hi_rsi - 2.0:
                bearish_divergence = True

    # 4. Institutional Distribution Penalty: หลุด EMA20 แต่วอลุ่มเทขายมหาศาล ไร้แรงรับ
    distribution_day = bool(ema_dist < -2.0 and rvol >= 1.8 and wick_ratio < 0.25)

    # 5. TTM Volatility Squeeze (Bollinger Bands บีบตัวใน Keltner Channels 1.5 ATR รอระเบิดทิศทาง)
    upper_kc = ema20 + (1.5 * atr)
    lower_kc = ema20 - (1.5 * atr)
    ttm_squeeze = bool(
        candle_range > 0 and upper_bb < upper_kc and lower_bb > lower_kc
        and curr_price >= ema20 and (macd_val > sig_val or rsi >= 50)
    )

    # 6. Bullish Fair Value Gap (FVG / Institutional Imbalance Support ในรอบ 10 แท่งล่าสุด)
    bullish_fvg = False
    fvg_zone = None
    if len(df) >= 10 and 'Low' in df.columns and 'High' in df.columns:
        lows_arr = df['Low'].values
        highs_arr = df['High'].values
        closes_arr = close.values
        n_bars = len(df)
        for idx in range(n_bars - 1, max(n_bars - 9, 1), -1):
            gap_lo = float(highs_arr[idx - 2])
            gap_hi = float(lows_arr[idx])
            if gap_hi > gap_lo * 1.002:
                # ตรวจว่าแท่งถัดมาไม่ปิดหลุดกรอบล่างของ FVG และราคาปัจจุบันกำลังย่อทดสอบหรือเกาะเหนือ FVG
                subsequent_closes = closes_arr[idx:]
                if float(np.min(subsequent_closes)) >= gap_lo * 0.995 and (gap_lo * 0.995 <= float(curr_price) <= gap_hi * 1.03):
                    bullish_fvg = True
                    fvg_zone = (round(gap_lo, 2), round(gap_hi, 2))
                    break

    # 7. Chandelier Trailing Stop (22-day High - 2.5x ATR สำหรับล็อกกำไรขาขึ้น)
    high_22d = float(high_col.rolling(window=min(len(df), 22)).max().iloc[-1])
    chandelier_stop = round(max(0.0, high_22d - (2.5 * atr)), 2)

    # 8. 20-day Return (%) สำหรับคำนวณ Relative Strength (RS Alpha) เทียบตลาด S&P 500
    ret_20d = float(((curr_price / close.iloc[-21]) - 1.0) * 100.0) if len(close) >= 21 else 0.0

    # Score Technical (Max 20)
    score = 0
    # Trend
    if curr_price > ema20 > ema50:
        score += 8
    elif curr_price > ema50:
        score += 5
    elif ema200 > 0 and curr_price > ema200:
        score += 3
    else:
        # Deep oversold rebound potential
        if rsi < 32:
            score += 4 # Rebound floor candidate
        else:
            score += 1
        
    # EMA distance penalty / Pullback Zone
    if ema_dist > 9.0:
        score -= 4 # Overextended chase penalty
    elif -2.0 <= ema_dist <= 3.0:
        score += 4 # Good entry zone on pullback
    elif ema_dist < -7.0 and rsi < 32:
        score += 4 # Extreme oversold pullback zone
    else:
        score += 2
        
    # RSI (All-Weather: Momentum + Value Dip)
    if 50 <= rsi <= 68:
        score += 4 # Strong momentum sweetspot
    elif 40 <= rsi < 50:
        score += 2 # Basing
    elif rsi < 30:
        score += 5 # Deep Oversold Panic Discount
    elif rsi < 40:
        score += 3 # Oversold pullback
    elif rsi > 70:
        score += 1 # Overbought
    else:
        score += 2
        
    # MACD & Volume
    if macd_val > sig_val:
        score += 2
    if rvol >= 1.2:
        score += 2

    # Bollinger Band Capitulation Bonus
    if percent_b < 0:
        score += 2 # Pierced below 2-sigma lower band (Capitulation)

    # Absorption Climax Bonus
    if absorption_climax:
        score += 2 # Institutional absorption wick

    # 5. Volume Dry-Up (Minervini VDU Bonus: +2)
    if volume_dryup:
        score += 2

    # 6. Minervini Stage 2 Uptrend Bonus (+2)
    if stage2_uptrend:
        score += 2

    # 7. Bullish / Bearish RSI Divergence (+2 / -2)
    if bullish_divergence:
        score += 2
    if bearish_divergence:
        score -= 2

    # 8. Heavy Institutional Distribution Penalty (-2)
    if distribution_day:
        score -= 2

    # 9. TTM Volatility Squeeze Bonus (+2 เมื่อบีบตัวพร้อมระเบิดขาขึ้น)
    if ttm_squeeze:
        score += 2

    # 10. Bullish Fair Value Gap (FVG Support Bonus: +2)
    if bullish_fvg:
        score += 2
        
    # 11. AMD Liquidity Sweep Bonus (+3 คะแนน - ดักจับการสับขาหลอกฝั่งขาขึ้น SSL)
    # ตรรกะ: ราคาแท่งล่าสุดจ้วงหลุด Low 5 วันก่อนหน้า (Sweep Liquidity) แล้วต้องดึงกลับขึ้นมาปิดเหนือ Low เดิมเท่านั้น
    # (ไส้ล่างยาวอย่างเดียวไม่พอ เพราะหุ้นที่ปิดใต้แนวรับคือ Breakdown ไม่ใช่ Sweep)
    amd_sweep = False
    if len(df) >= 6 and 'Low' in df.columns and 'High' in df.columns:
        prior_5d_low = float(df['Low'].iloc[-6:-1].min())
        if curr_low < prior_5d_low and float(curr_price) > prior_5d_low:
            amd_sweep = True
            score += 3

    # 12. BSL Liquidity Sweep Penalty (-3 คะแนน - ดักจับการกินสภาพคล่องยอดดอย ห้ามไล่ราคา)
    # ตรรกะ: ราคาแท่งล่าสุดแทงทะลุ High 5 วันก่อนหน้า แต่โดนเทขายสวนกลับลงมาปิดต่ำกว่า High เดิม
    # (หุ้นที่ปิดยืนเหนือ High เดิมได้คือ Breakout จริง แม้มีไส้บน)
    bsl_sweep = False
    if len(df) >= 6 and 'Low' in df.columns and 'High' in df.columns:
        prior_5d_high = float(df['High'].iloc[-6:-1].max())
        if curr_high > prior_5d_high and float(curr_price) < prior_5d_high:
            bsl_sweep = True
            score -= 3
        
    return {
        'price': float(curr_price),
        'ema20': float(ema20),
        'ema50': float(ema50),
        'ema200': float(ema200),
        'ema_dist': float(ema_dist),
        'rsi': float(rsi),
        'macd_bullish': bool(macd_val > sig_val),
        'rvol': float(rvol),
        'percent_b': float(percent_b),
        'absorption_climax': bool(absorption_climax),
        'amd_sweep': bool(amd_sweep),
        'bsl_sweep': bool(bsl_sweep),
        'atr': round(atr, 2),
        'volume_dryup': bool(volume_dryup),
        'stage2_uptrend': bool(stage2_uptrend),
        'bullish_divergence': bool(bullish_divergence),
        'bearish_divergence': bool(bearish_divergence),
        'distribution_day': bool(distribution_day),
        'ttm_squeeze': bool(ttm_squeeze),
        'bullish_fvg': bool(bullish_fvg),
        'fvg_zone': fvg_zone,
        'chandelier_stop': chandelier_stop,
        'ret_20d': round(ret_20d, 2),
        'raw_tech_score': score,
        'score': max(0, min(20, score))
    }

_shared_session = None
_thread_local = threading.local()

def get_session():
    global _shared_session
    is_main = (threading.current_thread() is threading.main_thread())
    if is_main and _shared_session is not None:
        return _shared_session
    if not is_main and getattr(_thread_local, "session", None) is not None:
        return _thread_local.session
    try:
        from curl_cffi import requests
        sess = requests.Session(impersonate="chrome")
        if is_main:
            _shared_session = sess
        else:
            _thread_local.session = sess
        return sess
    except Exception:
        return None

def analyze_macro():
    session = get_session()
    try:
        kwargs = {'session': session} if session else {}
        macro_data = yf.download(MACRO_TICKERS, period="3mo", interval="1d", progress=False, **kwargs)
        if macro_data.empty or 'Close' not in macro_data:
            raise ValueError("Empty macro data received from Yahoo Finance")
            
        close = macro_data['Close']
        spx = close['^GSPC'].dropna()
        vix = float(close['^VIX'].dropna().iloc[-1])
        tnx = float(close['^TNX'].dropna().iloc[-1])
        spx_curr = float(spx.iloc[-1])
        spx_ema50 = float(spx.ewm(span=50, adjust=False).mean().iloc[-1])
        spx_ret_20d = float(((spx_curr / float(spx.iloc[-21])) - 1.0) * 100.0) if len(spx) >= 21 else 0.0
        fx_thb = 33.67
        if 'THB=X' in close and not close['THB=X'].dropna().empty:
            fx_cand = float(close['THB=X'].dropna().iloc[-1])
            if 25.0 <= fx_cand <= 45.0:
                fx_thb = round(fx_cand, 2)
        
        if spx_curr > spx_ema50 and vix < 20:
            regime = "Risk-On (Bull)"
            hurdle = 80
            macro_score = 14
        elif spx_curr > spx_ema50 and vix <= 25:
            regime = "Neutral / Consolidation"
            hurdle = 83
            macro_score = 11
        else:
            regime = "Risk-Off (Correction / Caution)"
            hurdle = 88
            macro_score = 7
            
        return {
            'regime': regime,
            'hurdle': hurdle,
            'vix': vix,
            '10y_yield': tnx,
            'spx_above_ema50': bool(spx_curr > spx_ema50),
            'spx_ret_20d': round(spx_ret_20d, 2),
            'macro_score': macro_score,
            'fx_thb': fx_thb
        }
    except Exception as e:
        print(f"Warning in analyze_macro: {e}, using safe default regime")
        return {
            'regime': "Neutral / Consolidation",
            'hurdle': 83,
            'vix': 19.5,
            '10y_yield': 4.15,
            'spx_above_ema50': True,
            'spx_ret_20d': 0.0,
            'macro_score': 11,
            'fx_thb': 33.67
        }

def analyze_fundamentals_and_valuation(ticker_obj, is_etf=False, info=None):
    if is_etf:
        return {
            'fund_score': 25,
            'val_score': 16,
            'risk_score': 9,
            'fwd_pe': 'N/A',
            'peg': 'N/A',
            'rev_growth': 'N/A'
        }
        
    if info is None:
        try:
            info = ticker_obj.info or {}
        except Exception:
            info = {}
            
    q_type = str(info.get('quoteType', '')).upper()
    if q_type == 'ETF':
        return {
            'fund_score': 25,
            'val_score': 16,
            'risk_score': 9,
            'fwd_pe': 'N/A',
            'peg': 'N/A',
            'rev_growth': 'N/A'
        }

    sec_raw = info.get('sector', 'US Equities')
    rev_growth = info.get('revenueGrowth', 0) or 0
    earnings_growth = info.get('earningsGrowth', 0) or 0
    fwd_pe = info.get('forwardPE', None)
    peg = info.get('pegRatio', None)
    total_debt = info.get('totalDebt', 0) or 0
    fcf = info.get('freeCashflow', 0) or 0
    pm = info.get('profitMargins', 0) or 0
    roe = info.get('returnOnEquity', 0) or 0
    
    # 1. Fundamental Score (Max 30) - Sector Adaptive
    fund_score = 15 # base
    if sec_raw == 'Financial Services':
        if roe > 0.15: fund_score += 8
        elif roe > 0.10: fund_score += 4
        if pm > 0.20: fund_score += 7
        elif pm > 0.10: fund_score += 4
    elif sec_raw in ['Consumer Defensive', 'Utilities', 'Healthcare']:
        if rev_growth > 0.10: fund_score += 6
        elif rev_growth > 0.04: fund_score += 4
        if earnings_growth > 0.10: fund_score += 6
        elif earnings_growth > 0.05: fund_score += 3
        if roe > 0.15: fund_score += 3
    else:
        if rev_growth > 0.20:
            fund_score += 7
        elif rev_growth > 0.10:
            fund_score += 4
        elif rev_growth > 0:
            fund_score += 2
            
        if earnings_growth > 0.20:
            fund_score += 8
        elif earnings_growth > 0.10:
            fund_score += 4
        
    fund_score = max(0, min(30, fund_score))
    
    # 2. Valuation Score (Max 20) - Sector Adaptive Benchmarks
    val_score = 10 # base
    if peg is not None and peg > 0:
        if peg < 1.5:
            val_score += 8
        elif peg <= 2.2:
            val_score += 5
        elif peg > 3.0:
            val_score -= 3
    elif fwd_pe is not None and fwd_pe > 0:
        pe_bench = 18 if sec_raw in ['Financial Services', 'Energy'] else (28 if sec_raw in ['Consumer Defensive', 'Utilities'] else 35)
        if fwd_pe < pe_bench * 0.8:
            val_score += 7 if sec_raw in ['Financial Services', 'Energy', 'Consumer Defensive', 'Utilities'] else 6
        elif fwd_pe <= pe_bench * 1.0:
            val_score += 3
        elif fwd_pe > pe_bench * 1.5:
            val_score -= 3
    val_score = max(0, min(20, val_score))
    
    # 3. Debt & Balance sheet (Max 10)
    risk_score = 7
    if sec_raw == 'Financial Services':
        risk_score = 9 if roe > 0.12 else 7
    elif fcf > 0:
        debt_fcf = total_debt / (fcf + 1e-9)
        if debt_fcf < 2.5:
            risk_score += 3
        elif debt_fcf > 5.0:
            risk_score -= 3
    risk_score = max(0, min(10, risk_score))
    
    return {
        'fund_score': fund_score,
        'val_score': val_score,
        'risk_score': risk_score,
        'fwd_pe': round(fwd_pe, 1) if fwd_pe else 'N/A',
        'peg': round(peg, 2) if peg else 'N/A',
        'rev_growth': f"{round(rev_growth*100, 1)}%" if rev_growth else 'N/A'
    }

def apply_relative_strength(tech, macro):
    """คำนวณ Relative Strength (RS Alpha 20d) เทียบดัชนี S&P 500 และปรับคะแนนเทคนิค (รุก/รับ)"""
    if not isinstance(tech, dict) or not isinstance(macro, dict):
        return tech
    if 'ret_20d' not in tech or 'spx_ret_20d' not in macro:
        return tech
    ret_20d = float(tech.get('ret_20d', 0.0))
    spx_ret = float(macro.get('spx_ret_20d', 0.0))
    rs_alpha = round(ret_20d - spx_ret, 2)
    rs_leader = bool(rs_alpha >= 2.5 and (ret_20d > 0 or spx_ret < -1.0))
    rs_laggard = bool(rs_alpha <= -5.0 and float(tech.get('rsi', 50)) > 35)
    tech['rs_alpha'] = rs_alpha
    tech['rs_leader'] = rs_leader
    tech['rs_laggard'] = rs_laggard
    raw_sc = int(tech.get('raw_tech_score', tech.get('score', 10)))
    if rs_leader:
        raw_sc += 2
    elif rs_laggard:
        raw_sc -= 2
    tech['score'] = max(0, min(20, raw_sc))
    return tech

def compute_position_size(price, stop_loss, cash_thb, portfolio_value_usd=0.0, fx_thb=33.67, regime="Neutral"):
    """คำนวณขนาดไม้ลงทุนตามกฎ 1%-2% Portfolio Risk Rule & ATR Volatility Scaling (เกมรับมืออาชีพ)"""
    fx = float(fx_thb or 33.67)
    cash = max(0.0, float(cash_thb or 0.0))
    base_tranche = cash if cash <= 1500 else round(cash * 0.40, -1)
    if price <= 0 or cash <= 0:
        return {
            'recommended_thb': base_tranche,
            'recommended_usd': round(base_tranche / fx, 2),
            'shares': 0.0,
            'stop_pct': 0.0,
            'est_loss_thb': 0.0,
            'est_loss_usd': 0.0,
            'sizing_note': f"จัดสรรไม้แรก {base_tranche:,.0f} THB"
        }
    stop_pct = max((price - stop_loss) / price, 0.01) if (stop_loss > 0 and price > stop_loss) else 0.03
    # Volatility Scaling: หากระยะ Stop กว้างเกิน 5% ให้ลดขนาดไม้ลงตามสัดส่วนความผันผวน
    vol_scale = min(1.0, 0.05 / stop_pct)
    scaled_thb = base_tranche * vol_scale

    # 1%-2% Portfolio Equity Risk Cap (ถ้ารู้มูลค่าพอร์ตรวม)
    port_thb = max(0.0, float(portfolio_value_usd or 0.0) * fx) + cash
    if "Risk-Off" in str(regime):
        risk_pct = 0.01   # ตลาด Risk-Off คุมความเสี่ยงต่อไม้ไม่เกิน 1.0% ของพอร์ต
    elif "Bull" in str(regime):
        risk_pct = 0.02   # ตลาด Bull อนุญาตความเสี่ยงต่อไม้ 2.0%
    else:
        risk_pct = 0.015  # ตลาด Neutral คุมความเสี่ยง 1.5%

    if port_thb > cash and port_thb > 5000:
        risk_budget_thb = max(port_thb * risk_pct, cash * 0.15 * stop_pct)
        risk_capped_thb = risk_budget_thb / stop_pct
        target_thb = min(base_tranche, scaled_thb, risk_capped_thb)
    else:
        target_thb = min(base_tranche, scaled_thb)

    rec_thb = round(target_thb, -1) if target_thb >= 100 else round(target_thb, 0)
    rec_thb = max(min(cash, 50.0), min(base_tranche, rec_thb))
    rec_usd = round(rec_thb / fx, 2)
    shares = round(rec_usd / price, 5)
    est_loss_thb = round(rec_thb * stop_pct, 0)
    est_loss_usd = round(rec_usd * stop_pct, 2)
    stop_pct_val = round(stop_pct * 100, 1)

    if rec_thb < base_tranche * 0.95:
        sizing_note = f"ปรับลดตาม Volatility/1% Risk: {rec_thb:,.0f} THB (≈ ${rec_usd:.2f} | ความเสี่ยงถ้าโดน Stop -฿{est_loss_thb:,.0f} / -{stop_pct_val}%)"
    else:
        sizing_note = f"จัดสรรตามเกณฑ์ความเสี่ยง: {rec_thb:,.0f} THB (≈ ${rec_usd:.2f} | ความเสี่ยงถ้าโดน Stop -฿{est_loss_thb:,.0f} / -{stop_pct_val}%)"

    return {
        'recommended_thb': rec_thb,
        'recommended_usd': rec_usd,
        'shares': shares,
        'stop_pct': stop_pct_val,
        'est_loss_thb': est_loss_thb,
        'est_loss_usd': est_loss_usd,
        'sizing_note': sizing_note
    }

def compute_entry_zone(tech):
    """Quantitative Entry Zone & Risk/Reward Calculator (ATR-based dynamic volatility buffers)"""
    price = tech.get('price', 0)
    ema20 = tech.get('ema20', price)
    ema50 = tech.get('ema50', price)
    ema200 = tech.get('ema200', price)
    ema_dist = tech.get('ema_dist', 0)
    atr = float(tech.get('atr') or max(price * 0.02, 0.5))
    fvg_zone = tech.get('fvg_zone')
    chandelier_stop = float(tech.get('chandelier_stop') or max(0.0, price - (2.5 * atr)))

    if -2.0 <= ema_dist <= 2.5:
        # Tier 1: In Sweetspot (near EMA20)
        buy_low = ema20 * 0.985
        buy_high = ema20 * 1.02
        stop_loss = max(ema50 * 0.95, price - (1.5 * atr))
        target_price = price + (2.5 * atr)
        zone_type = "ใกล้เส้น EMA20 + 🧲FVG" if tech.get('bullish_fvg') else "ใกล้เส้น EMA20"
        entry_status = "🟢 อยู่ในโซนซื้อเหมาะสม (In Sweetspot)"
        entry_advice = "ราคาอยู่ในโซนย่อตัวที่ได้เปรียบต้นทุน เข้าซื้อสะสมได้ทันที"
        playbook_note = "จังหวะย่อตัวปกติในแนวโน้มขาขึ้น ทยอยสะสมตามแผนได้"
        buy_zone_str = f"${buy_low:.2f} - ${buy_high:.2f}"
    elif ema_dist > 2.5:
        # Tier 1: Extended
        buy_low = ema20 * 0.985
        buy_high = ema20 * 1.02
        stop_loss = max(ema50 * 0.95, ema20 - (1.5 * atr))
        target_price = price + (2.0 * atr)
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
            stop_loss = max(ema50 * 0.93, price - (1.5 * atr))
            target_price = max(ema20 + (1.5 * atr), price + (2.0 * atr))
            zone_type = "แนวรับ EMA50"
            entry_status = f"🟠 พักฐานลึก ({ema_dist:.1f}%) รอตั้งรับ EMA50"
            entry_advice = f"ราคาหลุด EMA20 กลายเป็นแนวต้าน แนะนำชะลอซื้อและรอดูแรงรับที่โซน EMA50 (${buy_low:.2f} - ${buy_high:.2f})"
            playbook_note = "[ถือต่อ] มีของห้ามตกใจขาย | [ช้อนซื้อ] รอราคายืน EMA50 ได้แล้วแบ่งไม้ซื้อ | [พัก] ถือเงินสดรอความนิ่ง"
            buy_zone_str = f"${buy_low:.2f} - ${buy_high:.2f}"
        elif ema200 and ema200 > 0 and price >= ema200 * 0.97:
            # Tier 3: Testing EMA200 Long-term Support
            buy_low = ema200 * 0.985
            buy_high = ema200 * 1.015
            stop_loss = max(ema200 * 0.93, price - (1.5 * atr))
            target_price = max(ema50 + (1.5 * atr), price + (2.0 * atr))
            zone_type = "แนวรับใหญ่ EMA200"
            entry_status = f"🔴 หลุดแนวรับ EMA50 ({ema_dist:.1f}%) รอรับ EMA200"
            entry_advice = f"ราคาหลุดทั้ง EMA20 และ EMA50 แนวโน้มระยะกลางเสียทรง แนะนำชะลอซื้อ รอดูแรงรับที่แนวรับใหญ่ EMA200 (${buy_low:.2f} - ${buy_high:.2f})"
            playbook_note = "[ถือต่อ] หุ้น Core ระยะยาวถือได้ | [ช้อนซื้อ] ชะลอรับมีด รอสร้างฐานที่ EMA200 | [พัก] ถือเงินสดปลอดภัยสุด"
            buy_zone_str = f"${buy_low:.2f} - ${buy_high:.2f}"
        else:
            # Tier 4: Major breakdown below EMA200
            buy_low = price * 0.92
            buy_high = price * 0.96
            stop_loss = price - (2.0 * atr)
            # หากมีสัญญาณ Capitulation / Bullish Divergence เป้าหมายรีบาวด์คือดึงกลับหา EMA20 (2.5x ATR)
            if tech.get('absorption_climax') or tech.get('bullish_divergence') or tech.get('rsi', 50) < 32:
                target_price = price + (2.5 * atr)
            else:
                target_price = price + (2.0 * atr)
            zone_type = "ชะลอซื้อ รอสร้างฐาน"
            entry_status = "🔴 เสียทรงขาลง (Breakdown)"
            entry_advice = "ราคาหลุดแนวรับสำคัญทุกเส้น แนะนำพักการลงทุน (Wait) รอราคาหยุดลงและสร้างฐานใหม่"
            playbook_note = "พักการเข้าซื้อทันที ห้ามรับมีด ถือเงินสดรอดูสถานการณ์"
            buy_zone_str = "ชะลอซื้อ (รอฐาน)"

    # BSL Sweep Trap Warning Override
    if tech.get('bsl_sweep'):
        entry_status = "⚠️ ระวังติดดอย (เกิด BSL Sweep ชนยอดดอย ห้ามไล่ราคา)"
        entry_advice = "ตรวจพบแรงเทขายกดทับหลังแทงทะลุ High 5 วัน (Liquidity Sweep) เสี่ยงพักฐานทันที ห้ามไล่ราคาเด็ดขาด"

    risk = max(price - stop_loss, 0.01)
    reward = max(target_price - price, 0.01)
    rr_ratio = round(reward / risk, 2)

    return {
        'buy_low': buy_low,
        'buy_high': buy_high,
        'stop_loss': stop_loss,
        'target_price': target_price,
        'rr_ratio': rr_ratio,
        'atr': atr,
        'chandelier_stop': round(chandelier_stop, 2),
        'fvg_zone': fvg_zone,
        'zone_type': zone_type,
        'entry_status': entry_status,
        'entry_advice': entry_advice,
        'playbook_note': playbook_note,
        'buy_zone_str': buy_zone_str
    }

def decide_action(ticker_sym, total_score, tech, fund_val, macro, is_etf, semi_exposure=None):
    """กฎตัดสิน BUY/HOLD/WAIT ชุดเดียว ใช้ร่วมกันทั้ง scan_stocks และ discord_bot
    คืนค่า (hurdle, raw_action, final_action, override_applied, override_reason)"""
    hurdle = macro['hurdle']
    is_panic_regime = "Risk-Off" in str(macro.get('regime', ''))
    rsi = tech.get('rsi', 50)

    # 1. Core ETF Relief (ETF ดัชนีกว้างมีความปลอดภัยสูง)
    if is_etf:
        if is_panic_regime:
            # ในสภาวะ Panic ถ้า ETF Oversold (RSI < 40) ให้ปรับลด Hurdle เหลือ 74
            hurdle = 74 if rsi < 40 else 78
        else:
            hurdle = min(hurdle, 80)
    # 2. Blue Chip Quality Relief (หุ้นพื้นฐานเกรด A งบแกร่ง หนี้ต่ำ)
    elif is_panic_regime:
        if fund_val['fund_score'] >= 22 and fund_val['risk_score'] >= 8 and rsi < 32:
            hurdle = 80  # ผ่อนปรน Hurdle สำหรับของดีลดราคาจัด

    if total_score >= hurdle:
        raw_action = "BUY"
    elif total_score >= 65:
        raw_action = "HOLD"
    else:
        raw_action = "WAIT"

    final_action = raw_action
    override_applied = False
    override_reason = ""
    if raw_action != "BUY":
        return hurdle, raw_action, final_action, override_applied, override_reason

    price = tech.get('price', 0)
    ema50 = tech.get('ema50', 0)
    ema200 = tech.get('ema200', 0)

    # PORTFOLIO OVERRIDE RULE (กลุ่มชิปเกินเพดาน)
    if ticker_sym in SEMI_TICKERS and semi_exposure is not None and semi_exposure >= TRUE_SEMI_CAP:
        final_action, override_reason = "HOLD", f"กลุ่มชิปเกินเพดาน {TRUE_SEMI_CAP * 100:.0f}%"
    # BSL FAKEOUT OVERRIDE (ห้ามซื้อหุ้นที่กำลังติดกับดัก BSL Sweep)
    elif tech.get('bsl_sweep', False):
        final_action, override_reason = "WAIT", "เกิด BSL Sweep (ติดกับดักราคายอดดอย ห้ามไล่ราคา)"
    # BEARISH RSI DIVERGENCE OVERRIDE (เกมรับ: ราคาทำ High ใหม่แต่ RSI แผ่วลง ห้ามไล่ซื้อ)
    elif tech.get('bearish_divergence', False):
        final_action, override_reason = "HOLD", "เกิด Bearish RSI Divergence (ราคาทำ High ใหม่แต่โมเมนตัมแผ่ว)"
    # OVEREXTENSION FILTER (ห้ามไล่ราคาเมื่อราคาวิ่งฉีกห่าง EMA20 เกิน 8%)
    elif tech.get('ema_dist', 0) > 8.0:
        final_action, override_reason = "WAIT", f"ราคา Overextended ห่าง EMA20 +{tech.get('ema_dist', 0):.1f}% (เสี่ยงไล่ราคาดอย)"
    # RSI OVERBOUGHT FILTER (ห้ามซื้อเมื่อ RSI เข้าเขต Overbought >= 70)
    elif rsi >= 70:
        final_action, override_reason = "HOLD", f"RSI Overbought ({rsi:.1f} >= 70 เสี่ยงย่อพักฐาน)"
    # DOWNTREND DEFENSE FILTER (ห้ามรับมีดเมื่อราคาอยู่ต่ำกว่าทั้ง EMA50 และ EMA200)
    # ข้ามเมื่อไม่มีข้อมูล EMA200 (ema200 <= 0), Core ETF, Absorption Climax หรือ RSI < 28
    elif (ema200 > 0 and price < ema50 and price < ema200
          and not is_etf and not tech.get('absorption_climax', False) and rsi >= 28):
        final_action, override_reason = "WAIT", "แนวโน้มขาลง (ราคาหลุดต่ำกว่าทั้ง EMA50 และ EMA200)"
    # ASYMMETRIC R:R GATEKEEPER (เกมรับ: ไม่เข้าเทรดหุ้นรายตัวถ้า R:R ต่ำกว่า 1.2:1)
    elif not is_etf and tech.get('atr', 0) > 0:
        ent_check = compute_entry_zone(tech)
        if ent_check.get('rr_ratio', 2.0) < 1.2:
            final_action, override_reason = "HOLD", f"R:R ต่ำกว่าเกณฑ์คุ้มเสี่ยง ({ent_check['rr_ratio']:.1f}:1 < 1.2:1)"

    override_applied = final_action != raw_action
    return hurdle, raw_action, final_action, override_applied, override_reason

def main():
    global DIRECT_SEMI_EXPOSURE
    from concurrent.futures import ThreadPoolExecutor
    load_latest_portfolio()
    print("Fetching market data...")
    macro = analyze_macro()
    session = get_session()
    
    def _eval_one(ticker_sym):
        is_etf = ticker_sym in ['VOO', 'QQQM']
        t = yf.Ticker(ticker_sym, session=session) if session else yf.Ticker(ticker_sym)
        hist = t.history(period="1y")
        if hist.empty:
            return None
            
        tech = calculate_technical(hist)
        apply_relative_strength(tech, macro)
        fund_val = analyze_fundamentals_and_valuation(t, is_etf=is_etf)
        
        # Portfolio fit score (Max 5)
        # If semiconductor, penalty for high existing weight
        if ticker_sym in SEMI_TICKERS:
            portfolio_fit = 1 # penalized due to 46% concentration
        elif ticker_sym in ['VOO', 'GOOGL', 'MSFT']:
            portfolio_fit = 5 # great diversification
        else:
            portfolio_fit = 3
            
        total_score = (
            fund_val['fund_score'] + 
            fund_val['val_score'] + 
            tech.get('score', 10) + 
            macro['macro_score'] + 
            fund_val['risk_score'] + 
            portfolio_fit
        )
        
        hurdle, raw_action, final_action, override_applied, override_reason = decide_action(
            ticker_sym, total_score, tech, fund_val, macro, is_etf, semi_exposure=DIRECT_SEMI_EXPOSURE
        )

        entry_info = compute_entry_zone(tech)

        return {
            'ticker': ticker_sym,
            'price': tech.get('price', 0),
            'ema20': tech.get('ema20', 0),
            'ema50': tech.get('ema50', 0),
            'ema200': tech.get('ema200', 0),
            'rsi': tech.get('rsi', 0),
            'ema_dist': tech.get('ema_dist', 0),
            'percent_b': tech.get('percent_b', 0.5),
            'absorption_climax': tech.get('absorption_climax', False),
            'amd_sweep': tech.get('amd_sweep', False),
            'bsl_sweep': tech.get('bsl_sweep', False),
            'atr': tech.get('atr', 0.0),
            'stage2_uptrend': tech.get('stage2_uptrend', False),
            'volume_dryup': tech.get('volume_dryup', False),
            'bullish_divergence': tech.get('bullish_divergence', False),
            'bearish_divergence': tech.get('bearish_divergence', False),
            'distribution_day': tech.get('distribution_day', False),
            'ttm_squeeze': tech.get('ttm_squeeze', False),
            'bullish_fvg': tech.get('bullish_fvg', False),
            'fvg_zone': tech.get('fvg_zone'),
            'chandelier_stop': tech.get('chandelier_stop', 0.0),
            'ret_20d': tech.get('ret_20d', 0.0),
            'rs_alpha': tech.get('rs_alpha', 0.0),
            'rs_leader': tech.get('rs_leader', False),
            'rs_laggard': tech.get('rs_laggard', False),
            'entry_info': entry_info,
            'score': total_score,
            'hurdle': hurdle,
            'raw_action': raw_action,
            'final_action': final_action,
            'override': override_applied,
            'override_reason': override_reason,
            'peg': fund_val['peg'],
            'fwd_pe': fund_val['fwd_pe']
        }

    scan_tickers = list(dict.fromkeys(WATCHLIST + list(PORTFOLIO_HOLDINGS.keys())))
    with ThreadPoolExecutor(max_workers=min(len(scan_tickers), 6)) as ex:
        all_results = [r for r in ex.map(_eval_one, scan_tickers) if r is not None]
        
    watchlist_set = set(WATCHLIST)
    results = [r for r in all_results if r['ticker'] in watchlist_set]
    results.sort(key=lambda x: x['score'], reverse=True)

    # Refresh portfolio holdings with live prices from scan results
    live_price_map = {
        r['ticker']: float(r['price'])
        for r in all_results
        if r.get('price') is not None and not np.isnan(float(r['price'])) and float(r['price']) > 0
    }
    if PORTFOLIO_HOLDINGS and live_price_map:
        tot_live_val = 0.0
        exact_c = 0.0
        for sym, h in PORTFOLIO_HOLDINGS.items():
            sh = float(h.get('shares', 0))
            avg_c = float(h.get('avg_cost_usd', 0))
            cost_u = float(h.get('total_cost_usd', round(sh * avg_c, 2)))
            fallback_p = float(h.get('current_price_usd') or avg_c)
            lp = live_price_map.get(sym, fallback_p if not np.isnan(fallback_p) else avg_c)
            val_u = round(sh * lp, 2)
            pnl_u = round(val_u - cost_u, 2)
            pnl_p = round((pnl_u / cost_u) * 100, 2) if cost_u > 0 else 0.0
            h['current_price_usd'] = round(lp, 2)
            h['current_value_usd'] = val_u
            h['pnl_usd'] = pnl_u
            h['pnl_pct'] = pnl_p
            tot_live_val += val_u
            exact_c += sh * avg_c
        if tot_live_val > 0:
            for sym, h in PORTFOLIO_HOLDINGS.items():
                h['weight_pct'] = round((float(h.get('current_value_usd', 0)) / tot_live_val) * 100, 2)
            semi_live_val = sum(float(h.get('current_value_usd', 0)) for s, h in PORTFOLIO_HOLDINGS.items() if s in SEMI_TICKERS)
            DIRECT_SEMI_EXPOSURE = semi_live_val / tot_live_val
            total_c = round(exact_c, 2) if exact_c > 0 else round(sum(float(h.get('total_cost_usd', 0)) for h in PORTFOLIO_HOLDINGS.values()), 2)
            tot_pnl = round(tot_live_val - total_c, 2)
            tot_pnl_pct = round((tot_pnl / total_c * 100), 2) if total_c > 0 else 0.0
            save_refreshed_portfolio(PORTFOLIO_HOLDINGS, total_c, tot_live_val, tot_pnl, tot_pnl_pct, fx_rate_thb=macro.get('fx_thb'))

    phase = determine_scan_phase()
    now_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
    now_str = now_dt.strftime("%d/%m/%Y %H:%M")
    print(f"\n[INFO] Scan Phase: {phase.upper()} (Thailand Time: {now_dt.strftime('%H:%M น.')})")

    # Generate Report according to Phase
    if phase == "morning":
        full_report = generate_morning_report(
            results=results,
            macro=macro,
            portfolio_holdings=PORTFOLIO_HOLDINGS,
            cash_thb=PORTFOLIO_CASH_THB,
            semi_exposure=DIRECT_SEMI_EXPOSURE,
            semi_cap=TRUE_SEMI_CAP,
            now_str=now_str
        )
        toast_buys = []  # รอบเช้าไม่มีคำแนะนำซื้อ
    elif phase == "prep":
        full_report = generate_prep_report(
            results=results,
            macro=macro,
            portfolio_holdings=PORTFOLIO_HOLDINGS,
            cash_thb=PORTFOLIO_CASH_THB,
            semi_exposure=DIRECT_SEMI_EXPOSURE,
            semi_cap=TRUE_SEMI_CAP,
            now_str=now_str
        )
        prep_candidates = [r for r in results if r['score'] >= r.get('hurdle', macro['hurdle']) and not r['override']]
        toast_buys = prep_candidates[:1]
    else:  # trading (23:00 น. - เหมือนเดิม)
        full_report = generate_trading_report(
            results=results,
            macro=macro,
            portfolio_holdings=PORTFOLIO_HOLDINGS,
            cash_thb=PORTFOLIO_CASH_THB,
            semi_exposure=DIRECT_SEMI_EXPOSURE,
            semi_cap=TRUE_SEMI_CAP,
            now_str=now_str
        )
        buys = [r for r in results if r['final_action'] == 'BUY']
        toast_buys = [buys[0]] if buys else []

    print("\n" + full_report)

    # Save output to latest_report.txt
    report_path = os.path.join(os.path.dirname(__file__), "latest_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(full_report)

    # Windows Desktop Notification (Toast / Notification)
    try:
        from win_toast import send_windows_notification
        send_windows_notification(toast_buys, macro['regime'], phase=phase)
    except Exception as e:
        print(f"⚠️ Windows toast notification failed: {type(e).__name__}: {e}", file=sys.stderr)

    # Discord Notification
    try:
        from discord_notifier import send_discord_notification
        send_discord_notification(full_report, buys=toast_buys, macro=macro)
    except Exception as e:
        print(f"⚠️ Discord notification failed: {type(e).__name__}: {e}", file=sys.stderr)

def determine_scan_phase() -> str:
    """
    คืนค่า 'morning' (07:00), 'prep' (20:00), หรือ 'trading' (23:00)
    1. ตรวจจาก CLI flag: --phase=morning|prep|trading หรือ --phase morning|prep|trading
    2. ตรวจจาก Environment variable: SCAN_PHASE=morning|prep|trading
    3. ตรวจจับอัตโนมัติตามเวลาประเทศไทย (UTC+7):
       - 05:00 - 11:59 -> morning (รอบเช้า 07:00 น.)
       - 17:00 - 21:59 -> prep (รอบเตรียมการ 20:00 น.)
       - อื่นๆ (22:00 - 04:59) -> trading (รอบตลาดเทรด 23:00 น.)
    """
    for arg in sys.argv[1:]:
        if arg.startswith("--phase="):
            v = arg.split("=", 1)[1].lower().strip()
            if any(x in v for x in ("07", "morn", "sum")): return "morning"
            if any(x in v for x in ("20", "prep", "plan")): return "prep"
            if any(x in v for x in ("23", "trade", "exec")): return "trading"
    for i, arg in enumerate(sys.argv):
        if arg == "--phase" and i + 1 < len(sys.argv):
            v = sys.argv[i + 1].lower().strip()
            if any(x in v for x in ("07", "morn", "sum")): return "morning"
            if any(x in v for x in ("20", "prep", "plan")): return "prep"
            if any(x in v for x in ("23", "trade", "exec")): return "trading"

    env_v = os.getenv("SCAN_PHASE", "").lower().strip()
    if env_v:
        if any(x in env_v for x in ("07", "morn", "sum")): return "morning"
        if any(x in env_v for x in ("20", "prep", "plan")): return "prep"
        if any(x in env_v for x in ("23", "trade", "exec")): return "trading"

    now_th = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
    hour = now_th.hour

    if 5 <= hour < 12:
        return "morning"
    elif 17 <= hour < 22:
        return "prep"
    else:
        return "trading"

def format_dca_simulation(portfolio_holdings, cash_thb, fx_thb=33.67, skip_symbols=()):
    """จำลองผลหลังถัวเฉลี่ย (DCA Simulator) สำหรับหุ้นในพอร์ตที่กำลังติดลบ
    skip_symbols: หุ้นที่ติดกฎห้ามซื้อเพิ่ม (เช่น กลุ่มชิปเกินเพดาน) จะไม่ถูกแนะนำให้ถัว"""
    if not portfolio_holdings or cash_thb < 50:
        return []
    fx = float(fx_thb or 33.67)
    tranche_thb = cash_thb if cash_thb <= 1500 else round(cash_thb * 0.40, -1)
    buy_usd = round(tranche_thb / fx, 2)
    if buy_usd <= 0:
        return []
    red_items = [
        (sym, h) for sym, h in portfolio_holdings.items()
        if sym not in skip_symbols
        and float(h.get('pnl_pct', 0)) < -1.0 and float(h.get('current_price_usd', 0)) > 0
    ]
    red_items.sort(key=lambda x: float(x[1].get('pnl_pct', 0)))
    if not red_items:
        return []
    sim_lines = [f"\n🧮 จำลองผลหลังถัวเฉลี่ยด้วยเงินสด ({tranche_thb:,.0f} THB ≈ ${buy_usd:.2f} @ {fx:.2f} THB/USD):"]
    for sym, h in red_items[:4]:
        sh = float(h.get('shares', 0))
        avg_c = float(h.get('avg_cost_usd', 0))
        old_cost = float(h.get('total_cost_usd', round(sh * avg_c, 2)))
        curr_p = float(h.get('current_price_usd', avg_c))
        old_pct = float(h.get('pnl_pct', 0))
        add_sh = buy_usd / curr_p
        new_sh = sh + add_sh
        new_cost = old_cost + buy_usd
        new_avg = new_cost / new_sh if new_sh > 0 else avg_c
        new_val = new_sh * curr_p
        new_pct = ((new_val - new_cost) / new_cost * 100) if new_cost > 0 else 0.0
        sim_lines.append(
            f"  • ถัว {sym:<5} (@ ${curr_p:.2f}): ได้เพิ่ม +{add_sh:.5f} หุ้น | "
            f"ทุนเฉลี่ยลด ${avg_c:.2f} -> ${new_avg:.2f} | ขาดทุนลดจาก {old_pct:.2f}% เหลือ {new_pct:.2f}%"
        )
    return sim_lines

def generate_morning_report(results, macro, portfolio_holdings, cash_thb, semi_exposure, semi_cap, now_str):
    """07:00 น. - วิเคราะห์ตลาดและแนวโน้ม (ไม่ใช่แนะนำให้ซื้อ)"""
    lines = []
    lines.append(f"🌅 สรุปภาพรวมตลาดและแนวโน้มประจำวัน ({now_str} - ตลาดสหรัฐฯ ปิดแล้ว)")
    lines.append(f"• Macro Regime: {macro['regime']} (VIX: {macro['vix']:.1f}, 10Y: {macro['10y_yield']:.2f}%)")
    lines.append(f"• เงินสดรอจัดสรรในพอร์ต: {cash_thb:,.1f} THB")
    semi_status = "🔒 ล็อกห้ามเพิ่ม Semi" if semi_exposure >= semi_cap else "✅ ปกติ"
    lines.append(f"• สัดส่วน Semiconductor: {semi_exposure * 100:.1f}% (เพดาน {semi_cap * 100:.0f}% -> {semi_status})\n")
    lines.append("────────────────────────────────────────")

    # 1. Market Trend & Sentiment
    lines.append("🌐 วิเคราะห์ภาพรวมตลาดและบรรยากาศการลงทุน:")
    if macro['regime'] == 'Risk-On (Bull)':
        m_desc = f"ตลาดอยู่ในโหมดกระทิง (Risk-On) ดัชนีหลักยืนเหนือ EMA50 ได้มั่นคง ความผันผวน VIX อยู่ในระดับต่ำ ({macro['vix']:.1f}) สะท้อนแรงกดดันขายจำกัด ตลาดเปิดรับความเสี่ยงได้ดี"
    elif 'Neutral' in macro['regime']:
        m_desc = f"ตลาดแกว่งตัวในกรอบสะสม (Neutral/Sideway) VIX อยู่ในระดับปานกลาง ({macro['vix']:.1f}) ดัชนียังรักษาทรงหลักได้ แต่ยังต้องรอปัจจัยใหม่ชี้นำทิศทาง"
    else:
        m_desc = f"ตลาดอยู่ในโหมดระมัดระวัง (Risk-Off) หรือ VIX สูง ({macro['vix']:.1f}) บรรยากาศมีความผันผวน แนะนำเน้นความปลอดภัยและประเมินแนวรับอย่างรอบคอบ"
    lines.append(f"  • ทรงตลาดรวม: {m_desc}")
    y_status = "ทรงตัวปกติ" if macro['10y_yield'] < 4.5 else ("เริ่มตึงตัว" if macro['10y_yield'] < 5.0 else "อยู่ในโซนสูง ควรระวังหุ้น Growth Valuation ตึง")
    lines.append(f"  • ดอกเบี้ยพันธบัตร (10Y Yield): {macro['10y_yield']:.2f}% ({y_status})\n")

    # 2. Watchlist Trend Health (No Buy signals!)
    lines.append("📈 สรุปแนวโน้มเชิงเทคนิคของหุ้นใน Watchlist (Watchlist Health):")
    lines.append("  (สรุปทิศทางราคาและโมเมนตัม ไม่ใช่คำแนะนำซื้อ)")
    for r in results:
        sym = r['ticker']
        p = r['price']
        rsi = r['rsi']
        ema_dist = r['ema_dist']
        ema20 = r.get('ema20', 0)
        ema50 = r.get('ema50', 0)

        if p > ema20 > ema50:
            trend_str = "📈 ขาขึ้นแข็งแกร่ง (เกาะเหนือ EMA20/50)"
        elif p > ema50:
            trend_str = "🔄 พักฐานทดสอบ EMA20 (ใกล้แนวรับสำคัญ)"
        else:
            trend_str = "📉 ชะลอตัว/อยู่ใต้ EMA50 (รอการสร้างฐาน)"

        notes = []
        if r.get('amd_sweep'):
            notes.append("🔥AMD Rejection (แรงซื้อสวนฐานล่าง)")
        if r.get('bsl_sweep'):
            notes.append("⚠️BSL Rejection (ติดแนวต้านยอดบน)")
        if rsi < 35:
            notes.append("โซน Oversold")
        elif rsi > 70:
            notes.append("โซน Overbought")

        note_str = f" | {', '.join(notes)}" if notes else ""
        lines.append(f"  • {sym:<5}: {trend_str} | ราคา ${p:.2f} (ห่าง EMA20 {ema_dist:+.1f}%) | RSI {rsi:.1f}{note_str}")

    # 3. Personal Portfolio Status
    if portfolio_holdings:
        exact_c = sum(float(h.get('shares', 0)) * float(h.get('avg_cost_usd', 0)) for h in portfolio_holdings.values())
        total_c = round(exact_c, 2) if exact_c > 0 else sum(h.get('total_cost_usd', 0) for h in portfolio_holdings.values())
        total_v = round(sum(h.get('current_value_usd', 0) for h in portfolio_holdings.values()), 2)
        tot_pnl = round(total_v - total_c, 2)
        tot_pnl_pct = (tot_pnl / total_c * 100) if total_c > 0 else 0
        pnl_sign = "+" if tot_pnl >= 0 else ""
        lines.append("\n💼 สรุปภาพรวมพอร์ตโฟลิโอของคุณ (Dime! Portfolio):")
        lines.append(f"  • ต้นทุนรวม: ${total_c:,.2f} | มูลค่า: ${total_v:,.2f} | P/L รวม: {pnl_sign}${tot_pnl:.2f} ({pnl_sign}{tot_pnl_pct:.2f}%)")
        for sym, h in portfolio_holdings.items():
            s_pnl = h.get('pnl_usd', 0)
            s_pct = h.get('pnl_pct', 0)
            s_sign = "+" if s_pnl >= 0 else ""
            avg_c = float(h.get('avg_cost_usd', 0))
            avg_c_str = f"{avg_c:.4f}".rstrip('0').rstrip('.')
            if '.' not in avg_c_str or len(avg_c_str.split('.')[1]) < 2:
                avg_c_str = f"{avg_c:.2f}"
            tot_c_item = float(h.get('total_cost_usd', round(float(h.get('shares', 0)) * avg_c, 2)))
            sh_str = f"{float(h.get('shares', 0)):.7f}".rstrip('0').rstrip('.')
            cur_v = float(h.get('current_value_usd', 0))
            lines.append(
                f"    {sym:<5}: ทุนรวม ${tot_c_item:.2f} (ทุน/หุ้น ${avg_c_str} x {sh_str} หุ้น) | มูลค่า ${cur_v:.2f} (พอร์ต {h.get('weight_pct', 0):.1f}%) | P/L {s_sign}${s_pnl:.2f} ({s_sign}{s_pct:.2f}%)"
            )
        lines.extend(format_dca_simulation(portfolio_holdings, cash_thb, macro.get('fx_thb', 33.67), skip_symbols=SEMI_TICKERS if semi_exposure >= semi_cap else ()))

    lines.append("\n📌 หมายเหตุรอบเช้า (07:00 น.):")
    lines.append("  • ตลาดสหรัฐฯ ปิดทำการแล้ว ยังไม่มีคำสั่งซื้อในรอบนี้")
    lines.append("  • ติดตามแผนเตรียมการก่อนซื้อและจุดตั้งรับก่อนตลาดเปิดในรอบ 20:00 น.")
    return "\n".join(lines)

def generate_prep_report(results, macro, portfolio_holdings, cash_thb, semi_exposure, semi_cap, now_str):
    """20:00 น. - เตรียมการก่อนซื้อ (ก่อนเปิดตลาดสหรัฐฯ)"""
    lines = []
    lines.append(f"🎯 แผนเตรียมการก่อนเปิดตลาดสหรัฐฯ ({now_str} - Pre-Market Strategy)")
    lines.append(f"• ตลาดสหรัฐฯ เตรียมเปิดทำการ (เวลา 20:30 / 21:30 น.)")
    lines.append(f"• Macro Hurdle Score คืนนี้: {macro['hurdle']} คะแนน (สภาวะ {macro['regime']})")
    first_tranche = cash_thb if cash_thb <= 1500 else round(cash_thb * 0.40, -1)
    tranche_desc = f"จัดสรรเต็มจำนวน (ยอดน้อย): {first_tranche:,.0f} THB" if cash_thb <= 1500 else f"ขนาดไม้แรก 40%: {first_tranche:,.0f} THB"
    lines.append(f"• เงินสดรอจัดสรร: {cash_thb:,.1f} THB ({tranche_desc})")
    semi_status = "🔒 ล็อกห้ามเพิ่ม Semi" if semi_exposure >= semi_cap else "✅ ปกติ"
    lines.append(f"• สถานะ Semi Concentration: {semi_exposure * 100:.1f}% (เพดาน {semi_cap * 100:.0f}% -> {semi_status})\n")
    lines.append("────────────────────────────────────────")

    # Candidates ready for BUY
    tot_port_usd = sum(float(h.get('current_value_usd', 0)) for h in (portfolio_holdings or {}).values())
    fx_val = float(macro.get('fx_thb', 33.67))
    candidates = [r for r in results if r['score'] >= r.get('hurdle', macro['hurdle']) and not r['override']]
    lines.append("🎯 แผนเตรียมการก่อนซื้อ (Candidate Watchlist & Pre-Buy Setup):")
    if candidates:
        lines.append("  (หุ้นตัวเต็งที่ผ่านเกณฑ์ Hurdle แนะนำวางแผนตั้งรับล่วงหน้า)")
        for c in candidates:
            tags = []
            if c.get('rs_leader'): tags.append(f"👑RS+{c.get('rs_alpha', 0):+.1f}%")
            if c.get('stage2_uptrend'): tags.append("🚀Stage2")
            if c.get('volume_dryup'): tags.append("💧VDU")
            if c.get('ttm_squeeze'): tags.append("🔋Squeeze")
            if c.get('bullish_fvg'): tags.append("🧲FVG")
            if c.get('bullish_divergence'): tags.append("⚡BullDiv")
            if c.get('amd_sweep'): tags.append("🔥AMD")
            tag_str = f" [{' | '.join(tags)}]" if tags else ""
            ent = c.get('entry_info') or compute_entry_zone(c)
            pos = compute_position_size(c['price'], ent['stop_loss'], cash_thb, tot_port_usd, fx_val, macro.get('regime', 'Neutral'))
            fvg_note = f" | โซน FVG: ${ent['fvg_zone'][0]:.2f}-${ent['fvg_zone'][1]:.2f}" if ent.get('fvg_zone') else ""
            lines.append(f"  ▶ {c['ticker']}: คะแนนความพร้อม {c['score']}/100{tag_str}")
            lines.append(f"    - ราคาปิดล่าสุด: ${c['price']:.2f} | RSI: {c['rsi']:.1f} | ATR: ${c.get('atr', 0):.2f}{fvg_note}")
            if c.get('ema20', 0) > 0:
                lines.append(f"    - แนวรับสำคัญ (EMA20): ${c['ema20']:.2f} (ห่าง {c['ema_dist']:+.1f}%)")
            lines.append(f"    - กรอบราคาตั้งรับ: {ent['buy_zone_str']} ({pos['sizing_note']})")
            lines.append(f"    - จุดตัดขาดทุน (Stop Loss): ${ent['stop_loss']:.2f} | เป้าหมายกำไร: ${ent['target_price']:.2f} (R:R {ent['rr_ratio']:.1f}:1) | 🛡️Trailing Stop: ${ent.get('chandelier_stop', 0):.2f}")
    else:
        lines.append("  🟡 คืนนี้ยังไม่มีหุ้นที่คะแนนถึงเกณฑ์ Hurdle สำหรับการเข้าซื้อ")
        lines.append(f"     คำแนะนำ: เตรียมถือเงินสด (Cash Preservation) {cash_thb:,.1f} THB รอดูสัญญาณหลังตลาดเปิด")

    # Overrides / Warnings
    overrides = [r for r in results if r['override']]
    if overrides or semi_exposure >= semi_cap:
        lines.append("\n⚠️ ข้อควรระวังและกฎคุมความเสี่ยงก่อนเปิดตลาด (Pre-Market Rules):")
        for o in overrides:
            reason = o.get('override_reason', 'เงื่อนไขคุมความเสี่ยง')
            lines.append(f"  • {o['ticker']}: ติดล็อก {reason} (ห้ามไล่ราคาเด็ดขาดตอนตลาดเปิด)")
        if semi_exposure >= semi_cap:
            lines.append(f"  • กลุ่ม Semiconductor แตะเพดาน {semi_exposure*100:.1f}% (ห้ามเพิ่มน้ำหนักกลุ่มชิปคืนนี้)")

    # Scoreboard
    lines.append("\n📋 สรุปคะแนนความพร้อมก่อนเปิดตลาด (Scoreboard):")
    for r in results:
        status_tag = "BUY-Ready" if r['score'] >= r.get('hurdle', macro['hurdle']) and not r['override'] else ("OVERRIDE" if r['override'] else ("HOLD" if r['score'] >= 65 else "WAIT"))
        status_emoji = "🟢" if status_tag == "BUY-Ready" else ("⚠️" if status_tag == "OVERRIDE" else ("🟡" if status_tag == "HOLD" else "⚪"))
        badges = []
        if r.get('rs_leader'): badges.append("👑RS+")
        elif r.get('rs_laggard'): badges.append("🐌Lag")
        if r.get('stage2_uptrend'): badges.append("🚀S2")
        if r.get('volume_dryup'): badges.append("💧VDU")
        if r.get('ttm_squeeze'): badges.append("🔋Sqz")
        if r.get('bullish_fvg'): badges.append("🧲FVG")
        if r.get('bullish_divergence'): badges.append("⚡Div")
        if r.get('bearish_divergence'): badges.append("⚠️BearDiv")
        if r.get('amd_sweep'): badges.append("🔥AMD")
        if r.get('bsl_sweep'): badges.append("⚠️BSL")
        badge_str = f" {' '.join(badges)}" if badges else ""
        lines.append(
            f"  {status_emoji} {r['ticker']:<5} : {status_tag:<9} (คะแนน {r['score']:>2}/100, RSI {r['rsi']:>4.1f}{badge_str})"
        )

    if portfolio_holdings:
        lines.extend(format_dca_simulation(portfolio_holdings, cash_thb, macro.get('fx_thb', 33.67), skip_symbols=SEMI_TICKERS if semi_exposure >= semi_cap else ()))

    lines.append("\n📌 คำแนะนำรอบเตรียมการ (20:00 น.):")
    lines.append("  • ตรวจสอบราคาช่วง Pre-Market และวางแผนตั้ง Limit Order ตามกรอบแนวรับ")
    lines.append("  • บอทจะสแกนสัญญาณเทรดจริงรอบตลาดเปิด (Active Trading Session) เวลา 23:00 น.")
    return "\n".join(lines)

def generate_trading_report(results, macro, portfolio_holdings, cash_thb, semi_exposure, semi_cap, now_str):
    """23:00 น. - เหมือนเดิม (Active Trading & Execution Scan)"""
    report_lines = []
    report_lines.append(f"📊 รายงานวิเคราะห์หุ้นและแจ้งเตือนประจำวัน ({now_str})")
    report_lines.append(f"• Macro Regime: {macro['regime']} (VIX: {macro['vix']:.1f}, 10Y: {macro['10y_yield']:.2f}%)")
    report_lines.append(f"• เกณฑ์คะแนนซื้อ (Hurdle): {macro['hurdle']} คะแนน")
    semi_status = f"🔒 ล็อกห้ามเพิ่ม Semi" if semi_exposure >= semi_cap else "✅ ปกติ"
    report_lines.append(f"• สถานะ Semi Concentration: {semi_exposure * 100:.1f}% (เพดาน {semi_cap * 100:.0f}% -> {semi_status})")
    first_tranche = cash_thb if cash_thb <= 1500 else round(cash_thb * 0.40, -1)
    tranche_desc = f"จัดสรรเต็มจำนวน (ยอดน้อย): {first_tranche:,.0f} THB" if cash_thb <= 1500 else f"ขนาดไม้แรก 40%: {first_tranche:,.0f} THB"
    report_lines.append(f"• เงินสดรอจัดสรร: {cash_thb:,} THB ({tranche_desc})\n")
    report_lines.append("────────────────────────────────────────")

    buys = [r for r in results if r['final_action'] == 'BUY']
    top_buys = [buys[0]] if buys else []
    tot_port_usd = sum(float(h.get('current_value_usd', 0)) for h in (portfolio_holdings or {}).values())
    fx_val = float(macro.get('fx_thb', 33.67))
    if top_buys:
        top_b = top_buys[0]
        report_lines.append("🟢 หุ้น/ETF แนะนำซื้อที่ดีที่สุด (Top Pick) วันนี้:")
        tags = []
        if top_b.get('rs_leader'): tags.append(f"👑RS+{top_b.get('rs_alpha', 0):+.1f}%")
        if top_b.get('stage2_uptrend'): tags.append("🚀Stage2")
        if top_b.get('volume_dryup'): tags.append("💧VDU")
        if top_b.get('ttm_squeeze'): tags.append("🔋Squeeze")
        if top_b.get('bullish_fvg'): tags.append("🧲FVG")
        if top_b.get('bullish_divergence'): tags.append("⚡BullDiv")
        if top_b.get('amd_sweep'): tags.append("🔥AMD")
        tag_str = f" [{' | '.join(tags)}]" if tags else ""
        ent = top_b.get('entry_info') or compute_entry_zone(top_b)
        pos = compute_position_size(top_b['price'], ent['stop_loss'], cash_thb, tot_port_usd, fx_val, macro.get('regime', 'Neutral'))
        report_lines.append(
            f"  ▶ {top_b['ticker']}: คะแนน {top_b['score']}/100{tag_str} | ราคา ${top_b['price']:.2f} "
            f"| RSI: {top_b['rsi']:.1f} | EMA Distance: {top_b['ema_dist']:+.1f}%\n"
            f"    - ขนาดไม้แนะนำ (Risk Sizing): {pos['sizing_note']}\n"
            f"    - กรอบราคาเข้าซื้อ: {ent['buy_zone_str']} | Cut-Loss: ${ent['stop_loss']:.2f} | เป้าหมาย: ${ent['target_price']:.2f} (R:R {ent['rr_ratio']:.1f}:1) | 🛡️Trailing Stop: ${ent.get('chandelier_stop', 0):.2f}"
        )
    else:
        report_lines.append("🟡 วันนี้ยังไม่มีตัวที่เข้าเงื่อนไข BUY (แนะนำ WAIT หรือถือเงินสดรอ)")

    # Show overrides if any
    overrides = [r for r in results if r['override']]
    if overrides:
        report_lines.append("\n⚠️ โดนล็อกด้วยกฎ Portfolio Override (คะแนนถึงแต่ห้ามซื้อเพิ่ม):")
        for o in overrides:
            reason = o.get('override_reason', 'เงื่อนไขคุมความเสี่ยง')
            report_lines.append(f"  • {o['ticker']}: คะแนน {o['score']}/100 (ผล: {o['final_action']} เพราะ {reason})")

    report_lines.append("\n📋 สรุปคะแนนทุกตัวใน Watchlist:")
    for r in results:
        status_emoji = "🟢" if r['final_action'] == 'BUY' else ("🟡" if r['final_action'] == 'HOLD' else "⚪")
        badges = []
        if r.get('rs_leader'): badges.append("👑RS+")
        elif r.get('rs_laggard'): badges.append("🐌Lag")
        if r.get('stage2_uptrend'): badges.append("🚀S2")
        if r.get('volume_dryup'): badges.append("💧VDU")
        if r.get('ttm_squeeze'): badges.append("🔋Sqz")
        if r.get('bullish_fvg'): badges.append("🧲FVG")
        if r.get('bullish_divergence'): badges.append("⚡Div")
        if r.get('bearish_divergence'): badges.append("⚠️BearDiv")
        if r.get('amd_sweep'): badges.append("🔥AMD")
        if r.get('bsl_sweep'): badges.append("⚠️BSL")
        badge_str = f" {' '.join(badges)}" if badges else ""
        report_lines.append(
            f"  {status_emoji} {r['ticker']:<5} : {r['final_action']:<4} (คะแนน {r['score']:>2}/100, RSI {r['rsi']:>4.1f}{badge_str})"
        )

    # Show personal portfolio holdings if available
    if portfolio_holdings:
        exact_c = sum(float(h.get('shares', 0)) * float(h.get('avg_cost_usd', 0)) for h in portfolio_holdings.values())
        total_c = round(exact_c, 2) if exact_c > 0 else sum(h.get('total_cost_usd', 0) for h in portfolio_holdings.values())
        total_v = round(sum(h.get('current_value_usd', 0) for h in portfolio_holdings.values()), 2)
        tot_pnl = round(total_v - total_c, 2)
        tot_pnl_pct = (tot_pnl / total_c * 100) if total_c > 0 else 0
        pnl_sign = "+" if tot_pnl >= 0 else ""
        report_lines.append("\n💼 ข้อมูลต้นทุนและพอร์ตของคุณ (Dime! Portfolio):")
        report_lines.append(f"  • ต้นทุนรวม: ${total_c:,.2f} | มูลค่า: ${total_v:,.2f} | รวม: {pnl_sign}${tot_pnl:.2f} ({pnl_sign}{tot_pnl_pct:.2f}%)")
        for sym, h in portfolio_holdings.items():
            s_pnl = h.get('pnl_usd', 0)
            s_pct = h.get('pnl_pct', 0)
            s_sign = "+" if s_pnl >= 0 else ""
            avg_c = float(h.get('avg_cost_usd', 0))
            avg_c_str = f"{avg_c:.4f}".rstrip('0').rstrip('.')
            if '.' not in avg_c_str or len(avg_c_str.split('.')[1]) < 2:
                avg_c_str = f"{avg_c:.2f}"
            tot_c_item = float(h.get('total_cost_usd', round(float(h.get('shares', 0)) * avg_c, 2)))
            sh_str = f"{float(h.get('shares', 0)):.7f}".rstrip('0').rstrip('.')
            cur_v = float(h.get('current_value_usd', 0))
            report_lines.append(
                f"    {sym:<5}: ทุนรวม ${tot_c_item:.2f} (ทุน/หุ้น ${avg_c_str} x {sh_str} หุ้น) | มูลค่า ${cur_v:.2f} (พอร์ต {h.get('weight_pct', 0):.1f}%) | P/L {s_sign}${s_pnl:.2f} ({s_sign}{s_pct:.2f}%)"
            )
        report_lines.extend(format_dca_simulation(portfolio_holdings, cash_thb, macro.get('fx_thb', 33.67), skip_symbols=SEMI_TICKERS if semi_exposure >= semi_cap else ()))

    return "\n".join(report_lines)

if __name__ == '__main__':
    main()
