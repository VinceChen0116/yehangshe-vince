"""
oos_report.py —— 回放 / 实盘结果按"样本内 / 样本外"分开统计（0 配额，只读交易明细）
==================================================================================
* 回放：用 --exclude 写明这次回放没包含哪些规则（如 bl_spr25_q 是 25% 价差，没有 spread_8）。
  包含的规则里最晚的"决定日"= 截止日；截止日及以前 = 样本内，之后 = 样本外。结论只看样本外。
* 实盘：每天用的是当时已生效的规则，天然是那套规则的样本外；按规则版本分段列出。
* 金额按 ROI × $650 / 笔归一（10/20 实盘每笔 1 张、之后按金额开仓，直接加美元不可比）。
用法：python3 oos_report.py --name bl_spr25_q --exclude spread_8
     python3 oos_report.py --name bl_spr8
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import os
import sys

from rules_registry import RULES, active_on

UNIT = 650.0


def load(pattern: str) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(pattern)):
        if f.endswith("_A.csv"):
            continue            # A 账户（严格异动）与 B 重复，只算 B
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                try:
                    roi = float(r["realized_roi_pct"])
                except (KeyError, ValueError):
                    continue
                rows.append(dict(day=dt.date.fromisoformat(r["entry_time"][:10]), roi=roi))
    return rows


def stats(rows: list[dict]) -> str:
    if not rows:
        return "| 0 | 0 | — | — | — | — | — |"
    days = len({r["day"] for r in rows})
    pnl = [r["roi"] / 100 * UNIT for r in rows]
    gain, loss = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    pf = f"{gain / loss:.2f}" if loss > 0 else "∞"
    win = 100 * sum(p > 0 for p in pnl) / len(pnl)
    return (f"| {days} | {len(rows)} | {sum(pnl):+,.0f} | {sum(pnl) / days:+,.0f} | {win:.0f}% | "
            f"{sum(r['roi'] for r in rows) / len(rows):+.1f}% | {pf} |")


HEAD = "| 段 | 天数 | 笔数 | 合计（$650/笔） | 日均 | 胜率 | 平均 ROI | 盈亏比 |\n|---|---|---|---|---|---|---|---|"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="backtests/exp/<name>")
    ap.add_argument("--exclude", default="", help="这次回放没有包含的规则 key，逗号分隔（见 rules_registry.py）")
    args = ap.parse_args(argv)
    excluded = {k for k in args.exclude.split(",") if k}
    unknown = excluded - {r.key for r in RULES}
    if unknown:
        print(f"未登记的规则：{unknown}")
        return 1
    included = [r for r in RULES if r.key not in excluded]
    cutoff = max(r.decided for r in included)
    core = next(r for r in RULES if r.key == "bl_core").decided

    rep = load(os.path.join("backtests", "exp", args.name, "trades_*.csv"))
    out = [f"# 样本内 / 样本外：{args.name}", "",
           f"回放包含的规则：{', '.join(r.key for r in included)}" + (f"；不含：{', '.join(sorted(excluded))}" if excluded else ""),
           f"截止日（包含规则里最晚的决定日）= {cutoff}；之后的交易日才是样本外。", "",
           "## 回放", "", HEAD,
           f"| 样本内：核心规则定规则用的天（≤ {core}） " + stats([r for r in rep if r["day"] <= core]),
           f"| 样本内：之后补充规则用过的天（{core} < 日期 ≤ {cutoff}） " + stats([r for r in rep if core < r["day"] <= cutoff]),
           f"| **样本外（> {cutoff}）** " + stats([r for r in rep if r["day"] > cutoff]), ""]
    if not [r for r in rep if r["day"] > cutoff]:
        out += ["> 这套规则还没有样本外的交易日，回放结果不能当证据。", ""]

    live = load(os.path.join("live", "trades_*.csv"))
    live = [r for r in live if r["day"] >= next(r for r in RULES if r.key == "bl_core").effective]
    out += ["## 实盘（每天用当时已生效的规则，天然是那套规则的样本外）", "", HEAD]
    eras = sorted({r.effective for r in RULES})
    for i, start in enumerate(eras):
        end = eras[i + 1] if i + 1 < len(eras) else None
        seg = [r for r in live if r["day"] >= start and (end is None or r["day"] < end)]
        keys = ", ".join(r.key for r in active_on(start))
        span = f"{start}" + (f" – {end - dt.timedelta(days=1)}" if end else " 起")
        out.append(f"| {span}（{keys}） " + stats(seg))
    out += ["", "## 每条规则的样本外天数（回放存档日）", "", "| 规则 | 决定日 | 样本外天数 |", "|---|---|---|"]
    days = sorted({r["day"] for r in rep})
    for r in RULES:
        out.append(f"| {r.key}：{r.name} | {r.decided} | {sum(d > r.decided for d in days)} |")
    text = "\n".join(out) + "\n"
    os.makedirs(os.path.join("backtests", "oos"), exist_ok=True)
    with open(os.path.join("backtests", "oos", f"{args.name}.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
