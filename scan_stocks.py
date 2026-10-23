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
import yfinance as yf
import pandas as pd
import numpy as np

# Config
WATCHLIST = ['VOO', 'QQQM', 'NVDA', 'TSM', 'AVGO', 'AMZN', 'GOOGL', 'MSFT']
MACRO_TICKERS = ['^GSPC', '^NDX', '^VIX', '^TNX'] # SP500, Nasdaq, VIX, 10Y Yield

# Portfolio Constraints & Holdings
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HOLDINGS_FILE = os.path.join(DATA_DIR, "portfolio_holdings.json") if os.path.exists(os.path.join(DATA_DIR, "portfolio_holdings.json")) else os.path.join(os.path.dirname(__file__), "portfolio_holdings.json")
PORTFOLIO_HOLDINGS = {}
PORTFOLIO_CASH_THB = 2320.45  # งบเงินสดคงเหลือหลังหักคำสั่งซื้อ VOO (เดิม 3,860.30 - 1,539.85 THB)
if os.path.exists(HOLDINGS_FILE):
    try:
        with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
            _h_data = json.load(f)
            PORTFOLIO_HOLDINGS = _h_data.get("holdings", {})
            _sum_cash = _h_data.get("portfolio_summary", {}).get("cash_thb")
            if _sum_cash is not None:
                PORTFOLIO_CASH_THB = float(_sum_cash)
    except Exception:
        pass

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
    if len(df) < 50:
        return {}
    close = df['Close']
    volume = df['Volume']
    
    # EMAs
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1] if len(df) >= 200 else ema50
    curr_price = close.iloc[-1]
    
    # EMA Distance
    ema_dist = ((curr_price - ema20) / ema20) * 100
    
    # RSI (14)
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
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

    # Bollinger Bands (20, 2)
    rolling_mean20 = close.rolling(window=20).mean()
    rolling_std20 = close.rolling(window=20).std()
    upper_bb = rolling_mean20.iloc[-1] + (2 * rolling_std20.iloc[-1])
    lower_bb = rolling_mean20.iloc[-1] - (2 * rolling_std20.iloc[-1])
    percent_b = (curr_price - lower_bb) / ((upper_bb - lower_bb) + 1e-9)

    # Volume Climax / Absorption Wick (ดักจับแรงซื้อสถาบันเมื่อมีแรงเทขายสะเด็ดน้ำ)
    absorption_climax = bool(rvol >= 1.5 and wick_ratio >= 0.35)

    # Score Technical (Max 20)
    score = 0
    # Trend
    if curr_price > ema20 > ema50:
        score += 8
    elif curr_price > ema50:
        score += 5
    elif curr_price > ema200:
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
        
    # 5. AMD Liquidity Sweep Bonus (+3 คะแนน - ดักจับการสับขาหลอกฝั่งขาขึ้น SSL)
    # ตรรกะ: ราคาแท่งล่าสุดจ้วงหลุด Low 5 วันก่อนหน้า (Sweep Liquidity) แล้วดึงกลับขึ้นมาปิดเหนือ Low หรือมีไส้ล่างยาว >= 35% (Rejection Hammer)
    amd_sweep = False
    if len(df) >= 6 and 'Low' in df.columns and 'High' in df.columns:
        prior_5d_low = float(df['Low'].iloc[-6:-1].min())
        if (curr_low < prior_5d_low) and ((float(curr_price) > prior_5d_low) or (wick_ratio >= 0.35)):
            amd_sweep = True
            score += 3

    # 6. BSL Liquidity Sweep Penalty (-3 คะแนน - ดักจับการกินสภาพคล่องยอดดอย ห้ามไล่ราคา)
    # ตรรกะ: ราคาแท่งล่าสุดแทงทะลุ High 5 วันก่อนหน้า แต่โดนเทขายสวนกลับลงมาปิดต่ำกว่า High เดิม หรือมีไส้บนยาว >= 35% (Shooting Star / Upper Wick Rejection)
    bsl_sweep = False
    if len(df) >= 6 and 'Low' in df.columns and 'High' in df.columns:
        prior_5d_high = float(df['High'].iloc[-6:-1].max())
        if (curr_high > prior_5d_high) and ((float(curr_price) < prior_5d_high) or (upper_wick_ratio >= 0.35)):
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
        'score': max(0, min(20, score))
    }

_shared_session = None

