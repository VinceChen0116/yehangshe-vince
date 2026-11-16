"""
oos_report.py —— 回放 / 实盘结果按"样本内 / 样本外"分开统计（0 配额，只读交易明细）
==================================================================================
* 回放：用 --exclude 写明这次回放没包含哪些规则（如 bl_spr25_q 是 25% 价差，没有 spread_8）。
  包含的规则里最晚的"决定日"= 截止日；截止日及以前 = 样本内，之后 = 样本外。结论只看样本外。
* 实盘：每天用的是当时已生效的规则，天然是那套规则的样本外；按规则版本分段列出。
* 金额按 ROI × $650 / 笔归一（10/20 实盘每笔 1 张、之后按金额开仓，直接加美元不可比）。
* 实盘日志里"买卖价差 … 不开仓"的次数按天列出（8% 价差上限的样本外证据）。
用法：python3 oos_report.py                                  # 只看实盘（每日收盘后，0 配额）
     python3 oos_report.py --name bl_spr25_q --exclude spread_8
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import os
import re
import sys
from zoneinfo import ZoneInfo

from rules_registry import RULES, active_on

UNIT = 650.0


def load(pattern: str) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(pattern)):
        if f.endswith(("_A.csv", "_L.csv")):
            continue            # A 账户（严格异动）与 B 重复；L 是并行的旧规则账户（单独统计）。只算 B
        with open(f, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                try:
                    roi = float(r["realized_roi_pct"])
                except (KeyError, ValueError):
                    continue
                rows.append(dict(day=dt.date.fromisoformat(r["entry_time"][:10]), roi=roi))
    return rows


def load_live() -> list[dict]:
    """实盘逐笔收益率。某天没有交易明细（进程异常退出没导出 CSV，如 10/29）就从 logs/live_<日期>.log 的平仓记录重建；
    A、B 账户同一信号同一秒平仓会各记一行，按（标的、方向、平仓分钟、ROI）去重。"""
    rows = load(os.path.join("live", "trades_*.csv"))
    have = {r["day"] for r in rows}
    for f in sorted(glob.glob(os.path.join("logs", "live_????-??-??.log"))):
        day = dt.date.fromisoformat(os.path.basename(f)[5:15])
        if day in have:
            continue
        seen = set()
        with open(f, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                m = re.search(r"^(\d\d:\d\d):\d\d .*阶段4·平仓】\S+ (\S+) (\S+) \|.*\| ROI ([+-]?[\d.]+)%", ln)
                if m and m.groups() not in seen:
                    seen.add(m.groups())
                    rows.append(dict(day=day, roi=float(m.group(4)), rebuilt=True))
    return rows


PARALLEL_START = dt.date(2026, 11, 16)   # 旧规则 L 账户与 baseline B 并行实盘起始日（用户 11/16 定）


def parallel_section() -> list[str]:
    """B（baseline）与 L（旧规则）同一标的池、同一份 Heatmap、同样按金额开仓，只比两边都有交易明细的天。"""
    days = sorted({os.path.basename(f)[7:17] for f in glob.glob(os.path.join("live", "trades_????-??-??_L.csv"))
                   if os.path.basename(f)[7:17] >= str(PARALLEL_START)})
    out = ["", f"## 并行对比：B（baseline） vs L（旧规则）（{PARALLEL_START} 起，同一标的池、每笔约 $650）", ""]
    if not days:
        return out + ["> 还没有并行交易日。"]
    rows = {"B": [], "L": []}
    for d in days:
        for k, f in (("B", f"trades_{d}.csv"), ("L", f"trades_{d}_L.csv")):
            p = os.path.join("live", f)
            if os.path.exists(p):
                with open(p, encoding="utf-8-sig") as fh:
                    for r in csv.DictReader(fh):
                        rows[k].append(dict(day=dt.date.fromisoformat(r["entry_time"][:10]), roi=float(r["realized_roi_pct"]),
                                            pnl=float(r["realized_pnl"])))
    out += [HEAD.replace("| 段 |", "| 账户 |", 1).replace("|---|", "|---|", 1)]
    for k, name in (("B", "B baseline"), ("L", "L 旧规则")):
        out.append(f"| {name}（实际 ${sum(r['pnl'] for r in rows[k]):+,.0f}） " + stats(rows[k]))
    out += ["", "| 日期 | B 笔数 | B 实际盈亏 | L 笔数 | L 实际盈亏 |", "|---|---|---|---|---|"]
    for d in days:
        b = [r for r in rows["B"] if str(r["day"]) == d]
        l = [r for r in rows["L"] if str(r["day"]) == d]
        out.append(f"| {d} | {len(b)} | {sum(r['pnl'] for r in b):+,.0f} | {len(l)} | {sum(r['pnl'] for r in l):+,.0f} |")
    return out


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
    ap.add_argument("--name", help="backtests/exp/<name>；不给就只统计实盘")
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

    out = []
    rep = load(os.path.join("backtests", "exp", args.name, "trades_*.csv")) if args.name else []
    if args.name:
        out += [f"# 样本内 / 样本外：{args.name}", "",
           f"回放包含的规则：{', '.join(r.key for r in included)}" + (f"；不含：{', '.join(sorted(excluded))}" if excluded else ""),
           f"截止日（包含规则里最晚的决定日）= {cutoff}；之后的交易日才是样本外。", "",
           "## 回放", "", HEAD,
           f"| 样本内：核心规则定规则用的天（≤ {core}） " + stats([r for r in rep if r["day"] <= core]),
           f"| 样本内：之后补充规则用过的天（{core} < 日期 ≤ {cutoff}） " + stats([r for r in rep if core < r["day"] <= cutoff]),
           f"| **样本外（> {cutoff}）** " + stats([r for r in rep if r["day"] > cutoff]), ""]
    else:
        out += ["# 样本内 / 样本外：实盘", ""]
    if args.name and not [r for r in rep if r["day"] > cutoff]:
        out += ["> 这套规则还没有样本外的交易日，回放结果不能当证据。", ""]

    live = load_live()
    live = [r for r in live if r["day"] >= next(r for r in RULES if r.key == "bl_core").effective]
    out += ["## 实盘（每天用当时已生效的规则，天然是那套规则的样本外）", ""]
    rebuilt = sorted({str(r["day"]) for r in live if r.get("rebuilt")})
    if rebuilt:
        out += [f"> {'、'.join(rebuilt)} 没有交易明细（进程异常退出），收益率由实盘日志的平仓记录重建。", ""]
    out += [HEAD]
    eras = sorted({r.effective for r in RULES})
    for i, start in enumerate(eras):
        end = eras[i + 1] if i + 1 < len(eras) else None
        seg = [r for r in live if r["day"] >= start and (end is None or r["day"] < end)]
        keys = ", ".join(r.key for r in active_on(start))
        span = f"{start}" + (f" – {end - dt.timedelta(days=1)}" if end else " 起")
        out.append(f"| {span}（{keys}） " + stats(seg))
    out += ["", "## 每条规则的样本外天数", "", "| 规则 | 决定日 | 实盘样本外天数 |" + (" 回放样本外天数 |" if rep else ""),
            "|---|---|---|" + ("---|" if rep else "")]
    live_days = sorted({r["day"] for r in live})
    rep_days = sorted({r["day"] for r in rep})
    for r in RULES:
        line = f"| {r.key}：{r.name} | {r.decided} | {sum(d > r.decided for d in live_days)} |"
        out.append(line + (f" {sum(d > r.decided for d in rep_days)} |" if rep else ""))
    spread = next(r for r in RULES if r.key == "spread_8")
    out += parallel_section()
    out += ["", f"## 价差上限挡掉的信号（实盘日志，{spread.effective} 起为 8%）", "", "| 日期 | 次数 | 合约 |", "|---|---|---|"]
    for f in sorted(glob.glob(os.path.join("logs", "live_????-??-??.log"))):
        day = os.path.basename(f)[5:15]
        if day < str(spread.effective):
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            seen, hits = set(), []
            for ln in fh:
                m = re.search(r"^(\d\d:\d\d):\d\d .*放弃】\S+ (\S+) 买卖价差 (\d+)%", ln)
                if not m:
                    continue
                # 日志时间是 UTC；A、B 两个账户共用同一信号，同一分钟同一合约只记一次
                utc = dt.datetime.fromisoformat(f"{day}T{m.group(1)}").replace(tzinfo=dt.timezone.utc)
                et = utc.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
                if (et, m.group(2)) in seen:
                    continue
                seen.add((et, m.group(2)))
                hits.append(f"{et} {m.group(2).split(':')[-1]}（{m.group(3)}%）")
        out.append(f"| {day} | {len(hits)} | {'、'.join(hits) or '—'} |")
    text = "\n".join(out) + "\n"
    os.makedirs(os.path.join("backtests", "oos"), exist_ok=True)
    with open(os.path.join("backtests", "oos", f"{args.name or 'live'}.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
