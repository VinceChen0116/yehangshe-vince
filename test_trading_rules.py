"""交易规则离线测试：到期选择（0DTE 否则最近到期）、行权价虚值上限、最低价格、09:45 前不开仓。0 配额。

运行：python -m unittest test_trading_rules -v
"""
from __future__ import annotations

import datetime as dt
import logging
import unittest

from config import SystemConfig, et_datetime, expiry_candidates, trading_dte
from detector import FullSignal, FlowConfirmation, StructureSignal, parse_heatmap
from paper_trader import PaperTrader, SessionEngine

logging.disable(logging.CRITICAL)

FRI = dt.date(2026, 10, 9)
WED = dt.date(2026, 10, 7)


def heatmap(session: dt.date, exps: list[str], spot: float = 100.0) -> dict:
    cells = []
    for e in exps:
        cells += [{"strike_usd": 99.0, "expiration": e, "net_dealer_gex_usd": 2e8},
                  {"strike_usd": 103.0, "expiration": e, "net_dealer_gex_usd": -2e8}]
    return {"data": {"ticker": "AAA", "spot_usd": spot, "session_date_et": session.isoformat(),
                     "expirations": exps, "cells": cells}}


class ExpiryTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SystemConfig()

    def test_trading_dte(self):
        self.assertEqual(trading_dte(FRI, FRI), 0)
        self.assertEqual(trading_dte(dt.date(2026, 10, 12), FRI), 1)      # 周五 → 周一
        self.assertEqual(trading_dte(dt.date(2026, 11, 27), dt.date(2026, 11, 25)), 1)  # 跳过感恩节
        self.assertEqual(trading_dte(dt.date(2026, 10, 8), FRI), -1)

    def test_prefers_0dte(self):
        s = parse_heatmap(heatmap(FRI, ["2026-10-09", "2026-10-12"]), self.cfg, FRI)
        self.assertEqual(s.expiration, "2026-10-09")

    def test_falls_back_to_1dte(self):
        s = parse_heatmap(heatmap(WED, ["2026-10-08", "2026-10-09"]), self.cfg, WED)
        self.assertEqual(s.expiration, "2026-10-08")

    def test_no_0dte_uses_nearest(self):
        s = parse_heatmap(heatmap(WED, ["2026-10-16", "2026-10-09"]), self.cfg, WED)
        self.assertEqual(s.expiration, "2026-10-09")

    def test_max_dte_cap(self):
        self.cfg.expiry_max_trading_dte = 1   # "0DTE 否则 1DTE"
        self.assertIsNone(parse_heatmap(heatmap(WED, ["2026-10-09", "2026-10-16"]), self.cfg, WED))

    def test_expiry_candidates(self):
        self.assertEqual(expiry_candidates(WED, 1), ["2026-10-07", "2026-10-08"])
        c = expiry_candidates(FRI, None, 3)
        self.assertEqual(c, ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"])


def signal(right: str, spot: float, pos: float, neg: float, exp: str = "2026-10-09",
           strikes: list[float] | None = None) -> FullSignal:
    if strikes is None:   # 现价附近的挂牌行权价网格，含两个节点
        step = 0.5 if spot < 50 else (1.0 if spot < 200 else (2.5 if spot < 500 else 5.0))
        base = round(spot / step) * step
        strikes = sorted({round(base + i * step, 2) for i in range(-10, 11)} | {pos, neg})
    cells = [{"strike_usd": k, "expiration": exp, "net_dealer_gex_usd": 1e6} for k in strikes]
    for c in cells:
        if c["strike_usd"] == pos:
            c["net_dealer_gex_usd"] = 2e8
        elif c["strike_usd"] == neg:
            c["net_dealer_gex_usd"] = -2e8
    payload = {"data": {"ticker": "AAA", "spot_usd": spot, "session_date_et": FRI.isoformat(),
                        "expirations": [exp], "cells": cells}}
    snap = parse_heatmap(payload, SystemConfig(), FRI)
    st = StructureSignal("SPRINGBOARD" if right == "C" else "CLIFF", "BUY_CALL" if right == "C" else "BUY_PUT",
                         right, pos, neg, spot, abs(spot - pos) / pos)
    return FullSignal("AAA", et_datetime(FRI, "10:00"), snap, st, FlowConfirmation(True, "test"), "NEUTRAL", 0)


class StrikeAndEntryTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SystemConfig()
        self.cfg.use_real_option_pricing = False
        self.trader = PaperTrader(self.cfg)
        self.now = et_datetime(FRI, "10:00")

    def test_call_strike_capped(self):
        p = self.trader.open_position(None, signal("C", 385.5, 382.5, 400.0), self.now)
        self.assertLessEqual(p.strike, 385.5 * 1.005)
        self.assertGreater(p.strike, 380.0)

    def test_put_strike_capped(self):
        p = self.trader.open_position(None, signal("P", 386.5, 390.0, 370.0), self.now)
        self.assertGreaterEqual(p.strike, 386.5 * 0.995)

    def test_near_node_kept(self):
        p = self.trader.open_position(None, signal("C", 1000.0, 995.0, 1005.0), self.now)
        self.assertEqual(p.strike, 1005.0)

    def test_listed_strike_not_rounded(self):
        # COIN 192.5：挂牌行权价不能被取整成不存在的 192
        p = self.trader.open_position(None, signal("C", 192.0, 190.0, 200.0, strikes=[185.0, 190.0, 192.5, 195.0, 200.0]),
                                      self.now)
        self.assertEqual(p.strike, 192.5)

    def test_itm_strike(self):
        self.cfg.strike_mode = "itm"
        self.cfg.itm_depth_pct = 0.01
        p = self.trader.open_position(None, signal("C", 768.66, 765.0, 780.0), self.now)
        self.assertEqual(p.strike, 760.0)          # 现价 768.66 × 0.99 ≈ 761，挂牌网格步长 5 → 760
        q = self.trader.open_position(None, signal("P", 761.84, 765.0, 745.0), self.now)
        self.assertEqual(q.strike, 770.0)          # 看跌：价内 = 高于现价（761.84 × 1.01 ≈ 769.5 → 770）

    def test_min_entry_price(self):
        self.cfg.min_option_price = 1_000.0
        self.assertIsNone(self.trader.open_position(None, signal("C", 100.0, 99.0, 100.5), self.now))

    def test_no_entries_before_0945(self):
        eng = SessionEngine(self.cfg, None, None, self.trader)
        self.assertFalse(eng.can_enter("AAA", et_datetime(FRI, "09:40"))[0])
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "09:45"))[0])

    def test_no_entries_midday(self):
        self.cfg.no_entry_windows = (("11:30", "14:30"),)
        eng = SessionEngine(self.cfg, None, None, self.trader)
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "11:25"))[0])
        self.assertFalse(eng.can_enter("AAA", et_datetime(FRI, "11:30"))[0])
        self.assertFalse(eng.can_enter("AAA", et_datetime(FRI, "14:25"))[0])
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "14:30"))[0])
        self.cfg.no_entry_windows = ()
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "12:00"))[0])


class RankingTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SystemConfig()
        self.cfg.use_real_option_pricing = False
        self.trader = PaperTrader(self.cfg)
        self.eng = SessionEngine(self.cfg, None, None, self.trader)
        self.now = et_datetime(FRI, "10:00")

    def _sig(self, ticker, score):
        s = signal("C", 100.0, 99.0, 100.5)
        s.ticker, s.score = ticker, score
        return s

    def test_higher_score_first_and_daily_cap(self):
        self.cfg.max_trades_per_day = 2
        self.eng.pending = {t: self._sig(t, sc) for t, sc in (("LOW", 0.2), ("HIGH", 0.9), ("MID", 0.5))}
        self.eng.flush(self.now)
        self.assertEqual([p.ticker for p in self.trader.positions], ["HIGH", "MID"])
        self.eng.pending = {"NEW": self._sig("NEW", 0.99)}
        self.eng.flush(self.now)
        self.assertEqual(len(self.trader.positions), 2)   # 当日上限已满

    def test_replace_weakest_when_full(self):
        self.cfg.max_open_positions = 2
        self.cfg.rank_replace_margin = 0.10
        self.eng.pending = {t: self._sig(t, sc) for t, sc in (("LOW", 0.55), ("MID", 0.70))}
        self.eng.flush(self.now)
        self.eng.pending = {"NEW": self._sig("NEW", 0.66)}          # 只比 LOW 高 0.11 → 换掉 LOW
        self.eng.flush(et_datetime(FRI, "10:05"))
        tickers = sorted(p.ticker for p in self.trader.open_positions())
        self.assertEqual(tickers, ["MID", "NEW"])
        self.assertEqual(next(p for p in self.trader.positions if p.ticker == "LOW").exit_reason, "REPLACED")
        self.eng.pending = {"X": self._sig("X", 0.70)}              # 不够高出 0.10 → 不换
        self.eng.flush(et_datetime(FRI, "10:10"))
        self.assertEqual(len(self.trader.open_positions()), 2)

    def test_replace_off_by_default(self):
        self.cfg.max_open_positions = 1
        self.eng.pending = {"LOW": self._sig("LOW", 0.5)}
        self.eng.flush(self.now)
        self.eng.pending = {"HI": self._sig("HI", 0.99)}
        self.eng.flush(et_datetime(FRI, "10:05"))
        self.assertEqual([p.ticker for p in self.trader.open_positions()], ["LOW"])

    def test_min_score(self):
        self.cfg.min_signal_score = 0.6
        self.eng.pending = {"A": self._sig("A", 0.5), "B": self._sig("B", 0.7)}
        self.eng.flush(self.now)
        self.assertEqual([p.ticker for p in self.trader.positions], ["B"])

    def test_score_range(self):
        from detector import score_signal
        s = signal("C", 100.0, 99.0, 100.5)
        sc, parts = score_signal(s.snapshot, s.structure, 0, [100, 100.2, 99.9, 100.1, 100.0], self.cfg)
        self.assertTrue(0 <= sc <= 1)
        self.assertEqual(set(parts), {"fuel", "floor", "proximity", "distance", "reach", "fresh"})
        self.assertAlmostEqual(parts["proximity"] + parts["distance"], 1.0, places=2)


class UnderlyingStopTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SystemConfig()
        self.cfg.use_real_option_pricing = False
        self.cfg.stop_mode = "underlying"
        self.cfg.underlying_stop_buffer_pct = 0.002
        self.trader = PaperTrader(self.cfg)
        self.now = et_datetime(FRI, "10:00")
        # 看涨：地板（正节点）99，现价 100；期权价格由测试指定（模拟真实价格源）
        self.p = self.trader.open_position(None, signal("C", 100.0, 99.0, 100.5), self.now)
        self.p.price_source = "real_data"
        self.opt = self.p.entry_mid * 0.8
        self.trader.price_provider = lambda sym, t: self.opt

    def test_option_drawdown_alone_does_not_stop(self):
        self.assertEqual(self.p.stop_pct, self.cfg.disaster_stop_pct)
        self.opt = self.p.entry_mid * 0.6                                     # 期权 -40%
        self.assertIsNone(self.trader.mark(self.p, 99.5, self.now))           # 标的仍在地板上方
        self.opt = self.p.entry_mid * 0.35                                    # 期权 -65% → 灾难止损
        self.assertEqual(self.trader.mark(self.p, 99.5, self.now).exit_reason, "STOP_LOSS")

    def test_break_below_floor_with_buffer_stops(self):
        self.assertIsNone(self.trader.mark(self.p, 99.0 * (1 - 0.001), self.now))   # 只穿 0.1%，未过缓冲
        closed = self.trader.mark(self.p, 99.0 * (1 - 0.003), self.now)             # 穿 0.3% > 0.2% 缓冲
        self.assertIsNotNone(closed)
        self.assertEqual(closed.exit_reason, "UNDERLYING_STOP")


