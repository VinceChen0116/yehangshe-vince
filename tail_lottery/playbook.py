"""Playbook（尾盘彩票，说明 7.6 节）：纯函数，输入当前读数和短期历史，输出倾向与证据清单。仅观察，非建议。

权重和阈值来自原系统 2026 年在 IBKR 行情上的标定，**不保证在其他时期或其他数据源（包括夜航社）上有效**。
"""
from __future__ import annotations

# ============================================================================ 阈值（集中放这里）
WINDOW_START, WINDOW_END = 15 * 60 + 30, 16 * 60     # 美东 15:30–16:00
LATE_START = 15 * 60 + 50                            # key_level / rejection_reversal 从 15:50 起
VERDICT_GAP = 21                                     # 两侧分差 ≥ 21 才给方向（单独一条 Charm 20 分不够）

W_STRIKE_BIAS = 40
STRIKE_BAND_PCT = 0.01                               # 现货 ±1% 带
STRIKE_LOOKBACK_S = 600
STRIKE_MIN_CONTRACTS = 200
STRIKE_RATIO = 0.15

W_CHARM = 20                                         # LOW_STABILITY 时减半

W_KEY_LEVEL = 15
KEY_NEAR_PCT = 0.0025                                # "现货附近" = 节点距现货 ≤ 0.25%（约 18 点；原文未给，我的默认）
KEY_LOOKBACK_S = 300                                 # 看最近 5 分钟有没有穿越 / 是否一直守在上方（原文未给，我的默认）

W_SKEW = 10
SKEW_PUT_RR = -0.005

W_BREAKOUT = 15
BREAKOUT_PTS = 3.0

W_REJECT, W_REJECT_CHARM, W_REJECT_FLOW = 15, 10, 10
REJECT_RETURN_S = 180
REJECT_TOUCH_PTS = 1.0                               # 距节点 ≤ 1 点算"触及"（原文未给，我的默认）
REJECT_MIN_SAMPLES = 3

W_GEX_REGIME = 15
GEX_MIN_COVERAGE = 0.20

ATM_BAND_PCT = 0.0025                                # ATM 附近 = 现货 ±0.25%（原文未给，我的默认）
ATM_RATIO = 0.10
ATM_MIN_CONTRACTS = 50

CALL, PUT, NEUTRAL, STAND_BY = "CALL", "PUT", "NEUTRAL", "STAND_BY"
LABEL = {CALL: "CALL", PUT: "PUT", NEUTRAL: "中性", STAND_BY: "待命"}
DISCLAIMER = "仅观察 · 非建议"


def _leg(name, weight=0, side=None, score=0, note="", abstain=None, info=False):
    return {"name": name, "weight": weight, "side": side, "score": score, "note": note,
            "abstain": abstain, "info": info}


def _side_of(x, node):
    return "above" if x > node else ("below" if x < node else "on")


# ============================================================================ 证据腿
def leg_strike_bias(inp):
    spot = inp["spot"]
    band = [r for r in inp["flow_600"] if abs(r["strike"] - spot) <= STRIKE_BAND_PCT * spot]
    cv = sum(r["contracts"] for r in band if r["right"] == "C")
    pv = sum(r["contracts"] for r in band if r["right"] == "P")
    cn = sum(r["buy"] - r["sell"] for r in band if r["right"] == "C")
    pn = sum(r["buy"] - r["sell"] for r in band if r["right"] == "P")
    tot = cv + pv
    if tot < STRIKE_MIN_CONTRACTS:
        return _leg("strike_bias", W_STRIKE_BIAS, abstain=f"带内 600 秒成交 {tot:.0f} 张 < {STRIKE_MIN_CONTRACTS}")
    vr, nr = (cv - pv) / tot, (cn - pn) / tot
    note = f"量差 {vr:+.2f} · 主动净量差 {nr:+.2f}（{tot:.0f} 张）"
    vs = CALL if vr >= STRIKE_RATIO else PUT if vr <= -STRIKE_RATIO else None
    ns = CALL if nr >= STRIKE_RATIO else PUT if nr <= -STRIKE_RATIO else None
    if vs and ns and vs != ns:
        return _leg("strike_bias", W_STRIKE_BIAS, abstain="成交量与主动净量方向相反 · " + note)
    side = ns or vs
    if not side:
        return _leg("strike_bias", W_STRIKE_BIAS, abstain=f"占比未达 ±{STRIKE_RATIO} · " + note)
    return _leg("strike_bias", W_STRIKE_BIAS, side, W_STRIKE_BIAS, note)


