"""实盘循环（虚拟时钟）：推送健康时 B 每 5 秒盯一次、A 仍每分钟；推送断开时 B 退回每分钟；提前收盘日 13:00 收（0 配额、不联网）。
python3 -m unittest tests.test_live_stream_loop -v"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main   # noqa: E402
from config import SystemConfig, et_datetime   # noqa: E402

DAY = dt.date(2026, 11, 30)   # 普通交易日（周一）


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=max(s, 0.01))


class FakePos:
    def __init__(self, sym):
        self.contract_symbol = sym


class FakeTrader:
    def __init__(self, has_pos):
        self.pos = [FakePos("O:NVDA261130C00150000")] if has_pos else []
        self.quote_provider = None

    def open_positions(self):
        return self.pos

    def open_for(self, t):
        return None


class FakeEngine:
    def __init__(self, clock):
        self.clock, self.marks = clock, []
        self.spot_override = None

    def on_snapshot(self, *a, **k):
        pass

    def flush(self, *a):
        pass

    def mark_open(self, now):
        self.marks.append(now)


class FakeAcc:
    def __init__(self, name, clock, has_pos=True):
        self.name, self.tickers = name, {"NVDA"}
        self.cfg = SystemConfig.load(None)
        self.detector = mock.Mock(rejected_spot=set())
        self.trader, self.engine = FakeTrader(has_pos), FakeEngine(clock)

    def status(self):
        return self.name


class FakeStream:
    def __init__(self, ok=True):
        self.ok, self.status, self.contracts = ok, "已连接", []

    def healthy(self):
        return self.ok

    def get(self, sym):
        return {"bid": 1.0, "ask": 1.1, "mid": 1.05} if self.ok else None

    def set_contracts(self, syms):
        self.contracts = list(syms)


class LiveLoopTest(unittest.TestCase):
    def run_day(self, day, stream_ok=True, until="10:00"):
        cfg = SystemConfig.load(None)
        cfg.flow_stream_record = False
        cfg.discord_heartbeat_min = 0
        cfg.force_close_time = until
        clock = Clock(et_datetime(day, "09:20"))
        b, a = FakeAcc("B", clock), FakeAcc("A", clock)
        a.cfg.intraday_rules = "baseline"
        client = mock.Mock(quota_remaining=99999)
        client.get.side_effect = lambda path, params=None: (
            {"data": {"session_date_et": day.isoformat()}} if "heatmap" in path else {"data": {"quotes": []}})
        stream = FakeStream(stream_ok)
        uni = [mock.Mock(ticker="NVDA")]
        with mock.patch.object(main, "et_now", clock.now), mock.patch.object(main.time, "sleep", clock.sleep), \
                mock.patch.object(main, "build_live_accounts", return_value=(uni, [b, a])), \
                mock.patch.object(main, "is_strict", return_value=False), \
                mock.patch.object(main, "finish_day"), \
                mock.patch("quote_stream.start_background", return_value=stream), \
                mock.patch("quote_stream.stop_background"):
            main.run_live_day(cfg, client, day, None)
        return b, a, stream, clock

    @staticmethod
    def gaps(marks):
        return [(y - x).total_seconds() for x, y in zip(marks, marks[1:])]

    def test_stream_b_every_5s_a_every_minute(self):
        b, a, stream, _ = self.run_day(DAY)
        self.assertGreater(len(b.engine.marks), 4 * len(a.engine.marks))
        self.assertLessEqual(max(self.gaps(b.engine.marks)), 60)     # 扫描间隙也不超过 1 分钟
        self.assertTrue(stream.contracts)                            # 已订阅持仓合约 + 另一边
        self.assertIn("NVDA261130P00150000", [c.split(":")[-1] for c in stream.contracts])
        self.assertGreaterEqual(min(self.gaps(a.engine.marks)), 19)  # A 不受影响（分钟 / 20 秒）

    def test_stream_down_falls_back_to_minute(self):
        b, a, _, _ = self.run_day(DAY, stream_ok=False)
        self.assertEqual(len(b.engine.marks), len(a.engine.marks))   # 与 A 一样每分钟

    def test_half_day_closes_at_1300(self):
        _, _, _, clock = self.run_day(dt.date(2026, 11, 27), until="16:00")
        self.assertLess(clock.t, et_datetime(dt.date(2026, 11, 27), "13:01"))


if __name__ == "__main__":
    unittest.main()
