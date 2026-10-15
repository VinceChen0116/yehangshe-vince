"""
overnight.py —— 尾盘隔夜单回测（真实口径）
==========================================
形态（P 日 15:50 的 Heatmap，到 15:55 才可用 —— 与实盘一致晚 5 分钟）：
  * CLIFF 悬崖做空：最大正 GEX（天花板）> 最大负 GEX（深渊），现货夹在两者之间 → 买 Put
  * SPRINGBOARD 跳板做多：最大正 GEX（地板）< 最大负 GEX（加速区），现货夹在两者之间 → 买 Call
  不限制现货离正节点的距离，逐笔记录距离（dist_pct），汇总时按 ≤1% / 1–2.5% / 2.5–5% / >5% 分组。
到期：P 日之后最近一个有存档的到期（当日到期的合约过不了夜，不用）。
行权价（Heatmap 挂牌行权价，三档都记录）：atm = 第一档虚值；otm5 / otm10 = 离 现货×(1∓5%/10%) 最近的虚值档。
价格（真实口径，与回放一致）：中间价 = 每分钟真实 IV + 真实标的价还原（实测与实盘报价中位误差 3%）；
  买入 = 中间价 + 当时真实 NBBO 半价差（ask），卖出 = 中间价 − 半价差（bid）；当时 bid 为 0 不开仓；
  P 日 15:55 买入，S 日 09:35 / 09:45 / 09:55 卖出三种都统计。每笔 1 张。
样本：本地存档里相邻交易日对 (P, S)；S 可以是今天（09:56 之后）。

用法：python3 overnight.py [--post]      输出 backtests/overnight/trades.csv、summary.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import sys

from archive import live_universe, load_archived_heatmap, rows_to_frames
from config import SystemConfig, et_datetime, et_now, expiry_candidates, next_trading_day, setup_logging
from detector import parse_heatmap
from main import HistoryClient
from paper_trader import occ_symbol
from yhs_client import YHSClient

log = logging.getLogger("yhs.overnight")
OUT_DIR = os.path.join("backtests", "overnight")
EXITS = (("x0935", "09:35"), ("x0945", "09:45"), ("x0955", "09:55"))
STRIKES = (("atm", 0.0), ("otm5", 0.05), ("otm10", 0.10))
DIST_BUCKETS = ((0.01, "≤1%"), (0.025, "1–2.5%"), (0.05, "2.5–5%"), (9.0, ">5%"))


def snapshot_near_close(cfg, day: dt.date, ticker: str):
    """P 日之后最近到期的 15:50 快照（只读存档）。返回 (到期, 快照) 或 (None, None)。"""
    for exp in expiry_candidates(day, None, cfg.expiry_search_trading_days):
        if exp == day.isoformat():
            continue
        rows = load_archived_heatmap(cfg, day, ticker, exp)
        if not rows:
            continue
        frames = rows_to_frames(ticker, day, exp, rows)
        times = [t for t in sorted(frames) if t <= et_datetime(day, "15:50")]
        if not times:
            return None, None
        return exp, parse_heatmap(frames[times[-1]], cfg, day)
    return None, None


def shape(snap):
    """不限距离的形态：返回 (结构, 方向, 距离) 或 None。"""
    pos, neg, spot = snap.king_call_strike, snap.king_put_strike, snap.spot
    if not pos or not neg or not spot:
        return None
    dist = abs(spot - pos) / pos
    if pos > neg and neg < spot <= pos:
        return "CLIFF", "P", dist
    if pos < neg and pos <= spot < neg:
        return "SPRINGBOARD", "C", dist
    return None


def pick_strike(snap, right: str, depth: float) -> float | None:
    spot = snap.spot
    side = [k for k in snap.strikes if (k > spot if right == "C" else k < spot)]
    if not side:
        return None
    if depth == 0:
        return min(side) if right == "C" else max(side)
    want = spot * (1 + depth if right == "C" else 1 - depth)
    return min(side, key=lambda k: abs(k - want))


def half_spread(hc, cfg, sym, t):
    """当时真实 NBBO 的单边半价差；bid 为 0 返回 None（没有买盘）；查不到用 slippage_pct。"""
    q = hc.hist_quote(sym, t)
    if q and q[0] <= 0:
        return None
    if q and q[1] > 0:
        return (q[1] - q[0]) / (q[1] + q[0])
    return cfg.slippage_pct


def bucket(dist: float) -> str:
    return next(lb for lim, lb in DIST_BUCKETS if dist <= lim)


def run(cfg, client, tickers: list, pairs: list) -> list:
    out = []
    for P, S in pairs:
        hp, hs = HistoryClient(client, cfg, P), HistoryClient(client, cfg, S)
        live_set = set(live_universe(P) or [])
        log.info("===== 隔夜 %s → %s =====", P, S)
        for t in tickers:
            exp, snap = snapshot_near_close(cfg, P, t)
            if snap is None or snap.spot <= 0:
                continue
            sh = shape(snap)
            if sh is None:
                continue
            struct, right, dist = sh
            seen = set()
            for sk, depth in STRIKES:
                K = pick_strike(snap, right, depth)
                if K is None or K in seen:
                    continue
                seen.add(K)
                sym = occ_symbol(t, exp, right, K)
                mid = hp.option_price(sym, et_datetime(P, "15:55"))
                if not mid or mid < cfg.min_option_price:
                    continue
                h = half_spread(hp, cfg, sym, et_datetime(P, "15:55"))
                if h is None:
                    continue
                cost = mid * (1 + h)
                row = {"night": f"{P}→{S}", "ticker": t, "in_live": t in live_set, "structure": struct, "right": right,
                       "expiration": exp, "strike_kind": sk, "contract": sym, "strike": K, "spot_close": round(snap.spot, 2),
                       "pos_node": snap.king_call_strike, "neg_node": snap.king_put_strike,
                       "dist_pct": round(dist * 100, 2), "dist_bucket": bucket(dist),
                       "otm_pct": round(abs(K / snap.spot - 1) * 100, 2), "entry": round(cost, 4)}
                und = hs.underlying_at(sym, et_datetime(S, "09:35"))
                row["gap_pct"] = round((und / snap.spot - 1) * 100, 3) if und else ""
                for name, hhmm in EXITS:
                    tt = et_datetime(S, hhmm)
                    px = hs.option_price(sym, tt)
                    hx = half_spread(hs, cfg, sym, tt) if px else None
                    bid = 0.0 if (px and hx is None) else (px * (1 - hx) if px else None)
                    row[f"roi_{name}"] = "" if bid is None else round((bid / cost - 1) * 100, 1)
                    row[f"pnl_{name}"] = "" if bid is None else round((bid - cost) * 100, 0)
                out.append(row)
                log.info("%s %s %s %s 距 %.1f%% | 成本 %.2f | 跳空 %s%% | 09:35 %s%% / 09:45 %s%% / 09:55 %s%%", P, t,
                         struct, sym, dist * 100, cost, row["gap_pct"], row["roi_x0935"], row["roi_x0945"], row["roi_x0955"])
    return out


def _stats(g, name):
    v = [r for r in g if r[f"pnl_{name}"] != ""]
    if not v:
        return None
    pnl = [r[f"pnl_{name}"] for r in v]
    best = max(pnl)
    return {"n": len(v), "pnl": round(sum(pnl)), "pnl_ex_best": round(sum(pnl) - best),
            "win_pct": round(100 * sum(x > 0 for x in pnl) / len(v), 1),
            "median_roi": sorted(r[f"roi_{name}"] for r in v)[len(v) // 2],
            "nights_up": sum(1 for n in sorted({r["night"] for r in v})
                             if sum(r[f"pnl_{name}"] for r in v if r["night"] == n) > 0),
            "nights": len({r["night"] for r in v})}


def summarize(rows: list) -> dict:
    res = {}
    for struct in ("CLIFF", "SPRINGBOARD"):
        for scope in ("all", "live"):
            for _, b in DIST_BUCKETS + ((None, "全部"),):
                for sk, _ in STRIKES:
                    g = [r for r in rows if r["structure"] == struct and r["strike_kind"] == sk
                         and (b == "全部" or r["dist_bucket"] == b) and (scope == "all" or r["in_live"])]
                    if g:
                        res[f"{struct}|{scope}|{b}|{sk}"] = {n: _stats(g, n) for n, _ in EXITS}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="尾盘隔夜单回测")
    ap.add_argument("--post", action="store_true", help="结果发 Discord")
    ap.add_argument("--log-file", default=os.path.join(OUT_DIR, "run.log"))
    ap.add_argument("--tickers", help="逗号分隔，只跑这些标的（默认 212 只存档池）")
    ap.add_argument("--nights", help="逗号分隔的 P 日（如 2026-10-14），默认全部")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    os.makedirs(OUT_DIR, exist_ok=True)
    setup_logging(cfg.log_level, args.log_file)
    root = os.path.join(cfg.data_dir, "archive", "heatmap")
    days = sorted(dt.date.fromisoformat(d) for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "_manifest.json")))
    pairs = []
    for P in days:
        S = next_trading_day(P)
        if S in days or (S == et_now().date() and et_now() >= et_datetime(S, "09:56")):
            pairs.append((P, S))
    with open(os.path.join(cfg.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
        tickers = json.load(f)["tickers"]
    if args.tickers:
        tickers = args.tickers.split(",")
    if args.nights:
        pairs = [(p, s) for p, s in pairs if p.isoformat() in args.nights.split(",")]
    client = YHSClient(cfg)
    fields = ["night", "ticker", "in_live", "structure", "right", "expiration", "strike_kind", "contract", "strike",
              "spot_close", "pos_node", "neg_node", "dist_pct", "dist_bucket", "otm_pct", "entry", "gap_pct"] + \
             [f"{k}_{n}" for n, _ in EXITS for k in ("roi", "pnl")]
    for P, S in pairs:   # 每晚单独一个文件：可以按晚并行跑（--nights），最后合并
        night_rows = run(cfg, client, tickers, [(P, S)])
        if not args.tickers:
            with open(os.path.join(OUT_DIR, f"trades_{P}.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(night_rows)
    rows = []
    for fn in sorted(os.listdir(OUT_DIR)):
        if fn.startswith("trades_2") and fn.endswith(".csv"):
            with open(os.path.join(OUT_DIR, fn), encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    for k in r:
                        if k.startswith(("roi_", "pnl_")) and r[k] != "":
                            r[k] = float(r[k])
                    r["in_live"] = r["in_live"] == "True"
                    rows.append(r)
    with open(os.path.join(OUT_DIR, "trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    nights = sorted({r["night"] for r in rows})
    summ = {"nights": nights, "tickers": len(tickers), "result": summarize(rows)}
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print(json.dumps(summ, ensure_ascii=False, indent=2))
    log.info("配额剩余 %s", client.quota_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
