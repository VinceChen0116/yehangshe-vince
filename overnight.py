"""
overnight.py —— 尾盘隔夜单回测（Playbook SOP）
==============================================
规则（用户提供的"尾盘隔夜单三大铁律"）：
  1. 形态：首选 SPRINGBOARD 跳板看涨（现价站稳最大正 GEX 地板）；CLIFF 悬崖做空作为对照一并统计
  2. 行权价：平值或顺向轻度虚值 1 档 —— 取 Heatmap 挂牌行权价中现价上方（看跌为下方）最近的一个
  3. 出场：次日开盘 09:30–09:45 强制跳空兑现；同时统计 09:45 与次日收盘（15:55）持有作对照
  4. 仓位：每笔 1 张（只看每笔收益率；实盘按总资金 3–5% 控制仓位）

实现：
  * 进场：P 日 15:50 的 Heatmap 快照判断形态，15:55 的期权价格买入（+2% 滑点）
  * 到期：P 日之后最近的到期（当日到期的合约过不了夜，不用），只读本地存档
  * 期权价格：与历史回放相同（contract-greeks-series 真实 IV + BS，缓存在 yhs_data/cache/），出场 −2% 滑点
  * 样本：本地存档里相邻的交易日对 (P, S)；S 可以是今天（只需要 S 日早盘的期权价格）

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

from archive import load_archived_heatmap, rows_to_frames
from config import SystemConfig, et_datetime, et_now, expiry_candidates, next_trading_day, setup_logging, trading_dte
from detector import detect_structure, parse_heatmap
from main import HistoryClient
from paper_trader import occ_symbol
from yhs_client import YHSClient

log = logging.getLogger("yhs.overnight")
OUT_DIR = os.path.join("backtests", "overnight")
EXITS = (("open_0935", "09:35"), ("open_0945", "09:45"), ("close", "15:55"))


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


def pick_strike(snap, right: str) -> float | None:
    spot = snap.spot
    side = [k for k in snap.strikes if (k >= spot if right == "C" else k <= spot)]
    if not side:
        return None
    return min(side) if right == "C" else max(side)


def run(cfg, client, tickers: list, pairs: list) -> list:
    out = []
    for P, S in pairs:
        hp, hs = HistoryClient(client, cfg, P), HistoryClient(client, cfg, S)
        log.info("===== 隔夜 %s → %s =====", P, S)
        for t in tickers:
            exp, snap = snapshot_near_close(cfg, P, t)
            if snap is None or snap.spot <= 0:
                continue
            st = detect_structure(snap, cfg)
            if st is None:
                continue
            K = pick_strike(snap, st.right)
            if K is None:
                continue
            sym = occ_symbol(t, exp, st.right, K)
            entry = hp.option_price(sym, et_datetime(P, "15:55"))
            if not entry or entry < cfg.min_option_price:
                continue
            cost = entry * (1 + cfg.slippage_pct)
            row = {"night": f"{P}→{S}", "ticker": t, "structure": st.structure, "right": st.right,
                   "expiration": exp, "dte_at_entry": trading_dte(dt.date.fromisoformat(exp), P),
                   "contract": sym, "strike": K, "spot_close": round(snap.spot, 4), "pos_node": st.pos_strike,
                   "neg_node": st.neg_strike, "entry": round(cost, 4)}
            und_open = hs.underlying_at(sym, et_datetime(S, "09:35"))
            row["gap_pct"] = round((und_open / snap.spot - 1) * 100, 3) if und_open else ""
            for name, hhmm in EXITS:
                px = hs.option_price(sym, et_datetime(S, hhmm))
                if px is None:
                    row[f"roi_{name}"] = ""
                    continue
                row[f"roi_{name}"] = round((px * (1 - cfg.slippage_pct) / cost - 1) * 100, 2)
            out.append(row)
            log.info("%s %s %s %s 成本 %.2f | 跳空 %s%% | 09:35 %s%% / 09:45 %s%% / 收盘 %s%%", P, t, st.structure, sym,
                     cost, row["gap_pct"], row["roi_open_0935"], row["roi_open_0945"], row["roi_close"])
    return out


def summarize(rows: list) -> dict:
    res = {}
    for struct in ("SPRINGBOARD", "CLIFF"):
        g = [r for r in rows if r["structure"] == struct]
        d = {"n": len(g)}
        for name, _ in EXITS:
            v = [r[f"roi_{name}"] for r in g if r[f"roi_{name}"] != ""]
            if v:
                d[name] = {"n": len(v), "avg_roi": round(sum(v) / len(v), 1), "win_pct": round(100 * sum(x > 0 for x in v) / len(v), 1),
                           "pnl_per_contract_usd": round(sum(r[f"roi_{name}"] / 100 * r["entry"] * 100 for r in g
                                                             if r[f"roi_{name}"] != ""), 0)}
        gaps = [r["gap_pct"] for r in g if r["gap_pct"] != ""]
        if gaps:
            sign = 1 if struct == "SPRINGBOARD" else -1
            d["gap_in_favor_pct"] = round(100 * sum(sign * x > 0 for x in gaps) / len(gaps), 1)
            d["avg_gap_pct"] = round(sum(gaps) / len(gaps), 3)
        both = [r for r in g if r["roi_open_0935"] != "" and r["roi_close"] != ""]
        if both:
            d["open_beats_close_pct"] = round(100 * sum(r["roi_open_0935"] > r["roi_close"] for r in both) / len(both), 1)
        d["by_night"] = {}
        for night in sorted({r["night"] for r in g}):
            v = [r["roi_open_0935"] for r in g if r["night"] == night and r["roi_open_0935"] != ""]
            gp = [r["gap_pct"] for r in g if r["night"] == night and r["gap_pct"] != ""]
            d["by_night"][night] = {"n": len(v), "avg_roi_0935": round(sum(v) / len(v), 1) if v else None,
                                    "win_pct": round(100 * sum(x > 0 for x in v) / len(v), 1) if v else None,
                                    "avg_gap_pct": round(sum(gp) / len(gp), 3) if gp else None}
        res[struct] = d
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="尾盘隔夜单回测")
    ap.add_argument("--post", action="store_true", help="结果发 Discord")
    ap.add_argument("--log-file", default=os.path.join(OUT_DIR, "run.log"))
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    os.makedirs(OUT_DIR, exist_ok=True)
    setup_logging(cfg.log_level, args.log_file)
    root = os.path.join(cfg.data_dir, "archive", "heatmap")
    days = sorted(dt.date.fromisoformat(d) for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "_manifest.json")))
    pairs = []
    for P in days:
        S = next_trading_day(P)
        if S in days or (S == et_now().date() and et_now() >= et_datetime(S, "09:50")):
            pairs.append((P, S))
    with open(os.path.join(cfg.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
        tickers = json.load(f)["tickers"]
    client = YHSClient(cfg)
    rows = run(cfg, client, tickers, pairs)
    fields = ["night", "ticker", "structure", "right", "expiration", "dte_at_entry", "contract", "strike", "spot_close",
              "pos_node", "neg_node", "entry", "gap_pct"] + [f"roi_{n}" for n, _ in EXITS]
    with open(os.path.join(OUT_DIR, "trades.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    summ = {"nights": [f"{p}→{s}" for p, s in pairs], "tickers": len(tickers), "result": summarize(rows)}
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summ, f, ensure_ascii=False, indent=2)
    print(json.dumps(summ, ensure_ascii=False, indent=2))
    log.info("配额剩余 %s", client.quota_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
