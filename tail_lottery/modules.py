"""计算模块：现货估算、Feed、Strike Leader、GEX、Skew、Charm（说明第 7 节）。

所有阈值集中在本文件顶部。数值来自原系统 2026 年在 IBKR 行情上的标定，**不保证在其他时期或其他数据源上有效**；
改成夜航社分钟粒度后，Feed 的阈值更只是待你调整的初值。
"""
from __future__ import annotations

import datetime as dt
import math
import statistics

from . import bsm
from .direction import BUY, classify_print, split_bucket_row

# ============================================================================ 阈值（集中放这里）
# --- 现货（平价反推）
SPOT_PARITY_STRIKES = 3            # 取离现货最近的 3 个行权价做平价反推，取中位数
SPOT_PARITY_MAX_SPREAD = 2.0       # 买卖价差 > $2 的行权价不用

# --- 新鲜度（原文：Greeks 超过 5 秒没更新算过期；夜航社是约 30 秒轮询，改为 120 秒）
CHAIN_STALE_S = 120                # 期权链报价时间距现在 > 120 秒 → 过期
GREEKS_STALE_S = 120               # 券商 IV 距现在 > 120 秒 → 不用，改本地反推

# --- Feed（原文 1/5/10 秒窗口 vs 1/3/5 分钟基线；夜航社只有分钟桶，改为"最新 1 分钟 vs 之前 5 分钟平均"）
FEED_MIN_ASK, FEED_MAX_ASK = 0.05, 4.00
FEED_BASELINE_MIN = 5              # 基线：之前 5 个 1 分钟桶的平均
FEED_BASELINE_NEED = 3             # 至少 3 个基线桶才出倍数类信号
FEED_SPIKE_X = 5.0                 # volume_spike / premium_spike 倍数（待使用者调整的初值）
FEED_ASK_SPIKE_X = 3.0             # ask_buy_spike 的"成交量显著放大"倍数（初值）
FEED_ASK_SHARE = 0.60              # ask_buy_spike 主动买占比
FEED_MIN_VOL = 20                  # 倍数类信号的最小张数，防止 1 张 vs 0.2 张这种噪声（初值）
FEED_MIN_BASE_VOL = 2.0            # 基线张数下限（基线为 0 时按此算倍数）
FEED_LARGE_CONTRACTS = 50          # large_ask_trade：单笔 ≥ 50 张 或 权利金 ≥ $5 万，且判为主动买
FEED_LARGE_PREMIUM = 50_000
FEED_MERGE_S = 90                  # 原文 10 秒内合并；分钟粒度下改为 90 秒（相邻两分钟合并为一条）

# --- Strike Leader
LEADER_WINDOWS = {"1m": 1, "5m": 5, "15m": 15, "day": None}
LEADER_ACTIVE_S = 120              # 2 分钟内有信号算"活跃"

# --- GEX
GEX_STRIKES_EACH_SIDE = 19         # 以现货为中心上下各 19 个行权价（原文 78 个合约）
GEX_MIN_COVERAGE = 0.20

# --- Skew
SKEW_MAX_SPREAD = 1.00             # 本地反推：价差 ≤ $1
SKEW_MAX_SPREAD_PCT = 0.25         #           且 ≤ 中间价 25%
SKEW_MIN_VEGA = 1.0                # vega（每 1.00 波动率）< 1：IV 变 1 个点价格变不到 1 美分，报价跳动就能让 IV 乱跳，不反推（原文未给数值，我的默认）
SKEW_MIN_COVERAGE = 0.35
SKEW_STRIKES_EACH_SIDE = 19
SKEW_RR_DELTA_TOL = 0.15           # 最接近 25Δ 的腿 |delta| 必须在 0.10–0.40 内，否则 RR25 不算（临近收盘 delta 塌缩时会出现；我的默认）

# --- Charm
CHARM_START = "15:30"
CHARM_MIN_T_S = 10                 # 距到期不足 10 秒不输出
CHARM_STAB_STRIKES = 10            # ATM 上下各 10 档
CHARM_STAB_COVERAGE = 0.70
CHARM_LOW_STAB_LAST_S = 120        # 距收盘不足 2 分钟 → LOW_STABILITY
MM_SIGN = -1                       # 做市商与客户相反（假设）


# ============================================================================ 工具
def years_to_close(now: dt.datetime, close: dt.datetime) -> float:
    return max(0.0, (close - now).total_seconds()) / bsm.YEAR_S


