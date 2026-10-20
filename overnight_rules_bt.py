"""
overnight_rules_bt.py —— 隔夜模拟实盘规则（overnight_live.py）的真实口径回测
==========================================================================
与实盘逐条一致：
  * 标的：212 只存档池 + 当晚的日内实盘名单（有日志时）
  * 结构：P 日标记 15:40、15:45 的两个 Heatmap 快照（15:50 前都已可用）都识别出同一结构，
    局部节点优先、其次全局节点；到期 = P 日之后最近一个有存档的到期；跳板买 Call、悬崖买 Put
  * Flow：P 日 15:00–15:50 同方向 Ask 侧（A/AA）单笔 ≥ $5 万 的大单至少 1 笔（15:50 之后的实盘扫描时还看不到）
  * 合约：第一档虚值，每笔约 $1,000（$750–$1,250 内最接近的张数，单张超 $1,250 不开）；15:55 买入价 = 真实 IV 还原中间价 + 当时真实 NBBO 半价差（ask）；
    当时 bid 为 0、或价差 > 中间价 25% 不开；每晚最多 10 笔，按 15:00 后大单金额排
  * 出场：S 日 09:35 按 bid（中间价 − 真实半价差）卖；bid 为 0 顺延到 09:40 / 09:45，仍为 0 按 0 记
用法：python3 overnight_rules_bt.py [--nights 2026-10-14,...] [--post]
输出：backtests/overnight_rules/trades.csv、summary.json
"""
from __future__ import annotations

import argparse
import copy
import csv
import dataclasses
import datetime as dt
import json
import logging
import os
import sys

from archive import live_universe, load_archived_heatmap, rows_to_frames
from config import SystemConfig, et_datetime, et_now, expiry_candidates, next_trading_day, parse_iso, setup_logging, to_et
from detector import FLOW_TRADES_PATH, detect_structure, extract_rows, parse_heatmap
from main import HistoryClient
from notify import from_config
from overnight import half_spread
from overnight_live import drop_conflicts, size_contracts
from sectors import sector_of
from paper_trader import occ_symbol
from yhs_client import YHSClient

log = logging.getLogger("yhs.overnight_rules")
OUT = os.path.join("backtests", "overnight_rules")
BARS = ("15:40", "15:45")
ENTRY = "15:55"
EXITS = ("09:35", "09:40", "09:45")
FLOW_SINCE, FLOW_UNTIL, FLOW_MIN = "15:00", "15:50", 50000   # 实盘 15:44 起扫描，15:50 之后的大单当时还看不到
MAX_SPREAD, MAX_POS = 0.25, 10
RANK_KEY = None   # 候选排序（对照实验用）；None = 实盘口径，按 15:00 后大单金额从大到小


def detect_tol(snap, cfg, lcfg, tol: float = 0.0):
    """局部节点优先、其次全局节点；tol > 0 时，现货在 ±tol 范围内挪一下能成立也算（如现货越过天花板 0.1% 仍算悬崖）。
    返回 (结构, 模式) 或 (None, None)。"""
    spots = [snap.spot] + ([snap.spot * (1 - tol), snap.spot * (1 + tol)] if tol else [])
    for sp in spots:
        s2 = dataclasses.replace(snap, spot=sp)
        for c, mode in ((lcfg, "local"), (cfg, "king")):
            st = detect_structure(s2, c)
            if st:
                return st, mode
    return None, None


def structure_at_close(cfg, lcfg, day: dt.date, ticker: str, next_day_only: bool = False,
                       tol: float = 0.0, bars: tuple = BARS, need: int = 0):
    """next_day_only：只用次日到期的合约；tol：结构判断的现货容差；bars / need：在这些快照里至少 need 根
    （默认全部）是同一结构才算，合约按最后一根成立的快照选。"""
    exps = expiry_candidates(day, None, cfg.expiry_search_trading_days)[1:]
    need = need or len(bars)
    for exp in (exps[:1] if next_day_only else exps):
        rows = load_archived_heatmap(cfg, day, ticker, exp)
        if not rows:
            continue
        frames = rows_to_frames(ticker, day, exp, rows)
        found = []
        for hm in bars:
            k = next((t for t in frames if t.strftime("%H:%M") == hm), None)
            snap = parse_heatmap(frames[k], cfg, day) if k else None
            if snap is not None:
                st, mode = detect_tol(snap, cfg, lcfg, tol)
                if st:
                    found.append((snap, st, mode))
        for kind in ("CLIFF", "SPRINGBOARD"):
            hits = [f for f in found if f[1].structure == kind]
            if len(hits) >= need:
                snap, st, mode = hits[-1]
                return exp, snap, st, mode
        return None
    return None


