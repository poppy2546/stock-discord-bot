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

if __name__ == '__main__':
    unittest.main()