def strike_window(contracts, spot, each_side):
    """以现货为中心上下各 each_side 个行权价的合约。"""
    strikes = sorted({c.strike for c in contracts})
    if not strikes or spot is None:
        return []
    below = [k for k in strikes if k <= spot][-each_side:]
    above = [k for k in strikes if k > spot][:each_side]
    keep = set(below) | set(above)
    return [c for c in contracts if c.strike in keep]


def by_strike(contracts):
    out = {}
    for c in contracts:
        out.setdefault(c.strike, {})[c.right] = c
    return out


# ============================================================================ 现货：平价反推
def parity_spot(chain, T: float, hint: float | None = None):
    """S = (C − P + K·e^{−rT}) · e^{qT}，取离现货最近的几个行权价的中位数。返回 (现货, 用到的行权价数)。"""
    pairs = []
    for k, d in by_strike(chain.contracts).items():
        c, p = d.get("C"), d.get("P")
        if not c or not p or not c.tradable or not p.tradable:
            continue
        if c.ask - c.bid > SPOT_PARITY_MAX_SPREAD or p.ask - p.bid > SPOT_PARITY_MAX_SPREAD:
            continue
        pairs.append((k, c.mid - p.mid))
    if not pairs:
        return None, 0
    if hint is None:   # 没有参考价：用 |C − P| 最小的行权价当参考
        hint = min(pairs, key=lambda x: abs(x[1]))[0]
    pairs.sort(key=lambda x: abs(x[0] - hint))
    est = [(diff + k * math.exp(-bsm.R * T)) * math.exp(bsm.Q * T) for k, diff in pairs[:SPOT_PARITY_STRIKES]]
    return statistics.median(est), len(est)


# ============================================================================ 实时报价 → 现用期权链
def window_symbols(chain, spot, each_side):
    return [c.symbol for c in strike_window(chain.contracts, spot, each_side)]


def nearest_contracts(contracts, spot, n):
    """离现货最近的 n 个行权价的全部合约。"""
    ks = sorted({c.strike for c in contracts}, key=lambda k: abs(k - spot))[:n]
    return [c for c in contracts if c.strike in set(ks)]


def live_snapshot(base, quotes, now, spot_hint, T, each_side=None, nearest=None):
    """用实时报价覆盖链快照（链快照只提供行权价 / 合约代码 / OI）。
    只保留拿到实时报价的合约；IV 清空（链快照的 IV 落后 13 分钟），gamma / delta 用实时报价本地反推的 IV 计算。
    返回 (ChainSnapshot, 现货, 平价用到的行权价数)。"""
    from dataclasses import replace
    from .source import ChainSnapshot
    cs, times = [], []
    pool = nearest_contracts(base.contracts, spot_hint, nearest) if nearest else \
        strike_window(base.contracts, spot_hint, each_side)
    for c in pool:
        q = quotes.get(c.symbol)
        if not q:
            continue
        bid, ask, at = q
        cs.append(replace(c, bid=bid, ask=ask, quoted_at=at, iv=None, delta=None, gamma=None))
        if at is not None:
            times.append(at)
    snap = ChainSnapshot(fetched_at=now, expiration=base.expiration, quote_as_of=min(times) if times else None,
                         greeks_as_of=None, oi_as_of=base.oi_as_of, underlying_price=None, underlying_as_of=None,
                         contracts=cs, notice="")
    spot, n = parity_spot(snap, T, hint=spot_hint)
    if spot is not None and T > 0:
        for c in cs:
            iv, _ = local_iv(c, spot, T)
            if iv is not None:
                c.gamma = bsm.gamma(spot, c.strike, T, iv)
                c.delta = bsm.delta(spot, c.strike, T, iv, c.right)
    return snap, spot, n


# ============================================================================ IV 选取（Skew 与 Charm 共用）
def local_iv(c, S, T):
    """本地反推，通过全部拒绝条件才返回 IV，否则 (None, 原因)。"""
    if not c.tradable:
        return None, "报价无效"
    spread = c.ask - c.bid
    mid = c.mid
    if spread > SKEW_MAX_SPREAD or (mid > 0 and spread > SKEW_MAX_SPREAD_PCT * mid) or mid <= 0:
        return None, "价差过大"
    lo, hi = bsm.bounds(S, c.strike, T, c.right)
    if not (lo < mid < hi):
        return None, "超出无套利边界"
    iv = bsm.implied_vol(mid, S, c.strike, T, c.right)
    if iv is None:
        return None, "反推失败"
    if bsm.vega(S, c.strike, T, iv) < SKEW_MIN_VEGA:
        return None, "vega 过小"
    return iv, "local"


