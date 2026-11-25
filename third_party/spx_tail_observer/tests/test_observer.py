import asyncio
from datetime import datetime, timezone
from pathlib import Path
import unittest

from observer.clock import Calendar, NY
from observer.config import Config
from observer.engine import Engine
from observer.mathmodel import bsm, implied, charm_per_minute, YEAR_SECONDS
from observer.subscriptions import Window
from observer.demo import Demo
from observer.app import Inbox, create_app
from observer.ibkr import IBKRAdapter

TS = datetime(2026, 11, 13, 15, 51, tzinfo=NY).timestamp()
KEY = (7555., "C")


def seeded(source="ibkr"):
    e = Engine(Config(source=source))
    for event in [
        {"kind": "status", "connected": True},
        {"kind": "chain", "date": "2026-11-13", "strikes": list(range(7455, 7656, 5))},
        {"kind": "spot_type", "value": 1},
        {"kind": "spot", "value": 7553.},
        {"kind": "subscribed", "key": KEY},
        {"kind": "md_type", "key": KEY, "value": 1},
        {"kind": "quote", "key": KEY, "field": "bid", "value": 1.},
        {"kind": "quote", "key": KEY, "field": "ask", "value": 1.1},
    ]:
        e.handle({"ts": TS, **event})
    return e


def emit(e, kind, offset=0, **fields):
    e.handle({"kind": kind, "ts": TS+offset, **fields})


class MathTests(unittest.TestCase):
    def test_known_bsm_reference_and_put_call_parity(self):
        c = bsm(100, 100, YEAR_SECONDS, .2, "C", .05, 0)
        p = bsm(100, 100, YEAR_SECONDS, .2, "P", .05, 0)
        self.assertAlmostEqual(c["price"], 10.450583572, places=7)
        self.assertAlmostEqual(c["delta"], .636830651, places=7)
        self.assertAlmostEqual(c["gamma"], .018762017, places=7)
        from math import exp
        self.assertAlmostEqual(c["price"]-p["price"], 100-100*exp(-.05), places=9)

    def test_iv_round_trip_and_arbitrage_rejection(self):
        price = bsm(7553, 7555, 600, .23, "C")["price"]
        self.assertAlmostEqual(implied(7553, 7555, 600, price, "C")["iv"], .23, places=7)
        self.assertIsNone(implied(100, 100, 600, 101, "C"))
        self.assertIsNone(implied(100, 90, 600, 1, "C"))

    def test_charm_elapsed_time_sign_and_expiration_guard(self):
        # Within the short-dated distribution: 1% OTM here numerically rounds to zero delta.
        self.assertLess(charm_per_minute(100, 100.05, 900, .2, "C"), 0)
        self.assertGreater(charm_per_minute(100, 99.95, 900, .2, "P"), 0)
        self.assertIsNone(charm_per_minute(100, 100, 9, .2, "C"))


