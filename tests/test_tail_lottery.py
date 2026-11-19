"""尾盘彩票精简版测试：全部跑在假行情源 / 合成数据上，不连网、不花配额。
运行：python3 -m unittest tests.test_tail_lottery -v
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import unittest
import urllib.request

from config import et_datetime
from tail_lottery import bsm, direction as D, modules as M, playbook as P
from tail_lottery.demo import DEMO_DAY, make_bucket, make_chain
from tail_lottery.engine import Engine
from tail_lottery.source import BigPrint, Contract, FakeSource, FlowBucket, parse_big_prints, parse_bucket_rows, parse_chain
from tail_lottery.store import Store

UTC = dt.timezone.utc
DAY = DEMO_DAY


def et(hhmm, sec=0):
    return et_datetime(DAY, hhmm).astimezone(UTC) + dt.timedelta(seconds=sec)


CLOSE = et("16:00")


def fmt(t):
    return t.strftime("%H:%M:%S")


# ============================================================================ BSM（手算核对）
class TestBSM(unittest.TestCase):
    def test_textbook_values(self):
        # Hull 例：S=K=100, T=1, r=5%, q=0, σ=20% → C=10.4506, Δ=0.6368, Γ=0.01876
        self.assertAlmostEqual(bsm.price(100, 100, 1, 0.2, "C", r=0.05, q=0), 10.4506, places=4)
        self.assertAlmostEqual(bsm.price(100, 100, 1, 0.2, "P", r=0.05, q=0), 5.5735, places=4)
        self.assertAlmostEqual(bsm.delta(100, 100, 1, 0.2, "C", r=0.05, q=0), 0.6368, places=4)
        self.assertAlmostEqual(bsm.gamma(100, 100, 1, 0.2, r=0.05, q=0), 0.018762, places=5)

    def test_charm_matches_finite_difference(self):
        S, K, T, v = 7330, 7340, 1800 / bsm.YEAR_S, 0.18
        h = 1e-7
        for r in ("C", "P"):
            fd = -(bsm.delta(S, K, T + h, v, r) - bsm.delta(S, K, T - h, v, r)) / (2 * h)
            self.assertAlmostEqual(bsm.charm(S, K, T, v, r) / fd, 1.0, places=4)

    def test_implied_vol_roundtrip(self):
        T = 1200 / bsm.YEAR_S
        px = bsm.price(7330, 7335, T, 0.21, "C")
        self.assertAlmostEqual(bsm.implied_vol(px, 7330, 7335, T, "C"), 0.21, places=4)
        self.assertIsNone(bsm.implied_vol(1e6, 7330, 7335, T, "C"))


# ============================================================================ 解析（夜航社真实返回的字段）
class TestParse(unittest.TestCase):
    def test_parse_chain_and_flow(self):
        chain = {"data": {"quote_as_of": "2026-11-17T20:50:00Z", "greeks_as_of": "2026-11-17T20:50:00Z",
                          "underlying_price_usd": 7340.5, "contracts": [
                              {"contract_symbol": "SPXW261117C07340000", "strike_usd": 7340, "right": "C", "bid_usd": 4.1,
                               "ask_usd": 4.3, "quoted_at": "2026-11-17T20:50:01Z", "open_interest": 8,
                               "implied_vol_pct": 0.323, "delta": 0.5, "gamma": 0.0032}]}, "_meta": {"notice": "stale"}}
        s = parse_chain(chain, et("15:50"), DAY)
        self.assertEqual(len(s.contracts), 1)
        c = s.contracts[0]
        self.assertEqual((c.strike, c.right, c.oi), (7340.0, "C", 8))
        self.assertAlmostEqual(c.mid, 4.2)
        self.assertEqual(s.notice, "stale")
        rows = parse_bucket_rows({"data": {"rows": [
            {"contract_symbol": "O:SPXW261117P07315000", "expiration": "2026-11-17", "right": "P", "strike_usd": 7315,
             "flow_contracts": 100, "gross_premium_usd": 5000, "net_premium_usd": 1000},
            {"contract_symbol": "O:SPXW261118P07315000", "expiration": "2026-11-18", "right": "P", "strike_usd": 7315,
             "flow_contracts": 1, "gross_premium_usd": 1, "net_premium_usd": 1}]}}, DAY)
        self.assertEqual(len(rows), 1)              # 非当日到期的行丢掉
        self.assertEqual(rows[0].symbol, "SPXW261117P07315000")
        bp = parse_big_prints({"data": [{"aggregate_id": "a", "occurred_at": "2026-11-17T20:39:47Z", "expiration": "2026-11-17",
                                         "right": "C", "min_strike_usd": 7510, "contracts": 60, "premium_usd": 60000,
                                         "avg_fill_usd": 10, "nbbo_bid_usd": 9.9, "nbbo_ask_usd": 10, "side": "at_ask",
                                         "contract_symbol": "O:SPXW261117C07510000"},
                                        {"aggregate_id": "pkg", "occurred_at": "2026-11-17T20:39:47Z", "right": None}]}, DAY)
        self.assertEqual([b.agg_id for b in bp], ["a"])


# ============================================================================ 成交方向
class TestDirection(unittest.TestCase):
    def test_broker_side(self):
        self.assertEqual(D.from_side("at_ask"), (D.BUY, 1.0))
        self.assertEqual(D.from_side("above_ask"), (D.BUY, 1.0))
        self.assertEqual(D.from_side("at_bid"), (D.SELL, 1.0))
        self.assertIsNone(D.from_side("mid"))

    def test_quote_rule_tier1(self):
        self.assertEqual(D.quote_rule(1.00, 0.90, 1.00, price_age_s=0.5)[:2], (D.BUY, 1.0))
        self.assertEqual(D.quote_rule(0.90, 0.90, 1.00, price_age_s=0.5)[:2], (D.SELL, 1.0))
        self.assertEqual(D.quote_rule(0.95, 0.90, 1.00, price_age_s=0.5)[:2], (D.UNKNOWN, 0.7))

    def test_quote_rule_tier2_same_price_repeat(self):
        # 同价连续成交：成交价没有新推送（3 秒前）但来自同一次订阅 → 仍用它，0.8 / 0.6
        self.assertEqual(D.quote_rule(1.00, 0.90, 1.00, price_age_s=3, same_subscription=True)[:2], (D.BUY, 0.8))
        self.assertEqual(D.quote_rule(0.95, 0.90, 1.00, price_age_s=3, same_subscription=True)[:2], (D.UNKNOWN, 0.6))

    def test_quote_rule_tier3(self):
        # 来自上一次订阅 / 从未收到 / 落在买卖价之外 → 中间价、未知、0.5
        self.assertEqual(D.quote_rule(1.00, 0.90, 1.00, price_age_s=3, same_subscription=False), (D.UNKNOWN, 0.5, 0.95))
        self.assertEqual(D.quote_rule(None, 0.90, 1.00), (D.UNKNOWN, 0.5, 0.95))
        self.assertEqual(D.quote_rule(1.20, 0.90, 1.00, price_age_s=0.1), (D.UNKNOWN, 0.5, 0.95))

    def test_big_print_falls_back_to_quote_rule(self):
        bp = BigPrint("x", et("15:40"), "", 7340, "C", 60, 6000, 1.0, 0.9, 1.0, None)
        self.assertEqual(D.classify_print(bp), (D.BUY, 1.0))

    def test_split_bucket_row(self):
        from tail_lottery.source import ContractFlow
        r = ContractFlow("x", 7340, "C", contracts=100, gross_premium=10_000, net_premium=4_000)
        buy, sell, unk, bp, sp = D.split_bucket_row(r, 0.8)
        self.assertAlmostEqual(bp, 6000)
        self.assertAlmostEqual(sp, 2000)
        self.assertAlmostEqual(buy + sell + unk, 100)
        self.assertAlmostEqual(unk, 20)


# ============================================================================ 现货与窗口
class TestSpotAndWindow(unittest.TestCase):
    def test_parity_spot(self):
        now = et("15:45")
        ch = make_chain(now, 7331.7, CLOSE, DAY)
        s, n = M.parity_spot(ch, M.years_to_close(now, CLOSE))
        self.assertEqual(n, 3)
        self.assertAlmostEqual(s, 7331.7, delta=0.15)

    def test_window_follows_spot_and_stays_bounded(self):
        """现货单向移动 100 点，GEX / Skew 用的合约窗口始终 ≤ 78 个且以现货为中心。"""
        now = et("15:40")
        for i in range(0, 101, 5):
            spot = 7300 + i
            ch = make_chain(now, spot, CLOSE, DAY, strikes_each=60)
            w = M.strike_window(ch.contracts, spot, M.GEX_STRIKES_EACH_SIDE)
            self.assertLessEqual(len(w), 78)
            ks = sorted({c.strike for c in w})
            self.assertTrue(ks[0] <= spot <= ks[-1])


# ============================================================================ Feed
def _store_with(chain, spot, buckets):
    st = Store()
    st.reset(DAY)
    st.chain = chain
    st.add_spot(chain.fetched_at, spot, True)
    for b in buckets:
        st.add_bucket(b)
    return st


class TestFeed(unittest.TestCase):
    def setUp(self):
        self.now = et("15:40")
        self.ch = make_chain(self.now, 7330, CLOSE, DAY)
        self.k = 7345.0
        self.q = {(c.strike, c.right): c for c in self.ch.contracts}[(self.k, "C")]
        self.assertTrue(0.05 <= self.q.ask <= 4.0)

    def _buckets(self, last_n, last_net=0.0, base_n=10):
        out = []
        for i in range(6):
            n = base_n if i < 5 else last_n
            net = 0.0 if i < 5 else last_net
            px = self.q.mid * 100
            out.append(make_bucket(et("15:33") + dt.timedelta(minutes=i), [(self.k, "C", n, n * px, net * n * px)]))
        return out

    def test_volume_and_premium_spike(self):
        st = _store_with(self.ch, 7330, self._buckets(60))
        M.run_feed(st, self.now, fmt)
        types = {s["type"] for s in st.signals}
        self.assertIn("volume_spike", types)
        self.assertIn("premium_spike", types)
        self.assertNotIn("ask_buy_spike", types)

    def test_ask_buy_spike(self):
        st = _store_with(self.ch, 7330, self._buckets(40, last_net=0.6))
        M.run_feed(st, self.now, fmt)
        s = [x for x in st.signals if x["type"] == "ask_buy_spike"]
        self.assertEqual(len(s), 1)
        self.assertGreaterEqual(s[0]["ask_share"], 0.6)

    def test_no_signal_below_threshold(self):
        st = _store_with(self.ch, 7330, self._buckets(40))
        M.run_feed(st, self.now, fmt)
        self.assertEqual([x for x in st.signals if x["type"] == "volume_spike"], [])

    def test_merge_within_window(self):
        st = _store_with(self.ch, 7330, self._buckets(60))
        M.run_feed(st, self.now, fmt)
        n0 = len(st.signals)
        b = make_bucket(et("15:39"), [(self.k, "C", 200, 200 * self.q.mid * 100, 0)])
        st.add_bucket(b)
        M.run_feed(st, self.now + dt.timedelta(seconds=60), fmt)
        self.assertEqual(len(st.signals), n0)                  # 合并，不新增
        v = [x for x in st.signals if x["type"] == "volume_spike"][0]
        self.assertEqual(v["count"], 2)
        self.assertEqual(v["contracts"], 260)

    def test_reject_untradable_and_itm_and_price_range(self):
        for mutate in (lambda c: setattr(c, "bid", 0.0), lambda c: setattr(c, "bid", c.ask + 0.1),
                       lambda c: setattr(c, "ask", 4.5)):
            ch = make_chain(self.now, 7330, CLOSE, DAY)
            q = {(c.strike, c.right): c for c in ch.contracts}[(self.k, "C")]
            mutate(q)
            st = _store_with(ch, 7330, self._buckets(60))
            M.run_feed(st, self.now, fmt)
            self.assertEqual(len(st.signals), 0)
        # 实值 Call 不看
        self.assertFalse(M.feed_eligible(Contract("x", 7300, "C", bid=1, ask=2), 7330))
        self.assertTrue(M.feed_eligible(Contract("x", 7360, "P", bid=1, ask=2), 7330) is False)

    def test_large_ask_trade_only_buys(self):
        st = _store_with(self.ch, 7330, [])
        st.add_big_prints([BigPrint("b1", self.now, "", self.k, "C", 60, 60 * self.q.ask * 100, self.q.ask, self.q.bid,
                                    self.q.ask, "at_ask"),
                           BigPrint("s1", self.now, "", self.k, "C", 60, 60 * self.q.bid * 100, self.q.bid, self.q.bid,
                                    self.q.ask, "at_bid"),
                           BigPrint("small", self.now, "", self.k, "C", 10, 2000, self.q.ask, self.q.bid, self.q.ask,
                                    "at_ask")], self.now)
        M.run_feed(st, self.now, fmt)
        self.assertEqual([(s["type"], s["contracts"]) for s in st.signals], [("large_ask_trade", 60)])


# ============================================================================ Strike Leader
class TestLeader(unittest.TestCase):
    def test_windows_and_net(self):
        now = et("15:45")
        ch = make_chain(now, 7330, CLOSE, DAY)
        bs = [make_bucket(et("15:30") + dt.timedelta(minutes=i), [(7335, "C", 100, 10_000, 4_000), (7325, "P", 50, 8_000, -2_000)],
                          call_known=1.0, put_known=1.0) for i in range(15)]
        st = _store_with(ch, 7330, bs)
        out = M.run_leader(st, now)
        one = {(r["strike"], r["right"]): r for r in out["1m"]}
        self.assertEqual(one[(7335, "C")]["volume"], 100)
        self.assertEqual(one[(7335, "C")]["net"], 40)          # 买 70 − 卖 30
        self.assertEqual((one[(7325, "P")]["buy"], one[(7325, "P")]["sell"]), (19, 31))   # 买 $3000 / 卖 $5000，每张 $160
        five = {(r["strike"], r["right"]): r for r in out["5m"]}
        self.assertEqual(five[(7335, "C")]["volume"], 500)
        self.assertEqual({(r["strike"], r["right"]): r for r in out["day"]}[(7335, "C")]["volume"], 1500)
        self.assertEqual(out["5m"][0]["strike"], 7335)         # 默认按权利金排序
        self.assertEqual(one[(7335, "C")]["dist"], 5.0)


# ============================================================================ GEX
class TestGex(unittest.TestCase):
    def _chain(self, items, spot=7330):
        now = et("15:40")
        cs = [Contract(f"{k}{r}", k, r, bid=1, ask=1.1, oi=oi, gamma=g) for k, r, oi, g in items]
        from tail_lottery.source import ChainSnapshot
        return ChainSnapshot(now, DAY, now, now, now, spot, now, cs)

    def test_formula_and_nodes(self):
        ch = self._chain([(7340, "C", 1000, 0.01), (7340, "P", 100, 0.01), (7320, "P", 2000, 0.01), (7320, "C", 10, 0.01),
                          (7330, "C", 500, 0.02), (7330, "P", 500, 0.02)])
        g = M.run_gex(ch, 7330)
        f = 100 * 7330 ** 2 * 0.01
        row = {e["strike"]: e for e in g["ladder"]}
        self.assertAlmostEqual(row[7340]["call"], 0.01 * 1000 * f)
        self.assertAlmostEqual(row[7340]["put"], -0.01 * 100 * f)
        self.assertAlmostEqual(row[7330]["net"], 0.0)
        self.assertEqual(g["call_wall"], 7340)
        self.assertEqual(g["put_wall"], 7320)
        self.assertEqual([n["strike"] for n in g["top3"]][:2], [7320, 7340])
        self.assertEqual(g["regime"], "负 Gamma")
        self.assertTrue(g["available"])

    def test_low_coverage_unavailable(self):
        items = [(7300 + 5 * i, r, None, None) for i in range(12) for r in "CP"] + [(7330, "C", 100, 0.01)]
        g = M.run_gex(self._chain(items), 7330)
        self.assertFalse(g["available"])
        self.assertNotIn("regime", g)


# ============================================================================ Skew / Charm
class TestSkewCharm(unittest.TestCase):
    def test_iv_source_priority(self):
        now = et("15:40")
        T = M.years_to_close(now, CLOSE)
        ch = make_chain(now, 7330, CLOSE, DAY, iv=0.2)
        c = [x for x in ch.contracts if x.strike == 7340 and x.right == "C"][0]
        c.iv = 0.33   # 券商 IV 故意与报价不一致
        self.assertEqual(M.select_iv(c, ch, 7330, T, now), (0.33, "broker"))     # 新鲜 → 用券商
        c.quoted_at = ch.greeks_as_of + dt.timedelta(seconds=5)                   # 报价比 IV 新 → 本地反推
        iv, src = M.select_iv(c, ch, 7330, T, now)
        self.assertEqual(src, "local")
        self.assertAlmostEqual(iv, 0.2, delta=0.02)
        c.quoted_at = ch.greeks_as_of
        ch.greeks_as_of = now - dt.timedelta(seconds=300)                         # 券商 IV 过期 → 本地
        self.assertEqual(M.select_iv(c, ch, 7330, T, now)[1], "local")

    def test_local_iv_rejections(self):
        now = et("15:40")
        T = M.years_to_close(now, CLOSE)
        self.assertEqual(M.local_iv(Contract("x", 7340, "C", bid=0, ask=1), 7330, T)[1], "报价无效")
        self.assertEqual(M.local_iv(Contract("x", 7340, "C", bid=1, ask=2.5), 7330, T)[1], "价差过大")
        self.assertEqual(M.local_iv(Contract("x", 7340, "C", bid=0.30, ask=0.50), 7330, T)[1], "价差过大")   # > 中间价 25%
        self.assertEqual(M.local_iv(Contract("x", 7300, "C", bid=10.0, ask=10.5), 7330, T)[1], "超出无套利边界")
        self.assertEqual(M.local_iv(Contract("x", 7400, "C", bid=0.05, ask=0.06), 7330, T)[1], "vega 过小")

    def test_rr25_sign(self):
        now = et("15:00")
        T = M.years_to_close(now, CLOSE)
        ch = make_chain(now, 7330, CLOSE, DAY, iv=0.2, skew=-0.05)   # Put 贵
        sk = M.run_skew(ch, 7330, T, now)
        self.assertTrue(sk["available"])
        self.assertLess(sk["risk_reversal_25d"], -0.005)
        self.assertAlmostEqual(sk["atm_iv"], 0.2, delta=0.01)
        self.assertGreater(sk["rr_legs"]["call"]["strike"], 7330)
        self.assertLess(sk["rr_legs"]["put"]["strike"], 7330)

    def test_charm_time_gate_and_stability(self):
        ch = make_chain(et("15:20"), 7330, CLOSE, DAY)
        self.assertFalse(M.run_charm(ch, 7330, et("15:20"), CLOSE, et("15:20").astimezone(dt.timezone(dt.timedelta(hours=-5))))["available"])
        t = et("15:59", 55)
        r = M.run_charm(make_chain(t, 7330, CLOSE, DAY), 7330, t, CLOSE, t.astimezone(dt.timezone(dt.timedelta(hours=-5))))
        self.assertEqual(r["reason"], "距到期不足 10 秒")
        t = et("15:40")
        loc = t.astimezone(dt.timezone(dt.timedelta(hours=-5)))
        r = M.run_charm(make_chain(t, 7330, CLOSE, DAY), 7330, t, CLOSE, loc)
        self.assertTrue(r["available"])
        self.assertEqual(r["stability"], "NORMAL")
        self.assertIn(r["direction"], ("BUY", "SELL"))
        sparse = make_chain(t, 7330, CLOSE, DAY, oi_fn=lambda k, rr: None if abs(k - 7330) <= 30 else 100)
        self.assertEqual(M.run_charm(sparse, 7330, t, CLOSE, loc)["stability"], "LOW_STABILITY")
        t2 = et("15:58", 30)
        r2 = M.run_charm(make_chain(t2, 7330, CLOSE, DAY), 7330, t2, CLOSE, t2.astimezone(dt.timezone(dt.timedelta(hours=-5))))
        self.assertEqual(r2["stability"], "LOW_STABILITY")

    def test_charm_hand_check(self):
        """单个合约手算：对冲流 = −(charm × dt × OI × 100 × 做市商符号)。"""
        t = et("15:40")
        T = M.years_to_close(t, CLOSE)
        c = Contract("x", 7340, "C", bid=None, ask=None, oi=1000, iv=0.2)
        from tail_lottery.source import ChainSnapshot
        ch = ChainSnapshot(t, DAY, t, t, t, 7330, t, [c])
        r = M.run_charm(ch, 7330, t, CLOSE, t.astimezone(dt.timezone(dt.timedelta(hours=-5))))
        expect = -(bsm.charm(7330, 7340, T, 0.2, "C") * 60 / bsm.YEAR_S * 1000 * 100 * -1)
        self.assertAlmostEqual(r["hedge_flow_delta_1m"], round(expect, 1), places=1)
        self.assertEqual(r["direction"], "BUY" if expect > 0 else "SELL")


# ============================================================================ Playbook
def _inp(now_hhmm="15:40", sec=0, **kw):
    now = et(now_hhmm, sec)
    base = {"now": now, "now_min": int(now_hhmm[:2]) * 60 + int(now_hhmm[3:]), "window_start": et("15:30"), "spot": 7330.0,
            "spots": [(now - dt.timedelta(seconds=30 * i), 7330.0) for i in range(20)][::-1], "flow_600": [], "flow_300": [],
            "gex": {"available": False, "coverage": 0.0}, "skew": {"available": False, "reason": "x"},
            "charm": {"available": False, "reason": "x"}, "day_volume": {}, "mids": {}, "blockers": []}
    base.update(kw)
    return base


def _legs(out):
    return {l["name"]: l for l in out["legs"]}


GEX_NEG = {"available": True, "coverage": 0.9, "total": -1e9, "top3": [{"strike": 7330}, {"strike": 7350}, {"strike": 7300}]}
GEX_POS = dict(GEX_NEG, total=1e9)


class TestPlaybook(unittest.TestCase):
    def test_outside_window_stand_by(self):
        for hh in ("15:29", "16:00", "10:00"):
            self.assertEqual(P.evaluate(_inp(hh))["verdict"], P.STAND_BY)

    def test_blockers_stand_by(self):
        out = P.evaluate(_inp(blockers=["行情中断"]))
        self.assertEqual(out["verdict"], P.STAND_BY)
        self.assertIn("行情中断", out["reason"])

    def test_strike_bias(self):
        flow = [{"strike": 7335, "right": "C", "contracts": 300, "buy": 200, "sell": 50},
                {"strike": 7325, "right": "P", "contracts": 100, "buy": 40, "sell": 40}]
        l = _legs(P.evaluate(_inp(flow_600=flow)))["strike_bias"]
        self.assertEqual((l["side"], l["score"]), (P.CALL, 40))
        l = _legs(P.evaluate(_inp(flow_600=flow[:1][:0] + [dict(flow[0], contracts=100)])))["strike_bias"]
        self.assertIn("< 200", l["abstain"])
        far = [dict(flow[0], strike=7500)]   # 带外不算
        self.assertIn("< 200", _legs(P.evaluate(_inp(flow_600=far)))["strike_bias"]["abstain"])
        conflict = [{"strike": 7335, "right": "C", "contracts": 300, "buy": 0, "sell": 200},
                    {"strike": 7325, "right": "P", "contracts": 100, "buy": 0, "sell": 0}]
        self.assertIn("相反", _legs(P.evaluate(_inp(flow_600=conflict)))["strike_bias"]["abstain"])

    def test_charm_alone_not_enough_and_low_stability_halves(self):
        ch = {"available": True, "direction": "BUY", "hedge_flow_delta_1m": 1000, "stability": "NORMAL"}
        out = P.evaluate(_inp(charm=ch))
        self.assertEqual(_legs(out)["charm_hedge_flow"]["score"], 20)
        self.assertEqual(out["verdict"], P.NEUTRAL)
        out = P.evaluate(_inp(charm=dict(ch, stability="LOW_STABILITY")))
        self.assertEqual(_legs(out)["charm_hedge_flow"]["score"], 10)
        self.assertIn("不可用", _legs(P.evaluate(_inp()))["charm_hedge_flow"]["abstain"])
        self.assertIn("计算失败", _legs(P.evaluate(_inp(charm={"error": True})))["charm_hedge_flow"]["abstain"])

    def test_verdict_gap_and_reference_and_conflict(self):
        flow = [{"strike": 7335, "right": "C", "contracts": 300, "buy": 200, "sell": 50}]
        ch = {"available": True, "direction": "SELL", "hedge_flow_delta_1m": -1, "stability": "NORMAL"}
        out = P.evaluate(_inp(flow_600=flow, charm=ch, day_volume={(7335, "C"): 900, (7340, "C"): 500, (7500, "C"): 9999},
                              mids={(7335, "C"): 2.1}))
        self.assertEqual((out["call_score"], out["put_score"]), (40, 20))
        self.assertEqual(out["verdict"], P.NEUTRAL)        # 分差 20 < 21
        self.assertTrue(out["conflict"])
        out = P.evaluate(_inp(flow_600=flow, day_volume={(7335, "C"): 900, (7500, "C"): 9999}, mids={(7335, "C"): 2.1}))
        self.assertEqual(out["verdict"], P.CALL)
        self.assertEqual(out["reference"], {"strike": 7335, "right": "C", "day_volume": 900, "mid": 2.1})
        self.assertFalse(out["conflict"])

    def test_skew_leg(self):
        sk = {"available": True, "risk_reversal_25d": -0.01, "atm_iv": 0.2}
        self.assertEqual(_legs(P.evaluate(_inp(skew=sk)))["skew_rr25"]["side"], P.PUT)
        self.assertIsNotNone(_legs(P.evaluate(_inp(skew=dict(sk, risk_reversal_25d=0.01))))["skew_rr25"]["abstain"])
        self.assertIn("不可用", _legs(P.evaluate(_inp()))["skew_rr25"]["abstain"])

    def test_gex_regime(self):
        flow = [{"strike": 7335, "right": "C", "contracts": 300, "buy": 200, "sell": 50}]
        l = _legs(P.evaluate(_inp(flow_600=flow, gex=GEX_NEG)))["gex_regime"]
        self.assertEqual((l["side"], l["score"]), (P.CALL, 15))
        l = _legs(P.evaluate(_inp(flow_600=flow, gex=GEX_POS)))["gex_regime"]
        self.assertEqual(l["score"], 0)
        self.assertIn("钉住风险", l["note"])
        self.assertIn("覆盖率", _legs(P.evaluate(_inp()))["gex_regime"]["abstain"])

    def _path(self, now_hhmm, pts):
        now = et(now_hhmm)
        n = len(pts)
        return [(now - dt.timedelta(seconds=30 * (n - 1 - i)), s) for i, s in enumerate(pts)]

    def test_key_level(self):
        self.assertIn("未到", _legs(P.evaluate(_inp("15:45", gex=GEX_NEG)))["key_level"]["abstain"])
        above = self._path("15:52", [7332, 7333, 7334, 7333, 7335])
        l = _legs(P.evaluate(_inp("15:52", gex=GEX_NEG, spots=above, spot=7335)))["key_level"]
        self.assertEqual(l["side"], P.CALL)
        down = self._path("15:52", [7334, 7332, 7329, 7327, 7326])
        self.assertEqual(_legs(P.evaluate(_inp("15:52", gex=GEX_NEG, spots=down, spot=7326)))["key_level"]["side"], P.PUT)
        far = dict(GEX_NEG, top3=[{"strike": 7500}])
        self.assertIn("附近没有", _legs(P.evaluate(_inp("15:52", gex=far)))["key_level"]["abstain"])

    def test_breakout_integrity(self):
        pts = [7328, 7329, 7334, 7335, 7331, 7328, 7327]   # 向上突破 7330 达 5 点后被打回下方
        out = P.evaluate(_inp("15:40", gex=GEX_NEG, spots=self._path("15:40", pts), spot=7327))
        self.assertEqual(_legs(out)["breakout_integrity"]["side"], P.PUT)
        small = [7328, 7329, 7331, 7332, 7329]
        out = P.evaluate(_inp("15:40", gex=GEX_NEG, spots=self._path("15:40", small), spot=7329))
        self.assertIn("不足 3 点", _legs(out)["breakout_integrity"]["abstain"])
        held = [7328, 7329, 7334, 7335, 7336]
        out = P.evaluate(_inp("15:40", gex=GEX_NEG, spots=self._path("15:40", held), spot=7336))
        self.assertIn("未被打回", _legs(out)["breakout_integrity"]["abstain"])

    def test_rejection_reversal(self):
        pts = [7322, 7325, 7329.5, 7327, 7324]   # 从下方触及 7330 后 60 秒内回到下方 → PUT
        ch = {"available": True, "direction": "SELL", "hedge_flow_delta_1m": -1, "stability": "NORMAL"}
        atm = [{"strike": 7330, "right": "P", "contracts": 100, "buy": 0, "sell": 0}]
        out = P.evaluate(_inp("15:52", gex=GEX_NEG, spots=self._path("15:52", pts), spot=7324, charm=ch, flow_300=atm))
        l = _legs(out)["rejection_reversal"]
        self.assertEqual((l["side"], l["score"]), (P.PUT, 35))
        out = P.evaluate(_inp("15:52", gex=GEX_NEG, spots=self._path("15:52", pts), spot=7324))
        self.assertEqual(_legs(out)["rejection_reversal"]["score"], 15)
        self.assertIn("仅 15:50", _legs(P.evaluate(_inp("15:45", gex=GEX_NEG)))["rejection_reversal"]["abstain"])
        few = P.evaluate(_inp("15:52", gex=GEX_NEG, spots=self._path("15:52", [7329, 7330])))
        self.assertIn("样本", _legs(few)["rejection_reversal"]["abstain"])
        flat = P.evaluate(_inp("15:52", gex=GEX_NEG, spots=self._path("15:52", [7310, 7311, 7312, 7311])))
        self.assertIn("没有触及", _legs(flat)["rejection_reversal"]["abstain"])
        self.assertIn("没有节点", _legs(P.evaluate(_inp("15:52")))["rejection_reversal"]["abstain"])

    def test_atm_flow_info_only(self):
        atm = [{"strike": 7330, "right": "C", "contracts": 100, "buy": 0, "sell": 0}]
        out = P.evaluate(_inp(flow_300=atm))
        l = _legs(out)["atm_flow_5m"]
        self.assertEqual((l["side"], l["score"], l["info"]), (P.CALL, 0, True))
        self.assertEqual(out["call_score"], 0)
        self.assertIn("< 50", _legs(P.evaluate(_inp()))["atm_flow_5m"]["abstain"])


class TestRuleChanges1119(unittest.TestCase):
    def test_charm_plus_gex_regime_not_enough(self):
        """负 Gamma 时只有 Charm 领先：gex_regime 不加分，结论为中性。"""
        ch = {"available": True, "direction": "BUY", "hedge_flow_delta_1m": 1000, "stability": "NORMAL"}
        out = P.evaluate(_inp(charm=ch, gex=GEX_NEG))
        self.assertEqual(_legs(out)["gex_regime"]["score"], 0)
        self.assertIn("只有 Charm", _legs(out)["gex_regime"]["note"])
        self.assertEqual(out["verdict"], P.NEUTRAL)
        flow = [{"strike": 7335, "right": "C", "contracts": 300, "buy": 200, "sell": 50}]
        out = P.evaluate(_inp(charm=ch, gex=GEX_NEG, flow_600=flow))   # 有非 Charm 证据 → 照常加分
        self.assertEqual(_legs(out)["gex_regime"]["score"], 15)
        self.assertEqual(out["call_score"], 75)

    def test_conflicting_direction_pushed_as_neutral(self):
        sent = []

        class N:
            def send(self, content="", embeds=None):
                sent.append(embeds[0]["title"] if embeds else content)
        eng = Engine(FakeSource(), notifier=N(), clock=lambda: et("15:40"))
        call_conf = {"verdict": P.CALL, "label": "CALL", "conflict": True, "legs": [], "call_score": 55, "put_score": 10}
        call_ok = dict(call_conf, conflict=False)
        neu = {"verdict": P.NEUTRAL, "label": "中性", "conflict": False, "legs": []}
        eng.store.reset(DAY)
        for pb in (neu, neu, call_conf, call_conf, call_ok, call_ok):
            eng._maybe_push(pb, et("15:40"))
        self.assertEqual(len(sent), 2)
        self.assertIn("中性", sent[0])
        self.assertIn("CALL", sent[1])
        self.assertNotIn("分歧", sent[1])

    def test_pin_waits_for_1510_frame(self):
        """判定的那一刻，15:10 那一分钟的完整轮（GEX 帧）必须已经算完。"""
        for start_sec in (0, 17, 45):   # 让报价轮落在一分钟里的不同位置
            c = Clock(et("15:08", start_sec))
            eng, src = _engine(c)
            while not eng.pin_checked and c.t < et("15:13"):
                eng.step()
                c.t += dt.timedelta(seconds=1)
            self.assertTrue(eng.pin_checked)
            self.assertIn(et("15:10"), eng.store.gex_frames)


class TestPin(unittest.TestCase):
    def _quotes(self, spot=7330):
        ch = make_chain(et("15:10"), spot, CLOSE, DAY)
        return {(c.strike, c.right): c for c in ch.contracts}

    def test_pin_ok_and_missing(self):
        g = {"available": True, "call_wall": 7335, "put_wall": 7325}
        spots = [(et("15:00", 30 * i), 7330 + (i % 3) - 1) for i in range(21)]
        res, why = P.pin_butterfly(g, g, 7330.5, spots, self._quotes())
        self.assertIsNotNone(res, why)
        self.assertIn("7320C", res["structure"])
        self.assertIsNone(P.pin_butterfly(None, g, 7330, spots, self._quotes())[0])
        self.assertIsNone(P.pin_butterfly(dict(g, put_wall=7310), g, 7330, spots, self._quotes())[0])
        wide = [(t, s + (8 if i == 5 else 0)) for i, (t, s) in enumerate(spots)]
        self.assertIsNone(P.pin_butterfly(g, g, 7330.5, wide, self._quotes())[0])
        moved = {"available": True, "call_wall": 7345, "put_wall": 7315}
        self.assertIsNone(P.pin_butterfly(g, moved, 7330.5, spots, self._quotes())[0])


# ============================================================================ 调度 / 降级 / 页面
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _engine(clock, chain_spot=lambda t: 7330.0, error_fn=None, buckets=True):
    bs = {}
    if buckets:
        t = et("14:40")
        while t < CLOSE:
            bs[t] = make_bucket(t, [(7335, "C", 50, 5000, 1000), (7325, "P", 30, 3000, 0)])
            t += dt.timedelta(minutes=1)

    class Src(FakeSource):
        def fetch_bucket(self, session, start, expiration):
            if start >= clock().replace(second=0, microsecond=0):
                return FlowBucket(start=start, present=False)
            return super().fetch_bucket(session, start, expiration)

    src = Src(chain_fn=lambda t: make_chain(t, chain_spot(t), CLOSE, DAY), buckets=bs, error_fn=error_fn)
    return Engine(src, clock=clock, push_enabled=False), src


def run_until(eng, clock, hhmm, step_s=5):
    end = et(hhmm)
    while clock.t < end:
        eng.step()
        clock.t += dt.timedelta(seconds=step_s)
    eng.step()


class TestEngine(unittest.TestCase):
    def test_normal_run_has_four_modules_and_no_skew_charm_panels(self):
        c = Clock(et("14:57"))
        eng, src = _engine(c)
        run_until(eng, c, "15:35")
        s = eng.snapshot
        self.assertEqual(set(s["modules"]), {"feed", "leader", "gex", "playbook", "pin"})
        self.assertNotEqual(s["modules"]["playbook"]["verdict"], P.STAND_BY, s["modules"]["playbook"].get("reason"))
        self.assertEqual(s["status"]["connection"], "已连接")
        self.assertAlmostEqual(s["status"]["spot"], 7330, delta=0.5)
        self.assertIsNotNone(eng.store.skew)
        self.assertIsNotNone(eng.store.charm)

    def test_disconnect_greys_out_and_recovers(self):
        c = Clock(et("15:30"))
        down = {"on": False}
        eng, src = _engine(c, error_fn=lambda t: down["on"])
        run_until(eng, c, "15:35")
        down["on"] = True
        run_until(eng, c, "15:38")
        s = eng.snapshot
        self.assertEqual(s["modules"]["playbook"]["verdict"], P.STAND_BY)
        self.assertIn("行情中断", s["modules"]["playbook"]["reason"])
        self.assertTrue(s["modules"]["gex"]["stale"])
        self.assertIn("断开", s["status"]["connection"])
        down["on"] = False
        run_until(eng, c, "15:40")
        self.assertEqual(eng.snapshot["status"]["connection"], "已连接")
        self.assertNotEqual(eng.snapshot["modules"]["playbook"]["verdict"], P.STAND_BY)

    def test_stale_chain_quotes_stand_by(self):
        c = Clock(et("15:30"))
        eng, src = _engine(c)
        src.chain_fn = lambda t: make_chain(t, 7330, CLOSE, DAY, quote_age_s=600)
        run_until(eng, c, "15:33")
        self.assertIn("过期", eng.snapshot["modules"]["playbook"]["reason"])

    def test_missing_buckets_give_up_and_flow_delay(self):
        c = Clock(et("15:20"))
        eng, src = _engine(c, buckets=False)
        run_until(eng, c, "15:40")
        self.assertGreater(eng.missing_buckets, 0)
        self.assertIn("成交数据延迟", eng.snapshot["modules"]["playbook"]["reason"])

    def test_module_exception_isolated(self):
        c = Clock(et("15:30"))
        eng, src = _engine(c)
        orig = M.run_skew
        M.run_skew = lambda *a: 1 / 0
        try:
            run_until(eng, c, "15:33")
        finally:
            M.run_skew = orig
        legs = _legs(eng.snapshot["modules"]["playbook"])
        self.assertIn("计算失败", legs["skew_rr25"]["abstain"])
        self.assertTrue(eng.snapshot["modules"]["gex"]["available"])

    def test_closed_days_and_hours(self):
        c = Clock(et_datetime(dt.date(2026, 11, 21), "15:40").astimezone(UTC))   # 周六
        eng, src = _engine(c)
        eng.step()
        self.assertIn("休市", eng.snapshot["closed"])
        self.assertEqual(src.calls, 0)
        c.t = et_datetime(dt.date(2026, 11, 27), "15:40").astimezone(UTC)        # 感恩节次日 13:00 收盘
        eng.step()
        self.assertIn("提前收盘", eng.snapshot["closed"])
        c.t = et("11:00")
        eng.step()
        self.assertEqual(src.calls, 0)                                           # 14:58 前不拉数据
        self.assertEqual(eng.snapshot["modules"]["playbook"]["verdict"], P.STAND_BY)

    def test_midnight_reset(self):
        c = Clock(et("15:30"))
        eng, src = _engine(c)
        run_until(eng, c, "15:33")
        self.assertTrue(eng.store.day)
        c.t = et_datetime(dt.date(2026, 11, 18), "00:01").astimezone(UTC)
        eng.step()
        self.assertEqual(eng.store.day, {})
        self.assertEqual(eng.store.session, dt.date(2026, 11, 18))

    def test_quota_floor_stops(self):
        c = Clock(et("15:30"))
        eng, src = _engine(c)
        src.quota_remaining = 100
        eng.step()
        self.assertEqual(src.calls, 0)
        self.assertIn("配额", eng.snapshot["modules"]["playbook"]["reason"])

    def test_web_endpoints(self):
        from tail_lottery.web import serve
        c = Clock(et("15:30"))
        eng, src = _engine(c)
        run_until(eng, c, "15:32")
        srv = serve(eng, "127.0.0.1", 0)
        try:
            port = srv.server_address[1]
            snap = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=5).read())
            self.assertIn("playbook", snap["modules"])
            dbg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/debug", timeout=5).read())
            self.assertIn("skew", dbg)
            self.assertTrue(dbg["series"])
            html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode()
            self.assertIn("仅观察", html)
        finally:
            srv.shutdown()
            srv.server_close()


class TestLiveQuotes(unittest.TestCase):
    def test_stale_base_chain_fresh_quotes(self):
        """链快照落后 13 分钟（报价和 IV 都旧），实时报价新鲜 → 一切照常，IV 全部本地反推。"""
        c = Clock(et("15:28"))
        eng, src = _engine(c)
        fresh_fn = src.chain_fn

        def stale_chain(day, now):
            src.calls += 1
            old = make_chain(now - dt.timedelta(minutes=13), 7300, CLOSE, DAY, iv=0.5, quote_age_s=800, greeks_age_s=800)
            for x in old.contracts:   # 行权价 / OI 与实时一致，只有价格和 IV 是旧的
                x.oi = 1000
            return old
        src.fetch_chain = stale_chain
        src.chain_fn = fresh_fn
        run_until(eng, c, "15:36")
        s = eng.snapshot
        self.assertNotEqual(s["modules"]["playbook"]["verdict"], P.STAND_BY, s["modules"]["playbook"].get("reason"))
        self.assertAlmostEqual(s["status"]["spot"], 7330, delta=0.5)     # 用实时报价，不是旧链的 7300
        self.assertTrue(s["modules"]["gex"]["available"])
        self.assertEqual(eng.store.skew["iv_sources"]["broker"], 0)
        self.assertGreaterEqual(eng.store.skew["iv_sources"]["local"], 10)   # 远虚值买价为 0 的反推不了
        self.assertAlmostEqual(eng.store.skew["atm_iv"], 0.20, delta=0.02)

    def test_quote_budget(self):
        """15:28 之前每 60 秒 6 个报价（另有 15:00 / 15:10 两次完整轮），之后每分钟 40 + 6。"""
        c = Clock(et("14:57"))
        eng, src = _engine(c, chain_spot=lambda t: 7331.3)
        run_until(eng, c, "15:20")
        early = list(src.quote_calls)
        self.assertEqual(sum(1 for n in early if n == 40), 3)   # 第一轮 + 15:00 + 15:10
        self.assertTrue(all(n in (6, 40) for n in early))
        self.assertLessEqual(sum(1 for n in early if n == 6), 23 + 2)   # 约每分钟 1 次
        src.quote_calls.clear()
        run_until(eng, c, "15:40")
        late = src.quote_calls
        self.assertEqual(sum(1 for n in late if n == 40), 13)   # 15:28–15:40 每分钟一次
        self.assertLessEqual(sum(late), 13 * 40 + 34 * 6 + 50)


class TestReadOnly(unittest.TestCase):
    def test_no_order_calls_in_code(self):
        import glob
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = glob.glob(os.path.join(root, "tail_lottery", "*.py")) + [os.path.join(root, "tail_live.py")]
        for f in files:
            with open(f, encoding="utf-8") as fh:
                src = fh.read()
            for bad in ("placeOrder", "place_order", "cancelOrder", "modify_order", "exercise", "method=\"POST\"",
                        "method='POST'"):
                self.assertNotIn(bad, src, f)


if __name__ == "__main__":
    unittest.main()