def select_iv(c, chain, S, T, now):
    """说明 7.4：①券商 IV 且新鲜；②本地反推；报价比券商 IV 更新时优先本地。返回 (iv, 来源)。"""
    g_at = chain.greeks_as_of
    broker_ok = (c.iv is not None and c.iv > 0 and g_at is not None
                 and (now - g_at).total_seconds() <= GREEKS_STALE_S)
    quote_newer = c.quoted_at is not None and g_at is not None and (c.quoted_at - g_at).total_seconds() > 1.0
    if broker_ok and not quote_newer:
        return c.iv, "broker"
    iv, why = local_iv(c, S, T)
    if iv is not None:
        return iv, "local"
    if broker_ok:
        return c.iv, "broker"
    return None, why


# ============================================================================ Feed
def feed_eligible(c, spot):
    """卖价 $0.05–$4.00 的平值和虚值合约。"""
    if c is None or c.ask is None or not (FEED_MIN_ASK <= c.ask <= FEED_MAX_ASK):
        return False
    half = 2.5   # 平值容差：半个 5 点行权价间距
    return c.strike >= spot - half if c.right == "C" else c.strike <= spot + half


def feed_reject(c):
    """拒绝条件：价格不可交易（买价为 0 或倒挂）、关键字段缺失。（报价时间差规则只适用于定时推送的券商，这里不适用。）"""
    if c is None:
        return "合约不在期权链里"
    if c.bid is None or c.ask is None:
        return "关键字段缺失"
    if c.bid <= 0 or c.ask < c.bid:
        return "价格不可交易"
    return None


def run_feed(store, now, et_fmt):
    """处理最新一个 1 分钟桶和新大单，产生 / 合并信号写进 store.signals。返回本轮新信号数。"""
    chain, spot = store.chain, store.spot
    if chain is None or spot is None:
        return 0
    quotes = {(c.strike, c.right): c for c in chain.contracts}
    made = 0
    starts = list(store.buckets.keys())
    if starts:
        cur = store.buckets[starts[-1]]
        if not getattr(cur, "_feed_done", False):
            cur._feed_done = True
            base_bs = [store.buckets[s] for s in starts[-1 - FEED_BASELINE_MIN:-1]]
            for row in cur.rows:
                c = quotes.get((row.strike, row.right))
                if not feed_eligible(c, spot) or feed_reject(c):
                    continue
                base_v = [sum(r.contracts for r in b.rows if r.strike == row.strike and r.right == row.right) for b in base_bs]
                base_p = [sum(r.gross_premium for r in b.rows if r.strike == row.strike and r.right == row.right) for b in base_bs]
                buy, sell, unk, _, _ = split_bucket_row(row, cur.known_ratio(row.right))
                share = buy / row.contracts if row.contracts else 0.0
                conf = round(cur.known_ratio(row.right), 2)
                if len(base_bs) < FEED_BASELINE_NEED or row.contracts < FEED_MIN_VOL:
                    continue
                bv = max(sum(base_v) / len(base_v), FEED_MIN_BASE_VOL)
                bp = max(sum(base_p) / len(base_p), FEED_MIN_BASE_VOL * 100 * max(c.mid or 0.05, 0.05))
                xv, xp = row.contracts / bv, row.gross_premium / bp
                hits = []
                if xv >= FEED_SPIKE_X:
                    hits.append(("volume_spike", xv))
                if xp >= FEED_SPIKE_X:
                    hits.append(("premium_spike", xp))
                if share >= FEED_ASK_SHARE and xv >= FEED_ASK_SPIKE_X:
                    hits.append(("ask_buy_spike", xv))
                for typ, x in hits:
                    made += _emit(store, now, et_fmt, typ, row.strike, row.right, row.contracts, row.gross_premium,
                                  row.gross_premium / row.contracts / 100 if row.contracts else None, c, share, x, conf)
    for bp in [p for p in store.big_prints if not getattr(p, "_feed_done", False)]:
        bp._feed_done = True
        d, conf = classify_print(bp)
        if d != BUY or not (bp.contracts >= FEED_LARGE_CONTRACTS or bp.premium >= FEED_LARGE_PREMIUM):
            continue
        c = quotes.get((bp.strike, bp.right))
        if not feed_eligible(c, spot) or feed_reject(c):
            continue
        made += _emit(store, bp.at, et_fmt, "large_ask_trade", bp.strike, bp.right, bp.contracts, bp.premium,
                      bp.price, c, 1.0, None, conf, bid=bp.bid, ask=bp.ask)
    return made


