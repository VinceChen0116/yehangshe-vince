import asyncio
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import httpx

from observer.clock import NY, Calendar
from observer.config import Config
from observer.nightwatch import Client, FlowSweep, NightwatchEngine, ProviderError, Service, contract, create_nightwatch_app, poll_index_spot, stamp, symbol


DAY = "2026-11-17"
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=NY).timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, NY).isoformat()


def engine():
    e = NightwatchEngine(Config(source="nightwatch", nightwatch_key="fake-test-only"))
    e.started_at = NOW-10
    e.roll_day(NOW)
    e.apply_gex({"ticker": "SPX", "session_date_et": DAY, "snapshot_at": iso(NOW),
                 "spot_usd": 7400, "state": "fresh", "strikes": [{"strike_usd": 7405, "net_gex_usd": 123}],
                 "summary": {"total_gex_usd": 123}}, {"data_freshness_seconds": 0}, NOW)
    return e


def order(identity="a", size=60, premium=12000, when=NOW, **other):
    return {"aggregate_id": identity, "kind": "order", "contract_symbol": "O:SPXW261117C07405000",
            "occurred_at": iso(when), "bucket_end_at": iso(when), "contracts": size,
            "premium_usd": premium, "nbbo_bid_usd": 1.9, "nbbo_ask_usd": 2.0, **other}


