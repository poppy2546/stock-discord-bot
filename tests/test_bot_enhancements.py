import os
import sys
import json
import time
import asyncio
import unittest
from unittest.mock import patch, MagicMock

# Ensure we import functions from discord_bot without starting discord bot network client
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import discord_bot

class TestBotEnhancements(unittest.TestCase):
    def setUp(self):
        # Use temp or isolate alerts file for testing
        self.test_user_id = "test_user_999"
        self.test_channel_id = 123456789

    def test_ticker_cache(self):
        """Test in-memory TTL caching for ticker data"""
        sym = "TEST_TICKER"
        mock_data = {
            'ticker': sym,
            'is_etf': False,
            'tech': {'price': 150.0, 'rsi': 55.0},
            'fund_val': {'score': 80, 'is_etf': False},
            'hist': MagicMock()
        }
        
        # Manually seed cache
        now = time.time()
        with discord_bot._ticker_cache_lock:
            discord_bot._ticker_cache[sym] = {'data': mock_data, 'timestamp': now}
            
        # Calling fetch_ticker_data should return cached data immediately
        cached_res = discord_bot.fetch_ticker_data(sym, force_refresh=False, ttl_seconds=60)
        self.assertIsNotNone(cached_res)
        self.assertEqual(cached_res['tech']['price'], 150.0)
        
        # Test expired TTL with mocked yf.Ticker to avoid slow live network call
        import pandas as pd
        with patch("discord_bot.yf.Ticker") as mock_yf:
            mock_yf.return_value.history.return_value = pd.DataFrame()
            expired_res = discord_bot.fetch_ticker_data(sym, force_refresh=False, ttl_seconds=-1)
            self.assertIsNone(expired_res)

    def test_two_tier_fundamentals_and_etf_fast_path(self):
        """Test 1-hour _fund_cache reuse, ETF t.info skip, and float32 DataFrame compaction"""
        import pandas as pd
        import numpy as np

        dates = pd.date_range("2025-01-01", periods=250, freq="B")
        raw_df = pd.DataFrame({
            "Open": np.linspace(100, 150, 250, dtype=np.float64),
            "High": np.linspace(102, 155, 250, dtype=np.float64),
            "Low": np.linspace(98, 148, 250, dtype=np.float64),
            "Close": np.linspace(101, 152, 250, dtype=np.float64),
            "Volume": np.full(250, 1_000_000, dtype=np.int64),
            "Dividends": np.zeros(250),
            "Stock Splits": np.zeros(250),
        }, index=dates)

        with patch("discord_bot.yf.Ticker") as mock_yf_cls:
            mock_ticker = MagicMock()
            mock_ticker.history.return_value = raw_df
            mock_ticker.info = {
                "quoteType": "EQUITY",
                "returnOnEquity": 0.25,
                "revenueGrowth": 0.20,
                "freeCashflow": 10_000_000,
                "trailingPE": 22.0,
                "forwardPE": 18.0,
            }
            mock_yf_cls.return_value = mock_ticker

            # 1. ETF in ETF_SYMBOLS should NOT access t.info at all
            discord_bot._ticker_cache.pop("VOO", None)
            discord_bot._fund_cache.pop("VOO", None)
            res_etf = discord_bot.fetch_ticker_data("VOO", force_refresh=True)
            self.assertIsNotNone(res_etf)
            self.assertTrue(res_etf["is_etf"])
            # Compacted DataFrame checks: only 5 columns and float32
            self.assertEqual(list(res_etf["hist"].columns), ["Open", "High", "Low", "Close", "Volume"])
            self.assertEqual(res_etf["hist"]["Close"].dtype, np.float32)

            # 2. Equity should cache fundamentals in _fund_cache so second fetch with force_refresh=True reuses _fund_cache
            sym_eq = "MOCK_EQ"
            discord_bot._ticker_cache.pop(sym_eq, None)
            discord_bot._fund_cache.pop(sym_eq, None)
            res_eq1 = discord_bot.fetch_ticker_data(sym_eq, force_refresh=True)
            self.assertIsNotNone(res_eq1)
            self.assertIn(sym_eq, discord_bot._fund_cache)

            # Replace .info with a property that raises if accessed again
            type(mock_ticker).info = property(lambda self: (_ for _ in ()).throw(AssertionError("t.info should not be called when _fund_cache is warm")))
            res_eq2 = discord_bot.fetch_ticker_data(sym_eq, force_refresh=True)
            self.assertIsNotNone(res_eq2)
            self.assertEqual(res_eq2["fund_val"]["fund_score"], res_eq1["fund_val"]["fund_score"])

    def test_bounded_cache_eviction(self):
        """Test _evict_oldest keeps cache size within max_items"""
        sample_cache = {f"K{i}": {"timestamp": float(i), "data": i} for i in range(10)}
        discord_bot._evict_oldest(sample_cache, 6)
        self.assertLessEqual(len(sample_cache), 6)
        # Oldest keys (K0..K3) should be evicted, newest (K9) retained
        self.assertNotIn("K0", sample_cache)
        self.assertIn("K9", sample_cache)

    def test_fast_path_ticker_validation_and_clean_ticker(self):
        """Test precompiled clean_ticker and 0-network fast path in is_valid_ticker"""
        self.assertEqual(discord_bot.clean_ticker("$nvda"), "NVDA")
        self.assertEqual(discord_bot.clean_ticker("BRK.B"), "BRK-B")

        with patch("discord_bot.yf.Ticker") as mock_yf:
            # Popular ticker & ETF should validate immediately without calling yf.Ticker
            self.assertTrue(discord_bot.is_valid_ticker("NVDA"))
            self.assertTrue(discord_bot.is_valid_ticker("QQQM"))
            mock_yf.assert_not_called()

    def test_cached_history_slicing_in_price_and_history_embeds(self):
        """Test build_price_embed, build_history_embed, and get_cached_price use _ticker_cache with zero extra HTTP calls"""
        import pandas as pd
        import numpy as np

        sym = "CACHED_SYM"
        dates = pd.date_range("2025-01-01", periods=252, freq="B")
        hist_df = pd.DataFrame({
            "Open": np.linspace(100, 200, 252, dtype=np.float32),
            "High": np.linspace(105, 205, 252, dtype=np.float32),
            "Low": np.linspace(95, 195, 252, dtype=np.float32),
            "Close": np.linspace(100, 200, 252, dtype=np.float32),
            "Volume": np.full(252, 2_000_000, dtype=np.float32),
        }, index=dates)

        with discord_bot._ticker_cache_lock:
            discord_bot._ticker_cache[sym] = {
                "timestamp": time.time(),
                "data": {
                    "ticker": sym,
                    "is_etf": False,
                    "tech": {
                        "price": 200.0, "rsi": 60.0, "ema20": 190.0, "ema50": 180.0,
                        "ema200": 150.0, "atr": 4.5, "high_52w": 205.0, "low_52w": 95.0,
                        "pct_from_high": -2.4, "rvol": 1.2, "ret_1m": 5.0, "ret_3m": 12.0,
                        "score": 85.0, "trend_pts": 40, "mom_pts": 30, "vol_pts": 15,
                        "ema_dist": 5.2, "vol_surge": False,
                    },
                    "fund_val": {"score": 80, "is_etf": False},
                    "hist": hist_df,
                },
            }

        with patch("discord_bot.yf.Ticker") as mock_yf:
            self.assertEqual(discord_bot.get_cached_price(sym), 200.0)
            price_embed = discord_bot.build_price_embed(sym)
            self.assertIn(sym, price_embed.title)
            for period in ("5d", "1mo", "3mo", "6mo", "1y"):
                hist_embed = discord_bot.build_history_embed(sym, period)
                self.assertIn(sym, hist_embed.title)
            mock_yf.assert_not_called()

    def test_shared_session_singleton(self):
        """Verify scan_stocks.get_session() reuses a single session object"""
        import scan_stocks
        s1 = scan_stocks.get_session()
        s2 = scan_stocks.get_session()
        self.assertIs(s1, s2)

    def test_portfolio_parsing_and_embed(self):
        """Test loading portfolio_holdings.json and embed builder"""
        portfolio_data = discord_bot.load_portfolio_data()
        self.assertIsNotNone(portfolio_data, "portfolio_holdings.json should be present and valid JSON")
        self.assertIn("holdings", portfolio_data)
        self.assertIn("portfolio_summary", portfolio_data)
        
        # Build embed with refresh_live_prices=False (fallback to file prices for fast deterministic test)
        embed = discord_bot.build_portfolio_embed(refresh_live_prices=False)
        self.assertIsNotNone(embed)
        self.assertIn("สรุปภาพรวมพอร์ตการลงทุน", embed.title)
        
        # Check field names
        field_names = [f.name for f in embed.fields]
        self.assertTrue(any("มูลค่าพอร์ตรวม" in name for name in field_names))
        self.assertTrue(any("กำไร/ขาดทุนรวม" in name for name in field_names))
        self.assertTrue(any("รายละเอียดสินทรัพย์" in name for name in field_names))

    def test_price_alert_lifecycle(self):
        """Test adding, listing, matching, and removing price alerts"""
        ticker = "NVDA"
        curr_price = 200.0
        
        # 1. Add alert target >= 220 (condition: gte)
        ok, msg = discord_bot.add_user_alert(
            user_id_str=self.test_user_id,
            channel_id=self.test_channel_id,
            ticker=ticker,
            target_price=220.0,
            current_price=curr_price
        )
        self.assertTrue(ok)
        self.assertIn("ตั้งแจ้งเตือน", msg)
        
        # 2. Duplicate alert check
        ok2, msg2 = discord_bot.add_user_alert(
            user_id_str=self.test_user_id,
            channel_id=self.test_channel_id,
            ticker=ticker,
            target_price=220.0,
            current_price=curr_price
        )
        self.assertFalse(ok2, "Should reject duplicate alert")
        
        # 3. Add alert target <= 180 (condition: lte)
        ok3, _ = discord_bot.add_user_alert(
            user_id_str=self.test_user_id,
            channel_id=self.test_channel_id,
            ticker=ticker,
            target_price=180.0,
            current_price=curr_price
        )
        self.assertTrue(ok3)
        
        # 4. List alerts
        alerts = discord_bot.load_user_alerts()
        user_alerts = alerts.get(self.test_user_id, [])
        self.assertEqual(len(user_alerts), 2)
        
        embed = discord_bot.build_alerts_embed("Tester", self.test_user_id)
        self.assertIn("รายการแจ้งเตือนที่กำลังเปิดใช้งาน", embed.description)
        
        # 5. Remove alerts
        deleted_count = discord_bot.remove_user_alert(self.test_user_id, ticker)
        self.assertEqual(deleted_count, 2)
        
        alerts_after = discord_bot.load_user_alerts()
        self.assertEqual(len(alerts_after.get(self.test_user_id, [])), 0)

    def test_alert_condition_matching(self):
        """Test trigger matching logic for GTE and LTE"""
        # GTE condition
        cond_gte = "gte"
        target_gte = 250.0
        self.assertTrue(250.0 >= target_gte if cond_gte == "gte" else 250.0 <= target_gte)
        self.assertTrue(255.0 >= target_gte if cond_gte == "gte" else 255.0 <= target_gte)
        self.assertFalse(245.0 >= target_gte if cond_gte == "gte" else 245.0 <= target_gte)
        
        # LTE condition
        cond_lte = "lte"
        target_lte = 180.0
        self.assertTrue(180.0 <= target_lte if cond_lte == "lte" else 180.0 >= target_lte)
        self.assertTrue(175.0 <= target_lte if cond_lte == "lte" else 175.0 >= target_lte)
        self.assertFalse(185.0 <= target_lte if cond_lte == "lte" else 185.0 >= target_lte)

    def test_is_portfolio_owner(self):
        """Test portfolio ownership resolution"""
        class DummyUser:
            def __init__(self, uid):
                self.id = uid

        # 1. Test None user
        self.assertFalse(asyncio.run(discord_bot.is_portfolio_owner(None)))

        # 2. Test with DISCORD_OWNER_ID set
        discord_bot.DISCORD_OWNER_ID = 123456789
        self.assertTrue(asyncio.run(discord_bot.is_portfolio_owner(DummyUser(123456789))))
        self.assertFalse(asyncio.run(discord_bot.is_portfolio_owner(DummyUser(999999999))))
        discord_bot.DISCORD_OWNER_ID = None

    def test_user_portfolio_operations(self):
        """Test user portfolio add, get, remove, clear"""
        test_uid = "unit_test_user_888"
        # Clean start
        discord_bot.clear_user_portfolio(test_uid)
        
        # Add holdings
        discord_bot.add_user_portfolio_holding(test_uid, "NVDA", 2.0, 150.0, "Semiconductor")
        discord_bot.add_user_portfolio_holding(test_uid, "VOO", 1.0, 500.0, "Core ETF")
        
        # Verify get
        p_data = discord_bot.get_user_portfolio_data(test_uid)
        self.assertIsNotNone(p_data)
        self.assertIn("NVDA", p_data["holdings"])
        self.assertEqual(p_data["holdings"]["NVDA"]["shares"], 2.0)
        self.assertEqual(p_data["holdings"]["NVDA"]["avg_cost_usd"], 150.0)
        self.assertIn("VOO", p_data["holdings"])
        
        # Build embed for this user
        embed = discord_bot.build_portfolio_embed(test_uid, "Tester", refresh_live_prices=False)
        self.assertIsNotNone(embed)
        self.assertIn("Tester's Portfolio", embed.title)
        
        # Remove holding
        discord_bot.remove_user_portfolio_holding(test_uid, "NVDA")
        p_after = discord_bot.get_user_portfolio_data(test_uid)
        self.assertNotIn("NVDA", p_after["holdings"])
        self.assertIn("VOO", p_after["holdings"])
        
        # Clear
        discord_bot.clear_user_portfolio(test_uid)
        p_cleared = discord_bot.get_user_portfolio_data(test_uid)
        self.assertEqual(len(p_cleared["holdings"]), 0)

        # Clean up test user key completely so production JSON stays clean
        ports = discord_bot.load_user_portfolios()
        ports.pop(test_uid, None)
        discord_bot.save_user_portfolios(ports)

    def test_persistent_views_and_supabase_sync(self):
        """Test that all interactive views are persistent (timeout=None, custom_id set) and Supabase sync works"""
        async def _check_views():
            views = [
                discord_bot.WatchlistView(),
                discord_bot.ScanView(),
                discord_bot.PortfolioView(),
                discord_bot.AlertsView(),
                discord_bot.CheckView(),
                discord_bot.PriceView(),
                discord_bot.HistoryView(),
                discord_bot.StatusView(),
            ]
            for v in views:
                self.assertIsNone(v.timeout)
                self.assertTrue(v.is_persistent())
                for item in v.children:
                    self.assertTrue(bool(getattr(item, "custom_id", None)))

        asyncio.run(_check_views())

        # Test Supabase restore & upsert with mocked urllib.request.urlopen
        with patch.object(discord_bot, "_SUPABASE_ENABLED", True), \
             patch.object(discord_bot, "SUPABASE_URL", "https://example.supabase.co"), \
             patch.object(discord_bot, "SUPABASE_KEY", "test-key"), \
             patch("discord_bot.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps([
                {
                    "user_id": "friend_123",
                    "watchlist": ["NVDA", "QQQM"],
                    "portfolio": {"holdings": {"QQQM": {"shares": 1.5, "avg_cost_usd": 200.0, "total_cost_usd": 300.0, "category": "Core ETF"}}},
                    "alerts": [],
                }
            ]).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            ok = discord_bot.restore_user_data_from_supabase()
            self.assertTrue(ok)
            self.assertEqual(discord_bot.get_user_symbols("friend_123"), ["NVDA", "QQQM"])
            self.assertIn("QQQM", discord_bot.get_user_portfolio_data("friend_123")["holdings"])

            # Clean up friend_123 from local files
            wl = discord_bot.load_user_watchlists()
            wl.pop("friend_123", None)
            discord_bot._write_json_local_only(discord_bot.USER_WATCHLIST_FILE, wl)
            pf = discord_bot.load_user_portfolios()
            pf.pop("friend_123", None)
            discord_bot._write_json_local_only(discord_bot.USER_PORTFOLIO_FILE, pf)

    def test_compute_entry_zone_dynamic_tiers(self):
        """Test multi-tier support logic when stock suffers deep pullback or breakdown"""
        # 1. Normal Uptrend (Sweetspot)
        tech_sweetspot = {'price': 100.0, 'ema20': 100.0, 'ema50': 95.0, 'ema200': 85.0, 'ema_dist': 0.0}
        ent1 = discord_bot.compute_entry_zone(tech_sweetspot)
        self.assertIn("In Sweetspot", ent1['entry_status'])
        self.assertIn("EMA20", ent1['zone_type'])
        self.assertEqual(ent1['buy_zone_str'], "$98.50 - $102.00")

        # 2. Deep Pullback below EMA20, holding above EMA50
        tech_deep_ema50 = {'price': 96.0, 'ema20': 102.0, 'ema50': 95.0, 'ema200': 85.0, 'ema_dist': -5.88}
        ent2 = discord_bot.compute_entry_zone(tech_deep_ema50)
        self.assertIn("รอตั้งรับ EMA50", ent2['entry_status'])
        self.assertIn("EMA50", ent2['zone_type'])
        # Buy zone should be around EMA50 ($93.58 - $96.42), NOT above price ($100+)
        self.assertTrue(ent2['buy_high'] <= 97.0)

        # 3. Breakdown below EMA50, holding above EMA200
        tech_deep_ema200 = {'price': 88.0, 'ema20': 105.0, 'ema50': 98.0, 'ema200': 86.0, 'ema_dist': -16.19}
        ent3 = discord_bot.compute_entry_zone(tech_deep_ema200)
        self.assertIn("EMA200", ent3['zone_type'])
        self.assertIn("รอรับ EMA200", ent3['entry_status'])
        self.assertTrue(ent3['buy_high'] <= 88.0)

        # 4. Major breakdown below EMA200
        tech_breakdown = {'price': 60.0, 'ema20': 100.0, 'ema50': 90.0, 'ema200': 80.0, 'ema_dist': -40.0}
        ent4 = discord_bot.compute_entry_zone(tech_breakdown)
        self.assertIn("ชะลอซื้อ", ent4['buy_zone_str'])

    def test_auto_watchdog_and_status(self):
        """Test process memory reading and build_status_embed"""
        mem_mb = discord_bot.get_process_memory_mb()
        self.assertIsInstance(mem_mb, float)
        self.assertGreaterEqual(mem_mb, 0.0)

        embed = discord_bot.build_status_embed()
        self.assertIsNotNone(embed)
        self.assertIn("System Status", embed.title)
        field_names = [f.name for f in embed.fields]
        self.assertTrue(any("Uptime" in f for f in field_names))
        self.assertTrue(any("RAM" in f for f in field_names))
        self.assertTrue(any("รีสตาร์ต" in f for f in field_names))

    def test_nan_close_handling_and_newer_local_portfolio_preservation(self):
        """Ensure trailing NaN row in Yahoo Finance does not produce NaN price, and Supabase restore does not overwrite newer local portfolio"""
        import pandas as pd
        import numpy as np
        import math
        import scan_stocks

        dates = pd.date_range("2026-01-01", periods=65, freq="B")
        closes = list(np.linspace(100.0, 130.0, 64)) + [np.nan]
        df = pd.DataFrame({
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [100000.0] * 64 + [np.nan],
        }, index=dates)
        tech = scan_stocks.calculate_technical(df)
        self.assertIn("price", tech)
        self.assertFalse(math.isnan(tech["price"]))
        self.assertAlmostEqual(tech["price"], 130.0, places=2)
        self.assertFalse(math.isnan(tech["ema_dist"]))

        # Test in-place DCA buy mode in add_user_portfolio_holding
        test_uid = "unit_test_user_888"
        discord_bot.clear_user_portfolio(test_uid)
        discord_bot.add_user_portfolio_holding(test_uid, "GOOGL", 1.0, 300.0, "Big Tech", mode="set")
        discord_bot.add_user_portfolio_holding(test_uid, "GOOGL", 1.0, 200.0, "Big Tech", mode="buy", spend_thb=6700.0)
        p_buy = discord_bot.get_user_portfolio_data(test_uid)
        self.assertAlmostEqual(p_buy["holdings"]["GOOGL"]["shares"], 2.0, places=5)
        self.assertAlmostEqual(p_buy["holdings"]["GOOGL"]["avg_cost_usd"], 250.0, places=2)
        self.assertAlmostEqual(p_buy["holdings"]["GOOGL"]["total_cost_usd"], 500.0, places=2)
        ports = discord_bot.load_user_portfolios()
        ports.pop(test_uid, None)
        discord_bot.save_user_portfolios(ports)

        # Test format_dca_simulation
        sim = scan_stocks.format_dca_simulation(
            {"GOOGL": {"shares": 0.67, "avg_cost_usd": 333.74, "total_cost_usd": 223.64, "current_price_usd": 279.84, "pnl_pct": -16.15}},
            788.07,
            33.67
        )
        self.assertTrue(len(sim) >= 2)
        self.assertIn("GOOGL", sim[1])

