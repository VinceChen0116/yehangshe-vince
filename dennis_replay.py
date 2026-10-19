"""
dennis_replay.py —— 用我们的真实价格重放 dennis-tape 的已平仓交易（交易记录 CSV 由用户提供，不入库）
用法：python3 dennis_replay.py --csv 交易记录.csv [--out backtests/dennis/replay.csv]
每笔算三种口径：
  记账      他记录的开仓价 / 平仓价
  同时点    他的开仓 / 平仓时刻，我们的真实价格：买 = 中间价 + 真实半价差，卖 = 中间价 − 真实半价差
            （中间价 = 真实 IV + 真实标的价 + BS，1 分钟；半价差取当时最近的真实 NBBO）
  同规则    他的开仓时刻按真实价买入，之后按 tape_follow_bt 的规则（−22% 止损 / +30% 起追踪 10%）逐分钟模拟
另外记录他的开仓价落在当时真实 bid–ask 的什么位置（0 = bid，0.5 = 中间，1 = ask）。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import statistics as st
import sys

from config import SystemConfig, et_datetime, setup_logging
from yhs_client import QuotaExhausted, YHSClient
import tape_follow_bt as tb

log = logging.getLogger("yhs.dennis_replay")


def occ(r) -> str:
    e = dt.date.fromisoformat(r["到期日"])
    return f'{r["标的"]}{e.strftime("%y%m%d")}{"C" if r["类型"] == "Call" else "P"}{int(round(float(r["行权价"]) * 1000)):08d}'


def at(s: str) -> dt.datetime:
    d = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    t = et_datetime(d.date(), d.strftime("%H:%M"))
    # 收盘后（如 16:03）或开盘前的时刻按当天 15:59 / 09:31 取价
    return min(max(t, et_datetime(d.date(), "09:31")), et_datetime(d.date(), "15:59"))


def mark_at(pr, core, t):
    """t 及以前最近的 1 分钟中间价（10 分钟内），没有就取 t 之后 5 分钟内的第一个。"""
    m = pr.marks(core, t.date())
    before = [x for x in m if x[0] <= t and (t - x[0]).total_seconds() <= 600]
    if before:
        return before[-1][1]
    after = [x for x in m if x[0] > t and (x[0] - t).total_seconds() <= 300]
    return after[0][1] if after else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", default=os.path.join("backtests", "dennis", "replay.csv"))
    ap.add_argument("--log-file")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    setup_logging(cfg.log_level, args.log_file)
    client = YHSClient(cfg)
    pr = tb.Pricer(client, cfg)
    out = []
    try:
        for r in csv.DictReader(open(args.csv, encoding="utf-8-sig")):
            if not r["平仓时间(ET)"].strip():
                continue
            core, qty = occ(r), int(r["张数"])
            ti, to = at(r["开仓时间(ET)"]), at(r["平仓时间(ET)"])
            pin, pout = float(r["开仓价"]), float(r["平仓价"])
            row = {"ticker": r["标的"], "contract": core, "qty": qty, "entry_time": r["开仓时间(ET)"],
                   "exit_time": r["平仓时间(ET)"], "reason": r["离场原因"], "dom_wall": r["主导墙"], "path": r["路径"],
                   "his_entry": pin, "his_exit": pout, "his_pnl": round((pout - pin) * 100 * qty, 1),
                   "his_roi": round((pout / pin - 1) * 100, 1)}
            mi, mo = mark_at(pr, core, ti), mark_at(pr, core, to)
            q = pr.h(ti.date()).hist_quote("O:" + core, ti)
            if q and q[1] > q[0] > 0:
                row["entry_pos_in_spread"] = round((pin - q[0]) / (q[1] - q[0]), 2)
                row["bid_in"], row["ask_in"] = q
            if mi and mo:
                hi = pr.hs(core, ti.date(), ti)
                ho = pr.hs(core, to.date(), to)
                buy = mi * (1 + (hi if hi is not None else cfg.slippage_pct))
                sell = 0.0 if ho is None else mo * (1 - ho)
                row.update({"mid_in": round(mi, 3), "mid_out": round(mo, 3), "real_in": round(buy, 3), "real_out": round(sell, 3),
                            "same_time_pnl": round((sell - buy) * 100 * qty, 1), "same_time_roi": round((sell / buy - 1) * 100, 1),
                            "mid_roi": round((mo / mi - 1) * 100, 1)})
            s = tb.simulate(pr, cfg, core, ti.date(), ti)
            if s:
                k = s["qty"]
                row.update({"rule_exit_time": s["exit_time"], "rule_reason": s["reason"], "rule_roi": s["roi_pct"],
                            "rule_pnl": round(s["pnl_usd"] / k * qty, 1) if k else 0})
            out.append(row)
            log.info("%s %s 记账 %+.1f%% | 同时点真实 %s | 同规则 %s", r["开仓时间(ET)"][:16], core, row["his_roi"],
                     f'{row["same_time_roi"]:+.1f}%' if "same_time_roi" in row else "—",
                     f'{row["rule_roi"]:+.1f}%（{row["rule_reason"]}）' if "rule_roi" in row else "—")
    except QuotaExhausted as e:
        log.critical("配额熔断：%s", e)
        return 3
    keys = []
    for r in out:
        keys += [k for k in r if k not in keys]
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(out)
    def tot(k):
        v = [r[k] for r in out if k in r]
        return len(v), round(sum(v)), round(100 * sum(x > 0 for x in v) / len(v)) if v else 0
    print("记账", tot("his_pnl"), "同时点真实", tot("same_time_pnl"), "同规则", tot("rule_pnl"))
    pos = [r["entry_pos_in_spread"] for r in out if "entry_pos_in_spread" in r]
    if pos:
        print("他的开仓价在真实 bid–ask 中的位置：中位 %.2f（0=bid 0.5=中间 1=ask），n=%d" % (st.median(pos), len(pos)))
    log.info("配额剩余 %s", client.quota_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