def _emit(store, t, et_fmt, typ, strike, right, n, prem, px, c, share, mult, conf, bid=None, ask=None):
    """同合约同类型 FEED_MERGE_S 秒内合并进已有那条。返回 1 = 新信号，0 = 合并。"""
    for s in reversed(store.signals):
        if (t - s["t"]).total_seconds() > FEED_MERGE_S:
            break
        if s["type"] == typ and s["strike"] == strike and s["right"] == right:
            s["contracts"] += n
            s["premium"] += prem
            s["count"] += 1
            s["last_t"] = t
            s["time"] = et_fmt(t)
            if mult is not None:
                s["multiple"] = round(max(s["multiple"] or 0, mult), 1)
            return 0
    store.signals.append({
        "t": t, "last_t": t, "time": et_fmt(t), "type": typ, "strike": strike, "right": right,
        "contracts": n, "premium": prem, "price": None if px is None else round(px, 2),
        "bid": c.bid if bid is None else bid, "ask": c.ask if ask is None else ask,
        "ask_share": round(share, 2), "multiple": None if mult is None else round(mult, 1),
        "confidence": conf, "count": 1})
    return 1


# ============================================================================ Strike Leader
def run_leader(store, now):
    """按 行权价 + C/P 汇总 1/5/15 分钟和当日。返回 {窗口: [行...]}，前端切换排序。"""
    chain, spot = store.chain, store.spot
    quotes = {(c.strike, c.right): c for c in chain.contracts} if chain else {}
    out = {}
    last_start = next(reversed(store.buckets)) if store.buckets else None
    for name, mins in LEADER_WINDOWS.items():
        agg = {}
        if mins is None:
            for key, d in store.day.items():
                agg[key] = [d.contracts, d.premium, d.buy, d.sell, d.unknown]
            t0 = None
        else:
            t0 = (last_start - dt.timedelta(minutes=mins - 1)) if last_start else now
            for b in store.buckets_since(t0):
                for r in b.rows:
                    buy, sell, unk, _, _ = split_bucket_row(r, b.known_ratio(r.right))
                    a = agg.setdefault((r.strike, r.right), [0.0, 0.0, 0.0, 0.0, 0.0])
                    a[0] += r.contracts
                    a[1] += r.gross_premium
                    a[2] += buy
                    a[3] += sell
                    a[4] += unk
        rows = []
        for (k, rt), (v, p, b, s, u) in agg.items():
            sigs = [x for x in store.signals if x["strike"] == k and x["right"] == rt and (t0 is None or x["t"] >= t0)]
            active = any((now - x["last_t"]).total_seconds() <= LEADER_ACTIVE_S for x in sigs)
            c = quotes.get((k, rt))
            rows.append({"strike": k, "right": rt, "volume": round(v), "premium": round(p), "buy": round(b),
                         "sell": round(s), "unknown": round(u), "net": round(b - s), "signals": len(sigs),
                         "active": active, "mid": None if c is None or c.mid is None else round(c.mid, 2),
                         "dist": None if spot is None else round(k - spot, 1)})
        rows.sort(key=lambda x: -x["premium"])
        out[name] = rows[:40]
    return out


# ============================================================================ GEX
def run_gex(chain, spot):
    """Call GEX = +γ·OI·100·S²·0.01；Put GEX = −γ·OI·100·S²·0.01（做市商持 Call 多头、Put 空头，是假设）。"""
    win = strike_window(chain.contracts, spot, GEX_STRIKES_EACH_SIDE)
    if not win:
        return {"available": False, "reason": "没有合约", "coverage": 0.0}
    valid = [c for c in win if c.gamma is not None and c.gamma >= 0 and c.oi is not None]
    coverage = len(valid) / len(win)
    f = 100 * spot * spot * 0.01
    ladder = {}
    for c in valid:
        e = ladder.setdefault(c.strike, {"strike": c.strike, "call": 0.0, "put": 0.0})
        if c.right == "C":
            e["call"] += c.gamma * c.oi * f
        else:
            e["put"] -= c.gamma * c.oi * f
    rows = sorted(ladder.values(), key=lambda e: -e["strike"])
    for e in rows:
        e["net"] = e["call"] + e["put"]
    total = sum(e["net"] for e in rows)
    res = {"coverage": round(coverage, 3), "ladder": rows, "total": total, "spot": spot}
    if coverage < GEX_MIN_COVERAGE or not rows:
        res.update(available=False, reason=f"覆盖率 {coverage:.0%} < {GEX_MIN_COVERAGE:.0%}")
        return res
    call_wall = max(rows, key=lambda e: e["call"])
    put_wall = min(rows, key=lambda e: e["put"])
    top3 = sorted(rows, key=lambda e: -abs(e["net"]))[:3]
    res.update(available=True, regime="正 Gamma" if total > 0 else "负 Gamma" if total < 0 else "零 Gamma",
               call_wall=call_wall["strike"] if call_wall["call"] > 0 else None,
               put_wall=put_wall["strike"] if put_wall["put"] < 0 else None,
               top3=[{"strike": e["strike"], "net": e["net"]} for e in top3])
    return res


