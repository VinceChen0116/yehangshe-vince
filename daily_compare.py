"""
daily_compare.py —— 每日收盘后：旧规则（baseline）vs 新规则（实盘现行）回测对比，发 Discord
==========================================================================================
  * 旧规则 = 2026-10-13 以前的实盘规则：现价距正节点 ≤ 2.5%
  * 新规则 = 当前 config 默认值（2026-10-14 起实盘）：现价距正节点 ≤ 1%（其余相同）
两组都在全部已存档交易日上跑（Heatmap 存档 + 期权价格缓存，已跑过的日期基本 0 配额），
输出到 backtests/exp/daily_old、daily_new，并把"今天 + 累计 + 逐日"对比发到 Discord；
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

OLD = ["--set", "node_distance_max_pct=0.025"]
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
    ap = argparse.ArgumentParser(description="旧规则 vs 新规则 每日对比")
    ap.add_argument("--no-post", action="store_true")
    args = ap.parse_args(argv)
    old, new = run("daily_old", OLD), run("daily_new", [])
    days = new["dates"]
    today = days[-1]
    o, n = old["overall"], new["overall"]
    bo, bn = old["breakdown"]["by_day"], new["breakdown"]["by_day"]
    rows = [("笔数", "trades", str), ("合计", "total_pnl", money), ("去最大一笔", "pnl_ex_best", money),
            ("胜率", "win_rate_pct", lambda v: f"{v}%"), ("盈亏比", "profit_factor", str)]
    table = [f"{'':<8}{'旧规则':>10}{'新规则':>10}"]
    table += [f"{label:<8}{('—' if o.get(k) is None else f(o.get(k))):>10}{('—' if n.get(k) is None else f(n.get(k))):>10}"
              for label, k, f in rows]
    loss_old = sum(1 for d in days if (bo.get(d, {}).get("total_pnl") or 0) < 0)
    loss_new = sum(1 for d in days if (bn.get(d, {}).get("total_pnl") or 0) < 0)
    table.append(f"{'亏损天数':<8}{f'{loss_old}/{len(days)}':>10}{f'{loss_new}/{len(days)}':>10}")
    per_day = "\n".join(f"{d[5:]}  旧 {money(bo.get(d, {}).get('total_pnl')):>7}  新 {money(bn.get(d, {}).get('total_pnl')):>7}"
                        for d in days)
    live = live_result(today)
    live_line = f"实盘（B 账户）{live[0]} 笔 {money(live[1])}" if live else "实盘交易明细未找到"
    summary = {"run_at": et_now().strftime("%Y-%m-%d %H:%M"), "days": days, "old": o, "new": n,
               "by_day": {d: {"old": bo.get(d, {}).get("total_pnl"), "new": bn.get(d, {}).get("total_pnl")} for d in days},
               "live_today": live}
    os.makedirs(os.path.join("backtests", "daily_compare"), exist_ok=True)
    with open(os.path.join("backtests", "daily_compare", f"{today}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n".join(table)); print(per_day); print(live_line)
    notifier = None if args.no_post else from_config(SystemConfig.load(None))
    if notifier:
        notifier.send(embeds=[{
            "title": f"📈 {today} 收盘：旧规则（离节点≤2.5%）vs 新规则（离节点≤1%）",
            "color": 0x2ECC71 if (bn.get(today, {}).get("total_pnl") or 0) >= (bo.get(today, {}).get("total_pnl") or 0) else 0xE67E22,
            "description": "```\n" + "\n".join(table) + "\n```",
            "fields": [
                {"name": f"今天（{today}）", "value": f"回测：旧 {money(bo.get(today, {}).get('total_pnl'))} / "
                                                   f"新 {money(bn.get(today, {}).get('total_pnl'))}\n{live_line}", "inline": False},
                {"name": f"逐日（累计 {len(days)} 天）", "value": "```\n" + per_day[-1000:] + "\n```", "inline": False},
            ],
            "footer": {"text": "历史回放 · 真实期权价格 · 每笔 1 张；实盘从 10/14 起用新规则"},
        }])
    return 0


if __name__ == "__main__":
    sys.exit(main())