def late_flow(cfg, client, day: dt.date, ticker: str, right: str) -> list:
    """P 日 15:00 之后同方向 Ask 侧 ≥ $5 万 的大单（从最新往前翻到 15:00；本地缓存，重跑 0 配额）。"""
    fp = os.path.join(cfg.data_dir, "cache", "flow_late", day.isoformat(), f"{ticker}_{right}.json")
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    since = et_datetime(day, FLOW_SINCE)
    out, cursor = [], None
    for _ in range(40):
        p = {"tickers": ticker, "kind": "order", "min_premium_usd": FLOW_MIN, "sides": "A,AA", "rights": right,
             "session_date": day.isoformat(), "sort_by": "occurredAt", "direction": "desc", "limit": 25, "cursor": cursor}
        payload = client.get(FLOW_TRADES_PATH, p)
        page = extract_rows(payload)
        keep = [t for t in page if to_et(parse_iso(t["occurred_at"])) >= since]
        out += [{k: t.get(k) for k in ("occurred_at", "contract_symbol", "premium_usd", "session_volume",
                                         "open_interest", "min_dte")} for t in keep]
        cursor = (payload.get("_meta") or {}).get("next_cursor") if isinstance(payload, dict) else None
        if not cursor or len(keep) < len(page):
            break
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp + f".{os.getpid()}.tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(fp + f".{os.getpid()}.tmp", fp)
    return out


def first_otm(snap, right: str):
    side = [k for k in snap.strikes if (k > snap.spot if right == "C" else k < snap.spot)]
    return (min(side) if right == "C" else max(side)) if side else None


def run_night(cfg, client, P: dt.date, S: dt.date, tickers: list, entry: str = ENTRY, next_day_only: bool = False,
              tol: float = 0.0, bars: tuple = BARS, need: int = 0, sizing: bool = True,
              max_dir: int = 0, max_sector: int = 0) -> list:
    lcfg = copy.copy(cfg)
    lcfg.node_mode = "local"
    hp, hs = HistoryClient(client, cfg, P), HistoryClient(client, cfg, S)
    cands = []
    for t in tickers:
        r = structure_at_close(cfg, lcfg, P, t, next_day_only, tol, bars, need)
        if r is None:
            continue
        exp, snap, st, mode = r
        until = et_datetime(P, FLOW_UNTIL)
        fl = [x for x in late_flow(cfg, client, P, t, st.right) if to_et(parse_iso(x["occurred_at"])) <= until]
        log.info("%s %s %s（%s）%.2f→%.2f 距 %.2f%% | 15:00 后大单 %d 笔", P, t, st.structure, mode, st.pos_strike,
                 st.neg_strike, st.distance_pct * 100, len(fl))
        if fl:
            cands.append((sum(float(x.get("premium_usd") or 0) for x in fl), len(fl), t, exp, snap, st, mode))
    cands.sort(key=RANK_KEY or (lambda x: -x[0]))
    cands, dropped = drop_conflicts(cands, lambda c: c[2], lambda c: c[5].right)
    if dropped:
        log.info("%s %s 同一家公司方向相反，都不开", P, "、".join(dropped))
    out = []
    dir_n, sec_n = {}, {}
    for usd, n, t, exp, snap, st, mode in cands:
        if len(out) >= MAX_POS:
            break
        if max_dir and dir_n.get(st.right, 0) >= max_dir:
            log.info("%s %s 放弃：同方向（%s）已 %d 笔", P, t, st.right, max_dir)
            continue
        sec = sector_of(t)
        if max_sector and sec_n.get(sec, 0) >= max_sector:
            log.info("%s %s 放弃：同板块（%s）已 %d 笔", P, t, sec, max_sector)
            continue
        K = first_otm(snap, st.right)
        if K is None:
            continue
        sym = occ_symbol(t, exp, st.right, K)
        te = et_datetime(P, entry)
        mid = hp.option_price(sym, te)
        q = hp.hist_quote(sym, te)
        if not mid or mid < cfg.min_option_price or (q and q[0] <= 0):
            log.info("%s %s 放弃：无价格或 bid 为 0", P, sym)
            continue
        if q and q[1] > 0 and (q[1] - q[0]) / ((q[1] + q[0]) / 2) > MAX_SPREAD:
            log.info("%s %s 放弃：价差 %.0f%%", P, sym, (q[1] - q[0]) / ((q[1] + q[0]) / 2) * 100)
            continue
        h = (q[1] - q[0]) / (q[1] + q[0]) if (q and q[1] > 0) else cfg.slippage_pct
        cost = mid * (1 + h)
        qty = size_contracts(cost) if sizing else 1
        if not qty:
            log.info("%s %s 放弃：单张 $%.0f 超过每笔上限", P, sym, cost * 100)
            continue
        bid, when = None, None
        for hm in EXITS:
            tx = et_datetime(S, hm)
            px = hs.option_price(sym, tx)
            hx = half_spread(hs, cfg, sym, tx) if px else None
            b = 0.0 if (px and hx is None) else (px * (1 - hx) if px else None)
            if b:
                bid, when = b, hm
                break
            if b == 0.0:
                bid, when = 0.0, hm
        if bid is None:
            log.info("%s %s 次日没有价格，跳过", P, sym)
            continue
        und = hs.underlying_at(sym, et_datetime(S, "09:35"))
        row = {"night": f"{P}→{S}", "ticker": t, "sector": sec, "structure": st.structure, "node_mode": mode, "contract": sym,
               "spot": round(snap.spot, 2), "pos_node": st.pos_strike, "neg_node": st.neg_strike,
               "dist_pct": round(st.distance_pct * 100, 2), "flow_late_n": n, "flow_late_usd": round(usd),
               "entry": round(cost, 3), "exit": round(bid, 3), "exit_time": when,
               "gap_pct": round((und / snap.spot - 1) * 100, 2) if und else "",
               "gap_right_way": (und > snap.spot) == (st.right == "C") if und else "",
               "qty": qty, "invested": round(cost * 100 * qty),
               "pnl_usd": round((bid - cost) * 100 * qty, 1), "roi_pct": round((bid / cost - 1) * 100, 1)}
        out.append(row)
        dir_n[st.right] = dir_n.get(st.right, 0) + 1
        sec_n[sec] = sec_n.get(sec, 0) + 1
        log.info("%s 开 %s %s 成本 %.2f → %s 卖 %.2f（%+.1f%%）跳空 %s%%", P, t, sym, cost, when, bid, row["roi_pct"], row["gap_pct"])
    return out


