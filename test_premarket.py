"""
test_premarket.py —— 盘前研究工作流离线测试（假客户端，0 配额）
运行：python -m unittest test_premarket -v
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import os
import shutil
import tempfile
import unittest

from config import SystemConfig, et_datetime
from premarket_workflow import (PremarketWorkflow, is_trading_day, market_clock, next_trading_day,
                                prev_trading_day, session_close_hhmm, utc_micros)
from yhs_client import YHSApiError

TARGET = dt.date(2026, 10, 12)
PREV = dt.date(2026, 10, 9)


def _iso_utc(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def daily_bars(prev2: float, prev: float, n: int = 20) -> list:
    days, d = [], PREV
    while len(days) < n:
        days.append(d)
        d = prev_trading_day(d)
    days.reverse()
    closes = [prev2] * (n - 1) + [prev]
    return [{"trading_date": day.isoformat(), "session": "regular", "open_usd": c, "high_usd": c + 1,
             "low_usd": c - 1, "close_usd": c} for day, c in zip(days, closes)]


def _read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


class FakeClient:
    """按路径返回与真实 API 同构的 JSON。"""

    def __init__(self, scen: dict, now_et: dt.datetime, flow_forbidden: bool = False):
        self.scen, self.now, self.flow_forbidden = scen, now_et, flow_forbidden
        self.billed_calls = self.free_calls = 0
        self.billed_by_endpoint = collections.Counter()
        self.quota_remaining = 99_000
        self.paths = []

    def discover(self):
        self.free_calls += 1
        return {"data": {"quota": {"monthly_remaining": self.quota_remaining},
                         "working_sets": {"dealer-heatmap": {"tickers": list(self.scen)}},
                         "capabilities_detail": []}}

    def get(self, path, params=None):
        params = params or {}
        self.paths.append((path, dict(params)))
        if path == "/v1/openapi.json":
            self.free_calls += 1
            return {"paths": {}}
        if path.startswith("/v1/flow/") and self.flow_forbidden:
            raise YHSApiError(403, "FLOW_ACCESS_RESTRICTED", "Flow data access is not enabled", path)
        self.billed_calls += 1
        self.billed_by_endpoint[path.split("/")[2]] += 1
        self.quota_remaining -= 1
        parts = path.strip("/").split("/")
        if path == "/v1/market/options-volume-rank":
            rows = [{"rank": i + 1, "ticker": t, "total_volume": 1_000_000 - i, "call_volume": 600_000,
                     "put_volume": 400_000, "total_open_interest": {"current": 2_000_000, "previous": 1_900_000,
                                                                     "change": 100_000, "change_pct": 100_000 / 1_900_000}}
                    for i, t in enumerate(self.scen)]
            return {"data": {"session_date": PREV.isoformat(), "as_of": "2026-10-09T04:00:00.000Z", "rows": rows},
                    "_meta": {"request_id": "r", "data_freshness_seconds": 1000}}
        t = parts[3] if parts[1] in ("derived", "stocks") else (parts[3] if parts[2] == "tickers" else None)
        s = self.scen.get(t, {})
        if parts[1] == "derived":
            cells = [{"strike_usd": k, "expiration": e, "net_dealer_gex_usd": g} for k, e, g in s["cells"]]
            return {"data": {"ticker": t, "generated_at": "2026-10-09T19:55:00.000Z", "session_date_et": PREV.isoformat(),
                             "spot_usd": 1.0, "expirations": sorted({c["expiration"] for c in cells}), "cells": cells},
                    "_meta": {"request_id": "r", "data_freshness_seconds": 10}}
        if parts[2] == "quote":
            q = s.get("quote")
            if q:
                return {"data": {"ticker": t, "ts": _iso_utc(self.now - dt.timedelta(seconds=10)),
                                 "bid_usd": q - 0.02, "ask_usd": q + 0.02}, "_meta": {}}
            return {"data": {"ticker": t, "ts": "2026-10-10T00:00:00.218Z", "bid_usd": 1, "ask_usd": 2},
                    "_meta": {"notice": "stale"}}
        if parts[2] == "ohlc":
            if params.get("candle_size") == "1d":
                return {"data": {"bars": daily_bars(s["prev2"], s["prev"])}, "_meta": {}}
            bars = []
            for hhmm, o, h, l, c in s.get("bars5", []):
                st = et_datetime(TARGET, hhmm)
                bars.append({"bar_start": _iso_utc(st), "bar_end": _iso_utc(st + dt.timedelta(minutes=5)),
                             "session": "regular", "open_usd": o, "high_usd": h, "low_usd": l, "close_usd": c})
            return {"data": {"bars": bars}, "_meta": {}}
        if parts[2] == "tickers" and parts[4] == "overview":
            ca, cb, pa, pb = s.get("flow", (0, 0, 0, 0))
            return {"data": {"ticker": t, "summary": {"calls": {"ask_premium_usd": ca, "bid_premium_usd": cb},
                                                       "puts": {"ask_premium_usd": pa, "bid_premium_usd": pb},
                                                       "mid_premium_usd": 10_000, "package_premium_pct": 12.0},
                             "top_contracts": [{"contract_symbol": f"O:{t}261016C00100000"}]}, "_meta": {}}
        if parts[2] == "trades":
            return {"data": [{"contract_symbol": f"O:{t}261016C00100000", "side": "ask", "premium_usd": 300_000},
                             {"contract_symbol": f"O:{t}261016P00090000", "side": "bid", "premium_usd": 200_000},
                             {"contract_symbol": f"O:{t}261016P00095000", "side": "mid", "premium_usd": 100_000}],
                    "_meta": {"next_cursor": "abc"}}
        return {"data": {"rows": []}, "_meta": {}}


E1, E2, E3 = "2026-10-16", "2026-10-23", "2026-10-30"
SCEN = {
    # A：昨日从 Wall 100 下方收到上方
    "AAA": {"prev2": 98.0, "prev": 101.5, "quote": 101.6,
            "cells": [(100, E1, 80e6), (105, E1, 20e6), (95, E1, 25e6), (102.5, E1, -10e6), (100, E2, 30e6)],
            "flow": (900_000, 100_000, 100_000, 300_000),
            "bars5": [("09:30", 101.6, 101.9, 100.05, 101.2), ("09:35", 101.2, 101.7, 101.0, 101.5),
                      ("09:40", 101.5, 102.0, 101.3, 101.8)]},
    # B：盘前跌穿 Wall 50；开盘收回原侧 → failed_breakout
    "BBB": {"prev2": 52.0, "prev": 51.0, "quote": 49.0,
            "cells": [(50, E1, 40e6), (46, E1, 15e6), (55, E2, 12e6)],
            "flow": (100_000, 500_000, 600_000, 100_000),
            "bars5": [("09:30", 49.5, 50.9, 49.4, 50.8), ("09:35", 50.8, 51.2, 50.6, 51.0)]},
    # C：昨日上穿近端负节点 200，上方正节点距离充足；无可信报价
    "CCC": {"prev2": 196.0, "prev": 203.0, "quote": None,
            "cells": [(200, E1, -60e6), (190, E1, 70e6), (220, E1, 50e6)]},
    # F：夹在 302.5 / 307.5 两个正节点之间（ATR≈2）
    "DDD": {"prev2": 305.0, "prev": 305.5, "quote": 305.4,
            "cells": [(302.5, E1, 50e6), (307.5, E1, 45e6), (300, E3, -5e6)]},
}


class CalendarTest(unittest.TestCase):
    def test_holidays_and_clock(self):
        self.assertFalse(is_trading_day(dt.date(2026, 4, 3)))      # Good Friday
        self.assertFalse(is_trading_day(dt.date(2026, 7, 3)))      # 7/4 周六 → 7/3 补休
        self.assertFalse(is_trading_day(dt.date(2026, 11, 26)))    # Thanksgiving
        self.assertTrue(is_trading_day(dt.date(2026, 10, 12)))     # Columbus Day NYSE 照常
        self.assertEqual(session_close_hhmm(dt.date(2026, 11, 27)), "13:00")
        c = market_clock(et_datetime(dt.date(2026, 10, 11), "18:00"))
        self.assertEqual((c.phase, c.target_day, c.prev_full_day), ("closed", TARGET, PREV))
        c = market_clock(et_datetime(dt.date(2026, 10, 9), "17:00"))
        self.assertEqual((c.phase, c.target_day, c.prev_full_day), ("postmarket", TARGET, PREV))
        self.assertEqual(next_trading_day(dt.date(2026, 12, 24)), dt.date(2026, 12, 28))

    def test_utc_micros(self):
        self.assertEqual(utc_micros(et_datetime(TARGET, "09:30")), 1_791_811_800 * 1_000_000)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.mkdtemp()
        self.cfg = SystemConfig()
        self.cfg.data_dir = os.path.join(self.tmp, "data")
        self.cfg.pm_report_dir = os.path.join(self.tmp, "reports")
        self.cfg.pm_shortlist_min = 4

    def tearDown(self):
        logging.disable(logging.NOTSET)
        shutil.rmtree(self.tmp)

    def run_wf(self, hhmm: str, flow_forbidden=False):
        now = et_datetime(TARGET, hhmm)
        client = FakeClient(SCEN, now, flow_forbidden)
        wf = PremarketWorkflow(self.cfg, client, now)
        report = wf.run()
        return wf, {r.entry.ticker: r for r in wf.results}, report, client

    def test_premarket_classification(self):
        wf, r, report, client = self.run_wf("08:50")
        self.assertEqual(wf.clock.phase, "premarket")
        self.assertEqual(wf.rank_info["status"], "previous_full_session")
        self.assertEqual(r["AAA"].setup_type, "positive_wall_reclaim_long")
        self.assertEqual(r["AAA"].crossing, "up:yesterday")
        self.assertEqual(r["AAA"].anchor["strike_usd"], 100)
        self.assertEqual(r["AAA"].flow.flow_support, "bullish")
        self.assertEqual(r["AAA"].status, "candidate")
        self.assertEqual(r["BBB"].setup_type, "positive_wall_loss_short")
        self.assertEqual(r["BBB"].crossing, "down:premarket")
        self.assertEqual(r["BBB"].flow.flow_support, "bearish")
        self.assertEqual(r["CCC"].setup_type, "negative_node_up_acceleration")
        self.assertEqual(r["CCC"].price.status, "quote_unavailable")
        self.assertEqual(r["CCC"].status, "waiting")
        self.assertEqual(r["DDD"].setup_type, "positive_node_bracket")
        self.assertEqual(r["DDD"].watch, "range_or_uncertain")
        # Flow 调用参数：上一完整交易日、1d/7d、single_leg_only
        ov = [p for path, p in client.paths if path.endswith("/overview")]
        self.assertTrue(all(p["session_date"] == PREV.isoformat() and p["single_leg_only"] == "true" for p in ov))
        self.assertEqual({p["window"] for p in ov}, {"1d", "7d"})
        # 开盘前不调 bucket
        self.assertFalse(any("/bucket" in path for path, _ in client.paths))
        text = _read(report)
        self.assertIn("quota.monthly_remaining", text)
        self.assertIn("long_watch", text)
        self.assertNotIn("sk_live", text)

    def test_flow_403_degrades(self):
        wf, r, report, client = self.run_wf("08:50", flow_forbidden=True)
        self.assertIn("flow.ticker_overview", wf.disabled)
        self.assertTrue(all(x.flow.flow_support == "unknown" for x in wf.shortlist))
        n_flow = sum(1 for path, _ in client.paths if path.startswith("/v1/flow/"))
        self.assertEqual(n_flow, 1)                      # 403 后停止调用该能力
        self.assertIn("FLOW_ACCESS_RESTRICTED", _read(report))

    def test_open_confirmation(self):
        wf, r, report, client = self.run_wf("09:46")
        self.assertEqual(wf.clock.phase, "regular")
        self.assertEqual(r["AAA"].status, "confirmed")
        self.assertEqual(r["BBB"].setup_type, "failed_breakout")
        self.assertEqual(r["BBB"].status, "invalidated")
        buckets = [p for path, p in client.paths if "/bucket" in path]
        self.assertTrue(buckets)
        self.assertIn(utc_micros(et_datetime(TARGET, "09:30")), {p["bucket_start_micros"] for p in buckets})

    def test_open_confirmation_without_flow_stays_candidate(self):
        self.cfg.pm_require_flow_for_confirm = True
        wf, r, report, client = self.run_wf("09:46", flow_forbidden=True)
        self.assertEqual(r["AAA"].status, "candidate")


if __name__ == "__main__":
    unittest.main()