def get_session():
    global _shared_session
    if _shared_session is not None:
        return _shared_session
    try:
        from curl_cffi import requests
        _shared_session = requests.Session(impersonate="chrome")
        return _shared_session
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
            'macro_score': macro_score
        }
    except Exception as e:
        print(f"Warning in analyze_macro: {e}, using safe default regime")
        return {
            'regime': "Neutral / Consolidation",
            'hurdle': 83,
            'vix': 19.5,
            '10y_yield': 4.15,
            'spx_above_ema50': True,
            'macro_score': 11
        }

def analyze_fundamentals_and_valuation(ticker_obj, is_etf=False):
    if is_etf:
        return {
            'fund_score': 24,
            'val_score': 16,
            'risk_score': 9,
            'fwd_pe': 'N/A',
            'peg': 'N/A',
            'rev_growth': 'N/A'
        }
        
    info = {}
    try:
        info = ticker_obj.info or {}
    except Exception as e:
        # Fallback if Yahoo blocks quoteSummary
        info = {}
    
    rev_growth = info.get('revenueGrowth', 0) or 0
    earnings_growth = info.get('earningsGrowth', 0) or 0
    fwd_pe = info.get('forwardPE', None)
    peg = info.get('pegRatio', None)
    total_debt = info.get('totalDebt', 0) or 0
    fcf = info.get('freeCashflow', 0) or 0
    
    # 1. Fundamental Score (Max 30)
    fund_score = 15 # base
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
        
    fund_score = min(30, fund_score)
    
    # 2. Valuation Score (Max 20)
    val_score = 10 # base
    if peg is not None and peg > 0:
        if peg < 1.5:
            val_score += 8
        elif peg <= 2.2:
            val_score += 5
        elif peg > 3.0:
            val_score -= 3
    elif fwd_pe is not None:
        if fwd_pe < 25:
            val_score += 6
        elif fwd_pe <= 35:
            val_score += 3
    val_score = max(0, min(20, val_score))
    
    # 3. Debt & Balance sheet (Max 10)
    risk_score = 7
    if fcf > 0:
        debt_fcf = total_debt / (fcf + 1e-9)
        if debt_fcf < 2.0:
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

