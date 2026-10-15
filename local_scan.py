"""
local_scan.py —— 收盘后按"局部节点"扫一遍尾盘跳板 / 悬崖，留作次日验证
=====================================================================
取当天 15:50 与 15:55 两个 Heatmap 快照（到期 = 今天之后最近一个有数据的到期，能过夜），用 node_mode="local" 识别，
两个快照都成立才算（避免尾盘单点噪音）。结果写 backtests/local_scan/<日期>.csv。
用法：python3 local_scan.py [--tickers A,B]
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import sys

from archive import fetch_heatmap_rows, live_universe, rows_to_frames
from config import SystemConfig, et_now, expiry_candidates, setup_logging
from detector import detect_structure, parse_heatmap
from yhs_client import YHSClient

OUT = os.path.join("backtests", "local_scan")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    setup_logging("WARNING", None)
    lc = copy.copy(cfg)
    lc.node_mode = "local"
    day = et_now().date()
    if args.tickers:
        tickers = args.tickers.split(",")
    else:
        with open(os.path.join(cfg.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
            tickers = json.load(f)["tickers"]
        tickers += [t for t in (live_universe(day) or []) if t not in tickers]
    live = set(live_universe(day) or [])
    client = YHSClient(cfg)
    rows = []
    for t in tickers:
        for exp in expiry_candidates(day, None, 6)[1:]:
            try:
                raw = fetch_heatmap_rows(client, t, exp, day)
            except Exception as e:   # 无该到期 / 接口报错：换下一个到期
                raw = None
            if not raw:
                continue
            fr = rows_to_frames(t, day, exp, raw)
            sigs = []
            for hm in ("15:50", "15:55"):
                k = next((x for x in fr if x.strftime("%H:%M") == hm), None)
                snap = parse_heatmap(fr[k], cfg, day) if k else None
                sigs.append((snap, detect_structure(snap, lc) if snap else None,
                             detect_structure(snap, cfg) if snap else None))
            (s1, a, g1), (s2, b, g2) = sigs
            if a and b and a.structure == b.structure and a.pos_strike == b.pos_strike:
                rows.append({"date": day.isoformat(), "ticker": t, "in_live": t in live, "expiration": exp,
                             "structure": b.structure, "spot_1555": round(s2.spot, 2), "pos_node": b.pos_strike,
                             "pos_gex_m": round(b.pos_gex / 1e6, 2), "neg_node": b.neg_strike,
                             "neg_gex_m": round(b.neg_gex / 1e6, 2), "dist_pct": round(b.distance_pct * 100, 2),
                             "target_move_pct": round((b.neg_strike / s2.spot - 1) * 100, 2),
                             "pos_share_pct": round(100 * b.pos_gex / (s2.total_abs_gex or 1), 1),
                             "king_view": g2.structure if g2 else "无"})
                print(t, b.structure, exp, "现货", round(s2.spot, 2), "节点", b.pos_strike, "→", b.neg_strike, flush=True)
            break
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{day}.csv"), "w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f"共 {len(rows)} 个；配额剩余 {client.quota_remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