def leg_charm(inp):
    ch = inp.get("charm") or {}
    if ch.get("error"):
        return _leg("charm_hedge_flow", W_CHARM, abstain="Charm 计算失败")
    if not ch.get("available"):
        return _leg("charm_hedge_flow", W_CHARM, abstain="Charm 不可用：" + str(ch.get("reason", "")))
    w = W_CHARM / 2 if ch.get("stability") == "LOW_STABILITY" else W_CHARM
    side = CALL if ch["direction"] == "BUY" else PUT
    note = f"对冲流 {ch['direction']} {ch['hedge_flow_delta_1m']:+,.0f} Δ/分钟 · {ch.get('stability')}"
    return _leg("charm_hedge_flow", W_CHARM, side, w, note)


def _nodes(inp):
    g = inp.get("gex") or {}
    return [n["strike"] for n in (g.get("top3") or [])] if g.get("available") else []


def leg_key_level(inp):
    if inp["now_min"] < LATE_START:
        return _leg("key_level", W_KEY_LEVEL, abstain="未到 15:50")
    spot = inp["spot"]
    near = [k for k in _nodes(inp) if abs(k - spot) <= KEY_NEAR_PCT * spot]
    if not near:
        return _leg("key_level", W_KEY_LEVEL, abstain="现货附近没有 Top-3 GEX 节点")
    recent = [s for t, s in inp["spots"] if (inp["now"] - t).total_seconds() <= KEY_LOOKBACK_S]
    if not recent:
        return _leg("key_level", W_KEY_LEVEL, abstain="价格样本不足")
    node = min(near, key=lambda k: abs(k - spot))
    sides = [_side_of(s, node) for s in recent if _side_of(s, node) != "on"]
    if sides and sides[0] != sides[-1]:
        side = CALL if sides[-1] == "above" else PUT
        return _leg("key_level", W_KEY_LEVEL, side, W_KEY_LEVEL, f"现货{'向上' if side == CALL else '向下'}穿越节点 {node:g}")
    if sides and all(s == "above" for s in sides):
        return _leg("key_level", W_KEY_LEVEL, CALL, W_KEY_LEVEL, f"现货守在节点 {node:g} 上方")
    return _leg("key_level", W_KEY_LEVEL, abstain=f"现货在节点 {node:g} 下方且未穿越（原规则只定义'守在上方'）")


def leg_skew(inp):
    sk = inp.get("skew") or {}
    if sk.get("error"):
        return _leg("skew_rr25", W_SKEW, abstain="Skew 计算失败")
    if not sk.get("available"):
        return _leg("skew_rr25", W_SKEW, abstain="Skew 不可用：" + str(sk.get("reason", "")))
    rr = sk["risk_reversal_25d"]
    note = f"RR25 {rr * 100:+.2f} 波动率点 · ATM IV {sk['atm_iv'] * 100:.1f}%" + (" · 覆盖不足" if sk.get("flag") else "")
    if rr <= SKEW_PUT_RR:
        return _leg("skew_rr25", W_SKEW, PUT, W_SKEW, note)
    return _leg("skew_rr25", W_SKEW, abstain="RR25 未低于 −0.5 点，不投票 · " + note)


def _session_spots(inp):
    return [(t, s) for t, s in inp["spots"] if inp["window_start"] <= t <= inp["now"]]


def leg_breakout(inp):
    nodes = _nodes(inp)
    if not nodes:
        return _leg("breakout_integrity", W_BREAKOUT, abstain="没有 GEX 节点")
    ss = [s for _, s in _session_spots(inp)]
    best = None
    for node in nodes:
        start = next((_side_of(s, node) for s in ss if _side_of(s, node) != "on"), None)
        if start is None:
            continue
        broke, back = False, False
        for s in ss:
            if not broke and ((start == "below" and s >= node + BREAKOUT_PTS) or (start == "above" and s <= node - BREAKOUT_PTS)):
                broke = True
            elif broke and _side_of(s, node) == start:
                back = True
        if broke and back and ss and _side_of(ss[-1], node) == start:
            best = (node, PUT if start == "below" else CALL)
        elif broke and best is None:
            best = (node, None)
    if best is None:
        return _leg("breakout_integrity", W_BREAKOUT, abstain=f"突破幅度不足 {BREAKOUT_PTS:g} 点")
    if best[1] is None:
        return _leg("breakout_integrity", W_BREAKOUT, abstain=f"节点 {best[0]:g} 已突破，未被打回")
    return _leg("breakout_integrity", W_BREAKOUT, best[1], W_BREAKOUT, f"节点 {best[0]:g} 突破 ≥{BREAKOUT_PTS:g} 点后被打回")


