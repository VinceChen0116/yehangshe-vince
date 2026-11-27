"""
baseline_replay.py —— baseline 规则的真实历史回放（与 baseline 实盘同一套代码：BaselineEngine + BaselineTrader）
===================================================================================================
* 标的：当天实盘 B 账户监控名单（logs/live_<日期>.log；没有就用 212 只存档池）
* Heatmap：本地存档（含当天全部已存档到期，拼成与实盘 snapshot 相同的结构）；标记 T 的快照在 T+5 才用
* 开仓：真实 IV + 真实标的价 BS（或 contract-intraday 真实成交）+ 当时真实 NBBO 半价差；bid 为 0 不开
* 出场：每分钟一次 —— 期权价同上，现价用该合约 1 分钟 greeks 序列里的真实标的价（实盘用 Call/Put 平价反推）
* 2026-11-26 起（config.replay_tick_quotes，与实盘 mark_5s 同口径）：开仓与每分钟盯仓改用当时真实 NBBO（contract-at-time，按 ask 买、
  按 bid 算可卖价，与实盘同一条报价路径）；用 1 分钟成交 K 线 + 1 分钟标的价判断这一分钟会不会碰到出场线，
  会碰到的分钟再按 5 秒逐点查真实 NBBO；标的价只有靠近现货止损 / 目标时才逐点查（stocks/at-time），否则按分钟线性插值。
  实盘 5 秒盯仓的现价用 Call/Put 平价反推，回放用真实标的价，口径差异只在现价来源。
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
from baseline import BaselineEngine, BaselineTrader, minute_needs_ticks
from baseline_frames import merged_frames
from config import SystemConfig, et_datetime, session_close_hhmm, setup_logging
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
    tick_s = cfg.bl_stream_mark_s if (cfg.replay_tick_quotes and cfg.bl_stream_mark_s > 0) else 0
    if tick_s:
        trader.quote_provider = hc.quote_now      # 与实盘同一条报价路径：按 ask 开仓、bid 为 0 不开、价差上限、按 bid 盯仓
        trader.spread_provider = None             # 真实 NBBO 已覆盖，不再用 30 分钟粒度的历史报价估价差
        engine.spot_override = lambda pos, now: hc.underlying_at(pos.contract_symbol, now) or pos.last_spot
    open_t = et_datetime(day, "09:30")
    close_t = et_datetime(day, session_close_hhmm(day))
    n_min = int((close_t - open_t).total_seconds() // 60)
    ticked = {"min": 0, "budget_hit": False}
    for m in range(5, n_min + 1):                 # 09:35 … 收盘，每分钟
        now = open_t + dt.timedelta(minutes=m)
        hc.now = now
        if m % 5 == 0:                            # 标记 now-5 的快照此刻可用
            label = now - dt.timedelta(minutes=5)
            for t, fr in frames.items():
                p = fr.get(label)
                if p is not None:
                    engine.on_snapshot(t, p, now, frame_t=label)
            t_entry = now + dt.timedelta(seconds=cfg.replay_entry_lag_s) if tick_s else now
            hc.now = t_entry                      # 按实盘实际开仓时刻（网格 + 约 85 秒）的真实报价成交
            engine.flush(t_entry)
            hc.now = now
        else:
            if tick_s:   # 每分钟盯仓用的真实报价：缺的话一次并发预取接下来 10 分钟（接口首次请求要等约 5 秒，逐个取太慢；最多多花 9 次）
                for p in trader.open_positions():
                    core = p.contract_symbol.split(":")[-1]
                    if now.strftime("%H:%M:%S") not in hc._tick_cache("optattime", core):
                        hc.prefetch_ticks("optattime", core, f"/v1/options/contract-at-time/{core}",
                                          [now + dt.timedelta(minutes=i) for i in range(10)
                                           if now + dt.timedelta(minutes=i) <= close_t])
            engine.mark_open(now)
        if tick_s and m < n_min:
            tick_minute(cfg, hc, engine, trader, now, tick_s, ticked)
    if tick_s:
        log.info("【回放·5 秒精查】%s：精查 %d 个持仓分钟，逐点查询 %d 次%s", day, ticked["min"], hc.tick_units(),
                 "（达到当日上限，之后只按分钟盯）" if ticked["budget_hit"] else "")
    hc.now = close_t
    finish_day(cfg, engine, trader, real, close_t)
    return trader.positions


def tick_minute(cfg, hc, engine, trader, now: dt.datetime, tick_s: int, ticked: dict) -> None:
    """这一分钟（now, now+1 分钟）里：先用 1 分钟成交 K 线与标的价判断会不会碰到出场线，会 → 按 tick_s 秒逐点用真实 NBBO 盯仓。
    只决定"查不查"这一分钟的逐点数据；每个逐点的出场判断只用该时刻及以前的数据（与实盘一样）。"""
    for p in list(trader.open_positions()):
        st = trader.state.get(p.position_id)
        if st is None or p.price_source != "quote":
            continue
        if hc.tick_units() >= cfg.replay_tick_budget_day:
            ticked["budget_hit"] = True
            return
        bar = hc.bars_1m(p.contract_symbol).get(now)
        if bar is None:
            continue                               # 这一分钟没有成交：报价大概率没动，按分钟盯
        q = hc.option_at(p.contract_symbol, now)
        half = (q["ask"] - q["bid"]) / 2 if q else 0.0
        rois = [((x - half) / p.entry_option_price - 1) * 100 for x in (bar[1], bar[2])] + [p.roi_pct]
        s0 = hc.underlying_at(p.contract_symbol, now) or p.last_spot
        s1 = hc.underlying_at(p.contract_symbol, now + dt.timedelta(minutes=1)) or s0
        held = (now - p.entry_time).total_seconds() / 60
        if not minute_needs_ticks(st, tuple(rois), (s0, s1, p.last_spot), held, trader.ew):
            continue
        ticked["min"] += 1
        ticks = [now + dt.timedelta(seconds=k * tick_s) for k in range(1, 60 // tick_s)
                 if now + dt.timedelta(seconds=k * tick_s) > p.entry_time]
        if not ticks:
            continue
        core = p.contract_symbol.split(":")[-1]
        hc.prefetch_ticks("optattime", core, f"/v1/options/contract-at-time/{core}", ticks)
        # 标的价只有靠近现货止损 / 目标时才逐点查真实值，否则按分钟线性插值（省配额）
        spot_near = bool(s0) and minute_needs_ticks(st, (-1e9, -1e9), (s0, s1, p.last_spot), held, trader.ew,
                                                    band_pct=0.0)
        if spot_near:
            hc.prefetch_ticks("stkattime", p.ticker, f"/v1/stocks/at-time/{p.ticker}", ticks)
        for tk in ticks:
            hc.now = tk
            frac = (tk - now).total_seconds() / 60
            spot = hc.stock_at(p.ticker, tk) if spot_near else (s0 + (s1 - s0) * frac if s0 and s1 else None)
            spot = spot or p.last_spot
            if spot and trader.mark(p, spot, tk):
                engine.last_exit_time[p.ticker] = tk
                break
        hc.now = now


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