class DataTests(unittest.TestCase):
    def test_post_close_trade_does_not_create_a_feed_event(self):
        e = seeded()
        closing = datetime(2026, 11, 13, 16, 0, tzinfo=NY).timestamp()
        emit(e, "spot", closing-TS, value=7553)
        emit(e, "trade", closing-TS, key=KEY, price=1.1, size=100, side="BUY")
        self.assertFalse(e.signals)
        self.assertFalse(e.options[KEY].trades)

    def test_first_volume_is_baseline_not_a_large_trade(self):
        e = seeded()
        emit(e, "quote", key=KEY, field="last", value=1.1)
        emit(e, "volume", key=KEY, value=100000)
        self.assertEqual(len(e.signals), 0)
        emit(e, "volume", .1, key=KEY, value=100005)
        self.assertEqual(e.daily[KEY]["volume"], 5)

    def test_unchanged_last_is_usable_only_in_current_subscription(self):
        e = seeded()
        emit(e, "quote", key=KEY, field="last", value=1.1)
        emit(e, "volume", key=KEY, value=100)
        emit(e, "volume", 5, key=KEY, value=160)
        t = e.options[KEY].trades[-1]
        self.assertEqual(t["side"], "BUY")
        self.assertEqual(t["quality"], .8)
        emit(e, "subscribed", 6, key=KEY)
        emit(e, "md_type", 6, key=KEY, value=1)
        for field, value in [("bid", 1.), ("ask", 1.1)]:
            emit(e, "quote", 6, key=KEY, field=field, value=value)
        emit(e, "volume", 6, key=KEY, value=1000)
        emit(e, "volume", 7, key=KEY, value=1060)
        t = e.options[KEY].trades[-1]
        self.assertEqual(t["side"], "UNKNOWN")
        self.assertEqual(t["quality"], .5)

    def test_outside_quote_last_is_unknown_and_midpoint_priced(self):
        e = seeded()
        emit(e, "quote", key=KEY, field="last", value=9)
        result = e.classify(e.options[KEY], TS)
        self.assertEqual(result[1:3], ("UNKNOWN", .5))
        self.assertAlmostEqual(result[0], 1.05)

    def test_sampled_quote_age_does_not_invalidate_unchanged_bid_ask(self):
        e = seeded()
        emit(e, "quote", 30, key=KEY, field="ask", value=1.1)
        emit(e, "quote", 30, key=KEY, field="last", value=1.1)
        self.assertEqual(e.classify(e.options[KEY], TS+30)[1], "BUY")
        e.quote_delivery = "streaming"
        self.assertIsNone(e.classify(e.options[KEY], TS+30))

    def test_delayed_or_disconnected_data_cannot_generate_signals(self):
        e = seeded()
        emit(e, "quote", key=KEY, field="last", value=1.1)
        emit(e, "volume", key=KEY, value=100)
        emit(e, "md_type", key=KEY, value=3)
        emit(e, "volume", 1, key=KEY, value=10000)
        self.assertFalse(e.signals)
        self.assertEqual(e.snapshot(TS+1)["playbook"]["direction"], "STAND_BY")
        emit(e, "status", 2, connected=False, message="断线")
        self.assertIsNone(e.options[KEY].cumulative)

    def test_large_trade_merges_without_duplicate_amounts(self):
        e = seeded()
        emit(e, "volume", key=KEY, value=100)
        for seconds, cumulative in [(1, 160), (2, 230)]:
            emit(e, "quote", seconds, key=KEY, field="last", value=1.1)
            emit(e, "volume", seconds, key=KEY, value=cumulative)
        self.assertEqual(len(e.signals), 1)
        row = e.signals[0]
        self.assertEqual(row["updates"], 2)
        self.assertEqual(row["size"], 130)
        self.assertAlmostEqual(row["premium"], 14300)

    def test_volume_reset_and_queue_gap_rebuild_baseline(self):
        e = seeded()
        emit(e, "volume", key=KEY, value=100)
        emit(e, "volume", 1, key=KEY, value=90)
        self.assertFalse(e.options[KEY].trades)
        e.data_gap(4, TS+2)
        emit(e, "volume", 3, key=KEY, value=20000)
        self.assertFalse(e.options[KEY].trades)
        self.assertEqual(e.dropped, 4)
        self.assertEqual(e.snapshot(TS+3)["playbook"]["direction"], "STAND_BY")

    def test_real_tick_path_without_aggregated_volume(self):
        e = seeded()
        emit(e, "trade", .5, key=KEY, price=1.1, size=75, side="BUY")
        self.assertEqual(e.options[KEY].trades[-1]["sampled"], False)
        self.assertEqual(e.signals[-1]["size"], 75)

    def test_day_roll_clears_old_contracts_totals_and_signals(self):
        e = seeded()
        emit(e, "trade", .5, key=KEY, price=1.1, size=75, side="BUY")
        tomorrow = datetime(2026, 11, 16, 9, 31, tzinfo=NY).timestamp()
        e.roll_day(tomorrow)
        self.assertFalse(e.daily)
        self.assertFalse(e.options)
        self.assertFalse(e.signals)
        self.assertIsNone(e.chain_date)