class TestLogicFixes(unittest.TestCase):
    """Regression tests สำหรับบั๊กที่แก้ไข (sweep, RSI, EMA200, regime, privacy, shared rules)"""

    def _df(self, opens, highs, lows, closes, n=60):
        import pandas as pd
        dates = pd.date_range("2026-01-01", periods=n, freq="D")
        return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1000] * n}, index=dates)

    def test_amd_sweep_requires_close_above_prior_low(self):
        import scan_stocks
        # ทะลุ low เดิม (100) แล้วปิดใต้ low ที่ 90 แม้มีไส้ล่างยาว -> Breakdown ไม่ใช่ sweep
        df = self._df([102.0]*59 + [95.0], [105.0]*59 + [96.0], [100.0]*59 + [85.0], [100.0]*59 + [90.0])
        self.assertFalse(scan_stocks.calculate_technical(df)["amd_sweep"])
        # ทะลุ low เดิมแล้วดึงกลับมาปิดเหนือ low -> sweep จริง
        df2 = self._df([102.0]*59 + [101.0], [105.0]*59 + [103.0], [100.0]*59 + [97.0], [101.0]*59 + [102.0])
        self.assertTrue(scan_stocks.calculate_technical(df2)["amd_sweep"])

    def test_bsl_sweep_ignores_strong_breakout(self):
        import scan_stocks
        # Breakout ปิด 117 เหนือ high เดิม 102 แม้มีไส้บน -> ไม่ใช่กับดัก
        df = self._df([100.0]*59 + [105.0], [102.0]*59 + [125.0], [98.0]*59 + [104.0], [100.0]*59 + [117.0])
        self.assertFalse(scan_stocks.calculate_technical(df)["bsl_sweep"])
        # แทงทะลุแล้วปิดกลับใต้ high เดิม -> กับดัก
        df2 = self._df([100.0]*59 + [101.0], [102.0]*59 + [106.0], [98.0]*59 + [99.0], [100.0]*59 + [100.5])
        self.assertTrue(scan_stocks.calculate_technical(df2)["bsl_sweep"])

    def test_rsi_uses_wilder_and_short_history_has_no_ema200(self):
        import scan_stocks
        import numpy as np
        closes = list(np.linspace(100, 130, 60))
        tech = scan_stocks.calculate_technical(self._df(closes, closes, closes, closes))
        self.assertGreater(tech["rsi"], 90)  # ขึ้นล้วนต้อง RSI สูงมาก
        self.assertEqual(tech["ema200"], 0.0)

    def test_decide_action_downtrend_skipped_without_ema200(self):
        import scan_stocks
        macro = {"hurdle": 80, "regime": "Risk-On (Bull)"}
        fv = {"fund_score": 25, "risk_score": 9}
        tech = {"price": 90, "ema50": 100, "ema200": 0.0, "rsi": 45, "ema_dist": -3}
        _, raw, final, ov, _ = scan_stocks.decide_action("XYZ", 85, tech, fv, macro, False)
        self.assertEqual((raw, final, ov), ("BUY", "BUY", False))
        tech["ema200"] = 110
        _, _, final, ov, _ = scan_stocks.decide_action("XYZ", 85, tech, fv, macro, False)
        self.assertEqual((final, ov), ("WAIT", True))

    def test_discord_bot_shares_rules_with_scanner(self):
        # ETF ใน Risk-Off + RSI < 40 ต้องได้ hurdle 74 เหมือน scan_stocks
        macro = {"hurdle": 88, "regime": "Risk-Off (Correction / Caution)", "macro_score": 7}
        fv = discord_bot.analyze_all_around_fundamentals(None, is_etf=True)
        tech = {"price": 100, "ema20": 102, "ema50": 101, "ema200": 95, "ema_dist": -2, "rsi": 35, "score": 17}
        ev = discord_bot.evaluate_stock({"ticker": "VOO", "tech": tech, "fund_val": fv}, macro)
        self.assertEqual(ev["score"], 25 + 16 + 17 + 7 + 9 + 5)
        self.assertEqual(ev["final_action"], "BUY")

    def test_morning_report_neutral_regime(self):
        import scan_stocks
        macro = {"regime": "Neutral / Consolidation", "hurdle": 83, "vix": 19.5, "10y_yield": 4.1, "macro_score": 11, "fx_thb": 33.67}
        rep = scan_stocks.generate_morning_report([], macro, {}, 1000, 0.2, 0.45, "now")
        self.assertIn("Neutral/Sideway", rep)
        self.assertNotIn("Risk-Off", rep)

    def test_webhook_strips_all_private_sections(self):
        import discord_notifier
        for header in ("💼 สรุปภาพรวมพอร์ตโฟลิโอของคุณ (Dime!):", "💼 ข้อมูลต้นทุนและพอร์ตของคุณ (Dime!):", "🧮 จำลองผลหลังถัวเฉลี่ย (x):"):
            txt = f"Public\n{header}\nSECRET $123\n\n📌 หมายเหตุ\n  • note"
            out = discord_notifier._strip_private_portfolio(txt)
            self.assertNotIn("SECRET", out)
            self.assertIn("Public", out)
            self.assertIn("📌 หมายเหตุ", out)

    def test_dca_sim_skips_locked_semi(self):
        import scan_stocks
        h = {"NVDA": {"shares": 1, "avg_cost_usd": 200, "total_cost_usd": 200, "current_price_usd": 150, "pnl_pct": -25},
             "MSFT": {"shares": 1, "avg_cost_usd": 400, "total_cost_usd": 400, "current_price_usd": 350, "pnl_pct": -12.5}}
        sim = "\n".join(scan_stocks.format_dca_simulation(h, 1000, 33.67, skip_symbols=scan_stocks.SEMI_TICKERS))
        self.assertIn("MSFT", sim)
        self.assertNotIn("NVDA", sim)

    def test_quant_enhancements_atr_vdu_and_rr(self):
        import scan_stocks
        import pandas as pd
        import numpy as np

        # 1. Test ATR and Volume Dry-Up
        dates = pd.date_range("2026-01-01", periods=60, freq="B")
        close = pd.Series(np.linspace(100, 120, 60), index=dates)
        # Pullback near EMA20 with low volume (VDU)
        close.iloc[-1] = close.iloc[-2] * 0.995
        vol = [1_000_000] * 59 + [400_000]  # RVOL ~ 0.4
        df = pd.DataFrame({"Open": close, "High": close + 2.0, "Low": close - 2.0, "Close": close, "Volume": vol}, index=dates)

        tech = scan_stocks.calculate_technical(df)
        self.assertIn("atr", tech)
        self.assertGreater(tech["atr"], 0.0)
        self.assertTrue(tech.get("volume_dryup"))

        # 2. Test compute_entry_zone returns dynamic stop loss, target and R:R ratio
        ent = scan_stocks.compute_entry_zone(tech)
        self.assertIn("target_price", ent)
        self.assertIn("rr_ratio", ent)
        self.assertGreater(ent["target_price"], tech["price"])
        self.assertLess(ent["stop_loss"], tech["price"])
        self.assertGreater(ent["rr_ratio"], 1.0)
        self.assertGreater(ent.get("chandelier_stop", 0.0), 0.0)

    def test_offensive_and_defensive_pro_techniques(self):
        import scan_stocks
        import pandas as pd
        import numpy as np

        # 1. Relative Strength (RS Leader vs RS Laggard)
        tech_leader = {"raw_tech_score": 15, "score": 15, "ret_20d": 8.0, "rsi": 55.0}
        macro = {"spx_ret_20d": 2.0, "regime": "Risk-On (Bull)", "hurdle": 80}
        scan_stocks.apply_relative_strength(tech_leader, macro)
        self.assertTrue(tech_leader["rs_leader"])
        self.assertFalse(tech_leader["rs_laggard"])
        self.assertEqual(tech_leader["score"], 17)

        tech_lag = {"raw_tech_score": 15, "score": 15, "ret_20d": -4.5, "rsi": 48.0}
        scan_stocks.apply_relative_strength(tech_lag, macro)
        self.assertTrue(tech_lag["rs_laggard"])
        self.assertFalse(tech_lag["rs_leader"])
        self.assertEqual(tech_lag["score"], 13)

        # 2. Bullish FVG detection (3-candle gap that holds)
        dates = pd.date_range("2026-01-01", periods=60, freq="B")
        closes = np.linspace(100, 110, 60)
        highs = closes + 0.5
        lows = closes - 0.5
        # Create Bullish FVG at -3 (idx-2=-5, idx=-3): low[-3] > high[-5] * 1.002
        highs[-5] = 108.0
        lows[-4] = 108.2
        closes[-4] = 111.0
        highs[-4] = 111.5
        lows[-3] = 109.0  # gap [108.0, 109.0]
        closes[-3] = 110.5
        lows[-2] = 109.2
        closes[-2] = 109.5
        lows[-1] = 108.5
        closes[-1] = 109.2  # retesting right near top of FVG (109.0)
        df_fvg = pd.DataFrame({
            "Open": closes - 0.2, "High": highs, "Low": lows, "Close": closes, "Volume": [1_000_000] * 60
        }, index=dates)
        t_fvg = scan_stocks.calculate_technical(df_fvg)
        self.assertTrue(t_fvg["bullish_fvg"])
        self.assertIsNotNone(t_fvg["fvg_zone"])

        # 3. Bearish Divergence override in decide_action
        fv = {"fund_score": 26, "risk_score": 9}
        tech_bdiv = {
            "price": 120.0, "ema20": 118.0, "ema50": 112.0, "ema200": 100.0,
            "rsi": 65.0, "ema_dist": 1.7, "atr": 2.0, "bearish_divergence": True
        }
        _, raw_act, final_act, ov, reason = scan_stocks.decide_action("NVDA", 86, tech_bdiv, fv, macro, False)
        self.assertEqual(raw_act, "BUY")
        self.assertEqual(final_act, "HOLD")
        self.assertTrue(ov)
        self.assertIn("Bearish RSI Divergence", reason)

        # 4. 1%-2% Risk & Volatility Position Sizing
        # Wide stop loss (15% distance: entry 100, stop 85) should scale down position size
        sizing_wide = scan_stocks.compute_position_size(
            100.0, 85.0, cash_thb=1000.0, fx_thb=34.0,
            portfolio_value_usd=2000.0, regime="Risk-Off (Correction)"
        )
        self.assertLess(sizing_wide["recommended_thb"], 1000.0)
        self.assertGreater(sizing_wide["stop_pct"], 14.0)

    def test_audit_bug_fixes(self):
        """Regression tests for 11 bugs/weaknesses found during logic audit"""
        import scan_stocks
        import discord_notifier
        import urllib.error

        # 1. Timezone-aware ISO timestamp comparison (UTC vs +07:00)
        # 2026-05-01T10:00:00+00:00 (17:00 TH) is NEWER than 2026-05-01T12:00:00+07:00 (12:00 TH),
        # even though string "10:00" < "12:00"
        ts_utc = scan_stocks._parse_iso_ts("2026-05-01T10:00:00+00:00")
        ts_th = scan_stocks._parse_iso_ts("2026-05-01T12:00:00+07:00")
        self.assertGreater(ts_utc, ts_th)

        # 2. Scanner and Bot ETF score consistency (both fund_score == 25)
        f_scan = scan_stocks.analyze_fundamentals_and_valuation(None, is_etf=True)
        f_bot = discord_bot.analyze_all_around_fundamentals(None, is_etf=True)
        self.assertEqual(f_scan["fund_score"], 25)
        self.assertEqual(f_scan["fund_score"], f_bot["fund_score"])

        # 3. Semi cap override & portfolio_fit penalty in discord_bot.evaluate_stock
        macro = {"hurdle": 80, "regime": "Risk-On (Bull)", "macro_score": 14, "spx_ret_20d": 1.0}
        fv_nvda = {
            "is_etf": False, "sector_raw": "Technology", "sector_display": "เทคโนโลยี",
            "fund_score": 28, "val_score": 16, "risk_score": 10, "portfolio_fit": 5,
            "fwd_pe": 30.0, "peg": 1.2, "rev_growth": "50%"
        }
        tech_nvda = {"price": 130.0, "ema20": 128.0, "ema50": 120.0, "ema200": 100.0, "rsi": 58.0, "ema_dist": 1.5, "atr": 3.0, "score": 18, "ret_20d": 5.0}
        ev_locked = discord_bot.evaluate_stock({"ticker": "NVDA", "tech": tech_nvda, "fund_val": fv_nvda}, macro, semi_exposure=0.50)
        self.assertEqual(ev_locked["portfolio_fit"], 1)
        self.assertEqual(ev_locked["raw_action"], "BUY")
        self.assertEqual(ev_locked["final_action"], "HOLD")
        self.assertTrue(ev_locked["override"])

        # 4. Global guild check allows DMs and works when MY_GUILD_ID is None
        class DummyCtx:
            def __init__(self, guild):
                self.guild = guild
        orig_gid = discord_bot.MY_GUILD_ID
        try:
            discord_bot.MY_GUILD_ID = 111
            self.assertTrue(asyncio.run(discord_bot.globally_block_unauthorized_guilds(DummyCtx(None))))
            discord_bot.MY_GUILD_ID = None
            self.assertTrue(asyncio.run(discord_bot.globally_block_unauthorized_guilds(DummyCtx(MagicMock(id=999)))))
        finally:
            discord_bot.MY_GUILD_ID = orig_gid

        # 5. Thread-local session isolation in scan_stocks.get_session()
        s_main = scan_stocks.get_session()
        worker_sessions = []
        import threading
        t = threading.Thread(target=lambda: worker_sessions.append(scan_stocks.get_session()))
        t.start()
        t.join()
        if s_main is not None and worker_sessions[0] is not None:
            self.assertIsNot(s_main, worker_sessions[0])

        # 6. Discord notifier 429 retry logic
        calls = []
        def fake_urlopen(req, timeout=10):
            url = getattr(req, "full_url", str(req))
            if "discord.com" not in url:
                resp = MagicMock()
                resp.read.return_value = b"{}"
                resp.__enter__.return_value = resp
                return resp
            calls.append(1)
            if len(calls) == 1:
                fp = MagicMock()
                fp.read.return_value = b'{"retry_after": 0.01}'
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, fp)
            resp = MagicMock()
            resp.read.return_value = b'{"id": "chan_1"}'
            resp.__enter__.return_value = resp
            return resp

        with patch("discord_notifier.urllib.request.urlopen", side_effect=fake_urlopen):
            res = discord_notifier._post_discord_json("https://discord.com/api/v10/test", {"content": "hi"}, {})
            self.assertEqual(res.get("id"), "chan_1")
            self.assertEqual(len(calls), 2)

    def test_security_hardening_and_edge_cases(self):
        """Regression tests for security audit fixes & stability bugs"""
        import math
        import pandas as pd
        import discord_notifier

        # 1. Ticker injection & traversal rejection
        for bad in ("@everyone", "<@123456>", "[click](https://evil.com)", "NVDA\n@here", "../../etc/passwd", "NVDA?x=1", "A" * 30):
            self.assertEqual(discord_bot.clean_ticker(bad), "", f"Should reject malicious ticker: {bad!r}")
        self.assertEqual(discord_bot.clean_ticker("S&P 500"), "SPY")
        self.assertEqual(discord_bot.clean_ticker("^GSPC"), "^GSPC")

        # 2. NaN / Inf / bool rejection in portfolio & alerts
        test_uid = "unit_test_user_888"
        self.assertFalse(discord_bot.add_user_portfolio_holding(test_uid, "NVDA", float("nan"), 100.0))
        self.assertFalse(discord_bot.add_user_portfolio_holding(test_uid, "NVDA", float("inf"), 100.0))
        self.assertFalse(discord_bot.add_user_portfolio_holding(test_uid, "NVDA", 1.0, float("nan")))
        self.assertFalse(discord_bot.add_user_portfolio_holding(test_uid, "", 1.0, 100.0))
        ok_nan, _ = discord_bot.add_user_alert(test_uid, 123, "NVDA", float("nan"))
        self.assertFalse(ok_nan)
        ok_inf, _ = discord_bot.add_user_alert(test_uid, 123, "NVDA", float("inf"))
        self.assertFalse(ok_inf)

        # 3. Slash command guild authorization check & admin check
        class DummyInter:
            def __init__(self, gid):
                self.guild_id = gid
                self.response = MagicMock()
                self.response.is_done.return_value = True

        orig_gid = discord_bot.MY_GUILD_ID
        try:
            discord_bot.MY_GUILD_ID = 111
            self.assertTrue(asyncio.run(discord_bot._tree_interaction_check(DummyInter(None))))
            self.assertTrue(asyncio.run(discord_bot._tree_interaction_check(DummyInter(111))))
            self.assertFalse(asyncio.run(discord_bot._tree_interaction_check(DummyInter(999))))

            ext_admin = MagicMock(id=55555)
            ext_admin.guild_permissions.administrator = True
            ext_guild = MagicMock(id=999)
            ext_chan = MagicMock()
            ext_chan.permissions_for.return_value = MagicMock(administrator=True)
            with patch.object(discord_bot, "is_portfolio_owner", return_value=False):
                self.assertFalse(asyncio.run(discord_bot._is_admin_or_owner(ext_admin, ext_guild, ext_chan)))
        finally:
            discord_bot.MY_GUILD_ID = orig_gid

        # 4. Webhook SSRF validation
        self.assertFalse(discord_notifier.send_discord_webhook("hi", "http://127.0.0.1:8080/steal"))
        self.assertFalse(discord_notifier.send_discord_webhook("hi", "https://evil.example/api/webhooks/1/2"))
        self.assertTrue(discord_notifier._is_valid_discord_webhook_url("https://discord.com/api/webhooks/123/abc"))

        # 5. Supabase config validation
        self.assertFalse(discord_bot._is_valid_supabase_config("http://example.supabase.co", "k", "user_profiles"))
        self.assertFalse(discord_bot._is_valid_supabase_config("https://example.supabase.co", "k", "user_profiles;DROP"))
        self.assertTrue(discord_bot._is_valid_supabase_config("https://example.supabase.co", "k", "user_profiles"))

        # 6. History & Price embed safety against zero close / trailing NaN / empty ticker
        self.assertIn("ไม่ถูกต้อง", discord_bot.build_price_embed("@@@").title)
        self.assertIn("ไม่ถูกต้อง", discord_bot.build_history_embed("@@@").title)
        sym_zero = "ZERO_TEST"
        dates = pd.date_range("2026-01-01", periods=3, freq="B")
        zero_df = pd.DataFrame({
            "Open": [0.0, 10.0, float("nan")],
            "High": [0.0, 11.0, float("nan")],
            "Low": [0.0, 9.0, float("nan")],
            "Close": [0.0, 10.0, float("nan")],
            "Volume": [100.0, 200.0, float("nan")],
        }, index=dates)
        with discord_bot._ticker_cache_lock:
            discord_bot._ticker_cache[sym_zero] = {
                "timestamp": time.time(),
                "data": {"ticker": sym_zero, "is_etf": False, "tech": {"price": 10.0}, "fund_val": {}, "hist": zero_df},
            }
        try:
            h_emb = discord_bot.build_history_embed(sym_zero, "5d")
            self.assertIn(sym_zero, h_emb.title)
            self.assertNotIn("nan", h_emb.description.lower())
        finally:
            with discord_bot._ticker_cache_lock:
                discord_bot._ticker_cache.pop(sym_zero, None)

        # 7. Watchlist toggle respects 50-item cap
        wl = discord_bot.load_user_watchlists()
        wl[test_uid] = [f"T{i}" for i in range(50)]
        discord_bot._write_json_local_only(discord_bot.USER_WATCHLIST_FILE, wl)
        try:
            ok_wl, _, _ = discord_bot._toggle_watchlist_ticker(test_uid, "NVDA")
            self.assertFalse(ok_wl)
        finally:
            wl = discord_bot.load_user_watchlists()
            wl.pop(test_uid, None)
            discord_bot._write_json_local_only(discord_bot.USER_WATCHLIST_FILE, wl)

    def test_qol_cash_autocomplete_confirm_and_error_handlers(self):
        """Tests for v2.9.2 QoL: cash management, contextual autocompletes, confirm view, error handlers"""
        from unittest.mock import AsyncMock
        test_uid = "unit_test_qol_999"

        # 1. Cash management: set / add / sub + invalid input rejection
        try:
            ok, val = discord_bot.set_user_portfolio_cash(test_uid, 5000.0, mode="set")
            self.assertTrue(ok)
            self.assertAlmostEqual(val, 5000.0)

            ok, val = discord_bot.set_user_portfolio_cash(test_uid, 1250.5, mode="add")
            self.assertTrue(ok)
            self.assertAlmostEqual(val, 6250.5)

            ok, val = discord_bot.set_user_portfolio_cash(test_uid, 10000.0, mode="sub")
            self.assertTrue(ok)
            self.assertAlmostEqual(val, 0.0)  # floored at 0

            # Invalid values
            self.assertFalse(discord_bot.set_user_portfolio_cash(test_uid, -100.0, mode="set")[0])
            self.assertFalse(discord_bot.set_user_portfolio_cash(test_uid, float("nan"), mode="add")[0])
            self.assertFalse(discord_bot.set_user_portfolio_cash(test_uid, float("inf"), mode="set")[0])
            self.assertFalse(discord_bot.set_user_portfolio_cash(test_uid, 100.0, mode="badmode")[0])

            # 2. Contextual autocomplete (portfolio + alert + ticker)
            discord_bot.add_user_portfolio_holding(test_uid, "PLTR", 2.0, 25.0, "AI")
            discord_bot.add_user_alert(test_uid, 123, "AMD", 200.0, current_price=150.0)

            fake_inter = MagicMock()
            fake_inter.user.id = test_uid
            port_choices = asyncio.run(discord_bot.portfolio_autocomplete(fake_inter, ""))
            self.assertTrue(any(c.value == "PLTR" for c in port_choices))

            alert_choices = asyncio.run(discord_bot.alert_autocomplete(fake_inter, ""))
            self.assertTrue(any(c.value == "AMD" for c in alert_choices))

            ticker_choices = asyncio.run(discord_bot.ticker_autocomplete(fake_inter, "PL"))
            self.assertTrue(any(c.value == "PLTR" and "พอร์ต" in c.name for c in ticker_choices))

            # 3. Global error handlers do not leak raw exception details
            err_inter = MagicMock()
            err_inter.command.name = "check"
            err_inter.response.is_done.return_value = False
            err_inter.response.send_message = AsyncMock()
            asyncio.run(discord_bot.on_app_command_error(err_inter, discord_bot.app_commands.AppCommandError("SECRET_SQL_TOKEN")))
            sent_msg = err_inter.response.send_message.call_args[0][0]
            self.assertNotIn("SECRET_SQL_TOKEN", sent_msg)

            # 4. ConfirmActionView enforces user ownership
            async def _run_confirm_check():
                called = {"ok": False}
                async def _cb(inter):
                    called["ok"] = True
                cv = discord_bot.ConfirmActionView(test_uid, _cb)
                other = MagicMock()
                other.user.id = "other_user_123"
                other.response.send_message = AsyncMock()
                await cv.confirm_btn.callback(other)
                self.assertFalse(called["ok"])
                other.response.send_message.assert_awaited_once()
            asyncio.run(_run_confirm_check())
        finally:
            p = discord_bot.load_user_portfolios()
            p.pop(test_uid, None)
            discord_bot._write_json_local_only(discord_bot.USER_PORTFOLIO_FILE, p)
            a = discord_bot.load_user_alerts()
            a.pop(test_uid, None)
            discord_bot._write_json_local_only(discord_bot.USER_ALERT_FILE, a)

    def test_quant_v210_regime_flexibility_and_scoring_accuracy(self):
        """Tests for v2.10.0 Quant Engine: 4-tier macro, stop_loss invariant, negative-FCF/loss penalties, earnings blackout, unified scoring"""
        import scan_stocks
        # 1. Stop loss must ALWAYS be strictly below price (no distorted R:R when ema50 > price)
        tech_ema50_above = {
            "price": 100.0,
            "ema20": 101.0,
            "ema50": 110.0,  # ema50 * 0.95 = 104.5 > price!
            "ema200": 95.0,
            "ema_dist": -1.0,
            "atr": 2.0,
        }
        ent = scan_stocks.compute_entry_zone(tech_ema50_above)
        self.assertLess(ent["stop_loss"], 100.0)
        self.assertLess(ent["rr_ratio"], 20.0)

        # 2. 4-tier Adaptive Macro Regime + 10Y Yield Pressure + Nasdaq Divergence
        bull = scan_stocks.classify_macro_regime(5500, 5300, vix=14.0, tnx=4.1, ndx_curr=19500, ndx_ema50=18800)
        self.assertEqual(bull["regime"], "Risk-On (Bull)")
        self.assertEqual(bull["macro_score"], 15)
        self.assertEqual(bull["hurdle"], 82)

        # High 10Y yield pressure (>= 5.0%) reduces macro_score & raises hurdle
        bull_high_rate = scan_stocks.classify_macro_regime(5500, 5300, vix=14.0, tnx=5.1, ndx_curr=19500, ndx_ema50=18800)
        self.assertEqual(bull_high_rate["macro_score"], 13)
        self.assertEqual(bull_high_rate["hurdle"], 84)

        # Nasdaq divergence trims macro_score
        bull_div = scan_stocks.classify_macro_regime(5500, 5300, vix=14.0, tnx=4.1, ndx_curr=18000, ndx_ema50=18800)
        self.assertLess(bull_div["macro_score"], bull["macro_score"])

        # Crisis regime (VIX >= 35)
        crisis = scan_stocks.classify_macro_regime(4800, 5200, vix=38.0, tnx=4.2)
        self.assertIn("Crisis", crisis["regime"])
        self.assertEqual(crisis["macro_score"], 4)
        self.assertGreaterEqual(crisis["hurdle"], 90)

        # Crisis position sizing uses tighter 0.75% risk cap than Bull (2.0%)
        pos_crisis = scan_stocks.compute_position_size(100.0, 90.0, 20000.0, portfolio_value_usd=500.0, fx_thb=34.0, regime=crisis["regime"])
        pos_bull = scan_stocks.compute_position_size(100.0, 90.0, 20000.0, portfolio_value_usd=500.0, fx_thb=34.0, regime=bull["regime"])
        self.assertLess(pos_crisis["recommended_thb"], pos_bull["recommended_thb"])

        # 3. Unprofitable / Cash-burning company is penalized (no free 32/60)
        bad_info = {
            "sector": "Technology",
            "revenueGrowth": -0.15,
            "earningsGrowth": -0.40,
            "profitMargins": -0.25,
            "returnOnEquity": -0.10,
            "forwardPE": -12.0,
            "freeCashflow": -500_000_000,
            "totalDebt": 2_000_000_000,
            "marketCap": 5_000_000_000,
        }
        bad_fv = scan_stocks.analyze_fundamentals_and_valuation(None, is_etf=False, info=bad_info)
        self.assertLessEqual(bad_fv["fund_score"], 5)
        self.assertLessEqual(bad_fv["val_score"], 5)
        self.assertLessEqual(bad_fv["risk_score"], 3)

        # 4. Unified scoring between scan_stocks and discord_bot (zero drift)
        good_info = {
            "sector": "Technology",
            "revenueGrowth": 0.28,
            "earningsGrowth": 0.35,
            "profitMargins": 0.32,
            "returnOnEquity": 0.40,
            "pegRatio": 1.1,
            "forwardPE": 28.0,
            "freeCashflow": 50_000_000_000,
            "totalDebt": 20_000_000_000,
            "marketCap": 2_000_000_000_000,
        }
        fv_scan = scan_stocks.analyze_fundamentals_and_valuation(None, is_etf=False, info=good_info)
        fv_bot = discord_bot.analyze_all_around_fundamentals(None, is_etf=False, info=good_info)
        for k in ("fund_score", "val_score", "risk_score", "portfolio_fit"):
            self.assertEqual(fv_scan[k], fv_bot[k], f"Mismatch on {k} between scanner and bot")

        # 5. Earnings Blackout Guard (<= 5 days downgrades BUY -> HOLD for stocks, not ETF)
        tech_ok = {"price": 150.0, "ema20": 149.0, "ema50": 140.0, "ema200": 120.0, "ema_dist": 0.6, "rsi": 55.0, "atr": 3.0}
        fv_earn = {**fv_scan, "earnings_days": 3, "earnings_soon": True}
        _, raw_a, final_a, ov, reason = scan_stocks.decide_action("NVDA", 90, tech_ok, fv_earn, bull, is_etf=False)
        self.assertEqual(raw_a, "BUY")
        self.assertEqual(final_a, "HOLD")
        self.assertTrue(ov)
        self.assertIn("งบ", reason)

if __name__ == '__main__':
    unittest.main()
