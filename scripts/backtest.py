"""
Offline Multi-Year Backtester & Weight Calibrator for Stock Advisor (v2.10.0)
- Uses real 5-year OHLCV data cached locally in data/backtest_cache/ (0 ms impact on Discord bot)
- Directly imports scan_stocks.py v2.10.0 functions (100% compatible with live engine)
- Zero Look-Ahead Bias: slices strictly up to signal date t, simulates forward Stop/Target/Time exit with 0.1% slippage
- Includes Walk-Forward Split (60% In-Sample / 40% Out-of-Sample) and Anti-Survivorship-Bias universe

Usage:
    python scripts/backtest.py                  # Run 5y backtest (v2.10.0 vs Unfiltered Baseline vs VOO)
    python scripts/backtest.py --calibrate      # Also run Walk-Forward Hurdle calibration sweep
    python scripts/backtest.py --refresh        # Force re-download 5y OHLCV cache from Yahoo Finance
"""

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import numpy as np
import pandas as pd
import yfinance as yf

import scan_stocks

CACHE_DIR = os.path.join(ROOT_DIR, "data", "backtest_cache")

# Mixed universe: Core Watchlist + Big Tech + Drawdown/Laggard stress tickers (reduces survivorship bias)
UNIVERSE = [
    "VOO", "QQQM", "NVDA", "TSM", "AVGO", "AMZN", "GOOGL", "MSFT",
    "AAPL", "META", "AMD", "TSLA",
    "INTC", "PYPL", "DIS", "BABA",
]
ETF_SET = {"VOO", "QQQM"}
MACRO_SYMS = ["^GSPC", "^NDX", "^VIX", "^TNX"]

SLIPPAGE_PCT = 0.001  # 0.10% round-trip slippage / spread estimate
HOLD_BARS = 20        # Max holding window (20 trading days ≈ 1 month) if neither Stop nor Target hit


def _cache_path(sym: str) -> str:
    safe = sym.replace("^", "IDX_").replace("=", "_")
    return os.path.join(CACHE_DIR, f"{safe}.csv")


def load_history(sym: str, period: str = "5y", refresh: bool = False) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(sym)
    if not refresh and os.path.exists(path):
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            if not df.empty and "Close" in df.columns:
                return df
        except Exception:
            pass

    sess = scan_stocks.get_session()
    t = yf.Ticker(sym, session=sess) if sess else yf.Ticker(sym)
    df = t.history(period=period, interval="1d")
    if df is not None and not df.empty:
        cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
        df = df[cols].dropna(subset=["Close"])
        # Normalize timezone-naive UTC date index for clean cross-ticker alignment
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_csv(path)
        return df
    return pd.DataFrame()


def load_dataset(period: str = "5y", refresh: bool = False):
    print(f"📦 Loading {period} OHLCV dataset ({len(UNIVERSE)} stocks + {len(MACRO_SYMS)} macro)...", flush=True)
    macro_dfs = {m: load_history(m, period=period, refresh=refresh) for m in MACRO_SYMS}
    stock_dfs = {s: load_history(s, period=period, refresh=refresh) for s in UNIVERSE}
    valid_stocks = {s: df for s, df in stock_dfs.items() if len(df) >= 250}
    print(f"✅ Loaded {len(valid_stocks)}/{len(UNIVERSE)} stocks with >=250 bars.", flush=True)
    return macro_dfs, valid_stocks


