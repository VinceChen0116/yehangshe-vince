import json
from importlib.util import find_spec
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from observer.flow_stream import FLOW_URL, PROTOCOL, StreamError, receive_flow
from observer.nightwatch import Client, FLOW_LOOKBACK, FlowSweep, ProviderError, Service
from test_nightwatch import DAY, NOW, engine, iso, order


class StreamCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setup_stream(self, client=None):
        e = engine()
        e.cfg.nightwatch_flow_stream = True
        s = Service(e.cfg, e, client)
        s.day, s.ready = DAY, True
        e.connected = True
        return e, s

    async def test_overlap_switches_to_stream_then_repairs_on_loss(self):
        row = order(session_date=DAY, when=NOW+.5)
        row["bucket_end_at"] = iso(NOW+240)  # a bucket bound is not a trade time
        requests = []

        def reply(r):
            requests.append(r.url.path)
            return httpx.Response(200, json={"data": [row], "_meta": {"next_cursor": None}})

        c = Client("fixture", transport=httpx.MockTransport(reply))
        self.addAsyncCleanup(c.close)
        e, s = self.setup_stream(c)
        s.due.update(gex=NOW+120, chain=NOW+120, quotes=NOW+120)
        s.stream_event(DAY, "subscribed", (), NOW)
        s.stream_event(DAY, "flow", [row], NOW+1)
        self.assertTrue(e.flow_gap)
        self.assertEqual(e.daily[(7405., "C")]["volume"], 60)
        self.assertEqual(e.signals[0]["last_ts"], NOW+.5)
        self.assertFalse(s.stream_covers(NOW+1))

        with patch("observer.nightwatch.time.time", return_value=NOW+2):
            await s.step(NOW+2)
        self.assertTrue(s.stream_proven)
        self.assertFalse(e.flow_gap)
        self.assertEqual(e.daily[(7405., "C")]["volume"], 60)
        self.assertEqual(requests, ["/v1/flow/trades"])
        self.assertEqual(c.units, 1)

        new = dict(row, contracts=90, premium_usd=18000)
        s.stream_event(DAY, "flow", [new, row], NOW+4)
        self.assertEqual(e.daily[(7405., "C")]["volume"], 90)
        s.stream_event(DAY, "heartbeat", (), NOW+28)
        self.assertEqual(e.flow_covered_at, NOW+28)
        with patch("observer.nightwatch.time.time", return_value=NOW+29):
            await s.step(NOW+29)
        self.assertEqual(requests, ["/v1/flow/trades"])

        s.stream_lost("离线测试断线")
        self.assertTrue(e.snapshot(NOW+30)["api"]["flow_gap"])
        self.assertEqual(e.flow_transport, "rest")
        c.next_send = 0
        with patch("observer.nightwatch.time.time", return_value=NOW+30):
            await s.step(NOW+30)
        self.assertEqual(requests, ["/v1/flow/trades"] * 2)
        self.assertFalse(e.flow_gap)
        self.assertEqual(e.daily[(7405., "C")]["volume"], 90)

    async def test_linked_head_finishes_finite_rest_sweep_without_probing_another_head(self):
        calls = []

        def reply(r):
            calls.append(r.url.params.get("cursor"))
            row = order(when=NOW-15, session_date=DAY)
            return httpx.Response(200, json={"data": [row], "_meta": {"next_cursor": None}})

        c = Client("fixture", transport=httpx.MockTransport(reply))
        self.addAsyncCleanup(c.close)
        e, s = self.setup_stream(c)
        s.stream_event(DAY, "subscribed", (), NOW)
        sweep = FlowSweep(NOW+1, e.started_at)
        sweep.visit([NOW], "bridge-page-2")
        s.flow_backlog.append(sweep)
        s.flow_head_requested, s.flow_can_probe = NOW+1, True
        with patch("observer.nightwatch.time.time", return_value=NOW+29):
            await s.flow_page(NOW+29)
        self.assertEqual(calls, ["bridge-page-2"])
        self.assertTrue(s.stream_covers(NOW+29))
        self.assertEqual(e.flow_covered_at, NOW+1)  # an old page doesn't advance to its arrival clock

    async def test_head_started_before_ack_cannot_certify_the_handoff(self):
        clock = NOW
        calls = []

        def reply(r):
            nonlocal clock
            calls.append(r.url.path)
            if len(calls) == 1:
                clock += 1
                s.stream_event(DAY, "subscribed", (), clock)
            return httpx.Response(200, json={"data": [], "_meta": {"next_cursor": None}})

        c = Client("fixture", transport=httpx.MockTransport(reply))
        self.addAsyncCleanup(c.close)
        e, s = self.setup_stream(c)
        s.due.update(gex=NOW+120, chain=NOW+120, quotes=NOW+120)
        with patch("observer.nightwatch.time.time", side_effect=lambda: clock):
            await s.step()
            self.assertTrue(e.snapshot(clock)["api"]["flow_gap"])
            self.assertFalse(s.stream_proven)
            c.next_send = 0
            clock = NOW+8
            await s.step()
        self.assertTrue(s.stream_proven)
        self.assertFalse(e.flow_gap)
        self.assertEqual(calls, ["/v1/flow/trades"] * 2)

    async def test_waits_for_full_push_window_when_rest_never_reaches_acknowledgement(self):
        e, s = self.setup_stream()
        s.stream_event(DAY, "subscribed", (), NOW)
        e.flow_covered_at = NOW-1
        e.flow_gap = False
        self.assertFalse(s.stream_covers(NOW+1))
        for elapsed in range(25, FLOW_LOOKBACK+1, 25):
            s.stream_event(DAY, "heartbeat", (), NOW+elapsed)
            self.assertEqual(s.stream_covers(NOW+elapsed), elapsed >= FLOW_LOOKBACK)
        self.assertEqual(e.flow_covered_at, NOW+FLOW_LOOKBACK)
        self.assertFalse(s.stream_covers(NOW+FLOW_LOOKBACK+41))

    async def test_resync_holds_direction_coverage_despite_a_new_rest_head(self):
        e, s = self.setup_stream()
        s.stream_event(DAY, "subscribed", (), NOW)
        e.flow_gap, e.flow_covered_at = False, NOW+1
        self.assertTrue(s.stream_covers(NOW+1))
        s.stream_event(DAY, "resync", (), NOW+3)
        self.assertTrue(s.stream_repair)
        self.assertFalse(s.stream_covers(NOW+3))
        e.flow_gap, e.flow_covered_at = False, NOW+4
        self.assertTrue(e.snapshot(NOW+4)["api"]["flow_gap"])
        e.flow_covered_at = NOW+FLOW_LOOKBACK+6
        self.assertFalse(e.snapshot(NOW+FLOW_LOOKBACK+6)["api"]["flow_gap"])

    async def test_token_denial_preserves_rest_mode_and_does_not_retry(self):
        client = AsyncMock()
        client.flow_token.side_effect = ProviderError("SCOPE_INSUFFICIENT", status=403)
        e, s = self.setup_stream(client)
        with patch("observer.nightwatch.time.time", return_value=NOW):
            await s.stream_worker()
        client.flow_token.assert_awaited_once()
        self.assertFalse(s.blocked)
        self.assertEqual(e.flow_transport, "rest")
        self.assertIn("暂无推送权限", e.flow_stream_note)

    async def test_token_uses_paced_post_and_only_the_flow_host_at_zero_units(self):
        requests = []

        def reply(r):
            requests.append(r)
            url = FLOW_URL if len(requests) == 1 else "wss://untrusted.invalid/v1/flow/stream"
            return httpx.Response(200, json={"data": {"token": "short.fixture", "flow_url": url}})

        c = Client("long-fixture", max_units=1, transport=httpx.MockTransport(reply))
        self.addAsyncCleanup(c.close)
        self.assertEqual(await c.flow_token(), "short.fixture")
        self.assertEqual(c.units, 0)
        self.assertEqual(c.requests, 1)
        self.assertEqual((requests[0].method, str(requests[0].url)), ("POST", "https://api.yehangshe.com/v1/stream/token"))
        self.assertEqual(requests[0].headers["Authorization"], "Bearer long-fixture")
        c.next_send = 0
        with self.assertRaisesRegex(ProviderError, "INVALID_STREAM_TOKEN"):
            await c.flow_token()


