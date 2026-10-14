"""
daily_compare.py —— 每日收盘后：多组规则在全部存档日上的回测对比，发 Discord
==========================================================================
  * 旧规则 = 2026-10-13 以前的实盘规则：现价距正节点 ≤ 2.5%
  * 新规则 = 当前 config 默认值（2026-10-14 起实盘）：现价距正节点 ≤ 1%（其余相同）
  * Flow加分 = 新规则 + Flow 只作排序加分（近 15 分钟有 ≥$5 万 Ask 侧同向大单、当日量 ≥ OI → 分数 +0.2），不拦信号
各组都在全部已存档交易日上跑（Heatmap 存档 + 期权价格 / Flow 缓存，已跑过的日期基本 0 配额），
输出到 backtests/exp/daily_<组>，并把"今天 + 累计 + 逐日"对比发到 Discord；
如有当天实盘交易明细（live/trades_<日期>.csv，B 账户），一并列出。

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

GROUPS = [  # (实验名, 显示名, 相对当前默认配置的改动)
    ("daily_old", "旧规则", ["--set", "node_distance_max_pct=0.025"]),
    ("daily_new", "新规则", []),
    ("daily_flowbonus", "Flow加分", ["--set", 'flow_mode="bonus"']),
]
EXP = os.path.join("backtests", "exp")


def run(name: str, extra: list) -> dict:
    cmd = [sys.executable, "experiment.py", "--name", name, "--universe", "heatmap", "--no-post", "--force"] + extra
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="多组规则每日回测对比")
    ap.add_argument("--no-post", action="store_true")
    args = ap.parse_args(argv)
    res = {label: run(name, extra) for name, label, extra in GROUPS}
    labels = [label for _, label, _ in GROUPS]
    days = res["新规则"]["dates"]
    today = days[-1]
    ov = {lb: res[lb]["overall"] for lb in labels}
    bd = {lb: res[lb]["breakdown"]["by_day"] for lb in labels}
    day_pnl = lambda lb, d: bd[lb].get(d, {}).get("total_pnl")
    rows = [("笔数", "trades", str), ("合计", "total_pnl", money), ("去最大一笔", "pnl_ex_best", money),
            ("胜率", "win_rate_pct", lambda v: f"{v}%"), ("盈亏比", "profit_factor", str)]
    table = [f"{'':<8}" + "".join(f"{lb:>10}" for lb in labels)]
    for label, k, f in rows:
        table.append(f"{label:<8}" + "".join(f"{('—' if ov[lb].get(k) is None else f(ov[lb].get(k))):>10}" for lb in labels))
    table.append(f"{'亏损天数':<8}" + "".join(
        f"{f'{sum(1 for d in days if (day_pnl(lb, d) or 0) < 0)}/{len(days)}':>10}" for lb in labels))
    per_day = "\n".join(f"{d[5:]} " + " ".join(f"{money(day_pnl(lb, d)):>8}" for lb in labels) for d in days)
    per_day = f"{'':6}" + " ".join(f"{lb:>8}" for lb in labels) + "\n" + per_day
    live = live_result(today)
    live_line = f"实盘（B 账户）{live[0]} 笔 {money(live[1])}" if live else "实盘交易明细未找到"
    summary = {"run_at": et_now().strftime("%Y-%m-%d %H:%M"), "days": days,
               "groups": {lb: {"name": name, "overrides": extra, "overall": ov[lb]} for name, lb, extra in GROUPS},
               "by_day": {d: {lb: day_pnl(lb, d) for lb in labels} for d in days}, "live_today": live}
    os.makedirs(os.path.join("backtests", "daily_compare"), exist_ok=True)
    with open(os.path.join("backtests", "daily_compare", f"{today}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n".join(table)); print(per_day); print(live_line)
    notifier = None if args.no_post else from_config(SystemConfig.load(None))
    if notifier:
        notifier.send(embeds=[{
            "title": f"📈 {today} 收盘：旧规则 / 新规则（离节点≤1%，实盘）/ Flow加分",
            "color": 0x3498DB,
            "description": "```\n" + "\n".join(table) + "\n```",
            "fields": [
                {"name": f"今天（{today}）", "value": "回测：" + " / ".join(f"{lb} {money(day_pnl(lb, today))}" for lb in labels)
                                                   + f"\n{live_line}", "inline": False},
                {"name": f"逐日（累计 {len(days)} 天）", "value": "```\n" + per_day[-1000:] + "\n```", "inline": False},
            ],
            "footer": {"text": "历史回放 · 真实期权价格 · 每笔 1 张；实盘从 10/14 起用新规则；Flow加分 观察中"},
        }])
    return 0


if __name__ == "__main__":
    sys.exit(main())
