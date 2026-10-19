"""
tape_archive.py —— "跟大单"候选的数据存档（Flow 全市场大单 + 对应到期日的 Heatmap）
=====================================================================================
服务端 Heatmap 历史只保留 3 个交易日，且我们的常规存档只有近端到期（0DTE/1DTE/最近一个）。
dennis-tape 式策略跟的是 2–75 天到期的合约，所以每天收盘后：
  1. 拉全市场 Ask 侧（A/AA）单笔 ≥ $15 万 的大单（约 190 次配额）→ yhs_data/archive/tape_flow/<日期>.json
  2. 筛出候选：到期 2–75 天、价外 0–8%、当日量 > OI
  3. 候选涉及的（标的, 到期日）组合，存当天全天 Heatmap → yhs_data/archive/heatmap_tape/<日期>/<标的>/<到期>.json
     （已在常规存档里的跳过；每个组合约 4 次配额）
用法：python3 tape_archive.py [--date YYYY-MM-DD ...] [--max-pairs 300]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

from archive import _write_json, archive_root, fetch_heatmap_rows, heatmap_path, latest_closed_session, recent_sessions
from config import SystemConfig, et_now, setup_logging
from detector import FLOW_TRADES_PATH, extract_rows
from yhs_client import QuotaExhausted, YHSClient

log = logging.getLogger("yhs.tape_archive")
MIN_PREMIUM = 150000
DTE_RANGE = (2, 75)
OTM_MAX = 0.08


def flow_path(cfg, day: dt.date) -> str:
    return os.path.join(archive_root(cfg), "tape_flow", f"{day}.json")


def tape_heatmap_path(cfg, day: dt.date, ticker: str, exp: str) -> str:
    return os.path.join(archive_root(cfg), "heatmap_tape", day.isoformat(), ticker, f"{exp}.json")


def load_flow(client, cfg, day: dt.date) -> list:
    fp = flow_path(cfg, day)
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    rows, cursor = [], None
    for _ in range(400):
        p = {"kind": "order", "min_premium_usd": MIN_PREMIUM, "sides": "A,AA", "session_date": day.isoformat(),
             "sort_by": "occurredAt", "direction": "asc", "limit": 25, "cursor": cursor}
        payload = client.get(FLOW_TRADES_PATH, p)
        rows += extract_rows(payload)
        cursor = (payload.get("_meta") or {}).get("next_cursor") if isinstance(payload, dict) else None
        if not cursor:
            break
    keep = ("occurred_at", "contract_symbol", "premium_usd", "side", "has_sweep", "session_volume", "open_interest",
            "min_dte", "min_moneyness_pct", "right")
    rows = [{k: r.get(k) for k in keep} for r in rows]
    _write_json(fp, rows)
    log.info("【tape】%s 全市场 Ask 侧 ≥$%dK 大单 %d 笔", day, MIN_PREMIUM // 1000, len(rows))
    return rows


def is_candidate(x: dict) -> bool:
    d, m = x.get("min_dte"), float(x.get("min_moneyness_pct") or 0)
    return (d is not None and DTE_RANGE[0] <= d <= DTE_RANGE[1] and 0 <= m <= OTM_MAX
            and (x.get("session_volume") or 0) > (x.get("open_interest") or 0))


def parse_occ(sym: str):
    core = str(sym).split(":")[-1]
    return core[:-15], f"20{core[-15:-13]}-{core[-13:-11]}-{core[-11:-9]}", core[-9], int(core[-8:]) / 1000


def archive_tape_day(client, cfg, day: dt.date, max_pairs: int = 300) -> dict:
    flow = load_flow(client, cfg, day)
    cands = [x for x in flow if is_candidate(x)]
    pairs = {}
    for x in cands:   # 同一（标的, 到期）按大单金额合计排序，配额不够时先存大的
        t, exp, _, _ = parse_occ(x["contract_symbol"])
        pairs[(t, exp)] = pairs.get((t, exp), 0) + float(x.get("premium_usd") or 0)
    order = sorted(pairs, key=lambda k: -pairs[k])[:max_pairs]
    new = skipped = failed = 0
    for t, exp in order:
        fp = tape_heatmap_path(cfg, day, t, exp)
        if os.path.exists(fp) or os.path.exists(heatmap_path(cfg, day, t, exp)):
            skipped += 1
            continue
        try:
            rows = fetch_heatmap_rows(client, t, exp, day)
        except QuotaExhausted:
            raise
        except Exception as e:   # 该标的没有 Heatmap（不在夜航社覆盖内）等
            log.debug("【tape】%s %s %s Heatmap 获取失败：%s", day, t, exp, e)
            failed += 1
            _write_json(fp, [])
            continue
        _write_json(fp, rows)
        new += 1
    log.info("【tape】%s 候选大单 %d 笔，（标的, 到期）%d 组（存前 %d 组）：新存 %d、已有 %d、无数据 %d",
             day, len(cands), len(pairs), len(order), new, skipped, failed)
    return {"candidates": len(cands), "pairs": len(pairs), "new": new, "skipped": skipped, "failed": failed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跟大单候选数据存档")
    ap.add_argument("--date", action="append", help="指定交易日（可重复）；默认最近 3 个已收盘交易日")
    ap.add_argument("--max-pairs", type=int, default=300)
    ap.add_argument("--log-file")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    setup_logging(cfg.log_level, args.log_file)
    client = YHSClient(cfg)
    days = ([dt.date.fromisoformat(d) for d in args.date] if args.date
            else recent_sessions(latest_closed_session(et_now()), 3))
    try:
        for d in days:
            archive_tape_day(client, cfg, d, args.max_pairs)
    except QuotaExhausted as e:
        log.critical("配额熔断：%s", e)
        return 3
    log.info("配额剩余 %s", client.quota_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
