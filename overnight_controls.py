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
import re
import sys
import urllib.request

import overnight_rules_bt as bt
from paper_trader import occ_symbol
from archive import fetch_heatmap_rows, load_archived_heatmap, rows_to_frames
from config import SystemConfig, et_datetime, expiry_candidates, next_trading_day, parse_iso, to_et
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


HM_CACHE = os.path.join("yhs_data", "cache", "overnight_heatmap")


def daily_tickers_before(cfg, day: dt.date) -> set:
    """与 overnight_live.daily_expiry_tickers 相同，但只看 day 之前的存档（开仓当天 15:15 实盘能看到的）。"""
    root = os.path.join(cfg.data_dir, "archive", "heatmap")
    for d in sorted(os.listdir(root), reverse=True):
        try:
            a = dt.date.fromisoformat(d)
        except ValueError:
            continue
        if a >= day or next_trading_day(a).weekday() == 4:
            continue
        nxt = next_trading_day(a)
        return {t for t in os.listdir(os.path.join(root, d))
                if os.path.exists(fp := os.path.join(root, d, t, f"{nxt}.json")) and os.path.getsize(fp) > 1000}
    return set()


def use_live_expiry():
    """回测的到期日改成与实盘一致：先试 predicted_expiry（有每日到期 → 下一交易日，其余 → 下一个周五），
    存档里没有的到期从接口补拉 15:00 之后的 Heatmap（缓存在 HM_CACHE，不写共享存档）。
    修正：每日存档只存当天 + 下一交易日到期，周五晚上看不到下周五到期，回测原来会漏掉大部分标的。"""
    from overnight_live import predicted_expiry
    orig_sac, orig_load = bt.structure_at_close, bt.load_archived_heatmap
    daily_cache, cur = {}, {}

    def load(cfg, day, ticker, exp):
        rows = orig_load(cfg, day, ticker, exp)
        if rows:
            return rows
        fp = os.path.join(HM_CACHE, day.isoformat(), ticker, f"{exp}.json")
        if not os.path.exists(fp):
            try:
                rows = fetch_heatmap_rows(_client, ticker, exp, day, max_pages=3, start="15:00")
            except Exception:
                rows = []
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(rows, f)
        with open(fp, encoding="utf-8") as f:
            return json.load(f)

    def cands(day, *a, **k):
        out = expiry_candidates(day, *a, **k)
        pe = cur.get("exp")
        return [out[0], pe] + [e for e in out[1:] if e != pe] if pe else out

    def sac(cfg, lcfg, day, ticker, *a, **k):
        if day not in daily_cache:
            daily_cache[day] = daily_tickers_before(cfg, day)
        cur["exp"] = predicted_expiry(ticker, day, daily_cache[day])
        return orig_sac(cfg, lcfg, day, ticker, *a, **k)

    global _client
    _client = _client or YHSClient(SystemConfig.load(None))
    bt.structure_at_close, bt.load_archived_heatmap, bt.expiry_candidates = sac, load, cands


ETFS = {"SPY", "QQQ", "TQQQ", "SQQQ", "IWM", "DIA", "SOXL", "SOXS", "SPXL", "SPXS", "UPRO", "TNA", "TZA", "TLT", "UVXY"}
_OCC = re.compile(r"(\d{6})([CP])(\d{8})$")
_target = {}


def run_filter(argv) -> int:
    """现行规则 + 大单过滤（10/19 用户定的对照）：
       --no-etf          排除指数 / 杠杆 ETF
       --flow-max-dte N  大单只算 N 天内（自然日）到期的合约
       --flow-contract   大单必须正好是要买的那张合约（同到期日、同行权价）
       --flow-vol-ge-oi  大单合约成交时的当日成交量 ≥ 未平仓数（新开仓，排除滚动换月 / 平仓 / 对冲）"""
    no_etf = "--no-etf" in argv
    exact = "--flow-contract" in argv
    vol_oi = "--flow-vol-ge-oi" in argv
    if "--live-expiry" in argv:
        use_live_expiry()
    max_dte = int(argv[argv.index("--flow-max-dte") + 1]) if "--flow-max-dte" in argv else 0
    rest, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
        elif a == "--flow-max-dte":
            skip = True
        elif a not in ("--no-etf", "--flow-contract", "--flow-vol-ge-oi", "--live-expiry"):
            rest.append(a)
    orig_sac, orig_lf = bt.structure_at_close, bt.late_flow

    def sac(cfg, lcfg, day, ticker, *a, **k):
        if no_etf and ticker in ETFS:
            return None
        r = orig_sac(cfg, lcfg, day, ticker, *a, **k)
        if r:
            exp, snap, st, _ = r
            K = bt.first_otm(snap, st.right)
            _target[(day, ticker, st.right)] = occ_symbol(ticker, exp, st.right, K).split(":")[-1] if K is not None else None
        return r

    def lf(cfg, client, day, ticker, right):
        out = orig_lf(cfg, client, day, ticker, right)
        sym = _target.get((day, ticker, right))
        keep = []
        for x in out:
            s = str(x.get("contract_symbol") or "").split(":")[-1]
            m = _OCC.search(s)
            if not m:
                continue
            if max_dte and (dt.datetime.strptime(m.group(1), "%y%m%d").date() - day).days > max_dte:
                continue
            if exact and s != sym:
                continue
            if vol_oi and not (x.get("session_volume") is not None and x.get("open_interest") is not None
                               and float(x["session_volume"]) >= float(x["open_interest"])):
                continue
            keep.append(x)
        return keep

    bt.structure_at_close, bt.late_flow = sac, lf
    return bt.main(rest)


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
    sys.exit(run_flow(rest) if cmd == "flow" else run_filter(rest) if cmd == "filter" else run_earnings(rest))