class WindowTests(unittest.TestCase):
    def test_one_hundred_point_move_never_exceeds_budget(self):
        w = Window(78, 19, 60)
        strikes = list(range(7300, 7901, 5))
        removed, added, _ = w.update(7500, strikes, 0)
        self.assertEqual(len(added), 78)
        original = set(w.active)
        w.update(7600, strikes, 1)
        self.assertEqual(set(w.active), original)
        removed, added, wanted = w.update(7600, strikes, 61)
        self.assertTrue(removed)
        self.assertEqual(set(w.active), wanted)
        for t in range(62, 300):
            w.update(7500+t/2, strikes, t)
            self.assertLessEqual(len(w.active), 78)


class ModelAndDecisionTests(unittest.TestCase):
    def test_gex_formula_against_hand_calculated_contracts(self):
        e = seeded()
        e.strikes, e.spot = [100.], 100.
        for right, gamma in [("C", .01), ("P", .02)]:
            q = e.option((100., right))
            q.active, q.md_type = True, 1
            q.greeks, q.greek_at = {"iv": .2, "delta": .5 if right == "C" else -.5, "gamma": gamma}, TS
            q.oi, q.oi_date = 10, "2026-11-12"
        gex, _, _, _ = e.model_modules(TS)
        self.assertEqual(gex["coverage"], 1)
        self.assertEqual(gex["rows"][0]["call"], 1000)
        self.assertEqual(gex["rows"][0]["put"], -2000)
        self.assertEqual(gex["total"], -1000)

    def test_skew_selects_otm_25_delta_sides(self):
        e = seeded()
        e.strikes, e.spot = [95., 100., 105.], 100.
        for strike in e.strikes:
            for right in ("C", "P"):
                q = e.option((strike, right))
                q.active, q.md_type, q.greek_at = True, 1, TS
                iv = .2 if (strike, right) == (105., "C") else .3 if (strike, right) == (95., "P") else .25 if right == "C" else .26
                delta = .25 if (strike, right) == (105., "C") else -.25 if (strike, right) == (95., "P") else .5 if right == "C" else -.5
                q.greeks = {"iv": iv, "delta": delta, "gamma": .01}
        _, skew, _, _ = e.model_modules(TS)
        self.assertTrue(skew["available"])
        self.assertAlmostEqual(skew["rr25"], -.1)
        self.assertAlmostEqual(skew["atm_iv"], .255)

    def test_missing_oi_is_unavailable_not_zero(self):
        e = seeded()
        gex, _, charm, _ = e.model_modules(TS)
        self.assertFalse(gex["available"])
        self.assertIsNone(gex["total"])
        self.assertFalse(charm["available"])

    def test_stale_greeks_reject_wide_quote_and_use_valid_local_iv(self):
        e = seeded()
        q = e.options[KEY]
        q.greeks, q.greek_at = {"iv": .2, "gamma": .05, "delta": .4}, TS-6
        q.bid, q.ask = .1, 3
        self.assertIsNone(e.analytics(q, TS, 540))
        g = bsm(7553, 7555, 540, .23, "C")
        q.bid, q.ask, q.ask_at = g["price"]-.02, g["price"]+.02, TS
        a = e.analytics(q, TS, 540)
        self.assertEqual(a["source"], "报价反推 BSM")
        self.assertAlmostEqual(a["iv"], .23, places=7)

    def test_one_charm_vote_cannot_make_a_direction(self):
        e = seeded()
        gex = {"available": False, "top": [], "regime": "UNAVAILABLE"}
        skew = {"available": False, "reason": "missing", "rr25": None}
        charm = {"available": True, "direction": "BUY", "stability": "NORMAL", "flow_dollars_per_minute": 1000}
        result = e.playbook(TS, gex, skew, charm, 540, True)
        self.assertEqual(result["scores"]["CALL"], 20)
        self.assertEqual(result["direction"], "NEUTRAL")

    def test_before_window_is_always_standby(self):
        e = seeded()
        ts = datetime(2026, 11, 13, 14, 0, tzinfo=NY).timestamp()
        gex = {"available": False, "top": [], "regime": "UNAVAILABLE"}
        no_skew = {"available": False, "reason": "missing", "rr25": None}
        no_charm = {"available": False, "reason": "missing", "direction": "NEUTRAL"}
        self.assertEqual(e.playbook(ts, gex, no_skew, no_charm, 7200, True)["direction"], "STAND_BY")

    def test_early_close_calendar_and_holiday(self):
        c = Calendar()
        if c.cal is None:
            self.skipTest("exchange-calendars not installed")
        session = c.session(datetime(2026, 11, 27, 12, tzinfo=NY))
        self.assertEqual(session["close"].astimezone(NY).hour, 13)
        self.assertIsNone(c.session(datetime(2026, 11, 26, 12, tzinfo=NY))["open"])

    def test_demo_computes_models_and_broker_is_never_connected(self):
        e = Engine(Config(source="demo"))
        demo = Demo(e)
        demo.bootstrap()
        s = e.snapshot(demo.clock)
        self.assertTrue(s["demo"])
        self.assertTrue(s["gex"]["available"])
        self.assertEqual(s["active_options"], 78)
        self.assertGreaterEqual(len(s["signals"]), 1)
        self.assertEqual(s["gex"]["oi_date"], "2026-11-12")


