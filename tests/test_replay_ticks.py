"""回放 5 秒精查（2026-11-26 用户定）：哪些分钟需要逐点查（0 配额）。python3 -m unittest tests.test_replay_ticks -v"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from baseline import EW, minute_needs_ticks   # noqa: E402
from strategy_core_spec import PositionState   # noqa: E402


def st(**k):
    s = PositionState(position_id="X", ticker="NVDA", direction="BULLISH", entry_spot=100.0, entry_option_price=1.0,
                      initial_stop_loss_spot=99.0, stop_loss_spot=99.0, target_spot=102.0, is_0dte=True,
                      is_same_day_overnight=False, is_opening_scalp=False, quantity=1)
    for a, v in k.items():
        setattr(s, a, v)
    return s


class MinuteNeedsTicksTest(unittest.TestCase):
    def test_quiet_minute_skipped(self):
        self.assertFalse(minute_needs_ticks(st(), (2.0, 5.0), (100.4, 100.5), 3))

    def test_near_initial_stop(self):
        self.assertTrue(minute_needs_ticks(st(), (EW.initial_stop_loss_roi_pct + 3, -10.0), (100.4, 100.5), 3))

    def test_range_crosses_line(self):     # 分钟内从 +5% 冲到 −40%：跨过 −22% 止损
        self.assertTrue(minute_needs_ticks(st(), (5.0, -40.0), (100.4, 100.5), 3))

    def test_spot_near_stop_or_target(self):
        self.assertTrue(minute_needs_ticks(st(), (2.0, 5.0), (99.05, 99.5), 3))
        self.assertTrue(minute_needs_ticks(st(), (2.0, 5.0), (101.0, 101.95), 3))

    def test_new_high_after_breakeven(self):  # 已经过保本触发线、这一分钟创新高 → 追踪线会抬高
        self.assertTrue(minute_needs_ticks(st(peak_roi_pct=30.0, breakeven_locked=True, trailing_active=False),
                                           (28.0, 34.0), (100.6, 100.7), 6))


if __name__ == "__main__":
    unittest.main()


class HalfDayCutoffTest(unittest.TestCase):
    def test_noon_cutoff_on_early_close(self):
        import datetime as dt
        from unittest import mock
        from baseline import BaselineEngine
        from config import SystemConfig, et_datetime
        eng = BaselineEngine.__new__(BaselineEngine)
        eng.cfg = SystemConfig.load(None)
        eng.trader = mock.Mock(open_for=lambda t: None, trades_today=lambda t, d: [])
        eng.last_exit_for = lambda t: None
        half, normal = dt.date(2026, 11, 27), dt.date(2026, 11, 30)
        self.assertTrue(eng.can_enter("NVDA", et_datetime(half, "11:59"))[0])
        self.assertFalse(eng.can_enter("NVDA", et_datetime(half, "12:00"))[0])
        self.assertTrue(eng.can_enter("NVDA", et_datetime(normal, "12:30"))[0])
