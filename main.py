"""
main.py —— 系统主入口
====================
用法：
  python main.py --check                       # 免费：health + discover，验证 API Key 与剩余配额（0 配额）
  python main.py --replay                      # 模拟回放（本地仿 YHS 服务器，0 配额）
  python main.py --replay --source history --date 2026-10-09 --tickers NVDA,TSLA,AAPL
                                               # 历史回放：用真实 Heatmap 历史（最近 3 个交易日）+ 当日 Flow
  python main.py --live                        # 实盘盘中自适应监控（模拟盘，不下真实订单）
  python main.py --premarket                   # 盘前研究：活跃榜 → Heatmap 关键节点 → Flow 确认，输出观察名单报告

API Key：设置环境变量 YHS_API_KEY，或在 --config 指定的 JSON 中写 "api_key"。
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import logging
import os
import sys
import time

from config import (SystemConfig, et_datetime, et_now, hhmm_to_minutes, minutes_of_day, parse_iso,
                    next_trading_day, poll_interval_min, setup_logging, to_et)
from detector import (HEATMAP_PATH, FLOW_TRADES_PATH, SignalDetector, UniverseEntry, UniverseFilter,
                      extract_rows, pick, pick_num)
from paper_trader import CHAIN_PATH, PaperTrader, SessionEngine, bs_price_greeks
from yhs_client import QuotaExhausted, YHSApiError, YHSClient

log = logging.getLogger("yhs.main")


# =============================================================================
# 公共工具
# =============================================================================
def check_quota(client: YHSClient) -> int | None:
    """免费端点探测剩余配额。"""
    try:
        client.health()
    except YHSApiError as e:
        log.warning("health 检查失败：%s", e)
    try:
        d = client.discover()
        data = d.get("data", d) if isinstance(d, dict) else {}
        rem = pick_num(data, "quota.monthly_remaining", "monthly_remaining", "quota.remaining", "quota_remaining")
        if rem is not None and client.quota_remaining is None:
            client.quota_remaining = int(rem)
        user = pick(data, "user.email", "user.id", "user", default="?")
        log.info("discover：账户=%s，本月剩余配额=%s（免费端点，0 配额）", user if not isinstance(user, dict) else "已识别",
                 client.quota_remaining if client.quota_remaining is not None else rem)
    except YHSApiError as e:
        log.error("discover 失败：%s", e)
        if e.status in (401, 402, 403):
            log.error("请检查 API Key（https://yehangshe.com/app/account/api）与订阅状态")
        raise
    return client.quota_remaining


def trading_days_left_in_month(day: dt.date) -> int:
    last = calendar.monthrange(day.year, day.month)[1]
    return sum(1 for d in range(day.day, last + 1) if dt.date(day.year, day.month, d).weekday() < 5) or 1


def budget_cap(cfg: SystemConfig, quota_remaining: int | None, day: dt.date) -> int:
    """按剩余配额 / 剩余交易日推算今日可承受的标的数。"""
    rem = quota_remaining if quota_remaining is not None else cfg.monthly_quota
    usable = max(0, rem - cfg.quota_reserve)
    per_day = usable / trading_days_left_in_month(day)
    cap = int(per_day * (1 - cfg.flow_chain_budget_share) / max(1, cfg.est_polls_per_ticker_day))
    if rem < cfg.quota_soft_floor:
        cap = cap // 2
        log.warning("剩余配额 %d 低于软阈值 %d，进入节流模式，标的池再减半", rem, cfg.quota_soft_floor)
    log.info("【预算守卫】剩余配额 %d，本月剩余交易日 %d，日预算约 %.0f 次 → 今日最多监控 %d 只（上限 %d）",
             rem, trading_days_left_in_month(day), per_day, cap, cfg.max_universe_size)
    return max(0, min(cfg.max_universe_size, cap))


def sleep_until(target: dt.datetime, reason: str = "") -> None:
    while True:
        now = et_now()
        wait = (target - now).total_seconds()
        if wait <= 0:
            return
        if reason and wait > 120:
            log.info("等待至 %s ET（%s），约 %.0f 分钟", target.strftime("%m-%d %H:%M"), reason, wait / 60)
            reason = ""
        time.sleep(min(wait, 60))


def next_weekday(d: dt.date) -> dt.date:
    d = d + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def finish_day(cfg: SystemConfig, engine: SessionEngine, trader: PaperTrader, client: YHSClient,
               when: dt.datetime, reason: str = "EOD_FORCE_CLOSE") -> None:
    if reason == "EOD_FORCE_CLOSE":
        engine.end_of_day(when)
    else:
        trader.force_close_all(when, engine.last_spot, reason)
    trader.print_report()
    if any(not p.is_open for p in trader.positions):
        trader.export_trades_csv(cfg.trades_csv, append=True)
    log.info("检测统计：%s", engine.detector.stats)
    log.info(client.quota_summary())


# =============================================================================
# 实盘模式
# =============================================================================
def run_live(cfg: SystemConfig) -> None:
    client = YHSClient(cfg)
    log.info("实盘监控启动（模拟盘）。API Key=%s，节奏：早盘/尾盘 %d 分钟、午间 %d 分钟，限频 %d RPM / 间隔 %.1fs / 并发 %d",
             cfg.masked_key(), cfg.fast_poll_min, cfg.slow_poll_min, cfg.safe_rpm, cfg.min_request_interval_s,
             cfg.max_in_flight)
    check_quota(client)

    while True:
        now = et_now()
        day = now.date()
        if day.weekday() >= 5:
            sleep_until(et_datetime(next_weekday(day), cfg.universe_fetch_time), "周末休市")
            continue
        close_t = et_datetime(day, cfg.session_close)
        if now >= close_t:
            sleep_until(et_datetime(next_weekday(day), cfg.universe_fetch_time), "今日已收盘")
            continue
        fetch_t = et_datetime(day, cfg.universe_fetch_time)
        if now < fetch_t:
            sleep_until(fetch_t, "等待拉取当日标的池")

        try:
            run_live_day(cfg, client, day)
        except QuotaExhausted as e:
            log.critical("配额熔断，停止实盘监控：%s", e)
            return
        sleep_until(et_datetime(next_weekday(day), cfg.universe_fetch_time), "等待下一交易日")


def run_live_day(cfg: SystemConfig, client: YHSClient, day: dt.date) -> None:
    log.info("=" * 78)
    log.info("交易日 %s 开始", day)
    universe = UniverseFilter(cfg).build(client, day)
    cap = budget_cap(cfg, client.quota_remaining, day)
    universe = universe[:cap]
    if not universe:
        log.warning("今日标的池为空（或预算不足），跳过")
        return
    log.info("今日监控 %d 只：%s", len(universe), ", ".join(e.ticker for e in universe))
    detector = SignalDetector(cfg, universe)
    trader = PaperTrader(cfg)
    engine = SessionEngine(cfg, client, detector, trader)
    pool = [e.ticker for e in universe]
    last_poll: dict[str, dt.datetime] = {}
    open_t = et_datetime(day, cfg.session_open)
    close_t = et_datetime(day, cfg.force_close_time)
    session_verified = False

    try:
        sleep_until(open_t + dt.timedelta(seconds=cfg.poll_offset_s), "等待开盘")
        while True:
            now = et_now()
            if now >= close_t:
                break
            # 对齐到 5 分钟采样网格
            grid = open_t + dt.timedelta(minutes=5 * ((minutes_of_day(now) - minutes_of_day(open_t)) // 5))
            interval = poll_interval_min(cfg, grid)
            for t in list(pool):
                if t in detector.rejected_spot:
                    pool.remove(t)
                    continue
                has_pos = trader.open_for(t) is not None and cfg.poll_open_positions_fast
                lp = last_poll.get(t)
                if not has_pos and lp is not None and (grid - lp).total_seconds() < interval * 60:
                    continue
                if lp is not None and lp >= grid:
                    continue
                try:
                    payload = client.get(HEATMAP_PATH.format(ticker=t))
                except QuotaExhausted:
                    raise
                except YHSApiError as e:
                    if e.code == "NOT_FOUND":
                        log.warning("%s 不在 Heatmap 服务范围（404），移出标的池", t)
                        pool.remove(t)
                    else:
                        log.warning("%s Heatmap 拉取失败：%s", t, e)
                    continue
                last_poll[t] = grid
                if not session_verified:
                    sd = (payload.get("data") or {}).get("session_date_et")
                    if sd and sd != day.isoformat():
                        log.warning("Heatmap 会话日期 %s ≠ 今日 %s，判定今日休市（节假日），结束当日", sd, day)
                        return
                    session_verified = True
                engine.on_snapshot(t, payload, et_now())
            nxt = grid + dt.timedelta(minutes=5, seconds=cfg.poll_offset_s)
            log.info("[%s] 本轮完成：持仓 %d 笔，监控 %d 只，配额剩余 %s；下一轮 %s",
                     et_now().strftime("%H:%M:%S"), len(trader.open_positions()), len(pool),
                     client.quota_remaining, nxt.strftime("%H:%M:%S"))
            sleep_until(min(nxt, close_t))
        finish_day(cfg, engine, trader, client, close_t)
    except QuotaExhausted:
        finish_day(cfg, engine, trader, client, et_now(), "QUOTA_HALT")
        raise
    except KeyboardInterrupt:
        log.warning("收到中断信号，按最新现货清平所有模拟持仓并导出")
        finish_day(cfg, engine, trader, client, et_now(), "MANUAL_STOP")
        raise


# =============================================================================
# 历史回放（真实数据）
# =============================================================================
class HistoryClient:
    """历史回放的数据代理：
       * Heatmap：预先按 /v1/derived/heatmap/{t}/history 分页拉取整日 5 分钟快照，回放时本地供给（0 额外配额）
       * Flow：每只标的首次查询时一次性分页拉取当日 ≥$50k 大单并缓存，按回放时刻本地过滤（避免"未来函数"）
       * 期权链：历史时点无快照 → 抛错使交易器回退到 BS 现货换算（0 配额）
    """

    def __init__(self, real: YHSClient, cfg: SystemConfig, date: dt.date, max_flow_pages: int = 20):
        self.real, self.cfg, self.date = real, cfg, date
        self.now: dt.datetime = et_datetime(date, "09:30")
        self.frames: dict[str, dict[dt.datetime, dict]] = {}
        self.flow_cache: dict[str, list[dict]] = {}
        self.max_flow_pages = max_flow_pages
        self.intraday: dict[str, list[tuple[dt.datetime, float]]] = {}       # 合约 → [(bar 开始时间 ET, close)]
        self.greeks: dict[str, list[tuple[dt.datetime, float, float]]] = {}  # 合约 → [(时间 ET, 标的价, IV)]
        self.intraday_unavailable = False   # 回放日不是最近交易日时，contract-intraday 无数据，首次发现后不再请求

    def load_heatmap(self, ticker: str, expiration: str) -> int:
        frames, cursor, pages = {}, None, 0
        while True:
            # from/to 只传日期时服务端返回空集，必须传带时区的完整时间
            params = {"expiration": expiration,
                      "from": et_datetime(self.date, "09:30").isoformat(),
                      "to": et_datetime(self.date, "16:00").isoformat(),
                      "limit": 24, "cursor": cursor}
            payload = self.real.get(f"/v1/derived/heatmap/{ticker}/history", params)
            pages += 1
            for row in extract_rows(payload):
                ts = parse_iso(row.get("minute_at"))
                if ts is None:
                    continue
                et = to_et(ts)
                frames[et.replace(second=0, microsecond=0)] = {"data": {
                    "ticker": ticker, "generated_at": row.get("minute_at"),
                    "session_date_et": self.date.isoformat(), "spot_usd": row.get("spot_usd"),
                    "expirations": [row.get("expiration") or expiration],
                    "cells": [{"strike_usd": c.get("strike_usd"), "expiration": row.get("expiration") or expiration,
                               "net_dealer_gex_usd": c.get("net_dealer_gex_usd")} for c in row.get("cells") or []],
                }}
            cursor = (payload.get("_meta") or {}).get("next_cursor") if isinstance(payload, dict) else None
            if not cursor or pages >= 10:
                break
        self.frames[ticker] = frames
        log.info("【历史】%s 到期 %s 载入 %d 个 5 分钟快照（%d 页）", ticker, expiration, len(frames), pages)
        return len(frames)

    def _load_intraday(self, core: str) -> list[tuple[dt.datetime, float]]:
        """contract-intraday：单合约最近一个交易日的 1 分钟成交 bar（接口只给最新 150 根，约收盘前 2.5 小时）。"""
        if core in self.intraday:
            return self.intraday[core]
        bars: list[tuple[dt.datetime, float]] = []
        if self.intraday_unavailable:
            self.intraday[core] = bars
            return bars
        try:
            payload = self.real.get(f"/v1/options/contract-intraday/{core}")
            data = payload.get("data") if isinstance(payload, dict) else None
            for b in (data or {}).get("bars") or []:
                ts, px = parse_iso(b.get("start_time")), b.get("close_usd")
                if ts is not None and px is not None and float(px) > 0:
                    bars.append((to_et(ts), float(px)))
            bars.sort()
            if bars and bars[-1][0].date() != self.date:
                log.info("【历史】%s 分钟线为 %s（该接口仅含最近交易日），本次回放不再请求，全部改用真实 IV 定价",
                         core, bars[-1][0].date())
                self.intraday_unavailable = True
                bars = []
            elif bars:
                log.info("【历史】%s 真实成交 bar %d 根（%s–%s）", core, len(bars),
                         bars[0][0].strftime("%H:%M"), bars[-1][0].strftime("%H:%M"))
        except QuotaExhausted:
            raise
        except YHSApiError as e:
            log.warning("【历史】%s 分钟线获取失败：%s", core, e)
        self.intraday[core] = bars
        return bars

    def _load_greeks(self, core: str) -> list[tuple[dt.datetime, float, float]]:
        """contract-greeks-series：全天 5 分钟 (时间 ET, 标的价, IV)。snapshot_at 为不带时区的美东时间。"""
        if core in self.greeks:
            return self.greeks[core]
        rows: list[tuple[dt.datetime, float, float]] = []
        try:
            d = self.date.isoformat()
            payload = self.real.get(f"/v1/options/contract-greeks-series/{core}",
                                    {"from": d, "to": d, "interval": "5m"})
            data = payload.get("data") if isinstance(payload, dict) else None
            for r in (data or {}).get("rows") or []:
                ts = r.get("snapshot_at")
                S, iv, delta = r.get("underlying_price_usd"), r.get("implied_vol_pct"), r.get("delta")
                if not ts or not S or not iv or delta is None:
                    continue
                if abs(float(delta)) < 0.001:   # 开盘首个点常为未成形的异常值（δ≈0、IV 失真）
                    continue
                rows.append((et_datetime(self.date, ts[11:16]), float(S), float(iv)))
            rows.sort()
            log.info("【历史】%s 真实 IV 序列 %d 个 5 分钟点", core, len(rows))
        except QuotaExhausted:
            raise
        except YHSApiError as e:
            log.warning("【历史】%s Greeks 序列获取失败：%s", core, e)
        self.greeks[core] = rows
        return rows

    def option_price(self, contract: str, t: dt.datetime) -> float | None:
        """t 时刻的期权价格（只用 t 及以前的数据）：
           1) 5 分钟内有真实成交 bar → 用成交价；
           2) 否则用 15 分钟内最近的 greeks 点：BS(真实标的价, 真实 IV)，实测与成交价误差约 1–2 美分；
           3) 都没有 → None（交易器回退估算）。"""
        core = contract.split(":")[-1]
        last = None
        for ts, px in self._load_intraday(core):
            if ts > t:
                break
            last = (ts, px)
        if last is not None and (t - last[0]).total_seconds() <= 5 * 60:
            return last[1]
        g = None
        for row in self._load_greeks(core):
            if row[0] > t:
                break
            g = row
        if g is None or (t - g[0]).total_seconds() > 15 * 60:
            return None
        _, S, iv = g
        right, K = core[-9], int(core[-8:]) / 1000
        exp = dt.datetime.strptime(core[-15:-9], "%y%m%d").date()
        T = ((exp - t.date()).days + max(0, 16 * 60 - t.hour * 60 - t.minute) / (24 * 60)) / 365
        px, _, _ = bs_price_greeks(S, K, T, self.cfg.risk_free_rate, iv, right == "C")
        return px

    def frame_at(self, ticker: str, t: dt.datetime) -> dict | None:
        return self.frames.get(ticker, {}).get(t.replace(second=0, microsecond=0))

    def get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if path.startswith("/v1/options/chain-snapshot/"):
            raise YHSApiError(0, "HISTORY_NO_CHAIN", "历史回放不使用实时期权链，改用 BS 换算", path)
        if path == FLOW_TRADES_PATH:
            t = params.get("tickers", "")
            if t not in self.flow_cache:
                rows, cursor, pages = [], None, 0
                while pages < self.max_flow_pages:
                    p = {"tickers": t, "kind": "order", "min_premium_usd": params.get("min_premium_usd"),
                         "session_date": self.date.isoformat(), "sort_by": "occurredAt", "direction": "asc",
                         "limit": 100, "cursor": cursor}
                    payload = self.real.get(FLOW_TRADES_PATH, p)
                    pages += 1
                    rows += extract_rows(payload)
                    cursor = (payload.get("_meta") or {}).get("next_cursor")
                    if not cursor:
                        break
                self.flow_cache[t] = rows
                log.info("【历史】%s 当日 ≥$%s 大单 %d 笔已缓存（%d 页）", t, params.get("min_premium_usd"), len(rows), pages)
            out = []
            for r in self.flow_cache[t]:
                ts = parse_iso(pick(r, "occurred_at", "occurredAt", "executed_at", "timestamp", "time"))
                if ts is not None and to_et(ts) <= self.now:
                    out.append(r)
            return {"data": out, "_meta": {"next_cursor": None}}
        if "lookup" in path:
            params.setdefault("session_date", self.date.isoformat())
        return self.real.get(path, params)


def run_history_replay(cfg: SystemConfig, date: dt.date, tickers: list[str], expiration: str | None) -> dict:
    real = YHSClient(cfg)
    check_quota(real)
    # 到期日：有 0DTE 用 0DTE，否则 1DTE（交易日口径）；--expiration 可强制指定
    candidates = [expiration] if expiration else [date.isoformat(), next_trading_day(date).isoformat()]
    hc = HistoryClient(real, cfg, date)
    cfg.pcr_direction_filter = False  # 历史模式无当日榜单 PCR
    universe = [UniverseEntry(t, i + 1, 0, 0, 0, None, None, None, None, "NEUTRAL", 0.0, ["手动指定"])
                for i, t in enumerate(tickers)]
    loaded = []
    for t in tickers:
        for exp in candidates:
            try:
                if hc.load_heatmap(t, exp):
                    loaded.append(t)
                    break
            except YHSApiError as e:
                log.warning("%s 到期 %s 历史 Heatmap 载入失败：%s（Heatmap 历史仅保留最近 3 个交易日）", t, exp, e)
        else:
            log.info("【历史】%s 在 %s 没有 0DTE/1DTE 到期（%s），跳过该标的", t, date, "、".join(candidates))
    if not loaded:
        log.error("没有可回放的标的")
        return {}
    detector = SignalDetector(cfg, [u for u in universe if u.ticker in loaded])
    trader = PaperTrader(cfg)
    if cfg.use_real_option_pricing:
        trader.price_provider = hc.option_price
    engine = SessionEngine(cfg, hc, detector, trader)
    last_poll: dict[str, dt.datetime] = {}
    for i in range(78):
        now = et_datetime(date, "09:30") + dt.timedelta(minutes=5 * i)
        hc.now = now
        interval = poll_interval_min(cfg, now)
        for t in loaded:
            if t in detector.rejected_spot:
                continue
            has_pos = trader.open_for(t) is not None
            lp = last_poll.get(t)
            if not has_pos and lp is not None and (now - lp).total_seconds() < interval * 60:
                continue
            frame = hc.frame_at(t, now)
            if frame is None:
                continue
            last_poll[t] = now
            engine.on_snapshot(t, frame, now)
    finish_day(cfg, engine, trader, real, et_datetime(date, "16:00"))
    return trader.stats()


# =============================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="夜航社 Dealer GEX 异动期权捕捉与模拟复盘系统")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="实盘盘中自适应监控（模拟盘）")
    mode.add_argument("--replay", action="store_true", help="回放（默认本地模拟数据，0 配额）")
    mode.add_argument("--check", action="store_true", help="免费检查 API Key 与剩余配额")
    mode.add_argument("--premarket", action="store_true", help="盘前研究工作流：生成多头/空头/等待观察名单")
    ap.add_argument("--source", choices=["mock", "history"], default="mock", help="回放数据源")
    ap.add_argument("--date", help="历史回放日期 YYYY-MM-DD（最近 3 个交易日内）")
    ap.add_argument("--tickers", help="历史回放标的，逗号分隔，如 NVDA,TSLA")
    ap.add_argument("--expiration", help="历史回放使用的到期日（默认有 0DTE 用 0DTE，否则 1DTE）")
    ap.add_argument("--config", help="JSON 配置文件（可覆盖 SystemConfig 任意字段）")
    ap.add_argument("--now", help="盘前研究：覆盖当前美东时间（YYYY-MM-DDTHH:MM，仅影响时间判断，数据仍为实时）")
    ap.add_argument("--no-flow", action="store_true", help="盘前研究：不调用 Flow 能力")
    ap.add_argument("--replay-audit", help="盘前研究：用 yhs_data/premarket/<run> 审计目录离线重放（0 配额，便于调参）")
    ap.add_argument("--csv", help="交易明细输出路径（默认 trades_history.csv）")
    ap.add_argument("--log-level", default=None)
    ap.add_argument("--log-file", default=None)
    args = ap.parse_args(argv)

    cfg = SystemConfig.load(args.config)
    if args.csv:
        cfg.trades_csv = args.csv
    setup_logging(args.log_level or cfg.log_level, args.log_file)

    try:
        if args.check:
            check_quota(YHSClient(cfg))
            return 0
        if args.replay and args.source == "mock":
            from mock_replay import run_mock_replay
            run_mock_replay(cfg, cfg.trades_csv)
            return 0
        if args.premarket and args.replay_audit:
            from premarket_workflow import AuditReplayClient, run_premarket
            client = AuditReplayClient(args.replay_audit)
            now = et_datetime(dt.date.fromisoformat(args.now[:10]), args.now[11:16]) if args.now else client.recorded_now()
            log.info("盘前研究离线重放报告已生成：%s", run_premarket(cfg, client, now, use_flow=not args.no_flow))
            return 0
        if not cfg.api_key:
            log.error("未找到 API Key：请设置环境变量 YHS_API_KEY（在 https://yehangshe.com/app/account/api 生成），"
                      "或在 --config JSON 中填写 api_key")
            return 2
        if args.premarket:
            from premarket_workflow import run_premarket
            now = et_datetime(dt.date.fromisoformat(args.now[:10]), args.now[11:16]) if args.now else None
            tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
            report = run_premarket(cfg, YHSClient(cfg), now, tickers, use_flow=not args.no_flow)
            log.info("盘前研究报告已生成：%s", report)
            return 0
        if args.replay:
            if not (args.date and args.tickers):
                log.error("历史回放需要 --date 与 --tickers")
                return 2
            run_history_replay(cfg, dt.date.fromisoformat(args.date),
                               [t.strip().upper() for t in args.tickers.split(",") if t.strip()], args.expiration)
            return 0
        run_live(cfg)
        return 0
    except KeyboardInterrupt:
        log.warning("用户中断，退出")
        return 130
    except QuotaExhausted as e:
        log.critical("配额熔断：%s", e)
        return 3
    except YHSApiError as e:
        log.critical("API 错误：%s", e)
        return 4


if __name__ == "__main__":
    sys.exit(main())
