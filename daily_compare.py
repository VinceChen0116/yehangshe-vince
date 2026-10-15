"""
daily_compare.py —— 每日收盘后：所有候选规则在全部存档日上的回测对比，发 Discord
================================================================================
当前默认配置 = 实盘（2026-10-15 起"组合 A"）：现价距正节点 ≤1%、每天最多 25 笔、12:00 前最多 15 笔。
  * 旧规则     = 2026-10-13 以前：距节点 ≤2.5%、每天 15 笔
  * 10/14规则  = 距节点 ≤1%、每天 15 笔（10/14 一天的实盘规则）
  * 实盘A      = 当前默认
  * A+Flow加分 = 实盘A + Flow 只作排序加分（近 15 分钟 ≥$5 万 Ask 侧同向大单、当日量 ≥ OI → +0.2 分）
  * 组合C      = 每天 20 笔、12:00 前最多 10 笔
  * 午前≤10    = 每天 15 笔、12:00 前最多 10 笔
  * 上限25     = 每天 25 笔、上午不设配额
  * 局部节点   = 实盘A，但用现货附近的局部正/负 GEX 节点识别跳板/悬崖（2026-10-15 加入，见 config.node_mode）
所有组都是"真实回测"（2026-10-14 起的硬规则：回测必须尽量复刻每日模拟交易）：
  当天实盘监控的标的（logs/live_<日期>.log，缺失时退回 212 只）、Heatmap 晚 5 分钟可用、每 5 分钟盯市、
  开平仓按历史 NBBO 真实买卖价差（当时 bid 为 0 不开仓；查不到时单边 2.7%）；实盘A 与当天实盘逐笔对账。
各组都在全部已存档交易日上跑（存档 + 期权价格 / Flow 缓存，已跑过的日期基本 0 配额），输出到 backtests/exp/daily_<组>，
并把"累计 + 今天 + 逐日"对比发到 Discord；如有当天实盘交易明细（live/trades_<日期>.csv，B 账户），一并列出。
一个月后（约 11 月中）用这些累计结果做最终决定。

用法：python3 daily_compare.py [--no-post]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys

from config import SystemConfig, et_now
from notify import from_config

GROUPS = [  # (实验名, 显示名, 相对当前默认配置（= 实盘，2026-10-15 起组合 A）的改动)
    ("daily_old", "旧规则", ["--set", "node_distance_max_pct=0.025", "--set", "max_trades_per_day=15",
                           "--set", "max_trades_before=[]"]),
    ("daily_new", "10/14规则", ["--set", "max_trades_per_day=15", "--set", "max_trades_before=[]"]),
    ("daily_live", "实盘A", []),
    ("daily_flowbonus", "A+Flow加分", ["--set", 'flow_mode="bonus"']),
    ("daily_comboC", "组合C", ["--set", "max_trades_per_day=20", "--set", 'max_trades_before=["12:00",10]']),
    ("daily_morning10", "午前≤10", ["--set", "max_trades_per_day=15", "--set", 'max_trades_before=["12:00",10]']),
    ("daily_cap25", "上限25", ["--set", 'max_trades_before=[]']),
    ("daily_local", "局部节点", ["--set", 'node_mode="local"']),
]
EXP = os.path.join("backtests", "exp")


def run(name: str, extra: list) -> dict:
    cmd = [sys.executable, "experiment.py", "--name", name, "--universe", "live", "--no-post", "--force"] + extra
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(os.path.join(EXP, name, "summary.json"), encoding="utf-8") as f:
        return json.load(f)


def money(v) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def live_result(day: str):
    p = os.path.join("live", f"trades_{day}.csv")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return len(rows), sum(float(r["realized_pnl"]) for r in rows)


def reconcile(day: str) -> str:
    """当天实盘 vs 实盘A 真实回测：笔数、盈亏、按（标的, 方向）对上的笔数。"""
    lp, rp = os.path.join("live", f"trades_{day}.csv"), os.path.join(EXP, "daily_live", f"trades_{day}.csv")
    if not (os.path.exists(lp) and os.path.exists(rp)):
        return "（当天实盘或回测明细缺失，无法对账）"
    def load(p):
        with open(p, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    live, rep = load(lp), load(rp)
    key = lambda r: (r["ticker"], r["action"])
    lk, rk = {key(r) for r in live}, {key(r) for r in rep}
    pnl = lambda rows: sum(float(r["realized_pnl"]) for r in rows)
    return (f"实盘 {len(live)} 笔 {money(pnl(live))} ｜ 实盘A回测 {len(rep)} 笔 {money(pnl(rep))} ｜ "
            f"同标的同方向 {len(lk & rk)} 笔（只在实盘 {len(lk - rk)}，只在回测 {len(rk - lk)}）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="多组规则每日回测对比")
    ap.add_argument("--no-post", action="store_true")
    args = ap.parse_args(argv)
    res = {label: run(name, extra) for name, label, extra in GROUPS}
    labels = [label for _, label, _ in GROUPS]
    days = res["实盘A"]["dates"]
    today = days[-1]
    ov = {lb: res[lb]["overall"] for lb in labels}
    bd = {lb: res[lb]["breakdown"]["by_day"] for lb in labels}
    day_pnl = lambda lb, d: bd[lb].get(d, {}).get("total_pnl")
    table = [f"{'组别':<9}{'笔数':>5}{'合计':>9}{'去最大':>9}{'胜率':>7}{'盈亏比':>6}{'亏损天':>6}{'今天':>8}"]
    for lb in labels:
        o = ov[lb]
        losses = sum(1 for d in days if (day_pnl(lb, d) or 0) < 0)
        table.append(f"{lb:<9}{o.get('trades', 0):>5}{money(o.get('total_pnl')):>9}{money(o.get('pnl_ex_best')):>9}"
                     f"{str(o.get('win_rate_pct')) + '%':>7}{str(o.get('profit_factor')):>6}{f'{losses}/{len(days)}':>6}"
                     f"{money(day_pnl(lb, today)):>8}")
    head = f"{'':6}" + " ".join(f"{lb:>8}" for lb in labels)
    lines = [f"{d[5:]} " + " ".join(f"{money(day_pnl(lb, d)):>8}" for lb in labels) for d in days]
    while lines and len(head) + sum(len(x) + 1 for x in lines) > 990:   # Discord 字段上限 1024：只保留最近几天
        lines.pop(0)
    per_day = head + "\n" + "\n".join(lines)
    live = live_result(today)
    live_line = f"实盘（B 账户）{live[0]} 笔 {money(live[1])}" if live else "实盘交易明细未找到"
    recon = reconcile(today)
    summary = {"run_at": et_now().strftime("%Y-%m-%d %H:%M"), "days": days,
               "groups": {lb: {"name": name, "overrides": extra, "overall": ov[lb]} for name, lb, extra in GROUPS},
               "by_day": {d: {lb: day_pnl(lb, d) for lb in labels} for d in days}, "live_today": live}
    os.makedirs(os.path.join("backtests", "daily_compare"), exist_ok=True)
    with open(os.path.join("backtests", "daily_compare", f"{today}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n".join(table)); print(per_day); print(live_line); print(recon)
    notifier = None if args.no_post else from_config(SystemConfig.load(None))
    if notifier:
        notifier.send(embeds=[{
            "title": f"📈 {today} 收盘：全部候选规则对比（实盘A = 现行）",
            "color": 0x3498DB,
            "description": "```\n" + "\n".join(table) + "\n```",
            "fields": [
                {"name": f"今天（{today}）", "value": "回测：" + " / ".join(f"{lb} {money(day_pnl(lb, today))}" for lb in labels)
                                                   + f"\n{live_line}", "inline": False},
                {"name": "实盘 vs 实盘A回测（对账）", "value": recon, "inline": False},
                {"name": f"逐日（最近 {len(lines)} 天 / 累计 {len(days)} 天）", "value": "```\n" + per_day + "\n```", "inline": False},
            ],
            "footer": {"text": "真实回测（当天实盘标的 · 5 分钟盯市 · 晚 5 分钟用 Heatmap · 历史真实买卖价差）· 每笔 1 张；实盘 10/15 起用组合 A；约 11 月中做最终决定"},
        }])
    return 0


if __name__ == "__main__":
    sys.exit(main())