@unittest.skipUnless(find_spec("websockets"), "optional Flow WebSocket dependency")
class StreamProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_heartbeat_and_single_connection_fallback(self):
        from websockets.exceptions import ConnectionClosedError
        from websockets.frames import Close

        frames = iter([{"type": "hello"}, {"type": "ping"},
                       {"type": "flow", "channel": "flow", "rows": [{"not_subscribed": True}]},
                       {"type": "subscribed", "id": "spxw-0dte", "channel": "flow"},
                       {"type": "flow", "channel": "flow", "rows": [{"aggregate_id": "fixture"}]},
                       {"type": "resync", "channel": "flow"}, {"type": "ping"}])
        events, sent = [], []

        class Wire:
            subprotocol = PROTOCOL

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def send(self, data):
                sent.append(json.loads(data))

            async def recv(self):
                frame = next(frames, None)
                if frame is None:
                    raise ConnectionClosedError(Close(4409, "unpublished fixture detail"), None)
                return json.dumps(frame)

        def connect(url, **kwargs):
            self.assertEqual(url, FLOW_URL)
            self.assertEqual(kwargs["subprotocols"], [PROTOCOL, "short.fixture"])
            self.assertEqual(kwargs["compression"], "deflate")
            return Wire()

        with self.assertRaises(StreamError) as exit_:
            await receive_flow("short.fixture", DAY, lambda kind, rows: events.append((kind, rows)), connect=connect)
        self.assertTrue(exit_.exception.stop)
        self.assertIn("同账户已有", exit_.exception.code)
        self.assertEqual([x[0] for x in events], ["subscribed", "flow", "resync", "heartbeat"])
        self.assertEqual(events[1][1], [{"aggregate_id": "fixture"}])
        self.assertEqual(sent[0]["filter"], {"tickers": ["SPXW"], "expirations": [DAY], "kind": "order"})
        self.assertEqual(sent[1:], [{"type": "pong"}, {"type": "pong"}])