class ContractAndDataTests(unittest.TestCase):
    def test_precise_roots_dates_and_timezone_are_required(self):
        self.assertEqual(contract("O:SPXW261117C07405000", DAY), (7405., "C"))
        self.assertEqual(symbol((7405., "C"), DAY), "SPXW261117C07405000")
        self.assertIsNone(contract("SPX261117C07405000", DAY))
        self.assertIsNone(contract("SPXW261118C07405000", DAY))
        self.assertIsNone(stamp("2026-11-17T10:00:00"))

    def test_repeated_aggregate_and_revisions_do_not_double_count(self):
        e = engine()
        e.apply_flow([order()], NOW)
        e.apply_flow([order()], NOW+1)
        e.apply_flow([order(size=80, premium=16000)], NOW+2)
        self.assertEqual(e.daily[(7405., "C")]["volume"], 80)
        self.assertEqual(e.daily[(7405., "C")]["premium"], 16000)
        self.assertEqual(sum(s["size"] for s in e.signals), 60)

    def test_pre_start_orders_are_baselines_not_new_bursts(self):
        e = engine()
        old = order(when=NOW-30)
        old["bucket_end_at"] = iso(NOW)
        e.apply_flow([old], NOW)
        self.assertFalse(e.signals)
        self.assertFalse(e.daily)

    def test_order_outside_covered_source_window_cannot_vote_even_if_bucket_ends_now(self):
        e = engine()
        e.started_at = NOW-1200
        rows = [order(str(i),size=150,premium=30000,when=NOW-601-i) for i in range(2)]
        for row in rows:
            row["bucket_end_at"] = iso(NOW)
        e.apply_flow(rows,NOW,include_backfill=True)
        self.assertEqual(e.daily[(7405., "C")]["volume"], 300)
        b = e.snapshot(NOW)["playbook"]
        self.assertEqual(next(v for v in b["evidence"] if v["name"]=="strike_bias")["points"],0)
        self.assertEqual(e.atm_flow(NOW)["call"],0)

    def test_packages_and_crossed_or_future_data_are_rejected(self):
        e = engine()
        e.apply_flow([order(kind="package"), order("b", nbbo_bid_usd=3), order("c", when=NOW+10)], NOW)
        self.assertFalse(e.daily)
        self.assertFalse(e.signals)

    def test_source_quotes_are_not_retimed_and_missing_quotes_are_invalid(self):
        e = engine()
        e.apply_chain([{"contract_symbol": "SPXW261117C07400000", "open_interest": 20}], NOW)
        row = {"contract_symbol": "SPXW261117C07400000", "bid_usd": 1, "ask_usd": 1.1, "quoted_at": iso(NOW-80)}
        e.apply_quotes({"quotes": [row]}, NOW)
        self.assertEqual(e.option((7400, "C")).md_type, 0)
        self.assertEqual(e.option((7400, "C")).bid_at, NOW-80)
        row["quoted_at"] = iso(NOW)
        e.apply_quotes({"quotes": [row]}, NOW)
        self.assertEqual(e.option((7400, "C")).md_type, 1)
        e.apply_quotes({"quotes": [], "unavailable": [{"contract_symbol": row["contract_symbol"]}]}, NOW+1)
        self.assertEqual(e.option((7400, "C")).md_type, 0)

    def test_minor_clock_skew_does_not_leave_a_fresh_quote_stuck_invalid(self):
        e = engine()
        e.apply_chain([{"contract_symbol": "SPXW261117C07400000"}], NOW)
        row = {"contract_symbol": "SPXW261117C07400000", "bid_usd": 25.1, "ask_usd": 25.3,
               "quoted_at": iso(NOW+1)}
        e.apply_quotes({"quotes": [row]}, NOW)
        # Seven seconds after reception, the visible quote is six seconds old.
        # It must not remain invalid merely because it initially led the PC by 1s.
        s = e.snapshot(NOW+7)
        self.assertEqual(s["quote_coverage"], .5)
        self.assertTrue(next(q for q in s["api"]["quotes"] if q["right"] == "C")["fresh"])

    def test_missing_quote_does_not_revive_from_fresh_looking_old_prices(self):
        e = engine()
        e.apply_chain([{"contract_symbol": "SPXW261117C07400000"}], NOW)
        row = {"contract_symbol": "SPXW261117C07400000", "bid_usd": 25.1, "ask_usd": 25.3, "quoted_at": iso(NOW)}
        e.apply_quotes({"quotes": [row]}, NOW)
        e.apply_quotes({"quotes": [], "unavailable": [{"contract_symbol": row["contract_symbol"]}]}, NOW+1)
        s = e.snapshot(NOW+6)
        q = next(q for q in s["api"]["quotes"] if q["right"] == "C")
        self.assertEqual(q["bid"], 25.1)
        self.assertFalse(q["fresh"])
        self.assertEqual(q["label"], "本轮无报价")
        self.assertIn("上次有效报价", q["reason"])

    def test_large_future_time_and_real_staleness_are_still_rejected(self):
        e = engine()
        e.apply_chain([{"contract_symbol": "SPXW261117C07400000"}], NOW)
        row = {"contract_symbol": "SPXW261117C07400000", "bid_usd": 25.1, "ask_usd": 25.3, "quoted_at": iso(NOW+8)}
        e.apply_quotes({"quotes": [row]}, NOW)
        self.assertFalse(e.quote_fresh((7400,"C"), NOW))
        self.assertEqual(e.quote_diagnostic((7400,"C"), NOW)["label"], "时钟偏差")
        row["quoted_at"] = iso(NOW-46)
        e.apply_quotes({"quotes": [row]}, NOW)
        self.assertFalse(e.quote_fresh((7400,"C"), NOW))
        self.assertEqual(e.quote_diagnostic((7400,"C"), NOW)["label"], "报价过期")

    def test_native_gex_does_not_fabricate_call_put_breakdown(self):
        e = engine()
        g, _, charm, _ = e.model_modules(NOW)
        self.assertTrue(g["native"])
        self.assertTrue(g["available"])
        self.assertIsNone(g["rows"][0]["call"])
        self.assertFalse(charm["available"])
        self.assertFalse(e.model_modules(NOW+46)[0]["available"])

    def test_gex_uses_same_minor_clock_tolerance_as_quotes(self):
        e = engine()
        frame = {**e.native, "snapshot_at": iso(NOW+1)}
        e.apply_gex(frame, {"data_freshness_seconds": 15}, NOW)
        self.assertTrue(e.model_modules(NOW)[0]["available"])

    def test_old_gex_inputs_stay_unavailable_even_with_a_new_frame(self):
        e = engine()
        e.apply_gex(e.native, {"data_freshness_seconds": 60}, NOW)
        result = e.model_modules(NOW)[0]
        self.assertFalse(result["available"])
        self.assertIn("内容", result["reason"])
        self.assertEqual(result["input_age_seconds"], 60)
        self.assertEqual(result["content_age_seconds"], 60)
        self.assertFalse(result["rows"])

    def test_narrow_focus_is_ten_quotes_and_switches_with_price(self):
        e = engine()
        rows = [{"contract_symbol": symbol((k, r), DAY)} for k in range(7350, 7451, 5) for r in ("C", "P")]
        e.apply_chain(rows, NOW)
        before = set(e.focus)
        self.assertEqual(len(before), 10)
        e.apply_gex({"session_date_et": DAY, "snapshot_at": iso(NOW+1), "spot_usd": 7440,
                     "state": "fresh", "strikes": [], "summary": {}}, {}, NOW+1)
        self.assertEqual(len(e.focus), 10)
        self.assertNotEqual(before, e.focus)

    def test_day_roll_clears_dedup_and_native_state(self):
        e = engine()
        e.apply_flow([order()], NOW)
        e.roll_day(NOW+86400)
        self.assertFalse(e.aggregate_seen)
        self.assertFalse(e.focus)
        self.assertIsNone(e.native)
        self.assertFalse(e.daily)

    def test_connected_quotes_do_not_override_missing_flow_coverage(self):
        e = engine()
        e.apply_chain([{"contract_symbol": symbol((k,r),DAY)} for k in range(7380,7421,5) for r in ("C","P")], NOW)
        e.apply_quotes({"quotes": [{"contract_symbol": symbol(k,DAY), "bid_usd": 1.9,
                                     "ask_usd": 2., "quoted_at": iso(NOW)} for k in e.focus]}, NOW)
        e.last_flow_ok = NOW
        e.flow_gap = True
        s = e.snapshot(NOW)
        self.assertTrue(s["api"]["data_connection_ready"])
        self.assertFalse(s["healthy"])
        self.assertEqual(s["playbook"]["direction"], "STAND_BY")
        self.assertIn("缺口", s["playbook"]["reason"])
        self.assertTrue(s["api"]["flow_fresh"])
        self.assertEqual(s["api"]["flow_age_seconds"], 0)
        self.assertTrue(any("覆盖不足" in x for x in s["playbook"]["blockers"]))
        self.assertTrue(any("15:30" in x for x in s["playbook"]["blockers"]))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_charge_uses_successful_contracts_not_request_count(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": {"quotes": [{"contract_symbol": "a"}], "unavailable": [{"contract_symbol": "b"}]}, "_meta": {}},
                                  headers={"X-Quota-Remaining": "100"})
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        await c.get("/v1/options/quotes", {"contracts": "a,b,a"})
        self.assertEqual(c.units, 1)
        self.assertEqual(c.remaining, 99)
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(calls[0].url.host, "api.yehangshe.com")
        await c.close()

    async def test_unchanged_and_not_ready_do_not_charge_and_budget_blocks(self):
        responses = [httpx.Response(304), httpx.Response(202, json={"data": None, "_meta": {"retry_after_seconds": 5}})]
        c = Client("fake-test-only", max_units=1, transport=httpx.MockTransport(lambda r: responses.pop(0)))
        self.assertEqual((await c.get("/v1/derived/dealer-gex/SPX/live")).status, 304)
        c.next_send = 0
        with self.assertRaises(ProviderError):
            await c.get("/v1/derived/dealer-gex/SPX/live")
        self.assertEqual(c.units, 0)
        c.units = 1
        with self.assertRaisesRegex(ProviderError, "LOCAL_BUDGET"):
            await c.get("/v1/derived/dealer-gex/SPX/live")
        await c.close()

    async def test_redirects_are_not_followed_with_credentials(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://example.com/stolen"}, json={})
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError):
            await c.get("/v1/discover")
        self.assertEqual(len(calls), 1)
        await c.close()

    async def test_upstream_error_text_cannot_echo_the_key(self):
        c = Client("fake-test-only", transport=httpx.MockTransport(lambda r: httpx.Response(401,
                   json={"error": {"code": "UNAUTHORIZED", "message": "Invalid fake-test-only"}})))
        with self.assertRaises(ProviderError) as caught:
            await c.get("/v1/discover")
        self.assertNotIn("fake-test-only", caught.exception.detail)
        self.assertIn("[REDACTED]", caught.exception.detail)
        await c.close()

    async def test_calendar_closes_requests_and_does_not_add_1530_gate(self):
        e = engine()
        calls = []
        c = Client("fake-test-only", transport=httpx.MockTransport(lambda r: calls.append(r)))
        s = Service(e.cfg, e, c)
        sunday = datetime(2026, 11, 15, 10, tzinfo=NY).timestamp()
        with patch("observer.nightwatch.time.time", return_value=sunday):
            await s.step(sunday)
        self.assertFalse(calls)
        self.assertFalse(s.ready)
        # A weekday 10:00 is inside the collection window; no 15:30 start gate was added.
        await c.close()
        c.http = httpx.AsyncClient(base_url="https://api.yehangshe.com", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": {"quota": {}, "capabilities_detail": [
                {"command": k, "available": True, "weight": 1} for k in
                ("options.quotes", "options.chain_snapshot", "flow.trades", "derived.dealer_gex_live")
            ]}})))
        with patch("observer.nightwatch.time.time", return_value=NOW):
            await s.step(NOW)
        self.assertTrue(s.ready)
        await c.close()

    async def test_flow_uses_verified_page_limit_and_weekly_root(self):
        e = engine()
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [order()], "_meta": {"next_cursor": None}})
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        s = Service(e.cfg, e, c)
        with patch("observer.nightwatch.time.time", return_value=NOW):
            await s.run_job("flow", NOW)
        self.assertEqual(calls[0].url.params["limit"], "25")
        self.assertEqual(calls[0].url.params["tickers"], "SPXW")
        self.assertEqual(calls[0].url.params["kind"], "order")
        self.assertFalse(e.flow_gap)
        self.assertEqual(e.flow_polls, 1)
        await c.close()

    async def test_independent_index_poll_accepts_documented_reply_and_clears_on_rate_limit(self):
        t = datetime(2026,11,17,15,55,tzinfo=NY).timestamp()
        e = engine()
        replies = [httpx.Response(200,json={"status":"OK","results":[{"ticker":"I:SPX","value":7410,
                    "last_updated":round(t*1e9),"market_status":"regular_trading","timeframe":"REAL-TIME"}]}),
                   httpx.Response(429,headers={"Retry-After":"120"},json={"error":"not part of display"})]
        def handler(request):
            self.assertEqual(request.url.path,"/v3/snapshot/indices")
            self.assertEqual(dict(request.url.params),{"ticker":"I:SPX"})
            return replies.pop(0)
        async with httpx.AsyncClient(base_url="https://api.massive.com",transport=httpx.MockTransport(handler)) as c:
            with patch("observer.nightwatch.time.time",return_value=t):
                self.assertEqual(await poll_index_spot(c,e),5)
                self.assertEqual(list(e.index_samples),[(t,7410.)])
                self.assertEqual(await poll_index_spot(c,e),120)
                self.assertFalse(e.index_samples)
        self.assertNotIn("not part of display",e.index_note)

    async def test_stale_chain_cursor_restarts_from_page_one_after_backoff(self):
        e = engine()
        cursors = []
        def page(strike, cursor):
            return httpx.Response(200, headers={"ETag": f'"{strike}"'}, json={
                "data": {"expiration": DAY, "contracts": [{"contract_symbol": symbol((strike, "C"), DAY)}]},
                "_meta": {"next_cursor": cursor}
            })
        def handler(request):
            self.assertNotIn("if-none-match", request.headers)
            cursor = request.url.params.get("cursor")
            cursors.append(cursor)
            if len(cursors) == 1:
                return page(7300, "old-cursor")
            if cursor == "old-cursor":
                return httpx.Response(400, headers={"Retry-After": "7"}, json={"error": {
                    "code": "VALIDATION_ERROR", "message": "cursor no longer matches the current snapshot; restart pagination"
                }})
            return page(7400, "new-cursor") if cursor is None else page(7405, None)
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        s.ready, s.day = True, DAY
        s.due = dict.fromkeys(("gex", "quotes", "flow"), NOW+90)
        async def step(seconds):
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=NOW+seconds):
                await s.step()
        await step(0)
        self.assertEqual(s.chain_cursor, "old-cursor")
        await step(1)
        self.assertIsNone(s.chain_cursor)
        self.assertFalse(s.chain_rows)
        self.assertIn("从第一页", e.provider_errors["chain"])
        self.assertTrue(e.connected)
        await step(7)
        self.assertEqual(cursors, [None, "old-cursor"])
        await step(8)
        await step(9)
        self.assertEqual(cursors, [None, "old-cursor", None, "new-cursor"])
        self.assertIsNone(s.chain_cursor)
        self.assertNotIn("chain", e.provider_errors)
        self.assertEqual(len(s.chain_rows), 2)
        self.assertIn((7400, "C"), e.focus)

    async def test_dense_flow_continues_and_gex_refreshes_between_pages(self):
        t = datetime(2026, 11, 17, 15, 54, tzinfo=NY).timestamp()
        e = engine()
        e.started_at = t-120
        e.apply_chain([{"contract_symbol": symbol((7405, r), DAY)} for r in "CP"], t)
        # There are 100 orders, and one old boundary row. The 75th row has
        # exactly the watermark timestamp; the next page still has new IDs.
        timestamps = [t-i for i in range(1, 76)] + [t-75]*25 + [t-76]
        rows = [order(str(i), when=ts) for i, ts in enumerate(timestamps)]
        calls = []
        def handler(request):
            path = request.url.path
            cursor = request.url.params.get("cursor")
            calls.append((path.split("/")[-1], cursor))
            if path.endswith("/live"):
                return httpx.Response(200, json={
                    "data": {**e.native, "snapshot_at": iso(wall-1)},
                    "_meta": {"data_freshness_seconds": 2}})
            if path.endswith("/quotes"):
                return httpx.Response(200, json={"data": {"quotes": [
                    {"contract_symbol": symbol(key, DAY), "bid_usd": 1.9, "ask_usd": 2.0, "quoted_at": iso(wall)}
                    for key in e.focus]}})
            self.assertNotIn("if-none-match", request.headers)
            self.assertEqual(request.url.params["session_date"], DAY)
            i = int(cursor or 0)
            return httpx.Response(200, headers={"ETag": "mock-flow-never-reuse"}, json={"data": rows[i:i+25],
                        "_meta": {"next_cursor": str(i+25) if len(rows)>i+25 else None}})
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        s.ready, s.day = True, DAY
        s.flow_mark = t-75
        s.due["chain"] = t+90
        for i, offset in enumerate((0, 5, 10, 15, 19), 1):
            wall = t+offset
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=wall), patch("observer.nightwatch.asyncio.sleep", new_callable=unittest.mock.AsyncMock):
                await s.step()
            f = e.snapshot(wall)
            if i <= 4:
                self.assertEqual(s.flow_mark, t-75)
                self.assertEqual(f["api"]["flow_pending_pages"], i)
                self.assertEqual(f["playbook"]["direction"], "STAND_BY")
            else:
                self.assertFalse(f["api"]["flow_gap"])
        self.assertEqual([cursor for path, cursor in calls if path=="trades"], [None, "25", "50", "75", "100"])
        self.assertLess(calls.index(("trades", "25")), calls.index(("live", None), 1))
        self.assertLess(calls.index(("live", None), 1), calls.index(("trades", "50")))
        self.assertEqual(s.flow_mark, t-1)
        self.assertEqual(f["playbook"]["direction"], "CALL")
        q = e.option((7405, "C"))
        self.assertEqual(len(q.trades), 101)
        self.assertEqual([x["ts"] for x in q.trades], sorted(timestamps))
        self.assertTrue(all(row["ts"] >= t-60 for row in f["signals"]))

    async def test_dense_flow_reuses_overlapping_live_heads_without_rereading_them(self):
        t = datetime(2026, 11, 17, 15, 56, tzinfo=NY).timestamp()
        e = engine()
        e.started_at = t-120
        rows = [order(str(i), when=t-1-i*.2) for i in range(451)]
        newest = order("live", when=t+19)
        cursors = []

        def handler(request):
            cursor = request.url.params.get("cursor")
            i = int(cursor or 0)
            cursors.append(i)
            if not i and len(cursors) > 1:
                data, next_cursor = [newest, *rows[:24]], "head-next"
            else:
                data, next_cursor = rows[i:i+25], str(i+25) if i+25 < len(rows) else None
            return httpx.Response(200, json={"data": data, "_meta": {"next_cursor": next_cursor}})

        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        for offset in range(0, 61, 3):
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=t+offset):
                await s.flow_page(t+offset)
            if offset < 60:
                self.assertEqual(s.flow_mark, e.started_at)
            if offset == 21:
                self.assertIn("live", e.aggregate_seen)
                self.assertEqual(e.flow_pending_pages, 7)  # A head probe does not jump the cursor.
        self.assertEqual([p for p in cursors if p], list(range(25, 451, 25)))
        self.assertIsNone(s.flow_sweep)
        self.assertEqual(s.flow_mark, t+19)
        frame = e.snapshot(t+60)
        self.assertEqual(frame["api"]["flow_coverage_age_seconds"], 18)  # verified live head at +42
        self.assertFalse(frame["api"]["flow_gap"])
        self.assertTrue(e.snapshot(t+88)["api"]["flow_gap"])  # not dated from last historical page at +60
        self.assertEqual(e.daily[(7405, "C")]["volume"], 452*60)

    async def test_exact_time_at_live_head_queues_its_cursor_until_last_tied_id(self):
        e = engine()
        trail = []
        replies = [
            (None, [order("old", when=NOW-3)], "base1"),
            ("base1", [order("middle", when=NOW-4)], "base2"),
            (None, [order("new", when=NOW+19), order("old", when=NOW-3)], "live1"),
            ("live1", [order("tied", when=NOW-3), order("middle", when=NOW-4)], "unused"),
            ("base2", [order("bottom", when=NOW-5)], None),
        ]
        def handler(request):
            cursor, rows, nxt = replies.pop(0)
            self.assertEqual(request.url.params.get("cursor"), cursor)
            trail.append(cursor)
            return httpx.Response(200, json={"data": rows, "_meta": {"next_cursor": nxt}})
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        for offset in (0, 2, 21, 22, 23):
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=NOW+offset):
                await s.flow_page(NOW+offset)
            if offset == 21:
                self.assertEqual(e.flow_pending_ranges, 2)
                self.assertIn("new", e.aggregate_seen)
            if offset == 22:
                self.assertEqual(e.flow_pending_ranges, 1)
                self.assertEqual(s.flow_sweep.cursor, "base2")
                self.assertIsNone(e.flow_covered_at)  # Completing the live segment did not fill the older gap.
            if offset < 23:
                self.assertTrue(e.snapshot(NOW+offset)["api"]["flow_gap"])
        self.assertFalse(e.flow_gap)
        self.assertEqual(s.flow_mark, NOW+19)
        self.assertEqual(e.flow_covered_at, NOW+21)
        self.assertEqual(e.daily[(7405,"C")]["volume"], 5*60)

    async def test_full_flow_queue_keeps_recent_cursor_progress(self):
        e = engine()
        e.started_at = NOW-90
        replies = [
            ("recent", [-29, -30], "recent-2"),
            ("recent-2", [-52, -53], "irrelevant"),
            (None, [1, -27, -29], "irrelevant"),
            ("older", [-72], None),
        ]

        def handler(request):
            expected, times, cursor = replies.pop(0)
            self.assertEqual(request.url.params.get("cursor"), expected)
            return httpx.Response(200, json={"data": [order(str(t), when=NOW+t) for t in times],
                                             "_meta": {"next_cursor": cursor}})

        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        older = FlowSweep(NOW-50, e.started_at)
        older.visit([NOW-52, NOW-54], "older")
        recent = FlowSweep(NOW-25, older.high)
        recent.visit([NOW-27, NOW-28], "recent")
        s.flow_backlog.extend([older, recent])
        s.flow_can_probe, s.flow_head_requested = True, NOW-25
        for seconds in range(4):
            c.next_send = 0
            with (patch("observer.nightwatch.FLOW_MAX_PENDING_RANGES", 2),
                  patch("observer.nightwatch.time.time", return_value=NOW+seconds)):
                await s.flow_page(NOW+seconds)
            if seconds == 0:
                self.assertEqual(s.flow_sweep.cursor, "recent-2")
            if seconds < 3:
                self.assertTrue(e.flow_gap)
                self.assertIsNone(e.flow_covered_at)
        self.assertFalse(replies)
        self.assertFalse(e.flow_gap)
        self.assertEqual(e.flow_covered_at, NOW+2)  # Latest head time, not last old-page time.

    async def test_read_pages_can_prove_rolling_window_when_rest_of_cursor_is_too_old(self):
        e = engine()
        e.started_at = NOW-900

        def handler(request):
            self.assertIsNone(request.url.params.get("cursor"))  # No more requests for the expired gap.
            data = [order(str(offset), when=NOW+offset) for offset in (-1, -7, -8)]
            return httpx.Response(200, json={"data": data, "_meta": {"next_cursor": "unused"}})

        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        active = FlowSweep(NOW-5, e.started_at)
        active.visit([NOW-7, NOW-601], "old-tail")
        s.flow_backlog.append(active)
        with patch("observer.nightwatch.time.time", return_value=NOW):
            await s.flow_page(NOW)
        self.assertFalse(s.flow_backlog)
        self.assertEqual(e.flow_covered_at, NOW)
        self.assertFalse(e.snapshot(NOW)["api"]["flow_gap"])

    async def test_old_backlog_cannot_block_recent_coverage_and_boundary_ties_still_require_paging(self):
        e = engine()
        e.started_at = NOW-1500
        e.flow_covered_at = NOW-1000
        e.flow_gap = True
        replies = [
            ("recent", [order("edge1",when=NOW-600)], "tied"),
            ("tied", [order("edge2",when=NOW-600),order("older",when=NOW-601)], "older-page"),
        ]
        def handler(request):
            cursor,rows,next_cursor = replies.pop(0)
            self.assertEqual(request.url.params.get("cursor"),cursor)
            return httpx.Response(200,json={"data":rows,"_meta":{"next_cursor":next_cursor}})
        c = Client("fake-test-only",transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg,e,c)
        old = FlowSweep(NOW-850,NOW-1000)
        old.visit([NOW-850,NOW-860],"obsolete")
        recent = FlowSweep(NOW-5,old.high)
        recent.visit([NOW-6,NOW-10],"recent")
        s.flow_backlog.extend([old,recent])
        s.flow_mark = NOW-1000
        with patch("observer.nightwatch.time.time",return_value=NOW):
            await s.flow_page(NOW)
            self.assertEqual((e.flow_pending_ranges,e.flow_pending_pages),(1,2))
            self.assertTrue(e.snapshot(NOW)["api"]["flow_gap"])
            self.assertEqual(e.flow_covered_at,NOW-1000)
            c.next_send=0
            await s.flow_page(NOW)
        self.assertFalse(replies)
        self.assertFalse(e.snapshot(NOW)["api"]["flow_gap"])
        self.assertEqual(e.flow_covered_at,NOW-5)
        self.assertEqual(e.daily[(7405.,"C")]["volume"],120)

    async def test_new_sweep_limits_old_unverified_history_to_the_rolling_window(self):
        e = engine()
        e.started_at = NOW-1500
        def handler(request):
            self.assertIsNone(request.url.params.get("cursor"))
            return httpx.Response(200,json={"data":[order("recent",when=NOW-1),order("old",when=NOW-601)],
                                           "_meta":{"next_cursor":"all-older"}})
        c = Client("fake-test-only",transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg,e,c)
        with patch("observer.nightwatch.time.time",return_value=NOW):
            await s.flow_page(NOW)
        self.assertFalse(e.snapshot(NOW)["api"]["flow_gap"])
        self.assertEqual(e.flow_covered_at,NOW)
        self.assertEqual(e.daily[(7405.,"C")]["volume"],60)

    async def test_skipped_or_regressed_source_data_cannot_verify_direction(self):
        e = engine()
        replies = [
            ([order("a")], {"next_cursor": "old"}),
            ([order("b", when=NOW-1)], {"next_cursor": "more", "truncated": True}),
            ([], {"next_cursor": None}),
            ([order("b", when=NOW-1)], {"next_cursor": None}),
            ([order("a"),order("b", when=NOW-1)], {"next_cursor": None}),
        ]
        def handler(request):
            data, meta = replies.pop(0)
            return httpx.Response(200, json={"data": data,"_meta": meta})
        c = Client("fake-test-only",transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg,e,c)
        for offset, error in [(0,None),(1,"FLOW_SOURCE_TRUNCATED"),(11,"FLOW_HEAD_BEHIND"),(16,"FLOW_HEAD_BEHIND"),(21,None)]:
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=NOW+offset):
                if error:
                    with self.assertRaises(ProviderError) as ex:
                        await s.flow_page(NOW+offset)
                    self.assertEqual(ex.exception.code, error)
                else:
                    await s.flow_page(NOW+offset)
            if offset < 21:
                self.assertTrue(e.flow_gap)
                self.assertIsNone(e.flow_covered_at)
                self.assertEqual(s.flow_mark, e.started_at)
        self.assertFalse(e.flow_gap)
        self.assertEqual(e.daily[(7405,"C")]["volume"], 2*60)

    async def test_pagination_loop_does_not_skip_gap_when_retried(self):
        e = engine()
        trail = []

        def handler(request):
            cursor = request.url.params.get("cursor")
            trail.append(cursor)
            rows = [order("latest", when=NOW-1)] if not cursor else [order("middle", when=NOW-2)]
            if len(trail) == 4:
                rows += [order("missing", when=NOW-3)]
            return httpx.Response(200, json={"data": rows, "_meta": {
                "next_cursor": None if len(trail) == 4 else "again"}})

        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        s.ready, s.day = True, DAY
        s.due = dict.fromkeys(("gex", "quotes", "chain"), NOW+90)
        for offset in (0, 1, 6, 7):
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=NOW+offset):
                await s.step()
            if offset < 7:
                self.assertEqual(s.flow_mark, e.started_at)
                self.assertTrue(e.snapshot(NOW+offset)["api"]["flow_gap"])
        self.assertEqual(trail, [None, "again", None, "again"])
        self.assertFalse(e.snapshot(NOW+7)["api"]["flow_gap"])
        self.assertEqual(e.daily[(7405, "C")]["volume"], 3*60)

    async def test_gex_304_reports_a_check_without_resetting_content_age(self):
        e = engine()
        req = []
        def handler(request):
            req.append(request.headers.get("if-none-match"))
            if len(req) == 1:
                return httpx.Response(200, headers={"ETag": '"frame"'}, json={
                    "data": e.native,"_meta":{"data_freshness_seconds": 39}})
            return httpx.Response(304)
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg, e, c)
        for seconds in (0,10):
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=NOW+seconds):
                await s.run_job("gex", NOW+seconds)
        gex = e.snapshot(NOW+10)["gex"]
        self.assertEqual(req, [None, '"frame"'])
        self.assertEqual((gex["http_status"], gex["checked_at"]), (304, NOW+10))
        self.assertEqual(gex["content_age_on_arrival_seconds"], 39)
        self.assertEqual(gex["content_age_seconds"], 49)
        self.assertFalse(gex["available"])

    async def test_flow_cursor_restarts_without_skipping_or_counting_twice(self):
        e = engine()
        trail = []
        def handler(request):
            cursor = request.url.params.get("cursor")
            trail.append(cursor)
            if cursor:
                return httpx.Response(400, headers={"Retry-After": "5"}, json={"error": {
                    "code": "VALIDATION_ERROR", "message": "invalid cursor"}})
            return httpx.Response(200, json={"data": [order(when=NOW)],
                                              "_meta": {"next_cursor": "old" if len(trail)==1 else None}})
        c = Client("fake-test-only", transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(c.close)
        s = Service(e.cfg,e,c)
        s.ready, s.day = True, DAY
        s.due = dict.fromkeys(("gex", "quotes", "chain"), NOW+90)
        for sec in (0,1,5,6):
            c.next_send = 0
            with patch("observer.nightwatch.time.time", return_value=NOW+sec):
                await s.step()
            if sec < 6:
                self.assertEqual(s.flow_mark, e.started_at)
                self.assertTrue(e.flow_gap)
        self.assertEqual(trail, [None, "old", None])
        self.assertFalse(e.flow_gap)
        self.assertEqual(e.daily[(7405, "C")]["volume"], 60)


class RunLogTests(unittest.TestCase):
    def test_tail_log_survives_a_new_service_without_saving_market_data(self):
        t = datetime(2026, 11, 17, 15, 55, tzinfo=NY).timestamp()
        e = engine()
        e.apply_gex({**e.native, "snapshot_at": iso(t)}, {"data_freshness_seconds": 74}, t)
        e.last_flow_ok = t
        e.flow_gap = True
        e.flow_pending_pages = 9
        e.provider_errors["gex"] = "never-save-this-token-or-payload"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "tail-health.log"
            service = Service(e.cfg, e, None, run_log=path)
            service.log_lifecycle("started")
            frame = e.snapshot(t)
            service.record_health(frame)
            service.record_health(e.snapshot(t+1))
            service.log_lifecycle("stopped")
            data = path.read_text(encoding="utf-8")
            events = [json.loads(line) for line in data.splitlines()]
            self.assertEqual([x["event"] for x in events], ["started", "tail_data_check", "stopped"])
            self.assertEqual(events[1]["gex_age_on_arrival_s"], 74)
            self.assertEqual(events[1]["flow_pages_in_progress"], 9)
            self.assertFalse(events[1]["data_ready"])
            self.assertTrue(events[1]["gex_request_failed"])
            self.assertFalse({"direction", "spot", "scores", "quote", "blockers", "gex_request_error"} & events[1].keys())
            self.assertNotIn("never-save", data)
            self.assertNotIn("fake-test-only", data)
            Service(e.cfg, e, None, run_log=path).log_lifecycle("started")
            self.assertTrue(path.read_text(encoding="utf-8").startswith(data))


class RouteTests(unittest.TestCase):
    def test_pages_are_read_only_and_never_expose_key(self):
        from fastapi.testclient import TestClient
        cfg = Config(source="nightwatch", nightwatch_key="sensitive-placeholder")
        e = engine()
        c = Client("sensitive-placeholder", transport=httpx.MockTransport(lambda r: httpx.Response(503, json={"error": {"code": "OFFLINE"}})))
        app = create_nightwatch_app(cfg, c, e, run_log=None)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("quote-tiles", page.text)
            for path in ("/api/snapshot", "/api/debug", "/health"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("sensitive-placeholder", response.text)
                self.assertEqual(client.post(path).status_code, 405)
            self.assertEqual(client.get("/", headers={"host": "evil.example"}).status_code, 400)
        self.assertNotIn("sensitive-placeholder", repr(cfg))


if __name__ == "__main__":
    unittest.main()
