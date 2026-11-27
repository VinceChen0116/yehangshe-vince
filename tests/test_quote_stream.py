"""B 实时推送报价（quote_stream.py，2026-11-26 用户定）：消息处理 / 订阅差分 / 健康判断（0 配额、不联网）。
python3 -m unittest tests.test_quote_stream -v"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SystemConfig   # noqa: E402
from quote_stream import QuoteStream   # noqa: E402

C, P = "NVDA261127C00150000", "NVDA261127P00150000"


class QuoteStreamTest(unittest.TestCase):
    def setUp(self):
        self.qs = QuoteStream(SystemConfig.load(None))

    def test_ping_pong_and_hello(self):
        self.assertEqual(self.qs.handle({"type": "ping"}), [{"type": "pong"}])
        self.assertFalse(self.qs.healthy())
        self.qs.handle({"type": "hello"})
        self.assertTrue(self.qs.healthy())

    def test_subscribe_diff_and_quotes(self):
        qs = self.qs
        qs.handle({"type": "hello"})
        qs.set_contracts(["O:" + C, P])
        add, drop = qs._diff()
        self.assertEqual(add, sorted([C, P]))
        self.assertEqual(qs._diff(), ([], []))              # 已发出，等回执，不重复订阅
        qs.handle({"type": "subscribed", "channel": "option-quotes", "contracts": [C], "mode": "live"})
        qs.handle({"type": "subscribe_rejected", "channel": "option-quotes", "contracts": [P], "reason": "invalid"})
        qs.handle({"type": "data", "channel": "option-quotes", "rows": [
            {"contract_symbol": C, "quoted_at": "t", "bid_usd": 1.5, "ask_usd": 1.6, "mid_usd": 1.55}]})
        self.assertEqual(qs.get("O:" + C)["bid"], 1.5)
        self.assertIsNone(qs.get(P))                          # 被拒 → None（调用方退回 REST）
        self.assertEqual(qs._diff(), ([], []))                # 被拒的不反复重订
        # 无卖价 → 清掉，不能用旧报价
        qs.handle({"type": "data", "channel": "option-quotes", "rows": [
            {"contract_symbol": C, "bid_usd": None, "ask_usd": None, "mid_usd": None}]})
        self.assertIsNone(qs.get(C))
        # 平仓后不再需要 → 退订并清缓存
        qs.set_contracts([])
        self.assertEqual(qs._diff(), ([], [C]))

    def test_unhealthy_returns_none(self):
        qs = self.qs
        qs.handle({"type": "hello"})
        qs.set_contracts([C])
        qs._diff()
        qs.handle({"type": "subscribed", "contracts": [C], "mode": "snapshot"})
        qs.handle({"type": "data", "channel": "option-quotes", "rows": [{"contract_symbol": C, "bid_usd": 1, "ask_usd": 1.1}]})
        self.assertIsNotNone(qs.get(C))
        qs.last_msg = time.monotonic() - 120                  # 2 分钟没消息 → 不健康
        self.assertFalse(qs.healthy())
        self.assertIsNone(qs.get(C))
        qs.handle({"type": "ping"})
        qs.stopped = True                                     # 4409 等致命错误后 → 不健康
        self.assertIsNone(qs.get(C))


if __name__ == "__main__":
    unittest.main()
