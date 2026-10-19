#!/usr/bin/env python3
"""隔夜策略的两个对照分析（不改实盘规则）：

1. flow：只看大单的对照回测——与 overnight_rules_bt.py 完全相同（第一档虚值、按大单金额排、每笔约 $1,000、
   价差过滤、每晚 10 笔、同板块 3 笔），只是不要求 Heatmap 结构：15:00–15:50 有 Ask 侧 ≥ $5 万 大单即可，
   方向取 Call / Put 大单金额多的一边。
       python3 overnight_controls.py flow --out backtests/overnight_flow_only --nights 2026-10-07,...
2. earnings：给回测结果标财报夜（P 日盘后 / 时间未注明，或 S 日盘前 / 时间未注明 发财报），分开统计盈亏。
   财报日历来自 Nasdaq（api.nasdaq.com/api/calendar/earnings），缓存在 yhs_data/cache/earnings/<日期>.json。
       python3 overnight_controls.py earnings backtests/overnight_rules_gap1 backtests/overnight_flow_only
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import os
import sys
import urllib.request

import overnight_rules_bt as bt
from archive import load_archived_heatmap, rows_to_frames
from config import SystemConfig, et_datetime, expiry_candidates, parse_iso, to_et
from detector import StructureSignal, parse_heatmap
from yhs_client import YHSClient

_client = None


def flow_only_at_close(cfg, lcfg, day: dt.date, ticker: str, next_day_only: bool = False,
                       tol: float = 0.0, bars: tuple = bt.BARS, need: int = 0):
    """替代 structure_at_close：不看结构，只看 15:00–15:50 的同向大单，方向取金额多的一边。"""
    exps = expiry_candidates(day, None, cfg.expiry_search_trading_days)[1:]
    for exp in (exps[:1] if next_day_only else exps):
        rows = load_archived_heatmap(cfg, day, ticker, exp)
        if not rows:
            continue
        frames = rows_to_frames(ticker, day, exp, rows)
        k = next((t for t in frames if t.strftime("%H:%M") == bars[-1]), None)
        snap = parse_heatmap(frames[k], cfg, day) if k else None
        if snap is None:
            return None
        until = et_datetime(day, bt.FLOW_UNTIL)
        usd = {r: sum(float(x.get("premium_usd") or 0) for x in bt.late_flow(cfg, _client, day, ticker, r)
                      if to_et(parse_iso(x["occurred_at"])) <= until) for r in ("C", "P")}
        if not max(usd.values()):
            return None
        right = "C" if usd["C"] >= usd["P"] else "P"
        st = StructureSignal("FLOW", "BUY_CALL" if right == "C" else "BUY_PUT", right, 0.0, 0.0, snap.spot, 0.0)
        return exp, snap, st, "flow"
    return None


def run_flow(argv) -> int:
    global _client
    _client = YHSClient(SystemConfig.load(None))
    bt.structure_at_close = flow_only_at_close
    return bt.main(argv)


EARN_DIR = os.path.join("yhs_data", "cache", "earnings")


def earnings_on(day: dt.date) -> dict:
    """{代码: 时间}，时间为 time-pre-market / time-after-hours / time-not-supplied。"""
    fp = os.path.join(EARN_DIR, f"{day}.json")
    if not os.path.exists(fp):
        req = urllib.request.Request(f"https://api.nasdaq.com/api/calendar/earnings?date={day}",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = (json.load(r).get("data") or {}).get("rows") or []
        os.makedirs(EARN_DIR, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({x["symbol"].replace(".", "").replace("/", ""): x.get("time") for x in rows}, f, indent=0)
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def earnings_tag(night: str, ticker: str) -> str:
    """"P→S" 这一晚该标的是否跨财报：返回 '' 或说明（如 '10-14 盘后'）。"""
    P, S = (dt.date.fromisoformat(x) for x in night.split("→"))
    t = ticker.replace(".", "")
    tp, ts = earnings_on(P).get(t), earnings_on(S).get(t)
    if tp in ("time-after-hours", "time-not-supplied"):
        return f"{P:%m-%d} {'盘后' if tp == 'time-after-hours' else '时间未注明'}"
    if ts in ("time-pre-market", "time-not-supplied"):
        return f"{S:%m-%d} {'盘前' if ts == 'time-pre-market' else '时间未注明'}"
    d = P + dt.timedelta(days=1)
    while d < S:   # 周末 / 假日里发的
        if t in earnings_on(d):
            return f"{d:%m-%d}"
        d += dt.timedelta(days=1)
    return ""


def run_earnings(dirs) -> int:
    for d in dirs:
        rows = [r for f in sorted(glob.glob(os.path.join(d, "trades_2*.csv")))
                for r in csv.DictReader(open(f, encoding="utf-8")) if r.get("pnl_usd")]
        groups = {"财报夜": [], "非财报夜": []}
        for r in rows:
            tag = earnings_tag(r["night"], r["ticker"])
            groups["财报夜" if tag else "非财报夜"].append((r, tag))
        print(f"== {d}：{len(rows)} 笔，合计 ${sum(float(r['pnl_usd']) for r in rows):+,.0f}")
        for g, v in groups.items():
            pnl = [float(r["pnl_usd"]) for r, _ in v]
            print(f"  {g}：{len(v)} 笔，合计 ${sum(pnl):+,.0f}，胜率 {100 * sum(p > 0 for p in pnl) / len(pnl):.0f}%"
                  if pnl else f"  {g}：0 笔")
        for r, tag in groups["财报夜"]:
            print(f"    {r['night']} {r['ticker']:5s} {tag:12s} ${float(r['pnl_usd']):+,.0f}")
    return 0


if __name__ == "__main__":
    cmd, rest = sys.argv[1], sys.argv[2:]
    sys.exit(run_flow(rest) if cmd == "flow" else run_earnings(rest))