class QuotePricingTest(unittest.TestCase):
    """实盘实时报价：按 ask 开仓、按 bid 盯市与平仓；bid 为 0 时退回中间价减滑点。"""
    def setUp(self):
        self.cfg = SystemConfig()
        self.cfg.use_real_option_pricing = False
        self.trader = PaperTrader(self.cfg)
        self.q = {"bid": 1.00, "ask": 1.10, "mid": 1.05}
        self.trader.quote_provider = lambda sym: dict(self.q)
        self.now = et_datetime(FRI, "10:00")
        self.p = self.trader.open_position(None, signal("C", 100.0, 99.0, 100.5), self.now)

    def test_entry_at_ask(self):
        self.assertEqual(self.p.price_source, "quote")
        self.assertEqual(self.p.entry_option_price, 1.10)
        self.assertEqual((self.p.entry_bid, self.p.entry_ask), (1.00, 1.10))
        self.assertAlmostEqual(self.p.roi_pct, (1.00 / 1.10 - 1) * 100, places=4)   # 刚开仓即亏一个价差

    def test_mark_and_close_at_bid(self):
        self.q = {"bid": 1.50, "ask": 1.60, "mid": 1.55}
        self.assertIsNone(self.trader.mark(self.p, 100.2, et_datetime(FRI, "10:05")))
        self.assertAlmostEqual(self.p.roi_pct, (1.50 / 1.10 - 1) * 100, places=4)
        closed = self.trader.close(self.p, 100.2, et_datetime(FRI, "10:10"), "MANUAL_STOP")
        self.assertEqual(closed.exit_option_price, 1.50)

    def test_zero_bid_falls_back_to_mid(self):
        self.q = {"bid": 0.0, "ask": 1.20, "mid": 0.60}
        self.trader.mark(self.p, 100.0, et_datetime(FRI, "10:05"))
        self.assertAlmostEqual(self.p.roi_pct, (0.60 * (1 - self.cfg.slippage_pct) / 1.10 - 1) * 100, places=4)

    def test_no_quote_no_entry(self):
        self.trader.quote_provider = lambda sym: None
        self.assertIsNone(self.trader.open_position(None, signal("C", 100.0, 99.0, 100.5), self.now))

    def test_missing_quote_keeps_last_price(self):
        self.trader.quote_provider = lambda sym: None
        self.trader.mark(self.p, 100.0, et_datetime(FRI, "10:05"))
        self.assertAlmostEqual(self.p.roi_pct, (1.00 / 1.10 - 1) * 100, places=4)


class QuoteBookTest(unittest.TestCase):
    def test_batch_then_cache(self):
        from main import QuoteBook

        class FakeClient:
            def __init__(self):
                self.paths = []

            def get(self, path, params=None):
                self.paths.append((path, params))
                if path == "/v1/options/quotes":
                    return {"data": {"quotes": [{"contract_symbol": "AAA261009C00100000", "bid_usd": 1.0,
                                                 "ask_usd": 1.2, "mid_usd": 1.1}], "unavailable": []}}
                return {"data": {"contract_symbol": "AAA261009C00101000", "bid_usd": 0.5, "ask_usd": 0.6}}

        c = FakeClient()
        qb = QuoteBook(c)
        qb.new_round(["O:AAA261009C00100000", "AAA261009C00100000"])
        self.assertEqual(c.paths[0][1]["contracts"], "AAA261009C00100000")   # 去掉 O: 前缀并去重
        self.assertEqual(qb.get("O:AAA261009C00100000")["ask"], 1.2)
        self.assertEqual(qb.get("O:AAA261009C00101000")["mid"], 0.55)        # 新合约单独请求
        qb.get("O:AAA261009C00101000")
        self.assertEqual(len(c.paths), 2)                                     # 同一轮内走缓存
        qb.new_round([])
        self.assertEqual(len(c.paths), 2)                                     # 无持仓不请求


class UnderlyingTrailTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SystemConfig()
        self.cfg.use_real_option_pricing = False
        self.cfg.trail_mode = "underlying"
        self.cfg.underlying_trail_pct = 0.003
        self.trader = PaperTrader(self.cfg)
        self.now = et_datetime(FRI, "10:00")
        self.p = self.trader.open_position(None, signal("C", 100.0, 99.0, 100.5), self.now)
        self.p.price_source = "real_data"
        self.opt = self.p.entry_mid
        self.trader.price_provider = lambda sym, t: self.opt

    def test_trail_after_profit(self):
        self.opt = self.p.entry_mid * 1.2                                   # +20%：未激活
        self.assertIsNone(self.trader.mark(self.p, 100.5, self.now))
        self.assertFalse(self.p.und_trail_active)
        self.opt = self.p.entry_mid * 1.6                                   # 峰值 ≥ +35%：激活
        self.assertIsNone(self.trader.mark(self.p, 101.0, self.now))
        self.assertTrue(self.p.und_trail_active)
        self.assertIsNone(self.trader.mark(self.p, 101.0 * (1 - 0.002), self.now))   # 回撤 0.2% < 0.3%
        closed = self.trader.mark(self.p, 101.0 * (1 - 0.004), self.now)              # 回撤 0.4%
        self.assertEqual(closed.exit_reason, "UNDERLYING_TRAIL")

    def test_node_stop_before_activation(self):
        closed = self.trader.mark(self.p, 99.0 * (1 - 0.006), self.now)
        self.assertEqual(closed.exit_reason, "UNDERLYING_STOP")


if __name__ == "__main__":
    unittest.main()
