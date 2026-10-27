"""
live_exit_cf.py —— 实盘单反事实：如果加一条"死单提前平"，实盘结果会怎样
=====================================================================
* 开仓、其他出场全部用实盘真实结果（live/trades_<日期>.csv，10/23 只用 B 账户）。
* 只重建持仓期间每分钟的可卖价：中间价 = 该合约真实成交（5 分钟内）或 真实 IV + 真实标的价 BS（HistoryClient.option_price，
  与回放同源），bid ≈ 中间价 × (1 − 实盘开仓价差 / 2)。
* 某条规则在实盘平仓前触发 → 按触发那分钟的重建 bid 平；没触发 → 实盘真实结果不变。
* 先用实盘真实平仓价校准重建误差（重建 ROI vs 实盘 ROI @ 平仓分钟）。
用法：python3 live_exit_cf.py [--dates 2026-10-20,...]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

from config import SystemConfig
from main import HistoryClient
from yhs_client import YHSClient

DATES = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"]
# (名称, 持仓满 N 分钟, 期间最高 < X%, 当前 ≤ F%；F=None 不看当前)
VARIANTS = [
    ("A 10m <4% ≤-10%", 10, 4, -10),
    ("B 15m <6% ≤-10%", 15, 6, -10),
    ("C 15m <6% 不管当前", 15, 6, None),
    ("D 10m <4% 不管当前", 10, 4, None),
    ("E 20m <6% ≤-5%", 20, 6, -5),
]
GRID_N = [8, 10, 12, 15, 20]
GRID_X = [2, 4, 6]
GRID_F = [-5, -10, -15, None]


def load_trades(dates):
    out = []
    for ds in dates:
        with open(os.path.join("live", f"trades_{ds}.csv"), encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                out.append(dict(
                    date=ds, ticker=r["ticker"], sym=r["contract_symbol"], st=r.get("bl_structure", "")[:5],
                    entry=dt.datetime.fromisoformat(r["entry_time"]), exit=dt.datetime.fromisoformat(r["exit_time"]),
                    px=float(r["entry_option_price"]), spread=float(r["entry_spread_pct"]) / 100,
                    roi=float(r["realized_roi_pct"]), pnl=float(r["realized_pnl"]), peak=float(r["peak_roi_pct"]),
                    reason=r["exit_reason"]))
    return out


def build_paths(trades, cfg, real):
    """每笔：[(持仓分钟, 重建 ROI%), …]，从开仓后第 1 分钟到实盘平仓那一分钟。"""
    hcs = {}
    for t in trades:
        day = t["entry"].date()
        hc = hcs.setdefault(day, HistoryClient(real, cfg, day))
        path = []
        m = 1
        while True:
            now = t["entry"] + dt.timedelta(minutes=m)
            if now > t["exit"]:
                break
            mid = hc.option_price(t["sym"], now)
            if mid is not None:
                bid = mid * (1 - t["spread"] / 2)
                path.append((m, (bid / t["px"] - 1) * 100))
            m += 1
        t["path"] = path
        t["cost"] = abs(t["pnl"] / (t["roi"] / 100)) if abs(t["roi"]) > 0.3 else None


def apply(t, n, x, f):
    """返回 (是否提前平, ROI%, 盈亏$)。"""
    peak = -1e9
    for m, roi in t["path"]:
        peak = max(peak, roi)
        if m >= n and peak < x and (f is None or roi <= f):
            if t["cost"] is None:
                return False, t["roi"], t["pnl"]
            return True, roi, roi / 100 * t["cost"]
    return False, t["roi"], t["pnl"]


def summarize(trades, n, x, f):
    cut, tot, rois, changed = 0, 0.0, [], []
    for t in trades:
        hit, roi, pnl = apply(t, n, x, f)
        tot += pnl
        rois.append(roi)
        if hit:
            cut += 1
            changed.append((t, roi, pnl))
    wins = sum(r > 0 for r in rois)
    return dict(total=tot, cut=cut, win=wins, n=len(trades), avg_roi=sum(rois) / len(rois), changed=changed)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates")
    args = ap.parse_args(argv)
    dates = args.dates.split(",") if args.dates else DATES
    cfg = SystemConfig.load(None)
    real = YHSClient(cfg)
    trades = load_trades(dates)
    build_paths(trades, cfg, real)

    print("== 校准：重建 ROI（实盘平仓那一分钟）vs 实盘真实 ROI ==")
    errs = []
    for t in trades:
        if not t["path"]:
            print(f"  {t['date']} {t['ticker']:5s} 没有重建价格")
            continue
        last = t["path"][-1][1]
        rpeak = max(r for _, r in t["path"])
        errs.append(last - t["roi"])
        print(f"  {t['date'][5:]} {t['entry'].strftime('%H:%M')} {t['ticker']:5s} 实盘 {t['roi']:+6.1f}% 重建 {last:+6.1f}% "
              f"| 峰值 实盘 {t['peak']:+6.1f}% 重建 {rpeak:+6.1f}% | {len(t['path'])} 点")
    errs.sort()
    print(f"  误差中位数 {errs[len(errs) // 2]:+.1f} 个百分点，平均绝对误差 {sum(abs(e) for e in errs) / len(errs):.1f}")

    base = sum(t["pnl"] for t in trades)
    print(f"\n== 实盘基准：{len(trades)} 笔 {base:+.0f} ==")
    rows = []
    for name, n, x, f in VARIANTS:
        s = summarize(trades, n, x, f)
        rows.append(dict(name=name, **{k: v for k, v in s.items() if k != "changed"}))
        print(f"{name:20s} 提前平 {s['cut']:2d} 笔 | 合计 {s['total']:+7.0f}（{s['total'] - base:+6.0f}）| 胜 {s['win']}/{s['n']} | 平均 ROI {s['avg_roi']:+5.1f}%")
        for t, roi, pnl in s["changed"]:
            print(f"     {t['date'][5:]} {t['entry'].strftime('%H:%M')} {t['ticker']:5s} {t['st']} 实盘 {t['roi']:+6.1f}% {t['pnl']:+5.0f}"
                  f"（{t['reason']}）→ 提前平 {roi:+6.1f}% {pnl:+5.0f}")

    print("\n== 网格（看结果是否稳，不用来挑参数）：合计相对实盘的变化 ==")
    grid = {}
    for f in GRID_F:
        print(f"当前 ≤ {f if f is not None else '不管'}")
        for x in GRID_X:
            line = []
            for n in GRID_N:
                s = summarize(trades, n, x, f)
                grid[f"{n}|{x}|{f}"] = round(s["total"] - base, 1)
                line.append(f"{n}m:{s['total'] - base:+5.0f}({s['cut']})")
            print(f"   最高<{x}%  " + "  ".join(line))
    os.makedirs(os.path.join("backtests", "live_exit_cf"), exist_ok=True)
    with open(os.path.join("backtests", "live_exit_cf", "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(dates=dates, base=base, variants=rows, grid_delta=grid), fh, ensure_ascii=False, indent=2)
    print(f"\n配额剩余 {real.quota_remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
