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

    def test_min_entry_price(self):
        self.cfg.min_option_price = 1_000.0
        self.assertIsNone(self.trader.open_position(None, signal("C", 100.0, 99.0, 100.5), self.now))

    def test_no_entries_before_0945(self):
        eng = SessionEngine(self.cfg, None, None, self.trader)
        self.assertFalse(eng.can_enter("AAA", et_datetime(FRI, "09:40"))[0])
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "09:45"))[0])

    def test_no_entries_midday(self):
        eng = SessionEngine(self.cfg, None, None, self.trader)
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "11:25"))[0])
        self.assertFalse(eng.can_enter("AAA", et_datetime(FRI, "11:30"))[0])
        self.assertFalse(eng.can_enter("AAA", et_datetime(FRI, "14:25"))[0])
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "14:30"))[0])
        self.cfg.no_entry_windows = ()
        self.assertTrue(eng.can_enter("AAA", et_datetime(FRI, "12:00"))[0])


if __name__ == "__main__":
    unittest.main()
