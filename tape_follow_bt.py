"""
tape_follow_bt.py —— dennis-tape 式"跟大单"策略的真实数据回测
==================================================================
规则（反推自 dennis-tape，见 backtests/dennis/reverse_engineering.md）：
  触发：全市场 Ask 侧单笔 ≥$15 万大单，到期 2–75 天、价外 0–8%、当日量 > OI（tape_archive.is_candidate）
        + 过滤条件（--filter，见 FILTERS；带 Heatmap 的过滤只能用于有该到期 Heatmap 存档的交易日）
  开仓：大单后 5 分钟（等下一根 Heatmap），同一合约当天只跟第一笔符合条件的大单；15:50 以后不开
  价格：真实 IV + 真实标的价 + BS（1 分钟 greeks 序列），买入加 / 卖出减当时真实 NBBO 半价差
  止损：−22%；止盈：涨到 +30% 后启动追踪，回撤到 max(+30%, 峰值 × (1 − TRAIL)) 卖出
  持仓：可以过夜，最多 MAX_HOLD 个交易日，或到期前一天 15:50 时间离场
  仓位：--sizing dennis（默认）每笔约 $1,800、至少 1 张、单张超过 $3,600 不开（2026-10-19 用户定，与 tape_live 一致）；
        --sizing ours 每笔 $1,000（±25%，与隔夜单一致）
用法：python3 tape_follow_bt.py --dates 2026-10-02:2026-10-16 --filter F1 [--max-per-day 5]
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import logging
import os
import sys

from archive import heatmap_path, latest_closed_session, rows_to_frames
from config import SystemConfig, et_now, et_datetime, is_trading_day, next_trading_day, parse_iso, setup_logging, to_et
from main import HistoryClient
from overnight import half_spread
from overnight_live import size_contracts
from paper_trader import bs_price_greeks
from tape_archive import is_candidate, load_flow, parse_occ, tape_heatmap_path
from tape_filter_study import features, load_rows
from yhs_client import QuotaExhausted, YHSClient

log = logging.getLogger("yhs.tape_bt")
LAG_MIN = 5
LAST_ENTRY = "15:50"
STOP = -0.22
TRAIL_ON = 0.30
TRAIL = 0.10
MAX_HOLD = 8
SIZING = "dennis"


def qty_for(price: float) -> int:
    if SIZING == "ours":
        return size_contracts(price)
    per = price * 100
    if per <= 0 or per > 3600:
        return 0
    return max(1, int(round(1800 / per)))
LAST_DATA = latest_closed_session(et_now())


def _hm(f):
    return f["strike_rank"] <= 4 and f["strike_sign"] == "-" and f["aligned"] and f["otm"] >= 2


FILTERS = {   # f 是 tape_filter_study.features() 的结果（Heatmap 过滤）或只含 Flow 字段的 dict
    "F0": ("只用 Flow：≥$50万", lambda f: f["premium"] >= 500000, False),
    "F1": ("只用 Flow：≥$50万 + 到期≥14天 + 价外≥2%", lambda f: f["premium"] >= 500000 and (f["dte"] or 0) >= 14 and f["otm"] >= 2, False),
    "F2": ("只用 Flow：≥$30万 + 到期≥14天 + 价外≥2%", lambda f: f["premium"] >= 300000 and (f["dte"] or 0) >= 14 and f["otm"] >= 2, False),
    "H": ("前4大负节点+主导墙同向", lambda f: f["strike_rank"] <= 4 and f["strike_sign"] == "-" and f["aligned"], True),
    "W": ("主导墙 −上（所有到期合计）", lambda f: f.get("agg_wall") == "-上", True),
    "HW": ("前4大负节点+同向 + 主导墙 −上（合计）", lambda f: f["strike_rank"] <= 4 and f["strike_sign"] == "-" and f["aligned"]
           and f.get("agg_wall") == "-上", True),
    "A": ("前4大负节点+同向+到期≥14天+价外≥2%", lambda f: _hm(f) and (f["dte"] or 0) >= 14, True),
    "C": ("前4大负节点+同向+到期≥7天+价外≥2%", lambda f: _hm(f) and (f["dte"] or 0) >= 7, True),
    "D": ("前4大负节点+同向+价外≥2%", _hm, True),
}


def agg_wall(cfg, day, ticker, when, cache):
    """当天已存档的所有到期日（常规存档 + 跟单存档）在 when 时已可用的那根 Heatmap 按行权价合计 GEX，
    返回主导墙标签如 "-上"（与 dennis 的「主导墙」标注 25 笔对上 18 笔；单看交易到期只对上 9/16）。"""
    key = ("agg", ticker, when)
    if key in cache:
        return cache[key]
    exps = set()
    for base in (os.path.dirname(heatmap_path(cfg, day, ticker, "x")), os.path.dirname(tape_heatmap_path(cfg, day, ticker, "x"))):
        if os.path.isdir(base):
            exps |= {f[:-5] for f in os.listdir(base) if f.endswith(".json")}
    agg, spot = collections.defaultdict(float), None
    for e in exps:
        fk = ("frames", day, ticker, e)
        if fk not in cache:
            rows = load_rows(cfg, day, ticker, e)
            cache[fk] = rows_to_frames(ticker, day, e, rows) if rows else {}
        fr = cache[fk]
        ready = [k for k in fr if k + dt.timedelta(minutes=5) <= when]
        if not ready:
            continue
        d = fr[max(ready)]["data"]
        spot = spot or d.get("spot_usd")
        for c in d.get("cells") or []:
            if c.get("net_dealer_gex_usd") is not None:
                agg[float(c["strike_usd"])] += float(c["net_dealer_gex_usd"])
    lab = None
    if agg and spot:
        k, g = max(agg.items(), key=lambda kv: abs(kv[1]))
        lab = f'{"+" if g > 0 else "-"}{"上" if k > spot else "下"}'
    cache[key] = lab
    return lab


def flow_features(x):
    t, exp, right, K = parse_occ(x["contract_symbol"])
    return {"ticker": t, "exp": exp, "right": right, "strike": K, "dte": x.get("min_dte"),
            "otm": float(x.get("min_moneyness_pct") or 0) * 100, "premium": float(x.get("premium_usd") or 0),
            "contract": str(x["contract_symbol"]).split(":")[-1]}


class Pricer:
    """按交易日缓存 HistoryClient；mark(core, day) 给出该日 1 分钟 [(时间, BS 价)]。"""

    def __init__(self, client, cfg):
        self.client, self.cfg, self.hc = client, cfg, {}

    def h(self, day):
        if day not in self.hc:
            self.hc[day] = HistoryClient(self.client, self.cfg, day)
        return self.hc[day]

    def marks(self, core, day):
        _, exp, right, K = parse_occ(core)
        e = dt.date.fromisoformat(exp)
        out = []
        for t, S, iv, _d in self.h(day)._load_greeks(core):
            T = ((e - day).days + max(0, 16 * 60 - t.hour * 60 - t.minute) / (24 * 60)) / 365
            if T <= 0:
                continue
            px, _, _ = bs_price_greeks(S, K, T, self.cfg.risk_free_rate, iv, right == "C")
            if px and px > 0:
                out.append((t, px))
        return out

    def hs(self, core, day, t):
        return half_spread(self.h(day), self.cfg, "O:" + core, t)


def simulate(pr: Pricer, cfg, core, day, t_in):
    """开仓 + 持仓路径。返回 dict 或 None（没价格 / 买不起 / 无买盘）。"""
    m = [x for x in pr.marks(core, day) if x[0] >= t_in]
    if not m or (m[0][0] - t_in).total_seconds() > 5 * 60:
        return None
    t0, mid = m[0]
    if mid < cfg.min_option_price:
        return None
    h = pr.hs(core, day, t0)
    if h is None:
        return None
    cost = mid * (1 + h)
    qty = qty_for(cost)
    if not qty:
        return None
    _, exp, _, _ = parse_occ(core)
    last_day = dt.date.fromisoformat(exp) - dt.timedelta(days=1)
    peak, d, held = mid, day, 0
    reason = px = tx = None
    last = (t0, mid)
    while d <= LAST_DATA:
        cutoff = et_datetime(d, "15:50")
        for t, p in pr.marks(core, d):
            if t <= t0:
                continue
            last = (t, p)
            r = p / mid - 1   # 止损 / 追踪按中间价判断（与 dennis 的 −22.1% 一致），成交扣真实半价差
            peak = max(peak, p)
            if r <= STOP:
                reason = "hard_stop"
            elif peak / mid - 1 >= TRAIL_ON and p <= max(mid * (1 + TRAIL_ON), peak * (1 - TRAIL)):
                reason = "trailing_stop"
            elif (held + 1 >= MAX_HOLD or d >= last_day) and t >= cutoff:
                reason = "time_exit"
            if reason:
                break
        if reason:
            break
        held += 1
        d = next_trading_day(d)
    tx, px = last
    if not reason:
        reason = "持仓中(按最后价)"
    hx = pr.hs(core, tx.date(), tx)
    fill = 0.0 if hx is None else px * (1 - hx)
    return {"entry_time": t0.strftime("%Y-%m-%d %H:%M"), "entry": round(cost, 3), "mid_in": round(mid, 3), "qty": qty,
            "invested": round(cost * 100 * qty), "exit_time": tx.strftime("%Y-%m-%d %H:%M"), "exit": round(fill, 3),
            "reason": reason, "peak_pct": round((peak / mid - 1) * 100, 1),
            "pnl_usd": round((fill - cost) * 100 * qty, 1), "roi_pct": round((fill / cost - 1) * 100, 1)}


def day_range(spec):
    if ":" in spec:
        a, b = (dt.date.fromisoformat(x) for x in spec.split(":"))
        out = []
        while a <= b:
            if is_trading_day(a):
                out.append(a)
            a += dt.timedelta(days=1)
        return out
    return [dt.date.fromisoformat(x) for x in spec.split(",")]


def run(cfg, client, days, key, max_per_day):
    name, fn, need_hm = FILTERS[key]
    pr = Pricer(client, cfg)
    rows = []
    for day in days:
        flow = load_flow(client, cfg, day)
        seen, picked, cache = set(), 0, {}
        for x in sorted(flow, key=lambda r: r["occurred_at"]):
            if max_per_day and picked >= max_per_day:
                break
            if not is_candidate(x):
                continue
            core = str(x["contract_symbol"]).split(":")[-1]
            if core in seen:
                continue
            f = features(cfg, day, x, cache) if need_hm else flow_features(x)
            if f and need_hm:
                f["agg_wall"] = agg_wall(cfg, day, f["ticker"], to_et(parse_iso(x["occurred_at"])) + dt.timedelta(minutes=LAG_MIN), cache)
            if not f or not fn(f):
                continue
            seen.add(core)
            when = to_et(parse_iso(x["occurred_at"]))
            t_in = (when + dt.timedelta(minutes=LAG_MIN)).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
            if t_in > et_datetime(day, LAST_ENTRY):
                continue
            r = simulate(pr, cfg, core, day, t_in)
            if not r:
                log.info("%s %s 放弃：没价格 / 单张超过上限 / 无买盘", day, core)
                continue
            picked += 1
            r.update({"day": day.isoformat(), "flow_time": when.strftime("%H:%M"), "contract": core,
                      "flow_usd": round(f["premium"]), "dte": f["dte"], "otm_pct": round(f["otm"], 2)})
            rows.append(r)
            log.info("%s %s %s 大单 $%.0fK → %s 买 %.2f ×%d → %s %s 卖 %.2f（%+.1f%%）", day, r["flow_time"], core,
                     f["premium"] / 1e3, r["entry_time"][11:], r["entry"], r["qty"], r["exit_time"], r["reason"],
                     r["exit"], r["roi_pct"])
    return name, rows


def summarize(rows):
    n = len(rows)
    win = sum(1 for r in rows if r["pnl_usd"] > 0)
    gp = sum(r["pnl_usd"] for r in rows if r["pnl_usd"] > 0)
    gl = -sum(r["pnl_usd"] for r in rows if r["pnl_usd"] < 0)
    by = collections.Counter(r["reason"] for r in rows)
    per = collections.defaultdict(float)
    for r in rows:
        per[r["day"]] += r["pnl_usd"]
    return {"n": n, "win_pct": round(100 * win / n, 1) if n else 0, "pnl": round(sum(r["pnl_usd"] for r in rows)),
            "pf": round(gp / gl, 2) if gl else None, "invested": sum(r["invested"] for r in rows),
            "reasons": dict(by), "per_day": {k: round(v) for k, v in sorted(per.items())}}


def main(argv=None) -> int:
    global TRAIL, SIZING
    ap = argparse.ArgumentParser(description="跟大单策略真实数据回测")
    ap.add_argument("--dates", required=True, help="YYYY-MM-DD:YYYY-MM-DD 或逗号分隔")
    ap.add_argument("--filter", default="F1", choices=sorted(FILTERS))
    ap.add_argument("--max-per-day", type=int, default=5)
    ap.add_argument("--sizing", default=SIZING, choices=["dennis", "ours"])
    ap.add_argument("--trail", type=float, default=TRAIL, help="追踪止盈回撤比例（默认 0.10）")
    ap.add_argument("--out", default=os.path.join("backtests", "tape_follow"))
    ap.add_argument("--log-file")
    args = ap.parse_args(argv)
    TRAIL, SIZING = args.trail, args.sizing
    cfg = SystemConfig.load(None)
    setup_logging(cfg.log_level, args.log_file)
    client = YHSClient(cfg)
    try:
        name, rows = run(cfg, client, day_range(args.dates), args.filter, args.max_per_day)
    except QuotaExhausted as e:
        log.critical("配额熔断：%s", e)
        return 3
    s = summarize(rows)
    s.update({"filter": args.filter, "filter_name": name, "max_per_day": args.max_per_day, "trail": args.trail, "sizing": args.sizing, "dates": args.dates})
    os.makedirs(args.out, exist_ok=True)
    tag = f"{args.filter}_m{args.max_per_day}_t{int(round(args.trail * 100))}_{args.sizing}"
    if rows:
        with open(os.path.join(args.out, f"{tag}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    with open(os.path.join(args.out, f"{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    print(json.dumps(s, ensure_ascii=False))
    log.info("配额剩余 %s", client.quota_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
