"""baseline.py 的单元测试：python3 -m unittest tests.test_baseline -v（只用标准库，0 配额）"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import baseline as B                                   # noqa: E402
from config import SystemConfig, et_datetime           # noqa: E402
from detector import SignalDetector                    # noqa: E402
from strategy_core_spec import compute_continuous_option_ratchet  # noqa: E402

DAY = dt.date(2026, 10, 19)
EXP = DAY.isoformat()
NEXT = "2026-10-20"
CFG = SystemConfig.load(None)


def payload(spot, cells, extra=None, t=None):
    """cells: {strike: gex}（主结构到期 = 当天 0DTE）；extra: {exp: {strike: gex}}。"""
    cl = [{"strike_usd": k, "expiration": EXP, "net_dealer_gex_usd": g} for k, g in cells.items()]
    for e, cs in (extra or {}).items():
        cl += [{"strike_usd": k, "expiration": e, "net_dealer_gex_usd": g} for k, g in cs.items()]
    return {"data": {"ticker": "TEST", "spot_usd": spot, "session_date_et": EXP,
                     "generated_at": (t or et_datetime(DAY, "10:00")).isoformat(),
                     "expirations": sorted({c["expiration"] for c in cl}), "cells": cl}}


def run(spots, cells_seq, ticker="TEST", start="10:00", extra=None):
    """依次喂入快照（5 分钟一根），返回 (judge, 最后的信号, 最后的 snapshot)。cells_seq 可以是单个 dict（每根相同）。"""
    j = B.BaselineJudge(CFG)
    t0 = et_datetime(DAY, start)
    sig = snap = None
    for i, s in enumerate(spots):
        cells = cells_seq[i] if isinstance(cells_seq, list) else cells_seq
        t = t0 + dt.timedelta(minutes=5 * i)
        fr, snap, new = j.observe(ticker, payload(s, cells, extra, t), t, DAY)
        assert fr is not None and new
    sig = j.evaluate(ticker, snap, t + dt.timedelta(minutes=5))
    return j, sig, snap


M = 1_000_000
CLIFF_CELLS = {98.0: 10 * M, 98.5: -15 * M, 99.5: -3 * M, 100.75: 20 * M, 102.0: 5 * M, 97.0: 2 * M}
REJECT_PATH = [100.4, 100.6, 100.8, 100.9, 100.7, 100.5]   # 冲高后回落：5 分钟 −0.2%、15 分钟 −0.4%，开盘 +0.1%


class TestHelpers(unittest.TestCase):
    def test_min_gex(self):
        self.assertEqual(B.min_gex("QQQ", 700), 2_500_000)
        self.assertEqual(B.min_gex("NVDA", 217), 1_500_000)       # 217 × 8000 = 1.74M → 封顶 1.5M
        self.assertEqual(B.min_gex("SOFI", 20), 800_000)          # 20 × 8000 = 0.16M → 保底 0.8M
        self.assertAlmostEqual(B.min_gex("XYZ", 150), 1_200_000)
        self.assertFalse(B.is_index("SPY"))

    def test_context(self):
        j, _, _ = run([100.0, 100.1, 100.2, 100.3, 100.25], CLIFF_CELLS)
        cx = B.context(j.frames["TEST"])
        self.assertAlmostEqual(cx.tick_ret_5m, 100.25 / 100.3 - 1)
        self.assertAlmostEqual(cx.ret_15m, 100.25 / 100.1 - 1)   # 15 分钟前那根
        self.assertAlmostEqual(cx.vwap, sum([100.0, 100.1, 100.2, 100.3, 100.25]) / 5)
        self.assertAlmostEqual(cx.intraday_ret, 0.0025)

    def test_context_needs_15m(self):
        j, sig, _ = run([100.0, 100.1], CLIFF_CELLS)
        self.assertIsNone(B.context(j.frames["TEST"]).ret_15m)
        self.assertIsNone(sig)

    def test_parity_and_pair(self):
        self.assertEqual(B.pair_symbol("O:NVDA261019C00217500"), "O:NVDA261019P00217500")
        q = {"NVDA261019C00217500": {"bid": 1.0, "ask": 1.2, "mid": 1.1},
             "NVDA261019P00217500": {"bid": 0.6, "ask": 0.8, "mid": 0.7}}
        s = B.parity_spot("O:NVDA261019P00217500", lambda sym: q.get(sym.split(":")[-1]))
        self.assertAlmostEqual(s, 217.5 + 1.1 - 0.7)
        self.assertIsNone(B.parity_spot("O:NVDA261019P00217500", lambda sym: None))


class TestLayer2(unittest.TestCase):
    def test_growth_states(self):
        base = dict(CLIFF_CELLS)
        grow = {**base, 100.75: 24 * M}          # +20%
        decay = {**base, 100.75: 15 * M}         # −25%
        flat = {**base, 100.75: 21 * M}          # +5%
        for last, want in ((grow, "GROWING"), (decay, "DECAYING"), (flat, "FLAT")):
            j, _, _ = run(REJECT_PATH, [base] * 5 + [last])
            self.assertEqual(B.node_growth(j.frames["TEST"], 100.75), want)
        newc = {**base, 101.5: 9 * M}
        j, _, _ = run(REJECT_PATH, [base] * 5 + [newc])
        self.assertEqual(B.node_growth(j.frames["TEST"], 101.5), "NEW")
        back = [newc] + [base] * 4 + [newc]                   # 以前出现过又消失：不算 NEW
        j, _, _ = run(REJECT_PATH, back)
        self.assertEqual(B.node_growth(j.frames["TEST"], 101.5), "UNKNOWN")
        j, _, _ = run([100.5, 100.6], base)
        self.assertEqual(B.node_growth(j.frames["TEST"], 100.75), "UNKNOWN")   # 没有 9–27 分钟前的快照

    def test_migration(self):
        a = {98.0: -10 * M, 100.0: 20 * M}
        b = {98.0: -10 * M, 101.0: 20 * M}
        j, _, _ = run([100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6], [a] * 4 + [b] * 3)
        self.assertEqual(B.wall_migration(j.frames["TEST"], 1 * M), "MIGRATING_UP")
        j, _, _ = run([100.6, 100.5, 100.4, 100.3, 100.2, 100.1, 100.0], [b] * 4 + [a] * 3)
        self.assertEqual(B.wall_migration(j.frames["TEST"], 1 * M), "MIGRATING_DOWN")
        j, _, _ = run([100.0] * 7, a)
        self.assertEqual(B.wall_migration(j.frames["TEST"], 1 * M), "STABLE")
        j, _, _ = run([100.0] * 6, [a, b, a, b, a, b])
        self.assertEqual(B.wall_migration(j.frames["TEST"], 1 * M), "WHIPSAW")

    def test_confluence_conflict(self):
        fr = B.Frame(DAY and et_datetime(DAY, "10:00"), 100.0, EXP, sorted(CLIFF_CELLS.items()),
                     {EXP: dict(CLIFF_CELLS), NEXT: {100.75: 3 * M}, "2026-10-21": {100.75: 1 * M}})
        self.assertEqual(B.expiry_confluence(fr, 100.75), (2, False))
        fr.by_exp[NEXT] = {100.75: -3 * M}
        self.assertEqual(B.expiry_confluence(fr, 100.75), (1, True))


class TestCliff(unittest.TestCase):
    def test_pass(self):
        j, sig, _ = run(REJECT_PATH, CLIFF_CELLS)
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))
        self.assertEqual(sig.cand.structure_type, "CLIFF_STRUCTURE")
        self.assertEqual(sig.structure.right, "P")
        self.assertEqual(sig.cand.acting_strike, 100.75)
        self.assertEqual(sig.target_spot, 98.0)                         # 下方超大正节点（≥ 0.43 × 20M）
        self.assertEqual(sig.cand.trigger, "REJECTION_DROP")              # 离天花板 0.25% ≤ 0.6%
        self.assertAlmostEqual(sig.stop_spot, round(100.5 * 1.008, 2))     # 风险 max(0.8%, 0.25%+0.3%)
        self.assertGreaterEqual(sig.score, 55)

    def assertReject(self, spots, cells, text, extra=None):
        j, sig, _ = run(spots, cells, extra=extra)
        self.assertIsNone(sig)
        self.assertIn(text, j.last_reason["TEST"])

    def test_ceiling_too_close(self):
        self.assertReject([100.4, 100.6, 100.8, 100.9, 100.8, 100.74], CLIFF_CELLS, "离天花板 100.75 0.010%")

    def test_no_ceiling(self):
        self.assertReject(REJECT_PATH, {98.5: -15 * M, 97.0: 2 * M}, "上方没有显著正节点")

    def test_space(self):
        self.assertReject(REJECT_PATH, {99.8: -15 * M, 100.75: 20 * M}, "没有超大正节点承接（宁缺毋滥）")
        self.assertReject(REJECT_PATH, {99.8: -15 * M, 100.75: 20 * M, 99.9: 15 * M}, "下方空间 0.60%（目标 = 下方超大正节点 99.9")

    def test_target_is_next_big_positive(self):
        # 用户口径：目标 = 下方下一个超大正节点（支撑）；紧贴的负节点是燃料
        cells = {100.75: 20 * M, 100.2: -30 * M, 99.9: -10 * M, 99.0: 15 * M, 99.5: 3 * M, 97.0: -5 * M}
        j, sig, _ = run(REJECT_PATH, cells)
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))
        self.assertEqual(sig.target_spot, 99.0)                 # 99.5(+3M) 不够大，可以穿过
        self.assertIn("目标 = 下方超大正节点 99", sig.cand.notes)
        j, sig, _ = run(REJECT_PATH, {100.75: 20 * M, 100.2: -30 * M, 99.9: 12 * M})
        self.assertIsNone(sig)                                  # 下一个超大正节点只有 0.6% → 空间不够

    def test_chasm_skips_adjacent_negative(self):
        # 紧贴现价的大负节点（0.3%）是燃料；目标取 1%–4.5% 内最大的负节点
        j, sig, _ = run(REJECT_PATH, {**CLIFF_CELLS, 100.2: -40 * M})
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))
        self.assertEqual(sig.target_spot, 98.0)

    def test_rr(self):
        cells = {98.6: 10 * M, 98.8: -15 * M, 101.9: 20 * M}                # 离墙 1.39% → 风险 1.5%，空间 1.89% → 1.26
        self.assertReject(REJECT_PATH, cells, "盈亏比 1.26")

    def test_floor_block(self):
        self.assertReject(REJECT_PATH, {**CLIFF_CELLS, 100.2: 3 * M}, "下方 0.45% 内有强正地板 100.2")

    def test_gatekeeper_moves_target(self):
        j, sig, _ = run(REJECT_PATH, {**CLIFF_CELLS, 99.4: 12 * M})   # 1.09% 处强正节点 ≥ 0.45×20M
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))
        self.assertEqual(sig.target_spot, 99.4)

    def test_sprint(self):
        self.assertReject([99.0, 99.4, 99.8, 100.1, 100.4, 100.7], {**CLIFF_CELLS, 100.75: 0, 101.2: 20 * M}, "向上冲刺")

    def test_no_rejection(self):
        self.assertReject([100.6, 100.5, 100.4, 100.45, 100.5, 100.6], CLIFF_CELLS, "没有受阻回落")

    def test_path_without_negative(self):          # INTC 型：现价到目标之间只有正节点
        self.assertReject(REJECT_PATH, {100.75: 20 * M, 100.0: 3 * M, 98.0: 10 * M, 96.0: -5 * M}, "之间没有显著负节点")

    def test_path_balance(self):                   # 路径上正负差不多 → 走不动
        self.assertReject(REJECT_PATH, {100.75: 20 * M, 100.0: 6 * M, 99.0: -8 * M, 98.0: 10 * M}, "不到正节点合计")

    def test_outer_bigger_wall(self):              # TSLA 型：天花板外侧 3 格内有更大的正节点
        self.assertReject(REJECT_PATH, {**CLIFF_CELLS, 101.25: 30 * M}, "外侧 3 格内有更大的正节点 101.25")
        j, sig, _ = run(REJECT_PATH, {**CLIFF_CELLS, 102.5: 30 * M})        # 第 3.5 格：不算
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))

    def test_pin_negative(self):                   # AMD 型：路径上全到期最大负节点 ≥ 3 × 墙
        self.assertReject(REJECT_PATH, {**CLIFF_CELLS, 100.25: -65 * M}, "可能被钉住")

    def test_no_negative_target_fallback(self):    # META 型：下方没有大正节点 → 宁缺毋滥
        self.assertReject(REJECT_PATH, {100.75: 20 * M, 99.5: -6 * M, 98.5: -6 * M, 98.0: 4 * M}, "宁缺毋滥")

    def test_outside_wall_too_big(self):            # PLTR 型：天花板外侧（上方）有 2 倍以上的正节点 → 墙太弱
        cells = {**CLIFF_CELLS, 104.0: 41 * M}
        j, sig, _ = run(REJECT_PATH, cells)
        self.assertIsNone(sig)
        self.assertIn("墙太弱", j.last_reason["TEST"])
        j, sig, _ = run(REJECT_PATH, {**CLIFF_CELLS, 104.0: 39 * M})   # 20M ≥ 50% × 39M → 照常
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))

    def test_magnet_far_growing_wall(self):
        cells = [(98.0, 10 * M), (98.5, -15 * M), (99.5, -3 * M), (100.75, 20 * M), (102.0, 5 * M)]
        ok, _ = B.magnet_check(cells, [], 100.0, 100.75, 0.0075, above=True)     # 没有历史 → UNKNOWN → 放行
        self.assertTrue(ok)
        ok, _ = B.magnet_check(cells, [], 100.5, 100.75, 0.0025, above=True)     # 离墙 ≤ 0.6% 不查
        self.assertTrue(ok)

    def test_magnet_in_cliff(self):                 # 天花板 15 分钟长 50%、离现价 >0.6% → 磁吸不开
        seq = [{**CLIFF_CELLS, 100.75: g} for g in (12 * M, 12 * M, 12 * M, 14 * M, 16 * M, 20 * M)]
        path = [99.7, 99.9, 100.1, 100.2, 100.0, 99.8]
        j, sig, _ = run(path, seq)
        self.assertIsNone(sig)
        self.assertIn("磁吸", j.last_reason["TEST"])

    def test_score_gate(self):
        # 天花板在撤单（节点 DECAYING −16）+ 主正墙 30 分钟内上移且现价走高（MIGRATING_UP，与看跌相反 −12）：
        # 几何与触发都通过，但评分 58 − 16 − 12 = 30 < 55
        seq = [{**CLIFF_CELLS, 100.75: 40 * M}] * 4 + [{**CLIFF_CELLS, 100.75: 20 * M, 103.0: 30 * M}] * 3   # 新主墙在 4.5 格外
        j, sig, _ = run([100.0, 100.2, 100.4, 100.6, 100.8, 100.7, 100.5], seq)
        self.assertIsNone(sig)
        self.assertIn("评分 30 < 55", j.last_reason["TEST"])
        self.assertIn("节点 DECAYING", j.last_reason["TEST"])
        self.assertIn("墙体 MIGRATING_UP", j.last_reason["TEST"])


SPRING_CELLS = {99.5: 20 * M, 102.0: -15 * M, 101.0: 3 * M, 98.0: -2 * M, 102.5: 10 * M}
BOUNCE_PATH = [99.5, 99.6, 99.65, 99.7, 99.75, 99.95]


class TestSpringboard(unittest.TestCase):
    def test_pass(self):
        j, sig, _ = run(BOUNCE_PATH, SPRING_CELLS)
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))
        self.assertEqual(sig.cand.structure_type, "SPRINGBOARD_STRUCTURE")
        self.assertEqual(sig.structure.right, "C")
        self.assertEqual(sig.target_spot, 102.5)
        self.assertAlmostEqual(sig.stop_spot, round(99.95 * (1 - 0.008), 2))
        self.assertEqual(sig.cand.trigger, "REJECTION_BOUNCE")

    def test_near_floor_needs_growth_or_bounce(self):
        j, sig, _ = run([99.5, 99.6, 99.65, 99.7, 99.75, 99.8], SPRING_CELLS)   # 贴近踏板、5 分钟只 +0.05%
        self.assertIsNone(sig)
        self.assertIn("也没有反弹", j.last_reason["TEST"])
        grow = [SPRING_CELLS] * 5 + [{**SPRING_CELLS, 99.5: 22 * M}]           # +10% → GROWING
        j, sig, _ = run([99.5, 99.6, 99.65, 99.7, 99.75, 99.8], grow)          # 踏板在变大 → 放行
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))

    def test_floor_not_dominant(self):
        j, sig, _ = run(BOUNCE_PATH, {**SPRING_CELLS, 101.0: 18 * M})
        self.assertIn("不到上方天花板的 1.2 倍", j.last_reason["TEST"])

    def test_floor_too_close(self):
        j, sig, _ = run([99.5, 99.6, 99.7, 99.6, 99.55, 99.6], SPRING_CELLS)
        self.assertIn("离踏板 99.5 0.100%", j.last_reason["TEST"])

    def test_falling_knife(self):
        j, sig, _ = run([100.6, 100.4, 100.2, 100.0, 99.9, 99.85], SPRING_CELLS)
        self.assertIsNone(sig)
        self.assertIn("跳板：接飞刀", j.last_reason["TEST"])


class TestTrendArchetypes(unittest.TestCase):
    def test_squeeze(self):
        cells = {102.5: 40 * M, 99.0: 5 * M, 98.0: -6 * M, 101.0: -4 * M, 97.0: 2 * M}
        j, sig, _ = run([100.0, 100.1, 100.2, 100.35, 100.5, 100.6], cells)
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))
        self.assertEqual(sig.cand.structure_type, "SHORT_GAMMA_VACUUM_SQUEEZE")
        self.assertEqual(sig.target_spot, 102.5)

    def test_squeeze_wall_vs_near_node(self):       # AMZN 10/20 型：现价旁 −23M 大负节点，Call 墙只有 8.7M
        cells = {267.5: 8.74 * M, 265.0: -2.66 * M, 262.5: -23.07 * M, 260.0: 7.93 * M, 270.0: 3.41 * M}
        j, _, _ = run([262.0, 262.3, 262.6, 262.9, 263.2, 263.45], cells)
        frs = j.frames["TEST"]
        cand, why = B.find_squeeze("TEST", frs[-1], B.context(frs), B.min_gex("TEST", 263.45), "NEUTRAL")
        self.assertIsNone(cand)
        self.assertIn("吸不过去", why)

    def test_squeeze_negative_below_spot(self):     # GOOGL 10/20 11:06 型：现价下方 0.34% 有 −3.9M（≥ 0.2 × 18.7M）
        cells = {352.5: 18.67 * M, 350.0: -7.24 * M, 347.5: 1.11 * M, 345.0: 0.27 * M, 342.5: -3.88 * M, 340.0: 0.65 * M}
        j, _, _ = run([341.5, 342.0, 342.5, 343.0, 343.4, 343.68], cells)
        frs = j.frames["TEST"]
        cand, why = B.find_squeeze("TEST", frs[-1], B.context(frs), B.min_gex("TEST", 343.68), "NEUTRAL")
        self.assertIsNone(cand)
        self.assertIn("加速砸下", why)
        cells[342.5] = -3.5 * M                       # < 0.2 × 18.67M → 这一条放行
        j, _, _ = run([341.5, 342.0, 342.5, 343.0, 343.4, 343.68], cells)
        frs = j.frames["TEST"]
        cand, why = B.find_squeeze("TEST", frs[-1], B.context(frs), B.min_gex("TEST", 343.68), "NEUTRAL")
        self.assertNotIn("加速砸下", why or "")

    def test_squeeze_needs_node_between(self):    # SMCI 型：现价和目标之间没有节点（不到一格以上）
        cells = {102.5: 40 * M, 99.0: 5 * M, 98.0: -6 * M, 97.0: 2 * M}
        j, sig, _ = run([100.0, 100.1, 100.2, 100.35, 100.5, 100.6], cells)
        self.assertIsNone(sig)
        self.assertIn("之间没有 Heatmap 节点", j.last_reason["TEST"])

    def test_squeeze_concentration_positive_only(self):   # 集中度只看正节点合计：负节点再大也不稀释
        cells = {102.5: 40 * M, 99.0: 5 * M, 98.0: -600 * M, 101.0: -4 * M, 97.0: 2 * M}
        ok, _ = B.gap_check(list(cells.items()), 100.6, 102.5)
        self.assertTrue(ok)
        cells2 = {102.5: 40 * M, 99.0: 5 * M, 94.0: 195 * M, 98.0: -6 * M, 101.0: -4 * M, 97.0: 2 * M}
        j, sig, _ = run([100.0, 100.1, 100.2, 100.35, 100.5, 100.6], cells2)
        self.assertIsNone(sig)
        self.assertIn("占正节点合计 17%", j.last_reason["TEST"])

    def test_squeeze_bigger_positive_near_spot(self):
        cells = {102.5: 40 * M, 99.0: 5 * M, 100.0: 50 * M, 98.0: -6 * M, 101.0: -4 * M, 97.0: 2 * M}
        j, _, _ = run([100.0, 100.1, 100.2, 100.35, 100.5, 100.6], cells)
        frs = j.frames["TEST"]
        cand, why = B.find_squeeze("TEST", frs[-1], B.context(frs), B.min_gex("TEST", 100.6), "NEUTRAL")
        self.assertIsNone(cand)
        self.assertIn("更大的正节点 100", why)

    def test_avalanche(self):
        cells = {98.0: -30 * M, 99.0: -6 * M, 100.0: -5 * M, 101.5: 3 * M}
        path = [101.0, 100.8, 100.5, 100.2, 99.9, 99.6]
        j, sig, _ = run(path, cells)
        self.assertIsNone(sig)                                  # 默认关闭（2026-10-20 用户定先不做雪崩）
        CFG.bl_enable_avalanche = True
        try:
            j, sig, _ = run(path, cells)
        finally:
            CFG.bl_enable_avalanche = False
        self.assertIsNotNone(sig, j.last_reason.get("TEST"))
        self.assertEqual(sig.cand.structure_type, "SHORT_GAMMA_AVALANCHE")
        self.assertEqual(sig.target_spot, 98.0)
        self.assertEqual(sig.structure.right, "P")


class FakeQuotes:
    def __init__(self):
        self.q = {}
        self.default = None

    def get(self, sym):
        return self.q.get(sym.split(":")[-1], self.default)


def opened(direction="BEARISH", entry="10:30", exp=EXP, stop=101.3, target=98.5, ask=2.0, bid=1.9, stype=None):
    cfg = SystemConfig.load(None)
    tr = B.BaselineTrader(cfg)
    fq = FakeQuotes()
    tr.quote_provider = fq.get
    j, sig, snap = run(REJECT_PATH, CLIFF_CELLS)
    sig.cand.direction = direction
    sig.stop_spot, sig.target_spot = stop, target
    if stype:
        sig.cand.structure_type = stype
    snap.expiration = exp
    right = "P" if direction == "BEARISH" else "C"
    sig.structure.right = right
    fq.default = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    now = et_datetime(DAY, entry)
    p = tr.open_position(None, sig, now)
    assert p is not None, "开仓失败"
    fq.default = None
    fq.q[p.contract_symbol.split(":")[-1]] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    return tr, fq, p, now


class TestExits(unittest.TestCase):
    def tick(self, tr, fq, p, now, minutes, bid, spot):
        fq.q[p.contract_symbol.split(":")[-1]] = {"bid": bid, "ask": bid * 1.05, "mid": bid * 1.025}
        return tr.mark(p, spot, now + dt.timedelta(minutes=minutes))

    def frame(self, now, minutes, ceil_gex, spot=100.5):
        cells = dict(CLIFF_CELLS)
        if ceil_gex is None:
            cells.pop(100.75)
        else:
            cells[100.75] = ceil_gex
        t = now + dt.timedelta(minutes=minutes - 5)
        fr, _ = B.make_frame(payload(spot, cells, t=t), DAY, CFG, t)
        return fr

    def decay_feed(self, tr, fq, p, now, gexs, spots):
        """每 5 分钟：先按现价盯市（期权价不变），再喂一根新 Heatmap；返回平仓时第几根（没平仓 None）。"""
        for i, (g, sp) in enumerate(zip(gexs, spots), 1):
            if self.tick(tr, fq, p, now, 5 * i, 1.95, sp):
                return -i
            if tr.on_frame(p, self.frame(now, 5 * i, g, sp), sp, now + dt.timedelta(minutes=5 * i)):
                return i
        return None

    def test_start_node_decay_10m(self):                  # 天花板 20M → 18M → 16M，现价原地 → 10 分钟截断
        tr, fq, p, now = opened()
        self.assertEqual(self.decay_feed(tr, fq, p, now, [18 * M, 16 * M], [100.5, 100.45]), 2)
        self.assertEqual(p.exit_reason, "START_NODE_DECAY_10M")

    def test_start_node_decay_progress_20_to_30(self):    # 最远进度 27%（≥20%、<30%）→ 10 分钟不砍，20 分钟砍
        tr, fq, p, now = opened()                         # 开仓 100.5，起始点 100.75，目标 98.5：区间 2.25
        r = self.decay_feed(tr, fq, p, now, [19 * M, 18 * M, 17 * M, 16 * M], [99.9, 100.2, 100.3, 100.3])
        self.assertEqual(r, 4)
        self.assertEqual(p.exit_reason, "START_NODE_DECAY_20M")

    def test_start_node_decay_progress_over_30(self):     # 最远进度 > 30% → 起始点再小也不截断
        tr, fq, p, now = opened()
        self.assertIsNone(self.decay_feed(tr, fq, p, now, [19 * M, 18 * M, 17 * M, 16 * M], [99.7, 100.2, 100.3, 100.3]))

    def test_start_node_decay_chain_breaks(self):         # 中间有一根变大 / 缺这个行权价 → 连续中断
        tr, fq, p, now = opened()
        self.assertIsNone(self.decay_feed(tr, fq, p, now, [18 * M, 19 * M, None, 18 * M], [100.5] * 4))
        tr, fq, p, now = opened()
        self.assertIsNone(self.decay_feed(tr, fq, p, now, [18 * M, 18 * M], [100.5] * 2))   # 持平不算变小
        tr, fq, p, now = opened()                         # PLTR 型：每根只降 0.5%（< 2%）不算变小
        self.assertIsNone(self.decay_feed(tr, fq, p, now, [19.9 * M, 19.8 * M], [100.5] * 2))
        tr, fq, p, now = opened()                         # 刚好降 2% 算
        self.assertEqual(self.decay_feed(tr, fq, p, now, [19.6 * M, 19.208 * M], [100.5] * 2), 2)

    def test_start_node_decay_not_for_squeeze(self):
        tr, fq, p, now = opened(stype="SHORT_GAMMA_VACUUM_SQUEEZE")
        self.assertNotIn(p.position_id, tr.decay)
        self.assertIsNone(self.decay_feed(tr, fq, p, now, [18 * M, 16 * M], [100.5] * 2))

    def test_contracts_for(self):                     # 2026-10-20 用户定：每笔约 $650（$500–$800）
        for cost, n in ((100, 7), (90, 7), (120, 5), (250, 3), (300, 2), (600, 1), (450, 1), (900, 1), (50, 13)):
            self.assertEqual(B.contracts_for(cost), n, cost)

    def test_sizing_on_open(self):
        tr, fq, p, now = opened()                         # ask 2.0 → 单张 $200 → 3 张 $600
        self.assertEqual(p.contracts, 3)
        self.assertEqual(tr.meta[p.position_id]["bl_contracts"], "3")
        self.assertEqual(tr.state[p.position_id].quantity, 3)

    def test_scale_out_half_at_80(self):
        tr, fq, p, now = opened()                         # 3 张 @ 2.0
        self.assertIsNone(self.tick(tr, fq, p, now, 3, 3.62, 100.0))          # +81% → 卖 1 张，剩 2 张
        self.assertEqual(p.contracts, 2)
        self.assertIn("卖出 1 张", tr.meta[p.position_id]["bl_scale_out"])
        self.assertIsNotNone(self.tick(tr, fq, p, now, 4, 2.9, 100.1))        # 回落触发锁利，剩下 2 张平仓
        self.assertEqual(p.contracts, 3)                                       # 记录恢复为开仓张数
        self.assertAlmostEqual(p.realized_pnl, (3.62 - 2.0) * 100 + (2.9 - 2.0) * 200, places=2)
        self.assertAlmostEqual(p.realized_roi_pct, p.realized_pnl / 600 * 100, places=4)

    def test_ratchet_table(self):
        self.assertEqual(compute_continuous_option_ratchet(5), (-35.0, -999.0))
        self.assertEqual(compute_continuous_option_ratchet(8), (-12.0, -999.0))
        self.assertEqual(compute_continuous_option_ratchet(15), (-5.0, 6.25))
        self.assertEqual(compute_continuous_option_ratchet(30), (0.0, 17.5))
        self.assertEqual(compute_continuous_option_ratchet(50), (0.0, 34.1))
        self.assertEqual(compute_continuous_option_ratchet(100), (0.0, 77.9))
        self.assertEqual(compute_continuous_option_ratchet(30, True), (0.0, 25.5))

    def test_entry_at_ask(self):
        tr, fq, p, now = opened()
        self.assertEqual(p.entry_option_price, 2.0)
        self.assertEqual(p.price_source, "quote")
        st = tr.state[p.position_id]
        self.assertTrue(st.is_0dte)
        self.assertEqual(st.initial_stop_loss_spot, 101.3)

    def test_spread_cap(self):
        with self.assertRaises(AssertionError):
            opened(ask=2.4, bid=1.8)                          # 价差 0.6 / 中间价 2.1 = 28.6% > 25% → 不开
        tr, fq, p, now = opened(ask=2.3, bid=1.8)             # 24.4% → 开
        self.assertIsNotNone(p)

    def test_contract_cost_cap(self):
        with self.assertRaises(AssertionError):
            opened(ask=10.5, bid=10.3)                        # 单张 $1,050 > $1,000 → 不开
        tr, fq, p, now = opened(ask=9.9, bid=9.7)
        self.assertEqual(p.entry_option_price, 9.9)

    def test_initial_stop(self):
        tr, fq, p, now = opened()
        self.assertIsNone(self.tick(tr, fq, p, now, 1, 1.5, 100.6))           # −25%
        self.assertIsNotNone(self.tick(tr, fq, p, now, 2, 1.29, 100.7))       # −35.5%
        self.assertEqual(p.exit_reason, "INITIAL_STOP_LOSS")

    def test_no_8_min_truncation(self):              # 2026-10-20 用户定：0DTE 8 分钟截断已去掉，10 分钟推迟到 18 分钟
        tr, fq, p, now = opened()
        self.assertIsNone(self.tick(tr, fq, p, now, 8, 1.74, 100.6))          # 8 分钟、峰值 ≤ +1%、−13%
        self.assertIsNone(self.tick(tr, fq, p, now, 11, 1.58, 100.6))         # 11 分钟、−21%

    def test_dead_momentum_18m(self):
        tr, fq, p, now = opened()
        self.assertIsNone(self.tick(tr, fq, p, now, 17, 1.58, 100.6))         # 17 分钟、−21%
        self.assertIsNotNone(self.tick(tr, fq, p, now, 18, 1.58, 100.6))      # 18 分钟、最高 < +4%、≤ −20%
        self.assertEqual(p.exit_reason, "DEAD_MOMENTUM_TRUNCATE_10M")

    def test_stagnation_25m(self):
        tr, fq, p, now = opened()
        self.assertIsNone(self.tick(tr, fq, p, now, 24, 1.66, 100.6))         # 24 分钟、−17%
        self.assertIsNotNone(self.tick(tr, fq, p, now, 25, 1.66, 100.6))      # 25 分钟、最高 < +6%、≤ −15%
        self.assertEqual(p.exit_reason, "THETA_STAGNATION_TRUNCATE_25M")

    def test_trailing(self):
        tr, fq, p, now = opened()
        self.assertIsNone(self.tick(tr, fq, p, now, 3, 3.0, 100.0))           # +50% → 锁 34.1%
        self.assertIsNone(self.tick(tr, fq, p, now, 4, 2.75, 100.1))          # +37.5%
        self.assertIsNotNone(self.tick(tr, fq, p, now, 5, 2.66, 100.15))      # +33% ≤ 34.1%
        self.assertIn(p.exit_reason, ("TRAILING_STOP", "SPOT_PROFIT_STOP"))
        self.assertAlmostEqual(p.exit_option_price, 2.66)

    def test_spot_stop(self):
        tr, fq, p, now = opened()
        self.assertIsNotNone(self.tick(tr, fq, p, now, 2, 1.85, 101.31))
        self.assertEqual(p.exit_reason, "SPOT_STOP")

    def test_structure_target(self):
        tr, fq, p, now = opened()
        self.assertIsNotNone(self.tick(tr, fq, p, now, 6, 2.1, 98.4))
        self.assertEqual(p.exit_reason, "STRUCTURE_TARGET")

    def test_eod_0dte(self):
        tr, fq, p, now = opened(entry="14:30")
        self.assertIsNone(self.tick(tr, fq, p, now, 70, 2.05, 100.4))         # 15:40
        self.assertIsNotNone(self.tick(tr, fq, p, now, 80, 2.05, 100.4))      # 15:50
        self.assertEqual(p.exit_reason, "EOD_0DTE_CLOSE")

    def test_bullish_spot_ratchet(self):
        tr, fq, p, now = opened(direction="BULLISH", stop=99.7, target=103.0)
        self.assertIsNone(self.tick(tr, fq, p, now, 2, 2.6, 101.0))            # +30% → 现货止损 ≥ 开仓现价
        self.assertGreaterEqual(tr.state[p.position_id].stop_loss_spot, p.entry_spot)
        self.assertIsNotNone(self.tick(tr, fq, p, now, 3, 2.5, p.entry_spot - 0.01))
        self.assertEqual(p.exit_reason, "SPOT_PROFIT_STOP")


class TestNearTrigger(unittest.TestCase):
    def test_bands(self):
        tr, fq, p, now = opened()
        st = tr.state[p.position_id]
        tr.mark(p, 100.5, now + dt.timedelta(minutes=1))          # 先盯一次：峰值约 −5%
        self.assertFalse(B.near_trigger(st, -20.0, 100.5, 3))       # 离 −35% 还有 15 点，时间截断尚未生效
        self.assertTrue(B.near_trigger(st, -31.0, 100.5, 3))        # 离 −35% 4 点
        self.assertFalse(B.near_trigger(st, -9.0, 100.5, 8))        # 8 分钟截断已去掉
        self.assertTrue(B.near_trigger(st, -16.0, 100.5, 17))       # 18 分钟零动能截断线 −20% 在 5 点内
        self.assertTrue(B.near_trigger(st, 0.0, 101.25, 3))         # 现价离现货止损 101.3 不到 0.1%
        st.trailing_active, st.trailing_stop_level_pct, st.breakeven_locked = True, 34.1, True
        self.assertTrue(B.near_trigger(st, 37.0, 100.0, 20))        # 离锁利线 2.9 点
        self.assertFalse(B.near_trigger(st, 60.0, 100.0, 20))

    def test_tick_schedule(self):
        from main import next_mark_tick
        grid = et_datetime(DAY, "10:00")
        t = grid + dt.timedelta(seconds=25)
        seq = []
        for near in (False, True, True, True, True, False):
            t = next_mark_tick(t, grid, 25, 1, 20, near)
            seq.append(t.strftime("%H:%M:%S"))
        self.assertEqual(seq, ["10:01:25", "10:01:45", "10:02:05", "10:02:25", "10:02:45", "10:03:25"])


class TestEngine(unittest.TestCase):
    def test_no_position_cap(self):
        cfg = SystemConfig.load(None)
        tr = B.BaselineTrader(cfg)
        eng = B.BaselineEngine(cfg, None, SignalDetector(cfg, []), tr)
        now = et_datetime(DAY, "10:30")
        for i in range(30):                                          # 30 笔持仓（超过旧的 8 / 25 / 15 上限）
            tr.positions.append(B.Position(f"X{i}", f"T{i}", "BUY_PUT", "P", "CLIFF", "O:X", 1, EXP, now, 1, 1, 1, 0.5, 0.1,
                                           "quote", 1, 1))
        ok, why = eng.can_enter("NEW", now)
        self.assertTrue(ok, why)
        ok, why = eng.can_enter("T3", now)
        self.assertFalse(ok)                                         # 同一只票已有持仓

    def test_no_reevaluate_same_frame_and_1500_gate(self):
        cfg = SystemConfig.load(None)
        tr = B.BaselineTrader(cfg)
        eng = B.BaselineEngine(cfg, None, SignalDetector(cfg, []), tr)
        t0 = et_datetime(DAY, "10:00")
        for i, s in enumerate(REJECT_PATH):
            t = t0 + dt.timedelta(minutes=5 * i)
            eng.on_snapshot("TEST", payload(s, CLIFF_CELLS, t=t), t + dt.timedelta(minutes=5), frame_t=t)
        self.assertIn("TEST", eng.pending)
        eng.pending.clear()
        eng.on_snapshot("TEST", payload(REJECT_PATH[-1], CLIFF_CELLS, t=t), t + dt.timedelta(minutes=6), frame_t=t)
        self.assertNotIn("TEST", eng.pending)                                # 同一根快照不重复出信号
        ok, why = eng.can_enter("TEST", et_datetime(DAY, "15:00"))
        self.assertFalse(ok)
        ok, why = eng.can_enter("TEST", et_datetime(DAY, "09:40"))
        self.assertTrue(ok, why)                                              # baseline 不受旧的 09:45 限制


class TestRealData1019(unittest.TestCase):
    """10/19 实盘复盘的三笔（需要本地存档 yhs_data/archive/heatmap/2026-10-19）。"""

    def judge_at(self, ticker, label):
        from baseline_frames import merged_frames
        frames = merged_frames(CFG, DAY, ticker)
        if not frames:
            self.skipTest("没有 10/19 存档")
        j = B.BaselineJudge(CFG)
        for t, p in frames.items():
            fr, snap, _ = j.observe(ticker, p, t, DAY)
            if t.strftime("%H:%M") == label:
                return j, j.evaluate(ticker, snap, t + dt.timedelta(minutes=5))
        self.fail("没有这根快照")

    def test_nvda_put_rejected(self):
        j, sig = self.judge_at("NVDA", "10:50")
        self.assertIsNone(sig)
        self.assertIn("离天花板 217.5 0.018%", j.last_reason["NVDA"])

    def test_mu_call_rejected(self):
        j, sig = self.judge_at("MU", "11:20")
        self.assertIsNone(sig)
        self.assertIn("离踏板 925 0.143%", j.last_reason["MU"])
        self.assertIn("更大的正节点 925", j.last_reason["MU"])     # 逼空：现价 3 格内 925 比 Call 墙 950 大

    def test_meta_put_rejected(self):
        j, sig = self.judge_at("META", "10:40")
        self.assertIsNone(sig)

    def test_user_review_1020_no_cliff(self):
        """10/20 用户逐笔复盘指出不该开的 6 笔悬崖：INTC / MU / META / TSLA×2 / AMD。"""
        for t, lab, why in [("INTC", "09:45", "之间没有显著负节点"), ("MU", "10:00", "墙太弱"),
                            ("META", "10:30", "宁缺毋滥"), ("TSLA", "10:35", "更大的正节点 370"),
                            ("TSLA", "11:15", "更大的正节点 370"), ("AMD", "14:30", "更大的正节点 540")]:
            j, sig = self.judge_at(t, lab)
            self.assertFalse(sig is not None and sig.cand.structure_type == "CLIFF_STRUCTURE", f"{t} {lab} 不应是悬崖")
            self.assertIn(why, j.last_reason[t].split("；")[0], f"{t} {lab}: {j.last_reason[t]}")

    def test_pltr_1015_cliffs(self):
        """10/20 用户复盘：10/15 PLTR 两笔 195P 悬崖（数据时刻 09:55 / 10:35）不该开。"""
        global DAY
        old, DAY = DAY, dt.date(2026, 10, 15)
        try:
            for lab in ("09:50", "10:30"):
                j, sig = self.judge_at("PLTR", lab)
                self.assertFalse(sig is not None and sig.cand.structure_type == "CLIFF_STRUCTURE", lab)
                self.assertIn("墙太弱", j.last_reason["PLTR"].split("；")[0], j.last_reason["PLTR"])
        finally:
            DAY = old

    def test_smci_squeeze_one_grid(self):
        """10/20 用户：SMCI 11:20 现价 33.1、目标 33.5，中间没有节点 → 不开。"""
        j, sig = self.judge_at("SMCI", "11:20")
        self.assertIsNone(sig)
        self.assertIn("33.5 之间没有 Heatmap 节点", j.last_reason["SMCI"])


if __name__ == "__main__":
    unittest.main()