class AdapterAndServiceTests(unittest.TestCase):
    def test_quota_errors_reduce_budget_then_stop_retries(self):
        events = []
        adapter = IBKRAdapter(Config(), events.append)
        limits = []
        for _ in range(3):
            result = adapter.dispatch({"kind": "control_error", "ts": TS, "req_id": -1, "code": 101})
            limits.append(next(x["market_lines"] for x in result if x["kind"] == "capacity"))
        self.assertEqual(limits, [75, 56, 42])
        self.assertTrue(adapter.stop_event.is_set())

    def test_bounded_inbox_keeps_latest_and_reports_loss(self):
        inbox = Inbox(2)
        for i in range(5):
            inbox.put({"i": i})
        events, dropped = inbox.drain()
        self.assertEqual([x["i"] for x in events], [3, 4])
        self.assertEqual(dropped, 3)

    def test_adapter_issues_only_discovery_and_market_data_requests(self):
        try:
            from ibapi.contract import Contract
        except ImportError:
            self.skipTest("ibapi not installed")
        events, calls = [], []

        class Stub:
            def __getattr__(self, name):
                def f(*args):
                    calls.append((name, args))
                return f

        a = IBKRAdapter(Config(), events.append)
        a.client, a.Contract, a.session_date = Stub(), Contract, "2026-11-13"
        a.cfg.request_rate = 45
        a._execute("ready", None)
        a._execute("underlying_found", 416904)
        a._execute("subscribe", KEY)
        a._execute("resolved", (KEY, 123456))
        req_id = a.active[KEY]
        mapped = a.dispatch({"kind": "control_size", "ts": TS, "req_id": req_id, "tick_type": 8, "value": 1000})
        self.assertEqual(mapped[0]["kind"], "volume")
        a._execute("unsubscribe", KEY)
        self.assertFalse(a.dispatch({"kind": "control_price", "ts": TS+1, "req_id": req_id, "tick_type": 4, "value": 2}))
        allowed = {"reqMarketDataType", "reqContractDetails", "reqMktData", "reqSecDefOptParams", "cancelMktData"}
        self.assertLessEqual({name for name, _ in calls}, allowed)
        option_requests = [args for name, args in calls if name == "reqMktData" and args[1].secType == "OPT"]
        self.assertEqual(option_requests[0][2], "100,101")
        self.assertEqual(option_requests[0][3:5], (False, False))

    def test_demo_http_routes_and_query_controls(self):
        from fastapi.testclient import TestClient
        with TestClient(create_app(Config(source="demo"))) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertFalse(client.get("/health").json()["broker_connected"])
            response = client.get("/api/snapshot?window=60&sort=net")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["demo"])
            self.assertIn("series", client.get("/api/debug").json())
            self.assertEqual(client.post("/api/snapshot").status_code, 405)


if __name__ == "__main__":
    unittest.main()
