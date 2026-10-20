"""
baseline.py —— 按 docs/AGENT_REPRODUCTION_BLUEPRINT_HEATMAP_AND_TPSL.md + strategy_core_spec.py 实现的日内实盘规则
=====================================================================================================
2026-10-20 用户定：舍弃旧的"最大正/负节点 + 距离 ≤1% + 0–1 评分"规则，日内实盘改用这份 baseline。

直接复用 strategy_core_spec.py 的原函数（不重写，保证与参考实现一致）：
  * compute_local_gamma_regime()          第一层：局部 Gamma 环境
  * compute_structure_confirmation_score() 三层综合评分（基础 58，≥55 放行）
  * evaluate_exit_tick()                  出场：期权连续棘轮 L(M) + 现货五阶止损棘轮 + 8/10/25 分钟时间截断 + 15:50 0DTE 强平
文档只给了文字、没有参考代码的部分在本文件实现（第二层节点演化 / 墙体迁移、第三层五大原型几何与价格触发、中间关卡、
衰竭拦截、跨到期共振）。文档没写死、需要解读的地方都标了【解读】，见 BASELINE_NOTES。

时间口径：Heatmap 快照标记 T = T..T+5 这一段，现价是 T+5 前后的价格，T+5 之后才拿得到（2026-10-13 实测）。
所有 Δt 都按快照标记时间算（相邻快照相差 300 秒）。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from detector import FlowConfirmation, HeatmapSnapshot, StructureSignal, parse_heatmap
from paper_trader import PaperTrader, Position, SessionEngine
from strategy_core_spec import (ExitWeights, HeatmapWeights, PositionState, compute_local_gamma_regime,
                                compute_structure_confirmation_score, evaluate_exit_tick)

log = logging.getLogger("yhs.baseline")
HW, EW = HeatmapWeights(), ExitWeights()
INDEX_TICKERS = {"SPX", "SPXW", "NDX", "QQQ"}   # 文档原文的"指数"集合（SPY 不在内）
OUTER_WALL_STEPS = 3                             # 墙外侧 3 格以内有更大的正节点 → 结构不成立（2026-10-20 用户定）
PATH_NEG_RATIO = 1.5                             # 路径上显著负节点合计 ≥ 1.5 × 显著正节点合计（2026-10-20 用户定）
PIN_NEG_MULT = 3.0                               # 路径上全到期最大负节点 ≥ 3 × 墙 → 不开（2026-10-20 用户定）
BIG_NODE_RATIO = 0.43                            # "超大正节点" = GEX ≥ 0.43 × 起作用墙（2026-10-20 用户定；文档中间关卡仍是 0.45）
INTRADAY_STRUCT_END = "15:00"                    # 文档：盘中 < 15:00 用 0DTE / 最近周度；15:00 后是尾盘隔夜模式

BASELINE_NOTES = """
【解读】清单（文档没写死的地方，按以下方式实现；要改告诉我）：
1. 天花板 / 地板 = 现价上方 / 下方"最近的"显著正节点（GEX ≥ G_min）；不是全天最大的那个。
2. 目标位（2026-10-20 用户确认）：悬崖 / 跳板的重点都是超大正节点。目标 = 顺方向下一个超大正节点（悬崖 = 下方支撑，
   跳板 = 上方阻力），超大 = GEX ≥ max(0.85×G_min, 0.43×天花板/踏板 GEX)；"空间" = 现价到它的距离。
   【宁缺毋滥】没有超大正节点承接就不做（不退回负节点）。
2a. 路径：现价到目标之间必须有显著负节点，且显著负节点 |GEX| 合计 ≥ 1.5 × 显著正节点合计（正负差不多 = 走不动）。
2b. 墙外 3 格：天花板 / 踏板外侧 3 格以内（含第 3 格）有比它更大的正节点 → 结构不成立（价格会被更大的墙吸过去）。
2c. 钉住：主结构到期 |GEX| 最大的节点是负节点、在路径上或现价 ±0.5% 内、且 ≥ 3 × 墙 → 不开（逼空也查）。
3. 结构盈亏比 = 空间 ÷ max(0.8%, min(离墙距离, 1.2%) + 0.3%)；初始现货止损 = 开仓现价 ± 这个风险距离；目标 = 靶心行权价
   （中间关卡 ≥1.0% 时改为关卡行权价）。逼空 / 雪崩没有墙，风险距离取最小 0.8%。
4. 中间关卡在 0.40%–1.0% 之间：不否决、不改目标（文档只写了 <0.40% 与 ≥1.0% 两种情况）。
5. 节点 15 分钟演化：当天满 3 根后第一次出现 → NEW；否则取 9–26.7 分钟前、最接近 15 分钟的那根比较；
   没有可比较的快照（或那根里暂时没有这个行权价）→ UNKNOWN（不加不减分）。
6. 墙体 30 分钟迁移：取 10–35 分钟前、最接近 30 分钟的那根快照；WHIPSAW = 最近 6 根里主正墙有 ≥2 次幅度 >0.3% 的跳动且有上有下。
7. 5 分钟涨跌 = 对比上一根（Δt ≥ 180 秒）；15 分钟涨跌 = 11–25 分钟前、最接近 15 分钟的那根；VWAP = 当天已采集快照现价的均值。
   15 分钟涨跌还没有（开盘不足 15 分钟）就不出信号。
