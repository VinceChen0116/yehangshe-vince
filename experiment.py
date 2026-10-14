"""
experiment.py —— 控制变量回测
=============================
每次实验相对于默认配置（config.py + config.json）只改动 --set 指定的参数，
在所有已存档的交易日上跑历史回放，统计结果并追加到 backtests/EXPERIMENTS.md。

Heatmap 走本地存档、期权价格走本地缓存（yhs_data/cache/），同一组日期重跑基本 0 配额且结果可复现。

用法：
  python3 experiment.py --name baseline                                   # 当前默认规则
  python3 experiment.py --name no_midday --set no_new_entries_midday=true # 只改一个变量
  python3 experiment.py --name stop50 --set stop_loss_pct=-50 --baseline baseline
  python3 experiment.py --compare baseline,stop50                         # 只对比已有结果，不跑回测
  python3 experiment.py --name big --universe heatmap                     # 大样本标的池（212 只）
输出：backtests/exp/<name>/{trades_<日期>.csv, run.log, summary.json}
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import sys

from config import SystemConfig, setup_logging
from notify import experiment_summary, from_config as notify_from_config

log = logging.getLogger("yhs.experiment")

EXP_DIR = os.path.join("backtests", "exp")
REGISTRY = os.path.join("backtests", "EXPERIMENTS.md")


def parse_value(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def archived_dates(cfg: SystemConfig) -> list[str]:
    root = os.path.join(cfg.data_dir, "archive", "heatmap")
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "_manifest.json")))


def load_trades(name: str) -> list[dict]:
    d = os.path.join(EXP_DIR, name)
    rows = []
    for fn in sorted(os.listdir(d)):
        if fn.startswith("trades_") and fn.endswith(".csv"):
            day = fn[len("trades_"):-len(".csv")]
            with open(os.path.join(d, fn), encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    r["day"] = day
                    rows.append(r)
    return rows


def metrics(rows: list[dict], hit_pct: float = 15.0) -> dict:
    if not rows:
        return {"trades": 0}
    pnl = [float(r["realized_pnl"]) for r in rows]
    roi = [float(r["realized_roi_pct"]) for r in rows]
    peak = [float(r["peak_roi_pct"]) for r in rows]
    gw, gl = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
    stops = [x for x, r in zip(roi, rows) if r["exit_reason"] == "STOP_LOSS"]
    return {
        "trades": len(rows),
        "total_pnl": round(sum(pnl), 2),
        "pnl_ex_best": round(sum(pnl) - max(pnl), 2),
        "win_rate_pct": round(100 * sum(x > 0 for x in pnl) / len(rows), 1),
        "direction_pct": round(100 * sum(x >= hit_pct for x in peak) / len(rows), 1),
        "avg_peak_pct": round(sum(peak) / len(rows), 1),
        "avg_roi_pct": round(sum(roi) / len(rows), 1),
        "profit_factor": round(gw / gl, 2) if gl else None,
        "hard_stops": len(stops),
        "underlying_stops": sum(1 for r in rows if r["exit_reason"] == "UNDERLYING_STOP"),
        "hard_stop_avg_pct": round(sum(stops) / len(stops), 1) if stops else None,
        "hard_stop_worst_pct": round(min(stops), 1) if stops else None,
    }


def breakdown(rows: list[dict]) -> dict:
    def is_0dte(r):
        return r["contract_symbol"][-15:-9] == r["day"][2:].replace("-", "")
    out = {"by_day": {}, "0DTE": metrics([r for r in rows if is_0dte(r)]),
           "1DTE": metrics([r for r in rows if not is_0dte(r)]), "by_session": {}}
    for day in sorted({r["day"] for r in rows}):
        out["by_day"][day] = metrics([r for r in rows if r["day"] == day])
    for lo, hi, label in (("09:30", "11:30", "早盘"), ("11:30", "14:30", "午间"), ("14:30", "16:01", "尾盘")):
        out["by_session"][label] = metrics([r for r in rows if lo <= r["entry_time"][11:16] < hi])
    return out


def fmt_money(v) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def print_compare(names: list[str]) -> None:
    sums = {}
    for n in names:
        p = os.path.join(EXP_DIR, n, "summary.json")
        if not os.path.exists(p):
            print(f"找不到实验 {n}（{p}）")
            return
        with open(p, encoding="utf-8") as f:
            sums[n] = json.load(f)
    keys = [("trades", "笔数"), ("total_pnl", "合计"), ("pnl_ex_best", "去最大一笔"), ("win_rate_pct", "胜率%"),
            ("direction_pct", "方向正确%"), ("avg_peak_pct", "平均峰值%"), ("profit_factor", "盈亏比"),
            ("hard_stops", "硬止损笔数"), ("hard_stop_avg_pct", "硬止损平均%"),
            ("underlying_stops", "标的破位止损笔数")]
    w = max(12, *(len(n) + 2 for n in names))
    print("指标".ljust(12) + "".join(n.rjust(w) for n in names))
    for k, label in keys:
        vals = []
        for n in names:
            v = sums[n]["overall"].get(k)
            vals.append(fmt_money(v) if k in ("total_pnl", "pnl_ex_best") else ("—" if v is None else str(v)))
        print(label.ljust(12) + "".join(v.rjust(w) for v in vals))
    print("改动：" + "；".join(f"{n}={json.dumps(sums[n]['overrides'], ensure_ascii=False)}" for n in names))


def append_registry(name: str, summary: dict) -> None:
    new = not os.path.exists(REGISTRY)
    o = summary["overall"]
    with open(REGISTRY, "a", encoding="utf-8") as f:
        if new:
            f.write("# 回测实验记录（控制变量）\n\n"
                    "每行相对于当时默认配置只改动「改动」列的参数。由 `experiment.py` 自动追加。\n\n"
                    "| 实验 | 运行时间 | 改动 | 日期 | 笔数 | 合计 | 去最大一笔 | 胜率 | 方向正确 | 盈亏比 | 硬止损 笔/平均 | 午间合计 |\n"
                    "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        mid = summary["breakdown"]["by_session"]["午间"].get("total_pnl")
        stops = f"{o.get('hard_stops', 0)} / {o.get('hard_stop_avg_pct')}%"
        f.write(f"| {name} | {summary['run_at']} | `{json.dumps(summary['overrides'], ensure_ascii=False)}` | "
                f"{summary['dates'][0]}~{summary['dates'][-1]}（{len(summary['dates'])} 天） | {o.get('trades', 0)} | "
                f"{fmt_money(o.get('total_pnl'))} | {fmt_money(o.get('pnl_ex_best'))} | {o.get('win_rate_pct')}% | "
                f"{o.get('direction_pct')}% | {o.get('profit_factor')} | {stops} | {fmt_money(mid)} |\n")


def run(args) -> int:
    overrides = {}
    for item in args.set or []:
        if "=" not in item:
            print(f"--set 需要 key=value：{item}")
            return 2
        k, v = item.split("=", 1)
        overrides[k.strip()] = parse_value(v.strip())
    base = SystemConfig.load(args.config)
    for k in overrides:
        if not hasattr(base, k):
            print(f"未知配置项：{k}")
            return 2
    out = os.path.join(EXP_DIR, args.name)
    if os.path.exists(out) and not args.force:
        print(f"实验 {args.name} 已存在（{out}）；换个名字或加 --force 覆盖")
        return 2
    os.makedirs(out, exist_ok=True)
    for fn in os.listdir(out):
        os.remove(os.path.join(out, fn))
    setup_logging(base.log_level, os.path.join(out, "run.log"))
    dates = args.dates.split(",") if args.dates else archived_dates(base)
    if args.tickers:
        tickers = args.tickers.split(",")
    elif args.universe in ("heatmap", "live"):
        with open(os.path.join(base.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
            tickers = json.load(f)["tickers"]
    else:
        tickers = list(base.archive_tickers)
    if not dates:
        log.error("没有可用的存档日期；先运行 archive.py 或用 --dates 指定")
        return 2
    log.info("实验 %s：改动 %s，日期 %s，标的 %d 只", args.name, overrides or "无（基准）", ",".join(dates), len(tickers))
    overrides_note = dict(overrides)
    if args.universe != "config" or args.tickers:
        overrides_note["_标的"] = args.tickers or ("每天用当天实盘监控标的" if args.universe == "live"
                                                  else f"{args.universe}（{len(tickers)} 只）")

    from main import run_history_replay   # 延迟导入：避免 --compare 时加载整个回放模块
    for day in dates:
        cfg = SystemConfig.load(args.config)
        for k, v in overrides.items():
            cur = getattr(cfg, k)
            setattr(cfg, k, tuple(v) if isinstance(cur, tuple) and isinstance(v, list) else v)
        cfg.trades_csv = os.path.join(out, f"trades_{day}.csv")
        day_tickers = tickers
        if args.universe == "live":   # 复刻实盘：用当天实盘 B 账户的监控标的（没有实盘日志的日子退回 212 只）
            from archive import live_universe
            day_tickers = live_universe(dt.date.fromisoformat(day)) or tickers
            log.info("%s 标的：%s（%d 只）", day, "实盘当天监控" if day_tickers is not tickers else "212 只存档", len(day_tickers))
        run_history_replay(cfg, dt.date.fromisoformat(day), day_tickers, None)

    rows = load_trades(args.name)
    summary = {"name": args.name, "run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "overrides": overrides_note,
               "dates": dates, "tickers": tickers, "overall": metrics(rows), "breakdown": breakdown(rows)}
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    append_registry(args.name, summary)
    log.info("实验 %s 完成：%s", args.name, summary["overall"])
    if args.baseline:
        print_compare([args.baseline, args.name])
    notifier = None if args.no_post else notify_from_config(base)
    if notifier:
        base_sum = None
        bp = os.path.join(EXP_DIR, args.baseline or "", "summary.json")
        if args.baseline and os.path.exists(bp):
            with open(bp, encoding="utf-8") as f:
                base_sum = json.load(f)
        experiment_summary(notifier, summary, base_sum)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="控制变量回测")
    ap.add_argument("--name", help="实验名（输出到 backtests/exp/<name>/）")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", help="相对默认配置改动的参数，可重复")
    ap.add_argument("--dates", help="逗号分隔的日期，默认全部已存档日期")
    ap.add_argument("--tickers", help="逗号分隔的标的，默认 config.archive_tickers")
    ap.add_argument("--universe", choices=["config", "heatmap", "live"], default="live",
                    help="live（默认，真实回测）：每天用当天实盘 B 账户的监控标的，没有实盘日志的日子退回 212 只；"
                         "heatmap：212 只大样本标的池；config：config.archive_tickers")
    ap.add_argument("--baseline", help="跑完后与该实验对比")
    ap.add_argument("--compare", help="只对比已有实验，逗号分隔")
    ap.add_argument("--config", help="JSON 配置文件")
    ap.add_argument("--force", action="store_true", help="覆盖同名实验")
    ap.add_argument("--no-post", action="store_true", help="不单独发 Discord（批量跑完后统一发汇总）")
    args = ap.parse_args(argv)
    if args.compare:
        print_compare(args.compare.split(","))
        return 0
    if not args.name:
        ap.error("需要 --name 或 --compare")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
