"""实盘接线测试：LiveAccount(baseline) + QuoteBook + Call/Put 平价现价，按实盘主循环的调用顺序喂 10/19 存档快照（0 配额）。
python3 -m unittest tests.test_baseline_live -v"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SystemConfig, et_datetime   # noqa: E402

DAY = dt.date(2026, 10, 19)


class FakeClient:
    """只实现实盘循环会用到的 /v1/options/quotes 与 /v1/options/quote/{c}；报价 = 固定价 + 记录调用。"""

    def __init__(self):
        self.calls = []
        self.px = {}          # core → (bid, ask)
        self.quota_remaining = 99999

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/v1/options/quotes":
            cores = params["contracts"].split(",")
            return {"data": {"quotes": [self._q(c) for c in cores]}}
        if path.startswith("/v1/options/quote/"):
            return {"data": self._q(path.rsplit("/", 1)[-1])}
        from yhs_client import YHSApiError
        raise YHSApiError(404, "NOT_FOUND", "fake", path)

    def _q(self, core):
        bid, ask = self.px.get(core, (2.00, 2.10))
        return {"contract_symbol": core, "bid_usd": bid, "ask_usd": ask, "mid_usd": (bid + ask) / 2,
                "quoted_at": "2026-10-19T15:00:00Z"}


class TestLiveWiring(unittest.TestCase):
    def test_live_loop_sequence(self):
        from baseline_frames import merged_frames
        from main import LiveAccount, QuoteBook
        from baseline import BaselineEngine, BaselineTrader, pair_symbol, parity_spot
        frames = merged_frames(SystemConfig.load(None), DAY, "NVDA")
        if not frames:
            self.skipTest("没有 10/19 存档")
        cfg = SystemConfig.load(None)
        cfg.intraday_rules = "baseline"
        cfg.flow_record = False
        tmp = tempfile.mkdtemp()
        cfg.trades_csv = os.path.join(tmp, "trades.csv")
        client = FakeClient()
        acct = LiveAccount("B", cfg, client, [], None, "B")
        self.assertIsInstance(acct.engine, BaselineEngine)
        self.assertIsInstance(acct.trader, BaselineTrader)
        quotes = QuoteBook(client)
        acct.trader.quote_provider = quotes.get
        acct.engine.spot_override = lambda pos, now: parity_spot(pos.contract_symbol,
                                                                 lambda s: quotes.cache.get(s.split(":")[-1]))
        used_parity = []
        orig = acct.engine.spot_override

        def spy(pos, now):
            v = orig(pos, now)
            used_parity.append(v)
            return v
        acct.engine.spot_override = spy
        opened_syms = set()
        for label, payload in frames.items():
            now = label + dt.timedelta(minutes=5, seconds=25)
            if now.hour >= 16:
                break
            syms = [p.contract_symbol for p in acct.trader.open_positions()]
            quotes.new_round(syms + [pair_symbol(s) for s in syms])
            acct.engine.on_snapshot("NVDA", payload, now)        # 实盘：不传 frame_t，按 generated_at 取标记时间
            acct.engine.flush(now)
            for p in acct.trader.open_positions():
                opened_syms.add(p.contract_symbol)
            for k in range(1, 5):                                # 两次扫描之间每分钟盯仓
                tm = now + dt.timedelta(minutes=k)
                syms = [p.contract_symbol for p in acct.trader.open_positions()]
                if syms:
                    quotes.new_round(syms + [pair_symbol(s) for s in syms])
                    acct.engine.mark_open(tm)
        acct.engine.end_of_day(et_datetime(DAY, "16:00"))
        self.assertTrue(acct.trader.positions, "10/19 NVDA 应至少出一个 baseline 信号并开仓")
        self.assertTrue(any(v is not None for v in used_parity), "持仓盯市应使用 Call/Put 平价现价")
        req = [p for path, p in client.calls if path == "/v1/options/quotes"]
        self.assertTrue(any(any(c[-9] == "C" for c in r["contracts"].split(",")) and
                            any(c[-9] == "P" for c in r["contracts"].split(",")) for r in req),
                        "批量报价应同时包含 Call 与 Put（平价反推）")
        for p in acct.trader.positions:
            self.assertFalse(p.is_open)
            self.assertEqual(p.price_source, "quote")
            self.assertGreaterEqual(p.entry_time.hour * 60 + p.entry_time.minute, 9 * 60 + 30)
            self.assertLess(p.entry_time.hour * 60 + p.entry_time.minute, 15 * 60)
        acct.trader.export_trades_csv(cfg.trades_csv)
        with open(cfg.trades_csv, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows and rows[0]["bl_structure"] and rows[0]["bl_score"] and rows[0]["bl_stop_spot"])

    def test_legacy_switch(self):
        from main import LiveAccount
        from paper_trader import SessionEngine
        from baseline import BaselineEngine
        cfg = SystemConfig.load(None)
        cfg.intraday_rules = "legacy"
        acct = LiveAccount("B", cfg, FakeClient(), [], None, "B")
        self.assertIs(type(acct.engine), SessionEngine)
        self.assertNotIsInstance(acct.engine, BaselineEngine)


if __name__ == "__main__":
    unittest.main()