def main(argv=None) -> int:
    global OUT
    ap = argparse.ArgumentParser(description="隔夜实盘规则回测")
    ap.add_argument("--nights")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--entry", default=ENTRY, help="P 日买入时刻（默认 15:55）")
    ap.add_argument("--out", default=OUT, help="输出目录")
    ap.add_argument("--next-day-only", action="store_true", help="只买次日到期的合约")
    ap.add_argument("--tol", type=float, default=0.0, help="结构判断的现货容差（如 0.001 = 0.1%%）")
    ap.add_argument("--bars", default=",".join(BARS), help="用哪几根 Heatmap（逗号分隔）")
    ap.add_argument("--need", type=int, default=0, help="至少几根成立（默认全部）")
    ap.add_argument("--one-contract", action="store_true", help="每笔 1 张（旧口径；默认每笔约 $1,000）")
    ap.add_argument("--max-dir", type=int, default=0, help="同方向（Call / Put）每晚最多几笔（0 = 不限）")
    ap.add_argument("--max-sector", type=int, default=3, help="同板块每晚最多几笔（默认 3 = 实盘；0 = 不限，板块见 sectors.py）")
    ap.add_argument("--log-file", default=os.path.join(OUT, "run.log"))
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    OUT = args.out
    os.makedirs(OUT, exist_ok=True)
    setup_logging(cfg.log_level, args.log_file)
    root = os.path.join(cfg.data_dir, "archive", "heatmap")
    days = sorted(dt.date.fromisoformat(d) for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "_manifest.json")))
    pairs = [(P, next_trading_day(P)) for P in days
             if next_trading_day(P) in days or (next_trading_day(P) <= et_now().date()
                                                and et_now() >= et_datetime(next_trading_day(P), "09:50"))]
    if args.nights:
        pairs = [(p, s) for p, s in pairs if p.isoformat() in args.nights.split(",")]
    with open(os.path.join(cfg.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
        base = json.load(f)["tickers"]
    client = YHSClient(cfg)
    for P, S in pairs:
        tickers = base + [t for t in (live_universe(P) or []) if t not in base]
        rows = run_night(cfg, client, P, S, tickers, args.entry, args.next_day_only, args.tol,
                         tuple(args.bars.split(",")), args.need, not args.one_contract, args.max_dir, args.max_sector)
        with open(os.path.join(OUT, f"trades_{P}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["night"])
            w.writeheader()
            w.writerows(rows)
    allrows = []
    for fn in sorted(os.listdir(OUT)):
        if fn.startswith("trades_2") and fn.endswith(".csv"):
            with open(os.path.join(OUT, fn), encoding="utf-8") as f:
                allrows += [r for r in csv.DictReader(f) if r.get("pnl_usd")]
    nights = {}
    for r in allrows:
        nights.setdefault(r["night"], []).append(float(r["pnl_usd"]))
    pnl = [float(r["pnl_usd"]) for r in allrows]
    summ = {"trades": len(allrows), "total_pnl": round(sum(pnl)), "win_pct": round(100 * sum(p > 0 for p in pnl) / len(pnl), 1) if pnl else None,
            "pnl_ex_best": round(sum(pnl) - max(pnl)) if pnl else None,
            "by_night": {n: {"n": len(v), "pnl": round(sum(v)), "wins": sum(p > 0 for p in v)} for n, v in sorted(nights.items())}}
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print(json.dumps(summ, ensure_ascii=False, indent=2))
    log.info("配额剩余 %s", client.quota_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