def build_macro_for_date(macro_dfs: dict, dt: pd.Timestamp) -> dict:
    spx_df = macro_dfs.get("^GSPC")
    vix_df = macro_dfs.get("^VIX")
    tnx_df = macro_dfs.get("^TNX")
    ndx_df = macro_dfs.get("^NDX")

    spx_s = spx_df.loc[:dt, "Close"].dropna() if spx_df is not None and not spx_df.empty else pd.Series(dtype=float)
    vix_s = vix_df.loc[:dt, "Close"].dropna() if vix_df is not None and not vix_df.empty else pd.Series(dtype=float)
    tnx_s = tnx_df.loc[:dt, "Close"].dropna() if tnx_df is not None and not tnx_df.empty else pd.Series(dtype=float)
    ndx_s = ndx_df.loc[:dt, "Close"].dropna() if ndx_df is not None and not ndx_df.empty else pd.Series(dtype=float)

    if len(spx_s) < 50 or vix_s.empty or tnx_s.empty:
        return {
            "regime": "Neutral / Consolidation",
            "hurdle": 83,
            "vix": 19.5,
            "10y_yield": 4.15,
            "spx_above_ema50": True,
            "ndx_above_ema50": True,
            "spx_ret_20d": 0.0,
            "macro_score": 11,
        }

    spx_curr = float(spx_s.iloc[-1])
    spx_ema50 = float(spx_s.ewm(span=50, adjust=False).mean().iloc[-1])
    spx_ret_20d = float(((spx_curr / float(spx_s.iloc[-21])) - 1.0) * 100.0) if len(spx_s) >= 21 else 0.0
    vix = float(vix_s.iloc[-1])
    tnx = float(tnx_s.iloc[-1])
    ndx_curr = float(ndx_s.iloc[-1]) if len(ndx_s) >= 50 else None
    ndx_ema50 = float(ndx_s.ewm(span=50, adjust=False).mean().iloc[-1]) if len(ndx_s) >= 50 else None

    reg = scan_stocks.classify_macro_regime(spx_curr, spx_ema50, vix, tnx, ndx_curr, ndx_ema50)
    reg.update({"vix": vix, "10y_yield": tnx, "spx_ret_20d": round(spx_ret_20d, 2)})
    return reg


def simulate_trade(df: pd.DataFrame, entry_idx: int, stop_loss: float, target_price: float, max_bars: int = HOLD_BARS):
    """Simulate realistic forward exit using daily High/Low/Close (stop checked before target on same bar)."""
    entry_price = float(df["Close"].iloc[entry_idx]) * (1.0 + SLIPPAGE_PCT)
    end_idx = min(len(df) - 1, entry_idx + max_bars)
    if end_idx <= entry_idx or entry_price <= 0:
        return None

    exit_price = float(df["Close"].iloc[end_idx])
    exit_reason = "TIME"
    for j in range(entry_idx + 1, end_idx + 1):
        lo = float(df["Low"].iloc[j]) if "Low" in df.columns else float(df["Close"].iloc[j])
        hi = float(df["High"].iloc[j]) if "High" in df.columns else float(df["Close"].iloc[j])
        if stop_loss > 0 and lo <= stop_loss:
            exit_price = min(stop_loss, float(df["Open"].iloc[j])) if "Open" in df.columns else stop_loss
            exit_reason = "STOP"
            break
        if target_price > 0 and hi >= target_price:
            exit_price = target_price
            exit_reason = "TARGET"
            break

    ret_pct = ((exit_price / entry_price) - 1.0) * 100.0
    return {"ret_pct": ret_pct, "exit_reason": exit_reason}


def precompute_signals(macro_dfs: dict, stock_dfs: dict, step_bars: int = 5) -> list:
    """Precompute technical + macro + entry_zone + forward trade once per (ticker, bar) for fast calibration sweeps."""
    macro_cache = {}
    records = []
    for sym, df in stock_dfs.items():
        is_etf = sym in ETF_SET
        if is_etf:
            fv = {"fund_score": 25, "val_score": 16, "risk_score": 9, "portfolio_fit": 5, "earnings_days": None}
        else:
            fv = {"fund_score": 22, "val_score": 14, "risk_score": 8, "portfolio_fit": 4, "earnings_days": None}

        n = len(df)
        for i in range(210, n - HOLD_BARS - 1, step_bars):
            dt = df.index[i]
            if dt not in macro_cache:
                macro_cache[dt] = build_macro_for_date(macro_dfs, dt)
            macro = macro_cache[dt]

            sub_df = df.iloc[: i + 1]
            tech = scan_stocks.calculate_technical(sub_df)
            if not tech:
                continue
            scan_stocks.apply_relative_strength(tech, macro)
            ent = scan_stocks.compute_entry_zone(tech)
            sim = simulate_trade(df, i, ent["stop_loss"], ent["target_price"], max_bars=HOLD_BARS)
            if not sim:
                continue
            records.append({
                "date": dt,
                "ticker": sym,
                "is_etf": is_etf,
                "fv": fv,
                "tech": tech,
                "macro": macro,
                "sim": sim,
            })
    return records