def leg_rejection(inp, atm_side, charm_side):
    if not (LATE_START <= inp["now_min"] < WINDOW_END):
        return _leg("rejection_reversal", W_REJECT, abstain="仅 15:50–16:00")
    pts = [(t, s) for t, s in inp["spots"] if (inp["now"] - t).total_seconds() <= 600]
    if len(pts) < REJECT_MIN_SAMPLES:
        return _leg("rejection_reversal", W_REJECT, abstain=f"价格样本 {len(pts)} < {REJECT_MIN_SAMPLES}")
    nodes = _nodes(inp)
    if not nodes:
        return _leg("rejection_reversal", W_REJECT, abstain="没有节点")
    hit = None
    for node in nodes:
        for i, (t, s) in enumerate(pts):
            if abs(s - node) > REJECT_TOUCH_PTS and not (i and _side_of(pts[i - 1][1], node) != _side_of(s, node)):
                continue
            before = [_side_of(x, node) for _, x in pts[:i] if _side_of(x, node) != "on"]
            if not before:
                continue
            start = before[-1]
            after = [(tt, x) for tt, x in pts[i + 1:] if (tt - t).total_seconds() <= REJECT_RETURN_S]
            if any(_side_of(x, node) == start and abs(x - node) > REJECT_TOUCH_PTS for _, x in after) \
                    and _side_of(pts[-1][1], node) == start:
                hit = (node, PUT if start == "below" else CALL, t)
    if not hit:
        return _leg("rejection_reversal", W_REJECT, abstain="没有触及节点后 180 秒内回到起始侧的形态")
    node, side, _ = hit
    score, extra = W_REJECT, []
    if charm_side == side:
        score += W_REJECT_CHARM
        extra.append("Charm 一致 +10")
    if atm_side == side:
        score += W_REJECT_FLOW
        extra.append("ATM 5 分钟成交一致 +10")
    note = f"触及节点 {node:g} 后回到{'下方' if side == PUT else '上方'}" + ("；" + "，".join(extra) if extra else "")
    return _leg("rejection_reversal", W_REJECT + W_REJECT_CHARM + W_REJECT_FLOW, side, score, note)


def leg_atm_flow(inp):
    spot = inp["spot"]
    band = [r for r in inp["flow_300"] if abs(r["strike"] - spot) <= ATM_BAND_PCT * spot]
    cv = sum(r["contracts"] for r in band if r["right"] == "C")
    pv = sum(r["contracts"] for r in band if r["right"] == "P")
    tot = cv + pv
    if tot < ATM_MIN_CONTRACTS:
        return _leg("atm_flow_5m", 0, abstain=f"ATM 5 分钟成交 {tot:.0f} 张 < {ATM_MIN_CONTRACTS}", info=True), None
    r = (cv - pv) / tot
    side = CALL if r >= ATM_RATIO else PUT if r <= -ATM_RATIO else None
    note = f"Call/Put 占比差 {r:+.2f}（{tot:.0f} 张），只展示不计分"
    if not side:
        return _leg("atm_flow_5m", 0, abstain="占比差未达 ±0.1 · " + note, info=True), None
    return _leg("atm_flow_5m", 0, side, 0, note, info=True), side


def leg_gex_regime(inp, leader, leader_has_non_charm=True):
    g = inp.get("gex") or {}
    if g.get("error"):
        return _leg("gex_regime", W_GEX_REGIME, abstain="GEX 计算失败")
    if not g.get("available") or g.get("coverage", 0) < GEX_MIN_COVERAGE:
        return _leg("gex_regime", W_GEX_REGIME, abstain=f"GEX 覆盖率 {g.get('coverage', 0):.0%} < 20%")
    if g["total"] >= 0:
        return _leg("gex_regime", W_GEX_REGIME, None, 0, "正 Gamma：0 分，钉住风险")
    if leader is None:
        return _leg("gex_regime", W_GEX_REGIME, None, 0, "负 Gamma，但没有领先方向")
    if not leader_has_non_charm:   # 2026-11-19 用户定：只有 Charm 领先时不加分，否则 Charm 20 + 15 = 35 会单独构成结论
        return _leg("gex_regime", W_GEX_REGIME, None, 0, f"负 Gamma，但领先方（{leader}）只有 Charm 一条证据，不加分")
    return _leg("gex_regime", W_GEX_REGIME, leader, W_GEX_REGIME, f"负 Gamma，放大领先方（{leader}）")