# ============================================================================ Skew
def run_skew(chain, spot, T, now):
    win = strike_window(chain.contracts, spot, SKEW_STRIKES_EACH_SIDE)
    if not win or T <= 0:
        return {"available": False, "reason": "没有合约或已到期", "coverage": 0.0}
    ivs, srcs = {}, {"broker": 0, "local": 0}
    for c in win:
        iv, src = select_iv(c, chain, spot, T, now)
        if iv is not None:
            ivs[(c.strike, c.right)] = iv
            srcs[src] += 1
    coverage = len(ivs) / len(win)
    res = {"coverage": round(coverage, 3), "iv_sources": srcs, "atm_iv": None, "risk_reversal_25d": None}
    atm_k = min({c.strike for c in win}, key=lambda k: abs(k - spot))
    a = [ivs[(atm_k, r)] for r in ("C", "P") if (atm_k, r) in ivs]
    if a:
        res["atm_iv"] = round(sum(a) / len(a), 4)
    best = {"C": None, "P": None}
    for (k, r), iv in ivs.items():
        if (r == "C" and k <= spot) or (r == "P" and k >= spot):
            continue   # 只取虚值
        d = abs(bsm.delta(spot, k, T, iv, r))
        if best[r] is None or abs(d - 0.25) < abs(best[r][0] - 0.25):
            best[r] = (d, k, iv)
    for r in ("C", "P"):
        if best[r] and abs(best[r][0] - 0.25) > SKEW_RR_DELTA_TOL:
            best[r] = None
    if best["C"] and best["P"]:
        res["risk_reversal_25d"] = round(best["C"][2] - best["P"][2], 4)
        res["rr_legs"] = {"call": {"strike": best["C"][1], "delta": round(best["C"][0], 3)},
                          "put": {"strike": best["P"][1], "delta": round(-best["P"][0], 3)}}
    res["available"] = res["atm_iv"] is not None and res["risk_reversal_25d"] is not None
    if coverage < SKEW_MIN_COVERAGE:
        res["flag"] = "覆盖不足"
    if not res["available"]:
        res["reason"] = "ATM IV 或 RR25 算不出"
    return res


# ============================================================================ Charm
def run_charm(chain, spot, now, close, now_et):
    from config import hhmm_to_minutes, minutes_of_day
    if minutes_of_day(now_et) < hhmm_to_minutes(CHARM_START):
        return {"available": False, "reason": "15:30 之前不输出"}
    secs = (close - now).total_seconds()
    if secs < CHARM_MIN_T_S:
        return {"available": False, "reason": "距到期不足 10 秒"}
    T = secs / bsm.YEAR_S
    dt_years = 60.0 / bsm.YEAR_S
    mm_delta_1m = 0.0
    used = 0
    for c in chain.contracts:
        if c.oi is None or c.oi <= 0:
            continue
        iv, _ = select_iv(c, chain, spot, T, now)
        if iv is None:
            continue
        ch = bsm.charm(spot, c.strike, T, iv, c.right)
        mm_delta_1m += ch * dt_years * c.oi * 100 * MM_SIGN
        used += 1
    if used == 0:
        return {"available": False, "reason": "没有同时有 IV 和持仓量的合约"}
    hedge = -mm_delta_1m
    near = strike_window(chain.contracts, spot, CHARM_STAB_STRIKES)
    ok = [c for c in near if c.oi is not None and select_iv(c, chain, spot, T, now)[0] is not None]
    cov = len(ok) / len(near) if near else 0.0
    stability = "LOW_STABILITY" if cov < CHARM_STAB_COVERAGE or secs < CHARM_LOW_STAB_LAST_S else "NORMAL"
    return {"available": True, "direction": "BUY" if hedge > 0 else "SELL",
            "hedge_flow_delta_1m": round(hedge, 1), "hedge_flow_usd_1m": round(hedge * spot),
            "stability": stability, "stability_coverage": round(cov, 3),
            "contracts_used": used}
