"""
full_compare.py —— 所有定义过的规则，一次性按"真实回测"口径重跑并做大对比
========================================================================
口径（与每日模拟交易一致，见 HANDOFF J 节）：当天实盘监控的标的、Heatmap 晚 5 分钟可用、每 5 分钟盯市、
开平仓价 = 真实 IV + 真实标的价还原的中间价 ∓ 当时真实 NBBO 半价差（bid 为 0 不开仓）。
这个取价方法用 10/14 实盘 15 笔的 30 个真实报价校验过，中位误差 3.0%，是现有数据里误差最小的
（直接用 5 分钟真实成交均价 ±价差 8.7%，直接用 30 分钟/1 小时的历史 NBBO 7.1%）。

每组 = 现行实盘规则（组合 A：离节点 ≤1%、每天 25 笔、12:00 前 15 笔）只改一处；"规则组"几组是完整的规则集。
用法：python3 full_compare.py [--jobs 4] [--post] [--only 名1,名2]
输出：backtests/exp/full_<名>/、backtests/full_compare/<最后一天>.json / .md
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys

from config import SystemConfig, et_now
from notify import from_config

S = lambda *kv: [x for k in kv for x in ("--set", k)]
Q15 = ("max_trades_per_day=15", "max_trades_before=[]")
GROUPS = [  # (类别, 实验名, 显示名, 改动)
    ("规则组", "old", "旧规则(≤2.5%,15笔)", S("node_distance_max_pct=0.025", *Q15)),
    ("规则组", "r1014", "10/14规则(≤1%,15笔)", S(*Q15)),
    ("规则组", "A", "组合A(现行)", []),
    ("规则组", "C", "组合C(20笔,午前10)", S("max_trades_per_day=20", 'max_trades_before=["12:00",10]')),
    ("规则组", "m10", "15笔+午前≤10", S("max_trades_per_day=15", 'max_trades_before=["12:00",10]')),
    ("规则组", "cap25", "25笔不限午前", S("max_trades_before=[]")),
    ("规则组", "m12", "25笔+午前≤12", S('max_trades_before=["12:00",12]')),
    ("规则组", "nocap", "不限笔数", S("max_trades_per_day=0", "max_trades_before=[]")),
    ("节点", "local", "局部节点", S('node_mode="local"')),
    ("节点", "local_s0", "局部节点+评分不设门槛", S('node_mode="local"', "min_signal_score=0")),
    ("离节点", "dist05", "离节点≤0.5%", S("node_distance_max_pct=0.005")),
    ("离节点", "dist25", "离节点≤2.5%", S("node_distance_max_pct=0.025")),
    ("行权价", "atm", "平值ATM", S('strike_mode="atm"')),
    ("行权价", "itm1", "实值1%", S('strike_mode="itm"', "itm_depth_pct=0.01")),
    ("行权价", "itm2", "实值2%", S('strike_mode="itm"', "itm_depth_pct=0.02")),
    ("到期", "dte1", "到期≤1个交易日", S("expiry_max_trading_dte=1")),
    ("评分", "s0", "评分不设门槛", S("min_signal_score=0")),
    ("评分", "s08", "评分≥0.8", S("min_signal_score=0.8")),
    ("评分", "noprox", "评分去掉接近度", S('score_weights=[["fuel",0.3],["floor",0.2],["reach",0.2],["fresh",0.1]]')),
    ("评分", "firstcome", "先到先开(不排序)", S('signal_ranking="first_come"')),
    ("时段", "nomid", "午间11:30-14:30不开", S('no_entry_windows=[["11:30","14:30"]]')),
    ("时段", "start10", "10:00后才开", S('no_new_entries_before="10:00"')),
    ("持仓", "dir5", "同方向最多5笔", S("max_same_direction=5")),
    ("持仓", "rep05", "弱仓替换(差0.05)", S("rank_replace_margin=0.05")),
    ("持仓", "rep10", "弱仓替换(差0.10)", S("rank_replace_margin=0.1")),
    ("止损", "opt35", "期权-35%止损", S('stop_mode="option"', "stop_loss_pct=-35")),
    ("止损", "opt50", "期权-50%止损", S('stop_mode="option"', "stop_loss_pct=-50")),
    ("止损", "und02", "破位缓冲0.2%", S("underlying_stop_buffer_pct=0.002")),
    ("止盈", "ut02", "标的追踪0.2%", S('trail_mode="underlying"', "underlying_trail_pct=0.002")),
    ("止盈", "ut03", "标的追踪0.3%", S('trail_mode="underlying"', "underlying_trail_pct=0.003")),
    ("止盈", "ut05", "标的追踪0.5%", S('trail_mode="underlying"', "underlying_trail_pct=0.005")),
    ("Flow", "fbonus", "Flow加分($5万)", S('flow_mode="bonus"')),
    ("Flow", "fbonus150", "Flow加分($15万)", S('flow_mode="bonus"', "flow_min_premium_usd=150000")),
    ("Flow", "fgate", "Flow确认才开($5万)", S("require_flow_confirm=true")),
    ("Flow", "fgate150", "Flow确认才开($15万)", S("require_flow_confirm=true", "flow_min_premium_usd=150000")),
    ("Flow", "flook", "Flow回看30分($5万)", S("require_flow_confirm=true", 'flow_mode="lookback"')),
    ("Flow", "flook150", "Flow回看30分($15万)", S("require_flow_confirm=true", 'flow_mode="lookback"', "flow_min_premium_usd=150000")),
    ("Flow", "fnovoi", "Flow确认(不要求量≥OI)", S("require_flow_confirm=true", "flow_require_vol_ge_oi=false")),
    ("Flow", "ftight", "Flow确认(≤3天,≤3%价外)", S("require_flow_confirm=true", "flow_max_dte=3", "flow_max_moneyness_pct=0.03")),
]
EXP = os.path.join("backtests", "exp")
OUT = os.path.join("backtests", "full_compare")


def money(v) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def run_one(g) -> tuple:
    _, name, label, extra = g
    exp = f"full_{name}"
    cmd = [sys.executable, "experiment.py", "--name", exp, "--no-post", "--force"] + extra
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        return label, None, r.stderr.decode("utf-8", "replace")[-500:]
    with open(os.path.join(EXP, exp, "summary.json"), encoding="utf-8") as f:
        return label, json.load(f), ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全部规则真实回测大对比")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--only", help="只跑这些实验名（逗号分隔），其余读已有结果")
    args = ap.parse_args(argv)
    only = set(args.only.split(",")) if args.only else None
    todo = [g for g in GROUPS if only is None or g[1] in only]
    res, errs = {}, {}
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        for label, summ, err in ex.map(run_one, todo):
            print(("✓ " if summ else "✗ ") + label, flush=True)
            if summ:
                res[label] = summ
            else:
                errs[label] = err
    for cat, name, label, _ in GROUPS:   # 没重跑的组读已有结果
        p = os.path.join(EXP, f"full_{name}", "summary.json")
        if label not in res and label not in errs and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                res[label] = json.load(f)
    days = res["组合A(现行)"]["dates"]
    rows = []
    for cat, name, label, extra in GROUPS:
        if label not in res:
            continue
        o, bd = res[label]["overall"], res[label]["breakdown"]["by_day"]
        per = [bd.get(d, {}).get("total_pnl") for d in days]
        rows.append({"cat": cat, "name": name, "label": label, "overrides": extra, "trades": o.get("trades"),
                     "total": o.get("total_pnl"), "ex_best": o.get("pnl_ex_best"), "win": o.get("win_rate_pct"),
                     "pf": o.get("profit_factor"), "loss_days": sum(1 for v in per if (v or 0) < 0),
                     "by_day": dict(zip(days, per))})
    base = next(r["total"] for r in rows if r["name"] == "A")
    md = [f"# 全部规则真实回测对比（{days[0]} – {days[-1]}，{len(days)} 天）", "",
          "口径：当天实盘监控标的 · Heatmap 晚 5 分钟可用 · 每 5 分钟盯市 · 真实 IV 还原价 ∓ 真实 NBBO 半价差 · bid=0 不开仓 · 每笔 1 张", "",
          "| 类别 | 规则 | 笔数 | 合计 | 比现行 | 去最大一笔 | 胜率 | 盈亏比 | 亏损天 | " + " | ".join(d[5:] for d in days) + " |",
          "|---|---|---|---|---|---|---|---|---|" + "---|" * len(days)]
    for r in rows:
        md.append(f"| {r['cat']} | {r['label']} | {r['trades']} | {money(r['total'])} | {money(r['total'] - base)} | "
                  f"{money(r['ex_best'])} | {r['win']}% | {r['pf']} | {r['loss_days']}/{len(days)} | "
                  + " | ".join(money(r['by_day'][d]) for d in days) + " |")
    if errs:
        md += ["", "跑失败的组：" + "、".join(errs)]
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{days[-1]}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    with open(os.path.join(OUT, f"{days[-1]}.json"), "w", encoding="utf-8") as f:
        json.dump({"run_at": et_now().strftime("%Y-%m-%d %H:%M"), "days": days, "rows": rows, "errors": errs},
                  f, ensure_ascii=False, indent=2)
    print("\n".join(md))
    for k, v in errs.items():
        print(f"[失败] {k}: {v}")
    notifier = from_config(SystemConfig.load(None)) if args.post else None
    if notifier:
        ranked = sorted(rows, key=lambda r: r["total"], reverse=True)
        lines = [f"{r['label'][:14]:<14}{r['trades']:>4}{money(r['total']):>9}{money(r['ex_best']):>9}{r['loss_days']:>2}/{len(days)}"
                 for r in ranked]
        chunks, cur = [], []
        for ln in lines:   # Discord 单字段 1024 字符
            if sum(len(x) + 1 for x in cur) + len(ln) > 950:
                chunks.append(cur); cur = []
            cur.append(ln)
        chunks.append(cur)
        notifier.send(embeds=[{
            "title": f"🧪 全部规则真实回测（{days[0]} – {days[-1]}，{len(days)} 天，共 {len(rows)} 组）",
            "color": 0x9B59B6,
            "description": "按合计排序：规则 / 笔数 / 合计 / 去最大一笔 / 亏损天。各组 = 现行组合 A 只改一处（规则组为完整规则集）。",
            "fields": [{"name": "排名" if i == 0 else "（续）", "value": "```\n" + "\n".join(c) + "\n```", "inline": False}
                       for i, c in enumerate(chunks)],
            "footer": {"text": "真实回测：当天实盘标的 · 晚 5 分钟用 Heatmap · 5 分钟盯市 · 真实价差 · bid=0 不开仓"},
        }])
    return 0


if __name__ == "__main__":
    sys.exit(main())
