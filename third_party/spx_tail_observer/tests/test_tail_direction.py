from datetime import datetime
import unittest

from observer.clock import NY
from observer.engine import Engine
from observer.nightwatch import NightwatchEngine, symbol
from observer.config import Config


class TailDirectionTest(unittest.TestCase):
    def case(self, minute, prices, skew=None):
        now = datetime(2026, 11, 17, 15, minute, tzinfo=NY).timestamp()
        e = Engine()
        e.spot = prices[-1][1]
        e.spot_history.extend((now+t, price) for t, price in prices)
        gex = {"available": True, "top": [{"strike": 7000}], "regime": "POSITIVE"}
        skew = skew or {"available": False, "rr25": None, "reason": "无样本"}
        charm = {"available": True, "direction": "NEUTRAL", "reason": None}
        b = e.playbook(now, gex, skew, charm, (60-minute)*60, True)
        return b, {v["name"]: (v["side"], v["points"]) for v in b["evidence"]}

    def test_shallow_retouch_is_independent_of_breakout(self):
        path = [(-30, 7003), (-12, 7000), (0, 7002)]
        b, votes = self.case(53, path)
        self.assertEqual(votes["rejection_reversal"], ("CALL", 15))
        self.assertEqual(votes["breakout_integrity"], (None, 0))
        self.assertEqual(b["direction"], "NEUTRAL")
        _, early = self.case(47, path)
        self.assertEqual(early["rejection_reversal"], (None, 0))

    def test_deep_failed_breakout_can_vote_before_1550(self):
        path = [(-60, 6997), (-20, 7003), (0, 6998)]
        _, early = self.case(47, path)
        self.assertEqual(early["breakout_integrity"], ("PUT", 15))
        self.assertEqual(early["rejection_reversal"], (None, 0))
        b, late = self.case(53, path)
        self.assertEqual(late["rejection_reversal"], ("PUT", 15))  # Neutral Charm is not SELL.
        self.assertEqual(b["direction"], "PUT")

    def test_requires_an_ordered_return_and_three_unique_recent_samples(self):
        for path in (
            [(-181, 7004), (-20, 7000), (0, 7002)],
            [(-20, 7003), (-20, 7000), (0, 7002)],
            [(-20, 6996), (-10, 7000), (0, 7004)],
            [(-20, 7003), (-10, 6996), (0, 7000)],
        ):
            with self.subTest(path=path):
                _, votes = self.case(53, path)
                self.assertEqual(votes["rejection_reversal"], (None, 0))
                self.assertEqual(votes["breakout_integrity"], (None, 0))

    def test_near_reference_does_not_move_to_a_cheaper_tighter_or_busier_strike(self):
        t = datetime(2026, 11, 17, 15, 54, tzinfo=NY).timestamp()
        e = NightwatchEngine(Config(source="nightwatch"))
        e.roll_day(t)
        e.spot = 7002
        e.apply_chain([{"contract_symbol": symbol((k, r), e.day)} for k in (7000, 7005, 7010, 7015) for r in "CP"], t)
        e.apply_quotes({"quotes": [
            {"contract_symbol": symbol(key, e.day), "bid_usd": 2, "ask_usd": 2.2,
             "quoted_at": datetime.fromtimestamp(t-(50 if key[0] == 7010 else 0), NY).isoformat()}
            for key in e.focus
        ]}, t)
        for key, qty in [((7000, "C"), 50), ((7005, "C"), 340), ((7010, "C"), 900), ((7005, "P"), 1200)]:
            e.daily[key]["volume"] = qty
        self.assertIsNone(e.option((7005, "C")).cumulative)
        ref = e.playbook_reference(t, "C")
        self.assertEqual((ref["strike"], ref["right"], ref["price_state"]), (7000, "C", "QUOTED"))
        e.apply_quotes({"quotes":[{"contract_symbol":symbol(key,e.day), "bid_usd":.80,
                    "ask_usd":5.10 if key==(7000,"C") else 1., "quoted_at":datetime.fromtimestamp(t,NY).isoformat()}
                    for key in e.focus]},t)
        ref=e.playbook_reference(t,"C")
        self.assertEqual((ref["strike"],ref["ask"],ref["price_state"],ref["ask_ceiling"]), (7000,5.1,"ABOVE_LIMIT",4))
        e.spot=7002.5
        self.assertEqual(e.playbook_reference(t,"P")["strike"],7005)  # Same distance; ITM tie-break mirrored.
        e.apply_quotes({"quotes":[{"contract_symbol":symbol((7005,"C"),e.day), "bid_usd":.95,
                    "ask_usd":1., "quoted_at":datetime.fromtimestamp(t,NY).isoformat()}]},t)
        self.assertIsNone(e.playbook_reference(t,"C"))  # A fresh, cheaper fallback still isn't the nearest strike.
        self.assertIsNone(e.playbook_reference(t+50, "C"))