8. 跳板"已放量反弹"没有成交量数据，用 5 分钟涨幅 ≥ +0.10% 代替。
9. "强空头水下放宽至 2.7%"：当天跌幅 ≤ −1.2% 且现价 ≤ 0.995 × VWAP 时，悬崖离天花板最远放宽到 2.7%。
10. 跨到期共振 / 冲突：在起作用的行权价上，比较主结构到期之后的下 2 个到期；同号各 +6（上限 +12），紧挨着的下一个到期异号 −14。
11. 逼空"集中度 ≥18%" = 该 Call 墙 GEX ÷ 主结构到期全部节点 |GEX| 之和。
12. 文档没有规定合约怎么选：沿用原规则（买靶心行权价，最多虚值 0.5%，挂牌行权价里离靶心最近）。每笔 1 张，所以 +80% 分批止盈不会触发。
13. 出场用"能卖出的价格"算收益率（实盘 = 实时 bid；回放 = 中间价 − 真实半价差），和开仓成本（ask）对比。
14d. 执行层保护：买卖价差 > 中间价 25% 不开（2026-10-20 用户定）。
14a. 单张成本上限 $1,000（ask × 100，2026-10-20 用户定，文档没有）。
14. 组合上限（文档没有）：2026-10-20 用户定不限持仓数量 / 每天笔数；保留同一只票同时 1 笔、每天最多 2 笔、平仓后 30 分钟冷却。
14b. 2026-10-20 用户定：先不做雪崩结构（bl_enable_avalanche=False）。
14c. 实盘盯仓：平时每分钟；任一持仓离出场触发线 5 个百分点以内（现货离止损 / 目标 0.1% 以内）时改为每 20 秒（回放只有 1 分钟数据，仍按分钟）。
15. 盘中开仓只到 15:00（之后文档是尾盘隔夜模式；我们的隔夜单是另一套独立规则，不动）。
16. PINNING_MAGNET 只限 SPX/SPXW 而且没有方向，不开仓。
"""


def min_gex(ticker: str, spot: float) -> float:
    if ticker.upper() in INDEX_TICKERS:
        return HW.min_index_node_gex_usd
    return max(HW.min_smallcap_node_gex_usd, min(HW.min_stock_node_gex_usd, spot * HW.spot_gex_scale_factor))


def is_index(ticker: str) -> bool:
    return ticker.upper() in INDEX_TICKERS


# ============================================================================== 快照与日内上下文
@dataclass
class Frame:
    t: dt.datetime                  # 快照标记时间（ET）
    spot: float
    exp: str                        # 主结构到期（0DTE，没有就最近一期）
    cells: list                     # 主结构到期 [(strike, gex)]，按行权价升序
    by_exp: dict                    # 全部到期：exp → {strike: gex}


def _cells_by_exp(d: dict) -> dict:
    out: dict = {}
    for c in d.get("cells") or []:
        e, k, g = c.get("expiration"), c.get("strike_usd"), c.get("net_dealer_gex_usd")
        if e is None or k is None or g is None:
            continue
        out.setdefault(str(e), {})[float(k)] = float(g)
    return out


def make_frame(payload: dict, ref_date: dt.date, cfg, t: dt.datetime) -> tuple[Frame | None, HeatmapSnapshot | None]:
    snap = parse_heatmap(payload, cfg, ref_date)
    if snap is None:
        return None, None
    d = payload.get("data") or {}
    by_exp = _cells_by_exp(d)
    cells = sorted(by_exp.get(snap.expiration, {}).items())
    return Frame(t=t, spot=snap.spot, exp=snap.expiration, cells=cells, by_exp=by_exp), snap


def _pick(frames: list, now: dt.datetime, lo_s: float, hi_s: float, target_s: float):
    best = None
    for f in frames:
        d = (now - f.t).total_seconds()
        if lo_s <= d <= hi_s and (best is None or abs(d - target_s) < abs((now - best.t).total_seconds() - target_s)):
            best = f
    return best


@dataclass
class Context:
    spot: float
    open_spot: float
    vwap: float
    intraday_ret: float
    tick_ret_5m: float | None
    ret_15m: float | None
    day_high: float
    day_low: float
    n_frames: int


def context(frames: list) -> Context:
    cur = frames[-1]
    spots = [f.spot for f in frames]
    prev = None
    for f in reversed(frames[:-1]):
        if (cur.t - f.t).total_seconds() >= 180:
            prev = f
            break
    r15 = _pick(frames[:-1], cur.t, 660, 1500, 900)
    return Context(spot=cur.spot, open_spot=frames[0].spot, vwap=sum(spots) / len(spots),
                   intraday_ret=cur.spot / frames[0].spot - 1,
                   tick_ret_5m=(cur.spot / prev.spot - 1) if prev else None,
                   ret_15m=(cur.spot / r15.spot - 1) if r15 else None,
                   day_high=max(spots), day_low=min(spots), n_frames=len(frames))


# ============================================================================== 第二层：节点演化 / 墙体迁移
def node_growth(frames: list, strike: float) -> str:
    cur = frames[-1]
    g_now = dict(cur.cells).get(strike)
    if g_now is None:
        return "UNKNOWN"
    earlier = [f for f in frames[:-1] if f.exp == cur.exp]
    if len(frames) >= 3 and earlier and all(strike not in dict(f.cells) for f in earlier):
        return "NEW"                      # 当天满 3 根后第一次挤进 Top-10 榜单
    ref = _pick(frames[:-1], cur.t, 540, 1600, 900)
    if ref is None or ref.exp != cur.exp:
        return "UNKNOWN"
    g_prev = dict(ref.cells).get(strike)
    if g_prev is None:
        return "UNKNOWN"                  # 以前出现过、15 分钟前那根暂时不在榜单里：无法比较
    g = (abs(g_now) - abs(g_prev)) / (abs(g_prev) + 1e-6) * 100
    if g >= HW.node_growing_threshold_pct:
        return "GROWING"
    if g <= HW.node_decaying_threshold_pct:
        return "DECAYING"
    return "FLAT"


def _king_pos(cells: list):
    pos = [(k, g) for k, g in cells if g > 0]
    return max(pos, key=lambda c: c[1])[0] if pos else None


def _walls(cells: list, spot: float, gmin: float):
    """(天花板, 地板)：现价上方 / 下方最近的显著正节点（GEX ≥ G_min）的 (行权价, GEX)，没有则 None。"""
    above = [(k, g) for k, g in cells if g >= gmin and k > spot]
    below = [(k, g) for k, g in cells if g >= gmin and k <= spot]
    return (min(above, key=lambda c: c[0]) if above else None, max(below, key=lambda c: c[0]) if below else None)


def wall_migration(frames: list, gmin: float) -> str:
    cur = frames[-1]
    last6 = [f for f in frames[-6:] if f.exp == cur.exp]
    kps = [_king_pos(f.cells) for f in last6]
    jumps = []
    for a, b in zip(kps, kps[1:]):
        if a is not None and b is not None and abs(b - a) / cur.spot > 0.003:
            jumps.append(1 if b > a else -1)
    if len(jumps) >= 2 and 1 in jumps and -1 in jumps:
        return "WHIPSAW"
    ref = _pick(frames[:-1], cur.t, 600, 2100, 1800)
    if ref is None or ref.exp != cur.exp:
        return "STABLE"
    kp_now, kp_ref = _king_pos(cur.cells), _king_pos(ref.cells)
    ceil_now, floor_now = _walls(cur.cells, cur.spot, gmin)
    ceil_ref, floor_ref = _walls(ref.cells, ref.spot, gmin)
    up_struct = ((kp_now is not None and kp_ref is not None and kp_now > kp_ref)
                 or (floor_now and floor_ref and floor_now[0] > floor_ref[0])
                 or (ceil_ref is not None and ceil_now is None))
    down_struct = ((kp_now is not None and kp_ref is not None and kp_now < kp_ref)
                   or (ceil_now and ceil_ref and ceil_now[0] < ceil_ref[0]))
    if up_struct and cur.spot > 1.001 * ref.spot:
        return "MIGRATING_UP"
    if down_struct and cur.spot < 0.999 * ref.spot:
        return "MIGRATING_DOWN"
    return "STABLE"


def expiry_confluence(frame: Frame, strike: float) -> tuple[int, bool]:
    g0 = dict(frame.cells).get(strike)
    if g0 is None:
        return 0, False
    later = sorted(e for e in frame.by_exp if e > frame.exp)[:2]
    same, conflict = 0, False
    for i, e in enumerate(later):
        g = frame.by_exp[e].get(strike)
        if g is None:
            continue
        if g * g0 > 0:
            same += 1
        elif i == 0 and g * g0 < 0:
            conflict = True
    return same, conflict


def trunc_floors(cells: list) -> tuple[float, float]:
    pos = [g for _, g in cells if g > 0]
    neg = [abs(g) for _, g in cells if g < 0]
    return (min(pos) if len(pos) >= 8 else 0.0), (min(neg) if len(neg) >= 8 else 0.0)


# ============================================================================== 第三层：原型
@dataclass
class Candidate:
    structure_type: str            # CLIFF_STRUCTURE / SPRINGBOARD_STRUCTURE / SHORT_GAMMA_VACUUM_SQUEEZE / SHORT_GAMMA_AVALANCHE
    direction: str                 # BULLISH / BEARISH
    acting_strike: float           # 起作用的核心节点（墙 / 靶心），用于节点演化与跨到期
    acting_gex: float
    target_strike: float
    wall_dist: float | None        # 离墙距离（小数）；逼空 / 雪崩为 None
    target_space: float
    rr: float | None
    trigger: str
    notes: str = ""


def _gatekeeper(cells: list, spot: float, target: float, acting_gex: float, gmin: float, bullish: bool,
                index: bool) -> tuple[bool, float, str]:
    """中间关卡：现价与目标之间反向的强正节点。返回 (是否否决, 新目标, 说明)。"""
    thr = max(0.85 * gmin, 0.45 * abs(acting_gex))
    if bullish:
        gk = [(k, g) for k, g in cells if spot < k < target and g >= thr]
        near = min(gk, key=lambda c: c[0]) if gk else None
    else:
        gk = [(k, g) for k, g in cells if target < k < spot and g >= thr]
        near = max(gk, key=lambda c: c[0]) if gk else None
    if not near:
        return False, target, ""
    d = abs(near[0] - spot) / spot
    veto_d, move_d = (HW.min_reasonable_disp_index, HW.min_target_space_index) if index else \
        (HW.min_reasonable_disp_stock, 0.010)
    if d < veto_d:
        return True, target, f"中间关卡 {near[0]:g}（+{near[1] / 1e6:.1f}M）距 {d * 100:.2f}% < {veto_d * 100:.2f}%"
    if d >= move_d:
        return False, near[0], f"目标改为中间关卡 {near[0]:g}"
    return False, target, f"中间关卡 {near[0]:g} 距 {d * 100:.2f}%（不改目标）"


def _next_big_positive(cells: list, spot: float, acting_gex: float, gmin: float, below: bool):
    """悬崖 / 跳板的目标位：顺方向上下一个"超大正节点"（2026-10-20 用户确认：两种结构的重点都是超大正节点，
    下方 / 上方空间 = 到下一个正节点支撑 / 阻力的距离）。超大 = GEX ≥ max(0.85 × G_min, BIG_NODE_RATIO × 起作用墙的 GEX)
    （参照文档"中间关卡"的强正节点门槛，比例由用户定为 0.43）；比它小的正节点价格可以穿过。返回 (行权价, GEX) 或 None。"""
    thr = max(0.85 * gmin, BIG_NODE_RATIO * abs(acting_gex))
    if below:
        c = [(k, g) for k, g in cells if g >= thr and k < spot and (spot - k) / spot <= 0.045]
        return max(c, key=lambda x: x[0]) if c else None
    c = [(k, g) for k, g in cells if g >= thr and k > spot and (k - spot) / spot <= 0.045]
    return min(c, key=lambda x: x[0]) if c else None


def strike_step(cells: list, spot: float) -> float:
    """现价附近（±5%）的行权价间距（"一格"）：相邻行权价差的最小值。"""
    ks = sorted({k for k, _ in cells if abs(k / spot - 1) <= 0.05})
    diffs = [round(b - a, 4) for a, b in zip(ks, ks[1:]) if b - a > 1e-9]
    return min(diffs) if diffs else spot * 0.005


def outer_bigger_wall(cells: list, wall_k: float, wall_g: float, step: float, above: bool):
    """墙外侧 3 格以内（含第 3 格）有没有比墙更大的正节点（有 → 价格会被它吸过去，结构不成立；2026-10-20 用户定）。"""
    if above:
        c = [(k, g) for k, g in cells if wall_k < k <= wall_k + OUTER_WALL_STEPS * step + 1e-6 and g > wall_g]
    else:
        c = [(k, g) for k, g in cells if wall_k - OUTER_WALL_STEPS * step - 1e-6 <= k < wall_k and g > wall_g]
    return max(c, key=lambda x: x[1]) if c else None


def path_check(cells: list, spot: float, target: float, gmin: float) -> tuple[bool, str, tuple | None]:
    """现价到目标之间（不含目标）：必须有显著负节点，而且显著负节点 |GEX| 合计 ≥ PATH_NEG_RATIO × 显著正节点合计
    （正负差不多 = 走不动；2026-10-20 用户定）。返回 (通过, 说明, 路径上最大的负节点)。"""
    lo, hi = sorted((spot, target))
    path = [(k, g) for k, g in cells if lo < k < hi and abs(g) >= gmin]
    neg = [(k, g) for k, g in path if g < 0]
    pos = [(k, g) for k, g in path if g > 0]
    if not neg:
        return False, f"现价到目标 {target:g} 之间没有显著负节点（走不动）", None
    ns, ps = sum(-g for _, g in neg), sum(g for _, g in pos)
    biggest = min(neg, key=lambda c: c[1])
    if ps > 0 and ns < PATH_NEG_RATIO * ps:
        return False, (f"现价到目标 {target:g} 之间负节点合计 {ns / 1e6:.1f}M 不到正节点合计 {ps / 1e6:.1f}M 的 "
                       f"{PATH_NEG_RATIO:g} 倍（走不动）"), biggest
    return True, f"路径负 {ns / 1e6:.1f}M / 正 {ps / 1e6:.1f}M", biggest


def pin_check(cells: list, spot: float, target: float, wall_g: float) -> tuple[bool, str]:
    """主结构到期 |GEX| 最大的节点如果是负节点、在现价到目标的路径上或现价 ±0.5% 内，并且 ≥ PIN_NEG_MULT × 墙，
    价格很可能被它钉住 → 不开（2026-10-20 用户定）。返回 (否决, 说明)。"""
    if not cells:
        return False, ""
    k, g = max(cells, key=lambda c: abs(c[1]))
    if g >= 0:
        return False, ""
    lo, hi = sorted((spot, target))
    on_path = lo < k < hi or abs(k - spot) / spot <= 0.005
    if on_path and abs(g) >= PIN_NEG_MULT * abs(wall_g):
        return True, (f"路径上有全到期最大的负节点 {k:g}（{g / 1e6:.1f}M，是墙的 {abs(g) / abs(wall_g):.1f} 倍 ≥ "
                      f"{PIN_NEG_MULT:g} 倍），可能被钉住")
    return False, ""


def _stop_risk(wall_dist: float | None) -> float:
    if wall_dist is None:
        return HW.min_stop_loss_spot_pct
    return max(HW.min_stop_loss_spot_pct, min(wall_dist, 0.012) + 0.003)


def find_cliff(ticker: str, fr: Frame, cx: Context, gmin: float, migration: str) -> tuple[Candidate | None, str]:
    S, cells, idx = fr.spot, fr.cells, is_index(ticker)
    ceil, _ = _walls(cells, S, gmin)
    if not ceil:
        return None, "上方没有显著正节点（天花板）"
    d_ceil = (ceil[0] - S) / S
    strong_bear = cx.intraday_ret <= -0.012 and S <= 0.995 * cx.vwap
    max_d = HW.max_wall_dist_index if idx else (0.027 if strong_bear else HW.max_wall_dist_stock)
    min_d = HW.min_dist_cliff_index if idx else HW.min_dist_cliff_stock
    if not (min_d <= d_ceil <= max_d):
        return None, f"离天花板 {ceil[0]:g} {d_ceil * 100:.3f}% 不在 {min_d * 100:.2f}%–{max_d * 100:.1f}%"
    step = strike_step(cells, S)
    big = outer_bigger_wall(cells, ceil[0], ceil[1], step, above=True)
    if big:
        return None, (f"天花板 {ceil[0]:g}(+{ceil[1] / 1e6:.1f}M) 外侧 {OUTER_WALL_STEPS} 格内有更大的正节点 "
                      f"{big[0]:g}(+{big[1] / 1e6:.1f}M)，价格会被吸上去")
    min_sp = HW.min_target_space_index if idx else HW.min_target_space_stock
    sup = _next_big_positive(cells, S, ceil[1], gmin, below=True)
    if sup is None:                         # 宁缺毋滥：下方没有超大正节点承接就不做（2026-10-20 用户定，删掉负节点兜底）
        return None, "下方 4.5% 内没有超大正节点承接（宁缺毋滥）"
    tgt, tgt_note = sup[0], f"目标 = 下方超大正节点 {sup[0]:g}(+{sup[1] / 1e6:.1f}M)"
    space = (S - tgt) / S
    if space < min_sp:
        return None, f"下方空间 {space * 100:.2f}%（{tgt_note}）< {min_sp * 100:.1f}%"
    ok, path_note, chasm = path_check(cells, S, tgt, gmin)
    if not ok:
        return None, path_note
    pin, pin_note = pin_check(cells, S, tgt, ceil[1])
    if pin:
        return None, pin_note
    rr = space / _stop_risk(d_ceil)
    if rr < HW.min_reward_to_risk_ratio:
        return None, f"盈亏比 {rr:.2f} < {HW.min_reward_to_risk_ratio}"
    floor = [(k, g) for k, g in cells if g >= gmin and k < S and (S - k) / S <= 0.0045]
    if floor:
        return None, f"下方 0.45% 内有强正地板 {floor[-1][0]:g}"
    veto, target, gk_note = _gatekeeper(cells, S, tgt, ceil[1], gmin, False, idx)
    if veto:
        return None, gk_note
    t5, t15 = cx.tick_ret_5m, cx.ret_15m
    if t5 is None or t15 is None:
        return None, "开盘不足 15 分钟，没有 5 / 15 分钟涨跌"
    if t5 > 0.0025 and t15 > 0.0035:
        return None, f"向上冲刺（5 分钟 {t5 * 100:+.2f}%、15 分钟 {t15 * 100:+.2f}%）"
    trig1 = t5 <= 0.0005 and (t15 <= 0.0005 or S <= cx.vwap or migration == "MIGRATING_DOWN")
    trig2 = cx.intraday_ret <= -0.012 and S <= 0.995 * cx.vwap and t5 <= 0.0025
    if not (trig1 or trig2):
        return None, (f"没有受阻回落（5 分钟 {t5 * 100:+.2f}%、15 分钟 {t15 * 100:+.2f}%、现价 {'≤' if S <= cx.vwap else '>'} VWAP、"
                      f"当天 {cx.intraday_ret * 100:+.2f}%）")
    trigger = "REJECTION_DROP" if d_ceil <= 0.006 else "TREND_PULLBACK_REJECT"
    return Candidate("CLIFF_STRUCTURE", "BEARISH", ceil[0], ceil[1], target, d_ceil, space, rr, trigger,
                     f"天花板 {ceil[0]:g}(+{ceil[1] / 1e6:.1f}M) 距 {d_ceil * 100:.2f}% · 燃料 {chasm[0]:g}({chasm[1] / 1e6:.1f}M) · "
                     f"{path_note} · {tgt_note} 空间 {space * 100:.2f}% · R:R {rr:.2f}" + (f" · {gk_note}" if gk_note else "")), ""


def find_springboard(ticker: str, fr: Frame, cx: Context, gmin: float, migration: str, frames: list
                     ) -> tuple[Candidate | None, str]:
    S, cells, idx = fr.spot, fr.cells, is_index(ticker)
    ceil, floor = _walls(cells, S, gmin)
    if not floor:
        return None, "下方没有显著正节点（踏板）"
    g_ceil = ceil[1] if ceil else 0.0
    if floor[1] <= 1.2 * g_ceil:
        return None, f"踏板 {floor[0]:g}(+{floor[1] / 1e6:.1f}M) 不到上方天花板的 1.2 倍"
    d_floor = (S - floor[0]) / S
    min_d = HW.min_dist_spring_index if idx else HW.min_dist_spring_stock
    max_d = HW.max_wall_dist_index if idx else HW.max_wall_dist_stock
    if not (min_d <= d_floor <= max_d):
        return None, f"离踏板 {floor[0]:g} {d_floor * 100:.3f}% 不在 {min_d * 100:.2f}%–{max_d * 100:.1f}%"
    step = strike_step(cells, S)
    big = outer_bigger_wall(cells, floor[0], floor[1], step, above=False)
    if big:
        return None, (f"踏板 {floor[0]:g}(+{floor[1] / 1e6:.1f}M) 外侧 {OUTER_WALL_STEPS} 格内有更大的正节点 "
                      f"{big[0]:g}(+{big[1] / 1e6:.1f}M)，价格会被吸下去")
    min_sp = HW.min_target_space_index if idx else HW.min_target_space_stock
    res = _next_big_positive(cells, S, floor[1], gmin, below=False)
    if res is None:                         # 宁缺毋滥：上方没有超大正节点就不做
        return None, "上方 4.5% 内没有超大正节点作目标（宁缺毋滥）"
    tgt, tgt_note = res[0], f"目标 = 上方超大正节点 {res[0]:g}(+{res[1] / 1e6:.1f}M)"
    space = (tgt - S) / S
    if space < min_sp:
        return None, f"上方空间 {space * 100:.2f}%（{tgt_note}）< {min_sp * 100:.1f}%"
    ok, path_note, accel = path_check(cells, S, tgt, gmin)
    if not ok:
        return None, path_note
    pin, pin_note = pin_check(cells, S, tgt, floor[1])
    if pin:
        return None, pin_note
    rr = space / _stop_risk(d_floor)
    if rr < HW.min_reward_to_risk_ratio:
        return None, f"盈亏比 {rr:.2f} < {HW.min_reward_to_risk_ratio}"
    block = [(k, g) for k, g in cells if g >= gmin and k > S and (k - S) / S <= 0.0045]
    if block:
        return None, f"上方 0.45% 内有强天花板 {block[0][0]:g}"
    veto, target, gk_note = _gatekeeper(cells, S, tgt, floor[1], gmin, True, idx)
    if veto:
        return None, gk_note
    t5, t15 = cx.tick_ret_5m, cx.ret_15m
    if t5 is None or t15 is None:
        return None, "开盘不足 15 分钟，没有 5 / 15 分钟涨跌"
    if cx.n_frames < 3 or not (cx.intraday_ret >= 0.0005 or migration == "MIGRATING_UP"):
        return None, f"接飞刀（当天 {cx.intraday_ret * 100:+.2f}%，墙体 {migration}）"
    if not (t5 >= -0.0005 and t15 >= -0.0005):
        return None, f"没有企稳回升（5 分钟 {t5 * 100:+.2f}%、15 分钟 {t15 * 100:+.2f}%）"
    if d_floor <= 0.006:
        gs = node_growth(frames, floor[0])
        if gs not in ("GROWING", "NEW") and t5 < 0.0010:
            return None, f"贴近踏板（{d_floor * 100:.2f}%）但踏板 {gs}、也没有反弹"
    trigger = "REJECTION_BOUNCE" if d_floor <= 0.006 else "TREND_PULLBACK_HOLD"
    return Candidate("SPRINGBOARD_STRUCTURE", "BULLISH", floor[0], floor[1], target, d_floor, space, rr, trigger,
                     f"踏板 {floor[0]:g}(+{floor[1] / 1e6:.1f}M) 距 {d_floor * 100:.2f}% · 燃料 {accel[0]:g}({accel[1] / 1e6:.1f}M) · "
                     f"{path_note} · {tgt_note} 空间 {space * 100:.2f}% · R:R {rr:.2f}" + (f" · {gk_note}" if gk_note else "")), ""


def find_squeeze(ticker: str, fr: Frame, cx: Context, gmin: float, regime: str) -> tuple[Candidate | None, str]:
    S, cells = fr.spot, fr.cells
    if regime == "RANGE_BUFFER":
        return None, "RANGE_BUFFER 环境"
    above = [(k, g) for k, g in cells if g > 0 and k > S]
    if not above:
        return None, "上方没有正节点"
    cw = max(above, key=lambda c: c[1])
    tp, _ = trunc_floors(cells)
    total = sum(abs(g) for _, g in cells) or 1.0
    if cw[1] < max(gmin, 2.0 * tp) or cw[1] / total < 0.18:
        return None, f"Call 墙 {cw[0]:g} 不够强（占 {cw[1] / total * 100:.0f}%）"
    mid = [(k, g) for k, g in cells if S < k < cw[0] and g > 0.25 * cw[1]]
    if mid:
        return None, f"中间有正墙 {mid[0][0]:g}"
    pin, pin_note = pin_check(cells, S, cw[0], cw[1])
    if pin:
        return None, pin_note
    run = (cw[0] - S) / S
    if not (0.012 <= run <= 0.055):
        return None, f"跑道 {run * 100:.2f}% 不在 1.2%–5.5%"
    t5, t15 = cx.tick_ret_5m, cx.ret_15m
    if t5 is None or t15 is None:
        return None, "开盘不足 15 分钟"
    if not (cx.intraday_ret >= 0.0015 and t5 >= 0 and t15 >= 0.0008 and S >= cx.vwap):
        return None, "没有有效突破"
    return Candidate("SHORT_GAMMA_VACUUM_SQUEEZE", "BULLISH", cw[0], cw[1], cw[0], None, run,
                     run / _stop_risk(None), "EFFECTIVE_BREAKOUT",
                     f"Call 墙 {cw[0]:g}(+{cw[1] / 1e6:.1f}M) 跑道 {run * 100:.2f}%"), ""


def find_avalanche(ticker: str, fr: Frame, cx: Context, gmin: float, regime: str) -> tuple[Candidate | None, str]:
    S, cells = fr.spot, fr.cells
    if regime == "RANGE_BUFFER":
        return None, "RANGE_BUFFER 环境"
    net = sum(g for _, g in cells)
    local = sum(g for k, g in cells if S * 0.96 <= k <= S * 1.003)
    if not (net < -1_500_000 or local <= -gmin):
        return None, "没有负 Gamma 主导"
    support = sum(g for k, g in cells if S * 0.988 <= k <= S * 1.001)
    if support > 0:
        return None, "现价附近有正支撑"
    _, tn = trunc_floors(cells)
    thr = max(0.75 * gmin, 1.5 * tn)
    deep = [(k, g) for k, g in cells if S * (1 - 0.045) <= k <= S * (1 - 0.0085) and g <= -thr]
    if not deep:
        return None, "下方 0.85%–4.5% 没有深负靶心"
    ch = min(deep, key=lambda c: c[1])
    if any(g >= gmin for k, g in cells if ch[0] < k < S):
        return None, "中间有正地板"
    t5, t15 = cx.tick_ret_5m, cx.ret_15m
    if t5 is None or t15 is None:
        return None, "开盘不足 15 分钟"
    if not (cx.intraday_ret <= -0.004 and S <= cx.vwap and (t15 <= -0.0015 or (cx.intraday_ret <= -0.012 and t5 <= 0))):
        return None, "没有有效破位"
    space = (S - ch[0]) / S
    return Candidate("SHORT_GAMMA_AVALANCHE", "BEARISH", ch[0], ch[1], ch[0], None, space,
                     space / _stop_risk(None), "EFFECTIVE_BREAKDOWN",
                     f"深渊 {ch[0]:g}({ch[1] / 1e6:.1f}M) 空间 {space * 100:.2f}%"), ""


# ============================================================================== 判定入口
@dataclass
class BaselineSignal:
    """与 detector.FullSignal 字段兼容（PaperTrader.open_position 只用 ticker / snapshot / structure / score / flow）。"""
    ticker: str
    time_et: dt.datetime
    snapshot: HeatmapSnapshot
    structure: StructureSignal
    flow: FlowConfirmation
    score: float
    cand: Candidate
    regime: str
    growth: str
    migration: str
    confluence: int
    conflict: bool
    stop_spot: float
    target_spot: float
    bias: str = ""
    fatigue_tests: int = 0
    score_parts: dict = field(default_factory=dict)

    def describe(self) -> str:
        c = self.cand
        return (f"{c.structure_type}/{c.trigger} 评分 {self.score:.0f} | {c.notes} | 环境 {self.regime} · 节点 {self.growth} · "
                f"墙体 {self.migration} · 共振 {self.confluence}{' · 到期冲突' if self.conflict else ''} | "
                f"现货止损 {self.stop_spot:.2f} 目标 {self.target_spot:.2f}")


class BaselineJudge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.frames: dict = {}            # 标的 → [Frame]（当天）
        self.last_reason: dict = {}

    def observe(self, ticker: str, payload: dict, frame_t: dt.datetime, ref_date: dt.date):
        """记录一根快照。返回 (Frame, HeatmapSnapshot, 是否新快照)；同一根快照重复喂入时不重复记录、也不再出信号。"""
        fr, snap = make_frame(payload, ref_date, self.cfg, frame_t)
        if fr is None:
            return None, None, False
        hist = self.frames.setdefault(ticker, [])
        if hist and hist[-1].t.date() != frame_t.date():
            hist.clear()
        if hist and hist[-1].t >= frame_t:
            return hist[-1], snap, False
        hist.append(fr)
        return fr, snap, True

    def evaluate(self, ticker: str, snap: HeatmapSnapshot, now_et: dt.datetime) -> BaselineSignal | None:
        frames = self.frames.get(ticker) or []
        if not frames or snap is None:
            return None
        fr = frames[-1]
        cx = context(frames)
        gmin = min_gex(ticker, fr.spot)
        # 衰竭拦截
        bear_exh = cx.intraday_ret <= -HW.exhaustion_intraday_ret_pct and (
            fr.spot > cx.day_low * (1 + HW.exhaustion_rebound_from_extreme_pct) or (cx.ret_15m or 0) > -0.0025)
        bull_exh = cx.intraday_ret >= HW.exhaustion_intraday_ret_pct and (
            fr.spot < cx.day_high * (1 - HW.exhaustion_rebound_from_extreme_pct) or (cx.ret_15m or 0) < 0.0025)
        migration = wall_migration(frames, gmin)
        cells_d = [{"strike_usd": k, "net_dealer_gex_usd": g} for k, g in fr.cells]
        regime, _share = compute_local_gamma_regime(cells_d, fr.spot, gmin, migration, HW)
        reasons, best = [], None
        finders = [("悬崖", lambda: find_cliff(ticker, fr, cx, gmin, migration)),
                   ("跳板", lambda: find_springboard(ticker, fr, cx, gmin, migration, frames)),
                   ("逼空", lambda: find_squeeze(ticker, fr, cx, gmin, regime)),
                   ("雪崩", lambda: find_avalanche(ticker, fr, cx, gmin, regime))]
        if not getattr(self.cfg, "bl_enable_avalanche", True):
            finders = finders[:3]          # 2026-10-20 用户定：先不做雪崩结构
        for name, fn in finders:
            cand, why = fn()
            if cand is None:
                reasons.append(f"{name}：{why}")
                continue
            if (cand.direction == "BEARISH" and bear_exh) or (cand.direction == "BULLISH" and bull_exh):
                reasons.append(f"{name}：日内衰竭拦截（当天 {cx.intraday_ret * 100:+.1f}%）")
                continue
            growth = node_growth(frames, cand.acting_strike)
            conf, conflict = expiry_confluence(fr, cand.acting_strike)
            score, passed, _w = compute_structure_confirmation_score(
                cand.structure_type, regime, growth, migration, cand.trigger, conf, conflict,
                cx.tick_ret_5m or 0.0, cx.ret_15m or 0.0, HW)
            risk = _stop_risk(cand.wall_dist)
            stop = fr.spot * (1 + risk) if cand.direction == "BEARISH" else fr.spot * (1 - risk)
            sig = BaselineSignal(
                ticker=ticker, time_et=now_et, snapshot=snap,
                structure=StructureSignal("CLIFF" if cand.direction == "BEARISH" else "SPRINGBOARD",
                                          "BUY_PUT" if cand.direction == "BEARISH" else "BUY_CALL",
                                          "P" if cand.direction == "BEARISH" else "C",
                                          cand.acting_strike, cand.target_strike, fr.spot, cand.wall_dist or 0.0),
                flow=FlowConfirmation(ok=False, reason="仅 Heatmap 模式（baseline）"),
                score=score, cand=cand, regime=regime, growth=growth, migration=migration, confluence=conf,
                conflict=conflict, stop_spot=round(stop, 2), target_spot=cand.target_strike)
            if not passed:
                reasons.append(f"{name}：几何与触发通过但评分 {score:.0f} < {HW.min_passing_score:.0f}（{sig.describe()}）")
                continue
            if best is None or sig.score > best.score:
                best = sig
        self.last_reason[ticker] = "；".join(reasons)
        return best


# ============================================================================== 交易：出场用 spec 的 evaluate_exit_tick
REASON_CN = {
    "INITIAL_STOP_LOSS": "初始止损 −35%", "RATCHET_SOFT_STOP": "棘轮软止损", "BREAKEVEN_STOP": "保本止损 +3%",
    "TRAILING_STOP": "连续棘轮锁利", "HARD_TP_CAP": "硬顶 +250%", "SPOT_STOP": "现货止损", "SPOT_PROFIT_STOP": "现货锁利止盈",
    "STRUCTURE_TARGET": "到达结构目标", "MAX_HOLD": "持仓满 180 分钟", "EOD_0DTE_CLOSE": "15:50 0DTE 强平",
    "DEAD_MOMENTUM_TRUNCATE_0DTE_8M": "0DTE 8 分钟零动能截断", "DEAD_MOMENTUM_TRUNCATE_10M": "10 分钟零动能截断",
    "THETA_STAGNATION_TRUNCATE_25M": "25 分钟滞涨截断",
}


import notify as _notify          # noqa: E402
import paper_trader as _pt         # noqa: E402
_pt._REASON_CN.update(REASON_CN)
_notify.REASON_CN.update(REASON_CN)


def near_trigger(st: PositionState, roi: float, spot: float, held_min: float, band_pct: float = 5.0,
                 spot_band: float = 0.001) -> bool:
    """持仓是否靠近任何出场触发线（用于实盘把盯仓从 1 分钟加密到 20 秒，2026-10-20 用户定）：
    期权收益率离 初始止损 / 软止损 / 保本线 / 连续棘轮锁利线 / 硬顶 / 正在生效的时间截断线 在 band_pct 个百分点以内，
    或现价离现货止损线 / 结构目标在 spot_band（0.1%）以内。"""
    lines = [EW.hard_take_profit_roi_pct]
    if not st.breakeven_locked:
        lines.append(EW.initial_stop_loss_roi_pct)
    if st.soft_stop_level_pct > EW.initial_stop_loss_roi_pct:
        lines.append(st.soft_stop_level_pct)
    if st.breakeven_locked:
        lines.append(EW.breakeven_floor_roi_pct)
    if st.trailing_active:
        lines.append(st.trailing_stop_level_pct)
    if st.is_0dte and held_min >= EW.stage0_0dte_minutes - 1 and st.peak_roi_pct <= EW.stage0_0dte_max_mfe_pct:
        lines.append(EW.stage0_0dte_mae_floor_pct)
    if held_min >= EW.stage1_dead_minutes - 1 and st.peak_roi_pct < EW.stage1_dead_max_mfe_pct:
        lines.append(EW.stage1_dead_mae_floor_pct)
    if held_min >= EW.stage2_stag_minutes - 1 and st.peak_roi_pct < EW.stage2_stag_max_mfe_pct:
        lines.append(EW.stage2_stag_mae_floor_pct)
    if any(abs(roi - x) <= band_pct for x in lines):
        return True
    for lvl in (st.stop_loss_spot, st.target_spot):
        if lvl and spot and abs(spot - lvl) / spot <= spot_band:
            return True
    return False


class BaselineTrader(PaperTrader):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.max_contract_cost_usd = getattr(cfg, "bl_max_contract_cost_usd", 0.0)
        self.max_spread_pct = getattr(cfg, "bl_max_spread_pct", 0.0)
        self.state: dict = {}         # position_id → PositionState
        self.meta: dict = {}          # position_id → 开仓时的 baseline 信息（写进交易明细）

    def open_position(self, client, sig, now_et: dt.datetime) -> Position | None:
        p = super().open_position(client, sig, now_et)
        if p is None or not isinstance(sig, BaselineSignal):
            return p
        p.base_strike = sig.cand.acting_strike
        p.stop_pct = EW.initial_stop_loss_roi_pct
        st = PositionState(position_id=p.position_id, ticker=p.ticker, direction=sig.cand.direction,
                           entry_spot=p.entry_spot, entry_option_price=p.entry_option_price,
                           initial_stop_loss_spot=sig.stop_spot, stop_loss_spot=sig.stop_spot,
                           target_spot=sig.target_spot, is_0dte=(p.expiration == now_et.date().isoformat()),
                           is_same_day_overnight=False,
                           is_opening_scalp=(9 * 60 + 30 <= now_et.hour * 60 + now_et.minute < 10 * 60),
                           quantity=p.contracts)
        self.state[p.position_id] = st
        self.meta[p.position_id] = {"bl_structure": sig.cand.structure_type, "bl_trigger": sig.cand.trigger,
                                    "bl_score": f"{sig.score:.0f}", "bl_regime": sig.regime, "bl_growth": sig.growth,
                                    "bl_migration": sig.migration, "bl_wall": f"{sig.cand.acting_strike:g}",
                                    "bl_wall_gex_m": f"{sig.cand.acting_gex / 1e6:.1f}",
                                    "bl_target": f"{sig.target_spot:g}", "bl_stop_spot": f"{sig.stop_spot:.2f}",
                                    "bl_rr": f"{sig.cand.rr:.2f}" if sig.cand.rr is not None else "",
                                    "bl_notes": sig.cand.notes}
        log.info("【baseline·开仓依据】%s %s", p.position_id, sig.describe())
        return p

    def any_near_trigger(self, now_et: dt.datetime) -> bool:
        for p in self.open_positions():
            st = self.state.get(p.position_id)
            if st is not None and near_trigger(st, p.roi_pct, p.last_spot, (now_et - p.entry_time).total_seconds() / 60):
                return True
        return False

    def mark(self, p: Position, spot: float, now_et: dt.datetime) -> Position | None:
        st = self.state.get(p.position_id)
        if st is None:
            return super().mark(p, spot, now_et)
        if not p.is_open:
            return None
        mid = self.current_mid(p, spot, now_et)
        px = self._exit_value(p, mid)                    # 能卖出的价格（实盘 bid / 回放 中间价 − 半价差）
        held = (now_et - p.entry_time).total_seconds() / 60
        action, reason, _ = evaluate_exit_tick(st, spot, px, held, (now_et.hour, now_et.minute), EW)
        roi = (px / p.entry_option_price - 1) * 100
        p.last_spot, p.last_mid, p.roi_pct, p.marks = spot, mid, roi, p.marks + 1
        p.peak_roi_pct = max(p.peak_roi_pct, roi)
        p.max_adverse_roi_pct = min(p.max_adverse_roi_pct, roi)
        if action == "CLOSE_ALL":
            return self.close(p, spot, now_et, reason, mid)
        return None



# ============================================================================== 引擎：替换 SessionEngine 的信号部分
class BaselineEngine(SessionEngine):
    """接口与 SessionEngine 相同（on_snapshot / flush / mark_open / end_of_day），实盘与回放都用它。
    frame_time(payload, now) 决定快照标记时间：实盘取 generated_at，回放由调用方传入。"""

    def __init__(self, cfg, client, detector, trader: BaselineTrader):
        super().__init__(cfg, client, detector, trader)
        self.judge = BaselineJudge(cfg)
        self.spot_override = None     # 可选 callable(position, now) -> 实时现价（实盘 Call/Put 平价反推；回放 1 分钟真实标的价）

    def can_enter(self, ticker: str, now_et: dt.datetime):
        """baseline 开仓门槛（2026-10-20 用户定）：不限持仓数量 / 每天笔数 / 午前笔数；保留 15:00 截止、
        同一只票同时只有 1 笔（持仓中不出新信号）、同票每天最多 max_trades_per_ticker_day 笔、平仓后冷却。"""
        cfg = self.cfg
        if now_et.hour * 60 + now_et.minute >= 15 * 60:
            return False, "15:00 后不开日内单（baseline 尾盘为隔夜模式）"
        if self.trader.open_for(ticker) is not None:
            return False, "该标的已有持仓"
        if cfg.max_trades_per_ticker_day and len(self.trader.trades_today(ticker, now_et.date())) >= cfg.max_trades_per_ticker_day:
            return False, "该标的当日开仓次数已达上限"
        le = self.last_exit_time.get(ticker)
        if le and (now_et - le).total_seconds() < cfg.reentry_cooldown_min * 60:
            return False, "平仓冷却期中"
        return True, ""

    def on_snapshot(self, ticker: str, payload: dict, now_et: dt.datetime, frame_t: dt.datetime | None = None) -> None:
        d = payload.get("data") if isinstance(payload, dict) else None
        if frame_t is None:
            from config import parse_iso, to_et
            g = parse_iso((d or {}).get("generated_at"))
            frame_t = to_et(g) if g else now_et
        fr, snap, is_new = self.judge.observe(ticker, payload, frame_t, now_et.date())
        if fr is None:
            return
        self.last_spot[ticker] = fr.spot
        pos = self.trader.open_for(ticker)
        if pos is not None:
            spot = (self.spot_override(pos, now_et) if self.spot_override else None) or fr.spot
            if self.trader.mark(pos, spot, now_et):
                self.last_exit_time[ticker] = now_et
            return
        if not is_new:
            return
        if (now_et - frame_t).total_seconds() > self.cfg.max_snapshot_age_min * 60:
            log.info("【baseline】%s 快照 %s 已过时，不出信号", ticker, frame_t.strftime("%H:%M"))
            return
        ok, why = self.can_enter(ticker, now_et)
        if not ok:
            return
        sig = self.judge.evaluate(ticker, snap, now_et)
        if sig is not None:
            log.info("【baseline·信号】%s %s", ticker, sig.describe())
            self.pending[ticker] = sig
        else:
            log.debug("【baseline·无信号】%s %s", ticker, self.judge.last_reason.get(ticker, ""))

    def mark_open(self, now_et: dt.datetime) -> None:
        for pos in list(self.trader.open_positions()):
            spot = (self.spot_override(pos, now_et) if self.spot_override else None) or pos.last_spot \
                or self.last_spot.get(pos.ticker)
            if not spot:
                continue
            if self.trader.mark(pos, spot, now_et):
                self.last_exit_time[pos.ticker] = now_et

    def flush(self, now_et: dt.datetime) -> None:
        if not self.pending:
            return
        ranked = sorted(self.pending.values(), key=lambda s: -s.score)
        self.pending = {}
        opened, skipped = [], []
        for sig in ranked:
            ok, why = self.can_enter(sig.ticker, now_et)
            if not ok:
                skipped.append(f"{sig.ticker}({sig.score:.0f},{why})")
                continue
            self._record_flow(sig, now_et)
            if self.trader.open_position(self.client, sig, now_et) is not None:
                opened.append(f"{sig.ticker}({sig.score:.0f})")
        log.info("【baseline·排序】%s 本轮信号 %d 个 → 开仓 %s%s", now_et.strftime("%H:%M"), len(ranked),
                 "、".join(opened) or "无", ("；未开：" + "、".join(skipped[:8])) if skipped else "")


def pair_symbol(sym: str) -> str:
    """同到期同行权价的另一边（Call ↔ Put），用于 Call/Put 平价反推实时现价。"""
    core = sym.split(":")[-1]
    right = core[-9]
    other = "P" if right == "C" else "C"
    return ("O:" if sym.startswith("O:") else "") + core[:-9] + other + core[-8:]


def parity_spot(sym: str, quotes_get) -> float | None:
    """现价 ≈ 行权价 + Call 中间价 − Put 中间价（同到期同行权价；0DTE / 近月贴现可忽略）。任一边没有报价返回 None。"""
    core = sym.split(":")[-1]
    K = int(core[-8:]) / 1000
    c_sym = sym if core[-9] == "C" else pair_symbol(sym)
    p_sym = pair_symbol(c_sym)
    c, p = quotes_get(c_sym), quotes_get(p_sym)
    if not c or not p or (c.get("ask") or 0) <= 0 or (p.get("ask") or 0) <= 0:
        return None
    cm = c.get("mid") or (c["bid"] + c["ask"]) / 2
    pm = p.get("mid") or (p["bid"] + p["ask"]) / 2
    return K + cm - pm