def run_backtest_from_records(
    records: list,
    start_dt: pd.Timestamp = None,
    end_dt: pd.Timestamp = None,
    hurdle_offset: int = 0,
    use_overrides: bool = True,
    use_macro_adapt: bool = True,
) -> dict:
    """Fast evaluation over precomputed zero-look-ahead signal records."""
    trades = []
    for rec in records:
        dt = rec["date"]
        if start_dt is not None and dt < start_dt:
            continue
        if end_dt is not None and dt > end_dt:
            continue

        macro = dict(rec["macro"])
        if not use_macro_adapt:
            macro["regime"] = "Neutral / Consolidation"
            macro["hurdle"] = 80
            macro["macro_score"] = 12

        macro["hurdle"] = max(70, min(95, macro["hurdle"] + hurdle_offset))
        fv = rec["fv"]
        tech = rec["tech"]
        total_score = (
            fv["fund_score"]
            + fv["val_score"]
            + tech.get("score", 10)
            + macro["macro_score"]
            + fv["risk_score"]
            + fv["portfolio_fit"]
        )

        _, raw_action, final_action, _, _ = scan_stocks.decide_action(
            rec["ticker"], total_score, tech, fv, macro, rec["is_etf"], semi_exposure=None
        )
        chosen_action = final_action if use_overrides else raw_action
        if chosen_action != "BUY":
            continue

        trades.append({
            "date": dt,
            "ticker": rec["ticker"],
            "score": total_score,
            "regime": macro["regime"],
            **rec["sim"],
        })

    return summarize_trades(trades)


def summarize_trades(trades: list) -> dict:
    if not trades:
        return {
            "trades": 0, "win_rate": 0.0, "avg_ret": 0.0,
            "profit_factor": 0.0, "max_dd": 0.0, "cum_ret": 0.0,
            "target_rate": 0.0, "stop_rate": 0.0,
        }
    tdf = pd.DataFrame(trades).sort_values("date")
    rets = tdf["ret_pct"].values
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    win_rate = float(len(wins) / len(rets) * 100.0)
    avg_ret = float(np.mean(rets))
    gross_win = float(np.sum(wins)) if len(wins) else 0.0
    gross_loss = abs(float(np.sum(losses))) if len(losses) else 1e-9
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else 99.0

    # Equal-weight sequential equity curve (10% position size per signal to keep compounding realistic)
    eq = np.cumprod(1.0 + (rets / 100.0) * 0.10)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak * 100.0
    max_dd = float(np.min(dd))
    cum_ret = float((eq[-1] - 1.0) * 100.0)

    target_rate = float((tdf["exit_reason"] == "TARGET").mean() * 100.0)
    stop_rate = float((tdf["exit_reason"] == "STOP").mean() * 100.0)

    return {
        "trades": len(rets),
        "win_rate": round(win_rate, 1),
        "avg_ret": round(avg_ret, 2),
        "profit_factor": profit_factor,
        "max_dd": round(max_dd, 2),
        "cum_ret": round(cum_ret, 2),
        "target_rate": round(target_rate, 1),
        "stop_rate": round(stop_rate, 1),
    }


def _print_row(label: str, m: dict):
    print(
        f"  {label:<28} | Trades: {m['trades']:>4} | Win: {m['win_rate']:>5.1f}% | "
        f"Avg: {m['avg_ret']:>+6.2f}% | PF: {m['profit_factor']:>5.2f} | "
        f"MaxDD: {m['max_dd']:>6.2f}% | HitTP/SL: {m['target_rate']:>4.1f}%/{m['stop_rate']:>4.1f}%"
    )