# ============================================================================ 裁决
def evaluate(inp: dict) -> dict:
    """inp 字段：now(UTC) now_min(美东分钟数) window_start(UTC 15:30) spot spots[(t,s)] flow_600 flow_300
    gex skew charm day_volume{(k,r):张} mids{(k,r):中间价} blockers[str]"""
    base = {"disclaimer": DISCLAIMER, "legs": [], "call_score": 0, "put_score": 0, "conflict": False, "reference": None}
    if not (WINDOW_START <= inp["now_min"] < WINDOW_END):
        return dict(base, verdict=STAND_BY, label=LABEL[STAND_BY], reason="不在美东 15:30–16:00")
    if inp.get("blockers"):
        return dict(base, verdict=STAND_BY, label=LABEL[STAND_BY], reason="；".join(inp["blockers"]))
    if inp.get("spot") is None:
        return dict(base, verdict=STAND_BY, label=LABEL[STAND_BY], reason="没有现价")
    legs = [leg_strike_bias(inp), leg_charm(inp), leg_key_level(inp), leg_skew(inp), leg_breakout(inp)]
    atm_leg, atm_side = leg_atm_flow(inp)
    ch = inp.get("charm") or {}
    charm_side = (CALL if ch.get("direction") == "BUY" else PUT) if ch.get("available") else None
    legs.append(leg_rejection(inp, atm_side, charm_side))
    cs = sum(l["score"] for l in legs if l["side"] == CALL)
    ps = sum(l["score"] for l in legs if l["side"] == PUT)
    leader = CALL if cs > ps else PUT if ps > cs else None
    non_charm = any(l["side"] == leader and l["score"] > 0 and l["name"] != "charm_hedge_flow" for l in legs)
    reg = leg_gex_regime(inp, leader, non_charm)
    legs.append(reg)
    if reg["side"] == CALL:
        cs += reg["score"]
    elif reg["side"] == PUT:
        ps += reg["score"]
    legs.append(atm_leg)
    conflict = any(l["side"] == CALL and l["score"] > 0 and not l["info"] for l in legs) and \
        any(l["side"] == PUT and l["score"] > 0 and not l["info"] for l in legs)
    verdict = CALL if cs - ps >= VERDICT_GAP else PUT if ps - cs >= VERDICT_GAP else NEUTRAL
    out = dict(base, verdict=verdict, label=LABEL[verdict], legs=legs, call_score=cs, put_score=ps, conflict=conflict,
               reason="" if verdict != NEUTRAL else f"分差 {abs(cs - ps):g} < {VERDICT_GAP}")
    if verdict in (CALL, PUT):
        r = "C" if verdict == CALL else "P"
        spot = inp["spot"]
        cands = [(v, k) for (k, rr), v in inp.get("day_volume", {}).items()
                 if rr == r and abs(k - spot) <= STRIKE_BAND_PCT * spot]
        if cands:
            v, k = max(cands)
            out["reference"] = {"strike": k, "right": r, "day_volume": round(v), "mid": inp.get("mids", {}).get((k, r))}
    return out


# ============================================================================ 钉住蝴蝶（15:10 判定一次）
PIN_WALL_GAP = 10
PIN_MID_DIST = 4
PIN_RANGE = 3.5
PIN_MAX_DEV = 4.5
PIN_WING = 10


def pin_butterfly(g1500, g1510, spot1500, spots, quotes):
    """全部条件满足才返回提示，任何输入缺失返回 (None, 原因)。quotes: {(k,'C'): Contract}"""
    if not g1500 or not g1510 or not g1500.get("available") or not g1510.get("available"):
        return None, "缺 15:00 或 15:10 的 GEX"
    cw, pw = g1500.get("call_wall"), g1500.get("put_wall")
    if cw is None or pw is None or spot1500 is None or len(spots) < 2:
        return None, "缺墙位或现货"
    if abs(cw - pw) > PIN_WALL_GAP:
        return None, f"两墙相距 {abs(cw - pw):g} > {PIN_WALL_GAP}"
    mid = (cw + pw) / 2
    if abs(spot1500 - mid) > PIN_MID_DIST:
        return None, f"15:00 现货距中点 {abs(spot1500 - mid):.1f} > {PIN_MID_DIST}"
    ss = [s for _, s in spots]
    if max(ss) - min(ss) > PIN_RANGE or max(abs(s - mid) for s in ss) > PIN_MAX_DEV:
        return None, "15:00–15:10 振幅或偏离过大"
    if cw != g1510.get("call_wall") and pw != g1510.get("put_wall"):
        return None, "两侧墙都移动了"
    strikes = sorted({k for (k, r) in quotes if r == "C"})
    if not strikes:
        return None, "没有报价"
    K = min(strikes, key=lambda k: abs(k - mid))
    legs = [quotes.get((K - PIN_WING, "C")), quotes.get((K, "C")), quotes.get((K + PIN_WING, "C"))]
    if any(c is None or not c.tradable for c in legs):
        return None, "三条腿报价不全"
    debit = legs[0].ask - 2 * legs[1].bid + legs[2].ask
    return {"structure": f"买 {K - PIN_WING:g}C / 卖 2×{K:g}C / 买 {K + PIN_WING:g}C",
            "legs": [{"strike": c.strike, "bid": c.bid, "ask": c.ask} for c in legs],
            "est_debit": round(debit, 2), "mid_point": mid, "disclaimer": DISCLAIMER}, ""
