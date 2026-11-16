"""旧规则 L 账户与 baseline 并行（2026-11-16 用户定）：账户搭建、按金额开仓、交易明细文件名（0 配额）。
python3 -m unittest tests.test_parallel_legacy -v"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main   # noqa: E402
from config import SystemConfig   # noqa: E402
from paper_trader import PaperTrader   # noqa: E402


def entry(t, strict=False):
    e = mock.Mock()
    e.ticker, e.rank = t, 1
    e._strict = strict
    return e


class ParallelLegacyTest(unittest.TestCase):
    def build(self, **over):
        cfg = SystemConfig.load(None)
        cfg.trades_csv = "live/trades_2026-11-16.csv"
        for k, v in over.items():
            setattr(cfg, k, v)
        uni = [entry("NVDA", True), entry("AMD")]
        with mock.patch.object(main, "UniverseFilter") as uf, \
                mock.patch.object(main, "is_strict", side_effect=lambda e, c: e._strict), \
                mock.patch.object(main, "budget_cap", return_value=60):
            uf.return_value.build.return_value = list(uni)
            client = mock.Mock(quota_remaining=99999)
            return main.build_live_accounts(cfg, client, dt.date(2026, 11, 16), None)[1]

    def test_accounts(self):
        acc = {a.name: a for a in self.build()}
        self.assertEqual(sorted(acc), ["A", "B", "L"])
        self.assertEqual(acc["B"].cfg.intraday_rules, "baseline")
        self.assertEqual(acc["L"].cfg.intraday_rules, "legacy")
        self.assertEqual(acc["L"].tickers, acc["B"].tickers)            # 同一份标的池
        self.assertTrue(acc["L"].cfg.legacy_notional_sizing)
        self.assertFalse(acc["B"].cfg.legacy_notional_sizing)
        self.assertEqual(acc["L"].cfg.trades_csv, "live/trades_2026-11-16_L.csv")
        self.assertEqual(acc["B"].cfg.trades_csv, "live/trades_2026-11-16.csv")
        self.assertEqual(type(acc["L"].trader), PaperTrader)

    def test_switch_off(self):
        self.assertEqual(sorted(a.name for a in self.build(live_parallel_legacy=False)), ["A", "B"])

    def test_notional_sizing(self):
        cfg = SystemConfig.load(None)
        t = PaperTrader(cfg)
        self.assertEqual(t._contracts(1.00), cfg.contracts_per_trade)    # 旧规则原口径
        cfg.legacy_notional_sizing = True
        self.assertEqual(t._contracts(1.00), 7)    # $100 → 7 张（≈ $650，与 baseline 同规则）
        self.assertEqual(t._contracts(3.00), 2)
        self.assertEqual(t._contracts(9.00), 1)


if __name__ == "__main__":
    unittest.main()