def main():
    from concurrent.futures import ThreadPoolExecutor
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
        
        # Action Determination with Dynamic Hurdle (All-Weather Adaptation)
        hurdle = macro['hurdle']
        is_panic_regime = (macro['regime'] == "Risk-Off (Correction / Caution)")
        
        # 1. Core ETF Relief (VOO, QQQM มีความปลอดภัยสูงสุด ไม่มีวันล้มละลาย)
        if is_etf:
            if is_panic_regime:
                # ในสภาวะ Panic ถ้า ETF Oversold (RSI < 40) ให้ปรับลด Hurdle เหลือ 74
                hurdle = 74 if tech.get('rsi', 50) < 40 else 78
            else:
                hurdle = min(hurdle, 80)
        # 2. Blue Chip Quality Relief (หุ้นพื้นฐานเกรด A งบแกร่ง หนี้ต่ำ)
        elif is_panic_regime:
            if fund_val['fund_score'] >= 22 and fund_val['risk_score'] >= 8 and tech.get('rsi', 50) < 32:
                hurdle = 80 # ผ่อนปรน Hurdle สำหรับของดีลดราคาจัด

        if total_score >= hurdle:
            raw_action = "BUY"
        elif total_score >= 65:
            raw_action = "HOLD"
        else:
            raw_action = "WAIT"
            
        # PORTFOLIO OVERRIDE RULE
        override_applied = False
        override_reason = ""
        final_action = raw_action
        
        if ticker_sym in SEMI_TICKERS and DIRECT_SEMI_EXPOSURE >= TRUE_SEMI_CAP:
            if raw_action == "BUY":
                final_action = "HOLD"
                override_applied = True
                override_reason = "กลุ่มชิปเกินเพดาน 45%"
                
        # BSL FAKEOUT OVERRIDE (ห้ามซื้อหุ้นที่กำลังติดกับดัก BSL Sweep)
        if tech.get('bsl_sweep', False) and final_action == "BUY":
            final_action = "WAIT"
            override_applied = True
            override_reason = "เกิด BSL Sweep (ติดกับดักราคายอดดอย ห้ามไล่ราคา)"

        # OVEREXTENSION FILTER (ห้ามไล่ราคาเมื่อราคาวิ่งฉีกห่าง EMA20 เกิน 8%)
        elif tech.get('ema_dist', 0) > 8.0 and final_action == "BUY":
            final_action = "WAIT"
            override_applied = True
            override_reason = f"ราคา Overextended ห่าง EMA20 +{tech.get('ema_dist', 0):.1f}% (เสี่ยงไล่ราคาดอย)"

        # RSI OVERBOUGHT FILTER (ห้ามซื้อเมื่อ RSI เข้าเขต Overbought >= 70)
        elif tech.get('rsi', 0) >= 70 and final_action == "BUY":
            final_action = "HOLD"
            override_applied = True
            override_reason = f"RSI Overbought ({tech.get('rsi', 0):.1f} >= 70 เสี่ยงย่อพักฐาน)"

        # DOWNTREND DEFENSE FILTER (ห้ามรับมีดเมื่อราคาอยู่ต่ำกว่าทั้ง EMA50 และ EMA200)
        elif tech.get('price', 0) < tech.get('ema50', 0) and tech.get('price', 0) < tech.get('ema200', 0) and final_action == "BUY":
            # ไม่บล็อก Core ETF เพราะจุดต่ำกว่า EMA200 คือโอกาสทองในการ DCA ระยะยาว
            if is_etf:
                pass
            # ไม่บล็อกหุ้นที่เกิด Absorption Climax ชัดเจน หรือ Oversold ลึกระดับ Capitulation (RSI < 28)
            elif tech.get('absorption_climax', False) or tech.get('rsi', 50) < 28:
                pass
            else:
                final_action = "WAIT"
                override_applied = True
                override_reason = "แนวโน้มขาลง (ราคาหลุดต่ำกว่าทั้ง EMA50 และ EMA200)"

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
            'score': total_score,
            'hurdle': hurdle,
            'raw_action': raw_action,
            'final_action': final_action,
            'override': override_applied,
            'override_reason': override_reason,
            'peg': fund_val['peg'],
            'fwd_pe': fund_val['fwd_pe']
        }

    with ThreadPoolExecutor(max_workers=min(len(WATCHLIST), 6)) as ex:
        results = [r for r in ex.map(_eval_one, WATCHLIST) if r is not None]
        
    results.sort(key=lambda x: x['score'], reverse=True)

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
        pass

    # Discord Notification
    try:
        from discord_notifier import send_discord_notification
        send_discord_notification(full_report, buys=toast_buys, macro=macro)
    except Exception as e:
        pass

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
    elif macro['regime'] == 'Neutral':
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
        total_c = sum(h.get('total_cost_usd', 0) for h in portfolio_holdings.values())
        total_v = sum(h.get('current_value_usd', 0) for h in portfolio_holdings.values())
        tot_pnl = total_v - total_c
        tot_pnl_pct = (tot_pnl / total_c * 100) if total_c > 0 else 0
        pnl_sign = "+" if tot_pnl >= 0 else ""
        lines.append("\n💼 สรุปภาพรวมพอร์ตโฟลิโอของคุณ (Dime! Portfolio):")
        lines.append(f"  • ต้นทุนรวม: ${total_c:,.2f} | มูลค่า: ${total_v:,.2f} | P/L รวม: {pnl_sign}${tot_pnl:.2f} ({pnl_sign}{tot_pnl_pct:.2f}%)")
        for sym, h in portfolio_holdings.items():
            s_pnl = h.get('pnl_usd', 0)
            s_pct = h.get('pnl_pct', 0)
            s_sign = "+" if s_pnl >= 0 else ""
            lines.append(
                f"    {sym:<5}: ทุน ${h.get('avg_cost_usd', 0):.2f} (x{h.get('shares', 0):.4f} หุ้น) | พอร์ต {h.get('weight_pct', 0):.1f}% | P/L {s_sign}${s_pnl:.2f} ({s_sign}{s_pct:.2f}%)"
            )

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
    lines.append(f"• เงินสดรอจัดสรร: {cash_thb:,.1f} THB (ขนาดไม้แรก 40%: {cash_thb * 0.40:,.0f} THB)")
    semi_status = "🔒 ล็อกห้ามเพิ่ม Semi" if semi_exposure >= semi_cap else "✅ ปกติ"
    lines.append(f"• สถานะ Semi Concentration: {semi_exposure * 100:.1f}% (เพดาน {semi_cap * 100:.0f}% -> {semi_status})\n")
    lines.append("────────────────────────────────────────")

    # Candidates ready for BUY
    candidates = [r for r in results if r['score'] >= r.get('hurdle', macro['hurdle']) and not r['override']]
    lines.append("🎯 แผนเตรียมการก่อนซื้อ (Candidate Watchlist & Pre-Buy Setup):")
    if candidates:
        lines.append("  (หุ้นตัวเต็งที่ผ่านเกณฑ์ Hurdle แนะนำวางแผนตั้งรับล่วงหน้า)")
        for c in candidates:
            amd_tag = " | 🔥 AMD Sweep (+3)" if c.get('amd_sweep') else ""
            lines.append(f"  ▶ {c['ticker']}: คะแนนความพร้อม {c['score']}/100{amd_tag}")
            lines.append(f"    - ราคาปิดล่าสุด: ${c['price']:.2f} | RSI: {c['rsi']:.1f}")
            if c.get('ema20', 0) > 0:
                lines.append(f"    - แนวรับสำคัญ (EMA20): ${c['ema20']:.2f} (ห่าง {c['ema_dist']:+.1f}%)")
            lines.append(f"    - แผนการตั้งรับ: พิจารณาตั้ง Limit Order บริเวณ ${c.get('ema20', c['price']):.2f} - ${c['price']:.2f} (ไม้แรก {cash_thb * 0.40:,.0f} THB)")
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
        amd_badge = " 🔥AMD" if r.get('amd_sweep') else ""
        bsl_badge = " ⚠️BSL" if r.get('bsl_sweep') else ""
        lines.append(
            f"  {status_emoji} {r['ticker']:<5} : {status_tag:<9} (คะแนน {r['score']:>2}/100, RSI {r['rsi']:>4.1f}{amd_badge}{bsl_badge})"
        )

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
    report_lines.append(f"• เงินสดรอจัดสรร: {cash_thb:,} THB\n")
    report_lines.append("────────────────────────────────────────")

    buys = [r for r in results if r['final_action'] == 'BUY']
    top_buys = [buys[0]] if buys else []
    if top_buys:
        top_b = top_buys[0]
        report_lines.append("🟢 หุ้น/ETF แนะนำซื้อที่ดีที่สุด (Top Pick) วันนี้:")
        amd_tag = " | 🔥 AMD Sweep (+3)" if top_b.get('amd_sweep') else ""
        report_lines.append(
            f"  ▶ {top_b['ticker']}: คะแนน {top_b['score']}/100{amd_tag} | ราคา ${top_b['price']:.2f} "
            f"| RSI: {top_b['rsi']:.1f} | EMA Distance: {top_b['ema_dist']:+.1f}% "
            f"(แนะนำจัดสรรไม้แรก: {cash_thb * 0.40:,.0f} THB)"
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
        amd_badge = " 🔥AMD" if r.get('amd_sweep') else ""
        bsl_badge = " ⚠️BSL" if r.get('bsl_sweep') else ""
        report_lines.append(
            f"  {status_emoji} {r['ticker']:<5} : {r['final_action']:<4} (คะแนน {r['score']:>2}/100, RSI {r['rsi']:>4.1f}{amd_badge}{bsl_badge})"
        )

    # Show personal portfolio holdings if available
    if portfolio_holdings:
        total_c = sum(h.get('total_cost_usd', 0) for h in portfolio_holdings.values())
        total_v = sum(h.get('current_value_usd', 0) for h in portfolio_holdings.values())
        tot_pnl = total_v - total_c
        tot_pnl_pct = (tot_pnl / total_c * 100) if total_c > 0 else 0
        pnl_sign = "+" if tot_pnl >= 0 else ""
        report_lines.append("\n💼 ข้อมูลต้นทุนและพอร์ตของคุณ (Dime! Portfolio):")
        report_lines.append(f"  • ต้นทุนรวม: ${total_c:,.2f} | มูลค่า: ${total_v:,.2f} | รวม: {pnl_sign}${tot_pnl:.2f} ({pnl_sign}{tot_pnl_pct:.2f}%)")
        for sym, h in portfolio_holdings.items():
            s_pnl = h.get('pnl_usd', 0)
            s_pct = h.get('pnl_pct', 0)
            s_sign = "+" if s_pnl >= 0 else ""
            report_lines.append(
                f"    {sym:<5}: ต้นทุน ${h.get('avg_cost_usd', 0):.2f} (x{h.get('shares', 0):.4f} หุ้น) | พอร์ต {h.get('weight_pct', 0):.1f}% | P/L {s_sign}${s_pnl:.2f} ({s_sign}{s_pct:.2f}%)"
            )

    return "\n".join(report_lines)

if __name__ == '__main__':
    main()