def main():
    parser = argparse.ArgumentParser(description="Offline 5Y Backtester for Stock Advisor v2.10.0")
    parser.add_argument("--period", default="5y", help="History period (default: 5y)")
    parser.add_argument("--refresh", action="store_true", help="Force refresh Yahoo Finance cache")
    parser.add_argument("--step", type=int, default=5, help="Signal check stride in trading days (default: 5)")
    parser.add_argument("--calibrate", action="store_true", help="Run Walk-Forward Hurdle offset sweep")
    args = parser.parse_args()

    macro_dfs, stock_dfs = load_dataset(period=args.period, refresh=args.refresh)
    if "VOO" not in stock_dfs:
        print("❌ Missing VOO data; cannot run backtest.")
        sys.exit(1)

    all_dates = stock_dfs["VOO"].index[210:-HOLD_BARS]
    split_idx = int(len(all_dates) * 0.60)
    is_start, is_end = all_dates[0], all_dates[split_idx - 1]
    oos_start, oos_end = all_dates[split_idx], all_dates[-1]

    voo_is = stock_dfs["VOO"].loc[is_start:is_end, "Close"]
    voo_oos = stock_dfs["VOO"].loc[oos_start:oos_end, "Close"]
    voo_is_ret = ((float(voo_is.iloc[-1]) / float(voo_is.iloc[0])) - 1.0) * 100.0
    voo_oos_ret = ((float(voo_oos.iloc[-1]) / float(voo_oos.iloc[0])) - 1.0) * 100.0

    print("\n" + "=" * 105)
    print(f"📊 STOCK ADVISOR v2.10.0 OFFLINE BACKTEST ({args.period}, {len(stock_dfs)} tickers, 20d/TP/SL exit, 0.1% slippage)")
    print(f"   In-Sample  (60%): {is_start.date()} -> {is_end.date()} (VOO Buy&Hold: {voo_is_ret:+.1f}%)")
    print(f"   Out-of-Smp (40%): {oos_start.date()} -> {oos_end.date()} (VOO Buy&Hold: {voo_oos_ret:+.1f}%)")
    print("=" * 105)

    print("⚡ Precomputing 5y technical + macro signals (zero look-ahead)...", flush=True)
    records = precompute_signals(macro_dfs, stock_dfs, step_bars=args.step)

    for tag, s_dt, e_dt in [("IN-SAMPLE (60%)", is_start, is_end), ("OUT-OF-SAMPLE (40%)", oos_start, oos_end)]:
        print(f"\n▶ {tag}")
        m_v210 = run_backtest_from_records(records, s_dt, e_dt, use_overrides=True, use_macro_adapt=True)
        m_no_ov = run_backtest_from_records(records, s_dt, e_dt, use_overrides=False, use_macro_adapt=True)
        m_base = run_backtest_from_records(records, s_dt, e_dt, use_overrides=False, use_macro_adapt=False)
        _print_row("v2.10.0 (Full Engine)", m_v210)
        _print_row("No Overrides (Raw BUY)", m_no_ov)
        _print_row("Unfiltered Static Baseline", m_base)

    if args.calibrate:
        print("\n" + "=" * 105)
        print("🎛️ WALK-FORWARD HURDLE CALIBRATION SWEEP (offset applied to adaptive Hurdle)")
        print("=" * 105)
        best_off, best_pf = 0, -1.0
        for off in (-4, -2, 0, +2, +4):
            m_is = run_backtest_from_records(records, is_start, is_end, hurdle_offset=off)
            _print_row(f"IS  Hurdle offset {off:+d}", m_is)
            if m_is["trades"] >= 20 and m_is["profit_factor"] > best_pf:
                best_pf = m_is["profit_factor"]
                best_off = off
        print(f"\n🏆 Best In-Sample Offset: {best_off:+d} (PF {best_pf:.2f}) -> Validating on Out-of-Sample:")
        m_oos_best = run_backtest_from_records(records, oos_start, oos_end, hurdle_offset=best_off)
        _print_row(f"OOS Hurdle offset {best_off:+d}", m_oos_best)


if __name__ == "__main__":
    main()
