"""
baseline_replay.py —— baseline 规则的真实历史回放（与 baseline 实盘同一套代码：BaselineEngine + BaselineTrader）
===================================================================================================
* 标的：当天实盘 B 账户监控名单（logs/live_<日期>.log；没有就用 212 只存档池）
* Heatmap：本地存档（含当天全部已存档到期，拼成与实盘 snapshot 相同的结构）；标记 T 的快照在 T+5 才用
* 开仓：真实 IV + 真实标的价 BS（或 contract-intraday 真实成交）+ 当时真实 NBBO 半价差；bid 为 0 不开
* 出场：每分钟一次 —— 期权价同上，现价用该合约 1 分钟 greeks 序列里的真实标的价（实盘用 Call/Put 平价反推）
用法：python3 baseline_replay.py [--dates 2026-10-14,2026-10-15] [--name bl_base] [--set key=value ...]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

from archive import live_universe
from baseline import BaselineEngine, BaselineTrader
from baseline_frames import merged_frames
from config import SystemConfig, et_datetime, setup_logging
from detector import SignalDetector
from experiment import archived_dates, breakdown, metrics, parse_value
from main import HistoryClient, finish_day
from yhs_client import YHSClient

log = logging.getLogger("yhs.baseline_replay")
EXP_DIR = os.path.join("backtests", "exp")


def replay_day(cfg, real, day: dt.date, tickers: list, notifier=None) -> list:
    hc = HistoryClient(real, cfg, day)
    frames = {}
    for t in tickers:
        f = merged_frames(cfg, day, t)
        if f:
            frames[t] = f
    log.info("%s：%d 只有存档（共 %d 只）", day, len(frames), len(tickers))
    trader = BaselineTrader(cfg)
    trader.notifier = notifier                    # --notify：按实盘格式把开平仓推到 Discord（样式预览）
    trader.price_provider = hc.option_price
    trader.delta_provider = hc.option_delta
    trader.spread_provider = hc.hist_quote
    engine = BaselineEngine(cfg, hc, SignalDetector(cfg, []), trader)
    engine.spot_override = lambda pos, now: hc.underlying_at(pos.contract_symbol, now)
    open_t = et_datetime(day, "09:30")
    for m in range(5, 391):                       # 09:35 … 16:00，每分钟
        now = open_t + dt.timedelta(minutes=m)
        hc.now = now
        if m % 5 == 0:                            # 标记 now-5 的快照此刻可用
            label = now - dt.timedelta(minutes=5)
            for t, fr in frames.items():
                p = fr.get(label)
                if p is not None:
                    engine.on_snapshot(t, p, now, frame_t=label)
            engine.flush(now)
        else:
            engine.mark_open(now)
    finish_day(cfg, engine, trader, real, et_datetime(day, "16:00"))
    return trader.positions


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="baseline 规则真实历史回放")
    ap.add_argument("--dates")
    ap.add_argument("--name", default="bl_base")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--tickers")
    ap.add_argument("--notify", action="store_true", help="按实盘格式推送开平仓到 Discord（环境变量 DISCORD_WEBHOOK_URL）")
    args = ap.parse_args(argv)
    out = os.path.join(EXP_DIR, args.name)
    os.makedirs(out, exist_ok=True)
    for fn in os.listdir(out):
        os.remove(os.path.join(out, fn))
    base = SystemConfig.load(None)
    setup_logging(base.log_level, os.path.join(out, "run.log"))
    dates = args.dates.split(",") if args.dates else archived_dates(base)
    real = YHSClient(base)
    with open(os.path.join(base.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
        pool = json.load(f)["tickers"]
    for ds in dates:
        day = dt.date.fromisoformat(ds)
        cfg = SystemConfig.load(None)
        cfg.flow_record = False                   # 回放不查 Flow（省配额；Flow 只记录不决策）
        for item in args.set:
            k, v = item.split("=", 1)
            setattr(cfg, k.strip(), parse_value(v.strip()))
        cfg.trades_csv = os.path.join(out, f"trades_{ds}.csv")
        tickers = args.tickers.split(",") if args.tickers else (live_universe(day) or pool)
        notifier = None
        if args.notify:
            from notify import from_config
            notifier = from_config(cfg)
            if notifier is None:
                log.warning("没有配置 Discord Webhook，不推送")
            else:
                notifier = notifier.tagged(f"🧪【回放 {ds}】")
                notifier.text(f"以下是 {ds} baseline 规则真实回放，按实盘格式推送开平仓（时间 = 数据实际时刻，每笔约 $650）。")
        replay_day(cfg, real, day, tickers, notifier)
    from experiment import load_trades
    rows = load_trades(args.name)
    summary = {"name": args.name, "rules": "baseline", "dates": dates, "overrides": args.set,
               "overall": metrics(rows), "breakdown": breakdown(rows)}
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary["overall"], ensure_ascii=False))
    log.info("配额剩余 %s", real.quota_remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
