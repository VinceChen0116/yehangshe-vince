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
import copy
import json
import datetime as dt
import logging
import os
import sys
import time

from config import (SystemConfig, entry_blocked_window, et_datetime, et_now, expiry_candidates, hhmm_to_minutes,
                    minutes_of_day, parse_iso, poll_interval_min, setup_logging, to_et)
from detector import (HEATMAP_PATH, FLOW_TRADES_PATH, SignalDetector, UniverseEntry, UniverseFilter,
                      extract_rows, is_strict, pick, pick_num)
from archive import get_heatmap_rows, rows_to_frames
from notify import TAG_A, TAG_B, DiscordNotifier, from_config as notify_from_config
from paper_trader import CHAIN_PATH, PaperTrader, SessionEngine, bs_price_greeks
from yhs_client import QuotaExhausted, YHSApiError, YHSClient

log = logging.getLogger("yhs.main")
HIST_QUOTE_MAX_SPREAD = 0.5   # 回放历史 NBBO：价差 > 中间价 50% 视为坏报价丢弃（用 10/20–10/26 实盘真实价差校准：平均误差 6.2 → 2.3 个百分点）


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
    if trader.notifier:
        trader.notifier.daily_summary(when.date(), trader.stats(), trader.positions)
    if any(not p.is_open for p in trader.positions):
        trader.export_trades_csv(cfg.trades_csv, append=True)
    log.info("检测统计：%s", engine.detector.stats)
    log.info(client.quota_summary())


# =============================================================================
# 实盘模式
# =============================================================================
def run_live(cfg: SystemConfig, one_day: bool = False) -> None:
    """one_day=True：只跑今天这一个交易日，收盘（或判定休市）后退出——给云端每日定时任务用。"""
    client = YHSClient(cfg)
    log.info("实盘监控启动（模拟盘）。API Key=%s，节奏：早盘/尾盘 %d 分钟、午间 %d 分钟，限频 %d RPM / 间隔 %.1fs / 并发 %d",
             cfg.masked_key(), cfg.fast_poll_min, cfg.slow_poll_min, cfg.safe_rpm, cfg.min_request_interval_s,
             cfg.max_in_flight)
    check_quota(client)
    notifier = notify_from_config(cfg)
    if notifier:
        notifier.text(f"🚀 实盘监控（模拟盘）已启动，本月剩余配额 {client.quota_remaining}。"
                      f"开仓 {cfg.no_new_entries_before}–{cfg.no_new_entries_after} ET"
                      + ("，" + "、".join(f"{a}–{b}" for a, b in cfg.no_entry_windows) + " 不开新仓" if cfg.no_entry_windows else ""))
    else:
        log.info("未配置 discord_webhook_url，不推送 Discord")

    while True:
        now = et_now()
        day = now.date()
        if day.weekday() >= 5:
            if one_day:
                log.info("今天是周末，退出")
                return
            sleep_until(et_datetime(next_weekday(day), cfg.universe_fetch_time), "周末休市")
            continue
        close_t = et_datetime(day, cfg.session_close)
        if now >= close_t:
            if one_day:
                log.info("今日已收盘，退出")
                return
            sleep_until(et_datetime(next_weekday(day), cfg.universe_fetch_time), "今日已收盘")
            continue
        fetch_t = et_datetime(day, cfg.universe_fetch_time)
        if now < fetch_t:
            sleep_until(fetch_t, "等待拉取当日标的池")

        try:
            run_live_day(cfg, client, day, notifier)
        except QuotaExhausted as e:
            log.critical("配额熔断，停止实盘监控：%s", e)
            if notifier:
                notifier.text(f"⛔ 配额熔断，实盘监控已停止：{e}")
            return
        except KeyboardInterrupt:
            if notifier:
                notifier.text("⏹️ 实盘监控已手动停止。")
            raise
        except Exception as e:
            log.exception("实盘监控异常退出")
            if notifier:
                notifier.text(f"💥 实盘监控异常退出：{type(e).__name__}: {e}")
            raise
        if one_day:
            log.info("单日模式：今日结束，退出")
            return
        sleep_until(et_datetime(next_weekday(day), cfg.universe_fetch_time), "等待下一交易日")


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class QuoteBook:
    """实盘期权实时报价（/v1/options/quote(s)，每个返回的报价 1 配额）。
    每轮开始时用批量接口一次拉齐所有持仓合约（≤50 个/次）；本轮新开仓的合约单独拉一次。同一轮内重复读取走缓存。"""
    BATCH = 50

    def __init__(self, client):
        self.client = client
        self.cache: dict = {}
        self.calls = 0

    @staticmethod
    def _core(sym: str) -> str:
        return sym.split(":")[-1]   # 接口要求纯 OCC 代码，不带 "O:" 前缀

    def _store(self, q: dict) -> None:
        bid, ask, mid = _num(q.get("bid_usd")), _num(q.get("ask_usd")), _num(q.get("mid_usd"))
        if ask is None or ask <= 0:
            return
        bid = bid or 0.0
        self.cache[q.get("contract_symbol")] = {"bid": bid, "ask": ask, "mid": mid if mid else (bid + ask) / 2,
                                                "quoted_at": q.get("quoted_at")}

    def new_round(self, symbols) -> None:
        self.cache = {}
        cores = sorted({self._core(s) for s in symbols})
        for i in range(0, len(cores), self.BATCH):
            try:
                p = self.client.get("/v1/options/quotes", {"contracts": ",".join(cores[i:i + self.BATCH])})
                self.calls += 1
            except QuotaExhausted:
                raise
            except YHSApiError as e:
                log.warning("持仓报价拉取失败：%s", e)
                continue
            data = p.get("data") if isinstance(p, dict) else None
            for q in (data or {}).get("quotes") or []:
                self._store(q)
            for c in cores[i:i + self.BATCH]:
                self.cache.setdefault(c, None)   # 无报价：本轮不再重复请求

    def get(self, sym: str):
        core = self._core(sym)
        if core not in self.cache:
            self.cache[core] = None
            try:
                p = self.client.get(f"/v1/options/quote/{core}")
                self.calls += 1
                d = p.get("data") if isinstance(p, dict) else None
                if isinstance(d, dict):
                    self._store(d)
            except QuotaExhausted:
                raise
            except YHSApiError as e:
                log.info("%s 实时报价不可用：%s", core, e)
        return self.cache.get(core)


class LiveAccount:
    """实盘里的一套独立模拟账户（A 或 B）：自己的标的、检测器、持仓上限、交易明细与 Discord 标签。"""

    def __init__(self, name: str, cfg: SystemConfig, client, universe: list, notifier, tag: str):
        self.name, self.cfg, self.tag = name, cfg, tag
        self.tickers = {e.ticker for e in universe}
        self.detector = SignalDetector(cfg, universe)
        if cfg.intraday_rules == "baseline":
            from baseline import BaselineEngine, BaselineTrader
            self.trader = BaselineTrader(cfg)
            self.trader.notifier = notifier.tagged(tag) if notifier else None
            self.engine = BaselineEngine(cfg, client, self.detector, self.trader)
        else:
            self.trader = PaperTrader(cfg)
            self.trader.notifier = notifier.tagged(tag) if notifier else None
            self.engine = SessionEngine(cfg, client, self.detector, self.trader)

    def status(self) -> str:
        pos = self.trader.open_positions()
        done = [p for p in self.trader.positions if not p.is_open]
        return (f"{self.tag} 持仓 {len(pos)}" + ("（" + "，".join(f"{p.ticker} {p.roi_pct:+.0f}%" for p in pos) + "）" if pos else "")
                + f"，已平 {len(done)} 笔 ${sum(p.realized_pnl for p in done):+,.0f}")


def _with_suffix(path: str, suffix: str) -> str:
    root, ext = os.path.splitext(path)
    return f"{root}{suffix}{ext or '.csv'}"


def build_live_accounts(cfg: SystemConfig, client: YHSClient, day: dt.date, notifier) -> tuple[list, list]:
    """阶段 1：拉一次成交量榜（宽松条件），从中标出严格异动的 A 池，按 live_universe_mode 建账户。"""
    broad_cfg = copy.copy(cfg)
    broad_cfg.require_volume_anomaly = broad_cfg.require_pcr_extreme = False
    universe = UniverseFilter(broad_cfg).build(client, day)
    # A 池永远排在最前，配额不够截断时只截 B 池里成交量靠后的
    universe.sort(key=lambda e: (not is_strict(e, cfg), e.rank))
    cap = budget_cap(cfg, client.quota_remaining, day)
    mode = cfg.live_universe_mode
    strict = [e for e in universe if is_strict(e, cfg)]
    if mode == "strict":
        universe = strict
    universe = universe[:cap]
    strict = [e for e in strict if e in universe]
    accounts = []
    if mode in ("both", "broad"):
        bcfg = copy.copy(broad_cfg)
        bcfg.trades_csv = cfg.trades_csv
        accounts.append(LiveAccount("B", bcfg, client, universe, notifier, TAG_B))
    if mode in ("both", "strict") and strict:
        acfg = copy.copy(cfg)
        acfg.trades_csv = _with_suffix(cfg.trades_csv, "_A") if mode == "both" else cfg.trades_csv
        accounts.append(LiveAccount("A", acfg, client, strict, notifier, TAG_A))
    return universe, accounts


def next_mark_tick(tick: dt.datetime, grid: dt.datetime, offset_s: int, step_min: int, fast_s: int, near: bool) -> dt.datetime:
    """下一次持仓检查时刻：平时 = 下一个整 step_min 分钟（相对本轮网格 + offset）；靠近触发线时 = fast_s 秒后，
    但不会越过下一个整分钟检查点。"""
    base = grid + dt.timedelta(seconds=offset_s)
    k = int((tick - base).total_seconds() // (60 * step_min)) + 1
    minute_mark = base + dt.timedelta(minutes=step_min * k)
    if near and fast_s:
        return min(tick + dt.timedelta(seconds=fast_s), minute_mark)
    return minute_mark


def run_live_day(cfg: SystemConfig, client: YHSClient, day: dt.date, notifier=None) -> None:
    log.info("=" * 78)
    log.info("交易日 %s 开始（标的池模式：%s）", day, cfg.live_universe_mode)
    universe, accounts = build_live_accounts(cfg, client, day, notifier)
    if not universe or not accounts:
        log.warning("今日标的池为空（或预算不足），跳过")
        if notifier:
            notifier.text(f"⚠️ {day} 今日标的池为空，不交易。")
        return
    strict_set = {e.ticker for e in universe if is_strict(e, cfg)}
    for a in accounts:
        log.info("账户 %s 监控 %d 只：%s", a.name, len(a.tickers), ", ".join(sorted(a.tickers)))
    if notifier:
        notifier.text(f"📋 **{day}** 今日监控 {len(universe)} 只（🅰️ = 严格异动 A 池，共 {len(strict_set)} 只）：\n"
                      + ", ".join(("🅰️" if e.ticker in strict_set else "") + e.ticker for e in universe))
    quotes = QuoteBook(client) if cfg.live_quotes else None
    if quotes:
        for a in accounts:
            a.trader.quote_provider = quotes.get

    baseline_rules = cfg.intraday_rules == "baseline"

    def refresh_quotes() -> None:
        if quotes:
            syms = [p.contract_symbol for a in accounts for p in a.trader.open_positions()]
            if baseline_rules:   # 同行权价的另一边一起拉，用 Call/Put 平价反推实时现价（每个持仓多 1 配额）
                from baseline import pair_symbol
                syms += [pair_symbol(s) for s in syms]
            quotes.new_round(syms)

    if quotes and baseline_rules:
        from baseline import parity_spot
        for a in accounts:
            a.engine.spot_override = lambda pos, now: parity_spot(
                pos.contract_symbol, lambda sym: quotes.cache.get(sym.split(":")[-1]))

    pool = [e.ticker for e in universe]
    last_poll: dict[str, dt.datetime] = {}
    open_t = et_datetime(day, cfg.session_open)
    close_t = et_datetime(day, cfg.force_close_time)
    session_verified = False
    next_heartbeat = et_now() + dt.timedelta(minutes=cfg.discord_heartbeat_min or 0)

    def finish_all(when: dt.datetime, reason: str = "EOD_FORCE_CLOSE") -> None:
        refresh_quotes()   # 收盘平仓用最新报价
        for a in accounts:
            log.info("----- 账户 %s 收盘 -----", a.name)
            finish_day(a.cfg, a.engine, a.trader, client, when, reason)

    try:
        # Heatmap 只在常规时段更新，快照标记为 5 分钟段的开始时间、段结束后才可用：
        # 09:30 段要到 09:35 才出来；之前 snapshot 返回 503 READ_MODEL_UNAVAILABLE（2026-10-13 开盘实测）
        sleep_until(open_t + dt.timedelta(minutes=5, seconds=cfg.poll_offset_s), "等待开盘后第一个 Heatmap 快照")
        while True:
            now = et_now()
            if now >= close_t:
                break
            # 对齐到 5 分钟采样网格
            grid = open_t + dt.timedelta(minutes=5 * ((minutes_of_day(now) - minutes_of_day(open_t)) // 5))
            # baseline：全天 5 分钟一根（与回放一致：节点 15 分钟演化 / 5、15 分钟涨跌 / 起始点衰减都按 5 分钟一根设计）
            interval = cfg.fast_poll_min if baseline_rules else poll_interval_min(cfg, grid)
            refresh_quotes()   # 本轮盯市用的持仓报价：一次批量请求
            no_entry = entry_blocked_window(cfg, grid) is not None
            for t in list(pool):
                if any(t in a.detector.rejected_spot for a in accounts):
                    pool.remove(t)
                    continue
                has_pos = any(a.trader.open_for(t) is not None for a in accounts)
                if no_entry and not has_pos:
                    continue   # 禁止开新仓的时段（午间）只盯持仓标的，省配额
                lp = last_poll.get(t)
                if not (has_pos and cfg.poll_open_positions_fast) and lp is not None \
                        and (grid - lp).total_seconds() < interval * 60:
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
                    if sd and sd != day.isoformat() and et_now() < open_t + dt.timedelta(minutes=cfg.holiday_grace_min):
                        log.info("%s Heatmap 仍是 %s 的快照（今日首个快照尚未生成），本轮停止，下一轮再试", t, sd)
                        last_poll.pop(t, None)
                        break   # 其余标的也一定是旧快照，不再浪费配额
                    if sd and sd != day.isoformat():
                        log.warning("Heatmap 会话日期 %s ≠ 今日 %s，判定今日休市（节假日），结束当日", sd, day)
                        if notifier:
                            notifier.text(f"💤 {day} 判定休市（Heatmap 会话日期 {sd}），今日不交易。")
                        return
                    session_verified = True
                ts = et_now()
                for a in accounts:   # 同一份快照喂给每个账户，A 不额外消耗配额
                    if t in a.tickers:
                        a.engine.on_snapshot(t, payload, ts)
            for a in accounts:   # 本轮所有标的处理完，再按信号分数排序开仓
                a.engine.flush(et_now())
            nxt = grid + dt.timedelta(minutes=5, seconds=cfg.poll_offset_s)
            log.info("[%s] 本轮完成：%s；监控 %d 只，配额剩余 %s；下一轮 %s", et_now().strftime("%H:%M:%S"),
                     "；".join(f"{a.name} 持仓 {len(a.trader.open_positions())}" for a in accounts), len(pool),
                     client.quota_remaining, nxt.strftime("%H:%M:%S"))
            if notifier and cfg.discord_heartbeat_min and et_now() >= next_heartbeat:
                next_heartbeat = et_now() + dt.timedelta(minutes=cfg.discord_heartbeat_min)
                notifier.text(f"💓 {et_now().strftime('%H:%M')} ET 运行中｜监控 {len(pool)} 只｜"
                              + "｜".join(a.status() for a in accounts) + f"｜配额剩余 {client.quota_remaining}")
            # 两次扫描之间：持仓每 position_mark_min 分钟用实时报价盯一次（只拉持仓合约的报价，每个 1 配额）
            # baseline：任一持仓靠近出场触发线时，两次分钟检查之间再每 bl_fast_mark_s 秒加查一次
            step = cfg.position_mark_min if cfg.live_quotes else 0
            fast = cfg.bl_fast_mark_s if (baseline_rules and step) else 0
            tick = grid + dt.timedelta(seconds=cfg.poll_offset_s)
            while step:
                near = bool(fast) and any(getattr(a.trader, "any_near_trigger", lambda _t: False)(et_now()) for a in accounts)
                tick = next_mark_tick(tick, grid, cfg.poll_offset_s, step, fast, near)
                if tick >= nxt or tick >= close_t:
                    break
                sleep_until(tick)
                if any(a.trader.open_positions() for a in accounts):
                    refresh_quotes()
                    for a in accounts:
                        a.engine.mark_open(et_now())
            sleep_until(min(nxt, close_t))
        finish_all(close_t)
    except QuotaExhausted:
        finish_all(et_now(), "QUOTA_HALT")
        raise
    except KeyboardInterrupt:
        log.warning("收到中断信号，按最新现货清平所有模拟持仓并导出")
        finish_all(et_now(), "MANUAL_STOP")
        raise


# =============================================================================
# 历史回放（真实数据）
# =============================================================================
def _intraday_day(payload: dict) -> dt.date | None:
    """contract-intraday 返回的分钟线所属交易日（取最后一根 bar）。"""
    bars = ((payload.get("data") or {}).get("bars") or []) if isinstance(payload, dict) else []
    ts = parse_iso(bars[-1].get("start_time")) if bars else None
    return to_et(ts).date() if ts else None


class HistoryClient:
    """历史回放的数据代理：
       * Heatmap：预先按 /v1/derived/heatmap/{t}/history 分页拉取整日 5 分钟快照，回放时本地供给（0 额外配额）
       * Flow：每只标的首次查询时一次性分页拉取当日 ≥$50k 大单并缓存，按回放时刻本地过滤（避免"未来函数"）
       * 期权链：历史时点无快照 → 抛错使交易器回退到 BS 现货换算（0 配额）
    """

    def __init__(self, real: YHSClient, cfg: SystemConfig, date: dt.date, max_flow_pages: int = 120):
        self.real, self.cfg, self.date = real, cfg, date
        self.now: dt.datetime = et_datetime(date, "09:30")
        self.frames: dict[str, dict[dt.datetime, dict]] = {}
        self.flow_cache: dict[str, list[dict]] = {}
        self.max_flow_pages = max_flow_pages
        self.intraday: dict[str, list[tuple[dt.datetime, float]]] = {}       # 合约 → [(bar 开始时间 ET, close)]
        self.greeks: dict[str, list[tuple]] = {}  # 合约 → [(时间 ET, 标的价, IV, |delta|)]
        self.intraday_unavailable = False   # 回放日不是最近交易日时，contract-intraday 无数据，首次发现后不再请求

    def load_heatmap(self, ticker: str, expiration: str) -> int:
        """整日 5 分钟快照：本地存档优先（archive.py），没有则从 API 拉取并顺手存档。"""
        rows, src = get_heatmap_rows(self.real, self.cfg, ticker, expiration, self.date)
        frames = rows_to_frames(ticker, self.date, expiration, rows)
        self.frames[ticker] = frames
        log.info("【历史】%s 到期 %s 载入 %d 个 5 分钟快照（来源：%s）", ticker, expiration, len(frames),
                 "本地存档" if src == "archive" else "API")
        return len(frames)

    def _cached_get(self, kind: str, core: str, path: str, params: dict | None, keep=None) -> dict:
        """期权价格数据的本地缓存：已收盘交易日的数据不会再变，缓存后重跑回测 0 配额且结果可复现。
        缓存位置 yhs_data/cache/{kind}/{日期}/{合约}.json；keep(payload) 为 False 时不写缓存。"""
        fp = os.path.join(self.cfg.data_dir, "cache", kind, self.date.isoformat(), f"{core}.json")
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                return json.load(f)
        payload = self.real.get(path, params)
        closed = self.date < et_now().date() or et_now() >= et_datetime(self.date, "16:30")
        if closed and isinstance(payload, dict) and (keep is None or keep(payload)):
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            tmp = f"{fp}.{os.getpid()}.tmp"   # 多个回测并行跑时各写各的临时文件
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, fp)
        return payload

    def _load_intraday(self, core: str) -> list[tuple[dt.datetime, float]]:
        """contract-intraday：单合约最近一个交易日的 1 分钟成交 bar（接口只给最新 150 根，约收盘前 2.5 小时）。"""
        if core in self.intraday:
            return self.intraday[core]
        bars: list[tuple[dt.datetime, float]] = []
        if self.intraday_unavailable:
            self.intraday[core] = bars
            return bars
        try:
            payload = self._cached_get("intraday", core, f"/v1/options/contract-intraday/{core}", None,
                                       keep=lambda pl: _intraday_day(pl) == self.date)
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

    def _load_greeks(self, core: str) -> list[tuple]:
        """contract-greeks-series：全天 5 分钟 (时间 ET, 标的价, IV)。snapshot_at 为不带时区的美东时间。"""
        if core in self.greeks:
            return self.greeks[core]
        rows: list[tuple] = []
        try:
            d = self.date.isoformat()
            payload = self._cached_get(f"greeks_{self.cfg.greeks_interval}", core,
                                       f"/v1/options/contract-greeks-series/{core}",
                                       {"from": d, "to": d, "interval": self.cfg.greeks_interval})
            data = payload.get("data") if isinstance(payload, dict) else None
            for r in (data or {}).get("rows") or []:
                ts = r.get("snapshot_at")
                S, iv, delta = r.get("underlying_price_usd"), r.get("implied_vol_pct"), r.get("delta")
                if not ts or not S or not iv or delta is None:
                    continue
                # 09:30 开盘首个点是未成形的异常值（实测 IV 仅 6%~12%、δ≈0，会把期权算成 $0.01），一律丢弃
                if ts[11:16] == "09:30" or abs(float(delta)) < 0.001:
                    continue
                rows.append((et_datetime(self.date, ts[11:16]), float(S), float(iv), abs(float(delta))))
            rows.sort()
            log.info("【历史】%s 真实 IV 序列 %d 个点（%s）", core, len(rows), self.cfg.greeks_interval)
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
        _, S, iv, _d = g
        right, K = core[-9], int(core[-8:]) / 1000
        exp = dt.datetime.strptime(core[-15:-9], "%y%m%d").date()
        T = ((exp - t.date()).days + max(0, 16 * 60 - t.hour * 60 - t.minute) / (24 * 60)) / 365
        px, _, _ = bs_price_greeks(S, K, T, self.cfg.risk_free_rate, iv, right == "C")
        return px

    def hist_quote(self, contract: str, t: dt.datetime):
        """离 t 最近（前后 60 分钟内）的真实 NBBO (bid, ask)：/v1/options/chain-history 按 标的+到期+方向 拉一天的报价。
        接口每次最多约 780 行、从最低行权价开始截断，所以先取 30 分钟粒度，平值合约不在返回里再取 1 小时粒度。
        找不到返回 None（交易器退回固定滑点）。"""
        core = contract.split(":")[-1]
        ticker, exp = core[:-15], "20" + core[-15:-13] + "-" + core[-13:-11] + "-" + core[-11:-9]
        right, strike = core[-9], int(core[-8:]) / 1000
        for iv in ("30m", "1h"):
            key = f"{ticker}_{exp}_{right}"
            try:
                payload = self._cached_get(f"nbbo_{iv}", key, f"/v1/options/chain-history/{ticker}",
                                           {"expiration": exp, "right": right, "from": self.date.isoformat(),
                                            "to": self.date.isoformat(), "interval": iv})
            except QuotaExhausted:
                raise
            except YHSApiError as e:
                log.info("【历史】%s 历史报价获取失败：%s", core, e)
                return None
            data = payload.get("data") if isinstance(payload, dict) else None
            hit = next((c for c in (data or {}).get("contracts") or [] if abs(float(c.get("strike_usd") or 0) - strike) < 1e-6), None)
            if hit is None:
                continue
            # 只用来估计买卖价差和"有没有买盘"（不用作成交价），所以取离 t 最近的一个有效报价（前后 60 分钟内）：
            # 开盘后第一个有效报价在 10:00 / 10:30，09:45 的开仓需要用之后的报价估计价差
            best, gap = None, None
            for q in hit.get("quotes") or []:
                ts = (q.get("quoted_at") or "")[:16]
                if not ts:
                    continue
                bid_q, ask_q = float(q.get("bid_usd") or 0), float(q.get("ask_usd") or 0)
                if ask_q <= 0 or ts[11:16] == "09:30":
                    continue   # 开盘前的空报价 / 09:30 开盘那一条（常见 bid 0.01 / ask 20 之类的坏数据）
                if bid_q > 0 and (ask_q - bid_q) / ((ask_q + bid_q) / 2) > HIST_QUOTE_MAX_SPREAD:
                    continue   # 明显坏报价：不能拿它判"价差过大不开"，否则回放会悄悄跳过实盘会开的单（2026-10-27 对账）
                g = abs((et_datetime(self.date, ts[11:16]) - t).total_seconds())
                if g <= 3600 and (gap is None or g < gap):
                    best, gap = (float(q.get("bid_usd") or 0), float(q.get("ask_usd") or 0)), g
            return best
        return None

    def underlying_at(self, contract: str, t: dt.datetime) -> float | None:
        """t 时刻（含之前 5 分钟内）的真实标的价格，取自该合约的 1 分钟 greeks 序列（0 额外配额）。"""
        last = None
        for row in self._load_greeks(contract.split(":")[-1]):
            if row[0] > t:
                break
            last = row
        return last[1] if last is not None and (t - last[0]).total_seconds() <= 5 * 60 else None

    def option_delta(self, contract: str, t: dt.datetime) -> float | None:
        """t 时刻（含 5 分钟内下一个点，与开仓成交口径一致）的真实 |delta|。"""
        rows = self._load_greeks(contract.split(":")[-1])
        best = None
        for row in rows:
            if row[0] > t + dt.timedelta(minutes=5):
                break
            if (t - row[0]).total_seconds() <= 15 * 60:
                best = row
        return best[3] if best else None

    def frame_at(self, ticker: str, t: dt.datetime) -> dict | None:
        return self.frames.get(ticker, {}).get(t.replace(second=0, microsecond=0))

    def get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if path.startswith("/v1/options/chain-snapshot/"):
            raise YHSApiError(0, "HISTORY_NO_CHAIN", "历史回放不使用实时期权链，改用 BS 换算", path)
        if path == FLOW_TRADES_PATH:
            t = params.get("tickers", "")
            prem = params.get("min_premium_usd")
            fp = os.path.join(self.cfg.data_dir, "cache", "flow", self.date.isoformat(), f"{t}_{int(prem or 0)}_ask.json")
            if t not in self.flow_cache and os.path.exists(fp):   # 已收盘交易日的 Flow 不会再变：本地缓存，重跑 0 配额
                with open(fp, encoding="utf-8") as f:
                    self.flow_cache[t] = json.load(f)
            if t not in self.flow_cache:
                rows, cursor, pages = [], None, 0
                while pages < self.max_flow_pages:
                    # 只拉 Ask 侧（A = at_ask，AA = above_ask）：Flow 确认只认主动买入的大单；接口 limit 上限 25
                    p = {"tickers": t, "kind": "order", "min_premium_usd": params.get("min_premium_usd"),
                         "sides": "A,AA", "session_date": self.date.isoformat(), "sort_by": "occurredAt",
                         "direction": "asc", "limit": 25, "cursor": cursor}
                    payload = self.real.get(FLOW_TRADES_PATH, p)
                    pages += 1
                    rows += extract_rows(payload)
                    cursor = (payload.get("_meta") or {}).get("next_cursor")
                    if not cursor:
                        break
                self.flow_cache[t] = rows
                if cursor:
                    log.warning("【历史】%s 当日 Flow 超过 %d 页，后段被截断", t, self.max_flow_pages)
                log.info("【历史】%s 当日 ≥$%s 大单 %d 笔已缓存（%d 页）", t, prem, len(rows), pages)
                closed = self.date < et_now().date() or et_now() >= et_datetime(self.date, "16:30")
                if closed and not cursor:
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    tmp = f"{fp}.{os.getpid()}.tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(rows, f, ensure_ascii=False)
                    os.replace(tmp, fp)
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
    # 到期日：有 0DTE 用 0DTE，否则最近到期（受 expiry_max_trading_dte 限制）；--expiration 可强制指定
    candidates = [expiration] if expiration else expiry_candidates(date, cfg.expiry_max_trading_dte,
                                                                   cfg.expiry_search_trading_days)
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
                log.warning("%s 到期 %s 历史 Heatmap 载入失败：%s（服务端 Heatmap 历史仅保留最近 3 个交易日，"
                            "更早的日期需有 archive.py 存档）", t, exp, e)
        else:
            log.info("【历史】%s 在 %s 没有可用到期（已尝试 %s ~ %s），跳过该标的", t, date, candidates[0], candidates[-1])
    if not loaded:
        log.error("没有可回放的标的")
        return {}
    detector = SignalDetector(cfg, [u for u in universe if u.ticker in loaded])
    trader = PaperTrader(cfg)
    if cfg.use_real_option_pricing:
        trader.price_provider = hc.option_price
        trader.delta_provider = hc.option_delta
        if cfg.replay_hist_spread:
            trader.spread_provider = hc.hist_quote
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
            # 实盘在 T 段结束（T+5 分钟）后才拿得到标记为 T 的快照：回放同样延后 replay_bar_delay_min 分钟使用
            frame = hc.frame_at(t, now - dt.timedelta(minutes=cfg.replay_bar_delay_min))
            if frame is None:
                continue
            last_poll[t] = now
            engine.on_snapshot(t, frame, now)
        engine.flush(now)
        # 两个 5 分钟 Heatmap 快照之间，对持仓逐分钟盯市（只用期权价格数据，不产生新的开仓信号，0 额外配额）
        if cfg.intraday_mark_min and cfg.use_real_option_pricing:
            for m in range(cfg.intraday_mark_min, 5, cfg.intraday_mark_min):
                tm = now + dt.timedelta(minutes=m)
                hc.now = tm
                for p in trader.open_positions():
                    if p.price_source != "real_data":
                        continue
                    spot = hc.underlying_at(p.contract_symbol, tm) or engine.last_spot.get(p.ticker, p.last_spot)
                    if trader.mark(p, spot, tm):
                        engine.last_exit_time[p.ticker] = tm
    finish_day(cfg, engine, trader, real, et_datetime(date, "16:00"))
    return trader.stats()


# =============================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="夜航社 Dealer GEX 异动期权捕捉与模拟复盘系统")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="实盘盘中自适应监控（模拟盘）")
    ap.add_argument("--one-day", action="store_true", help="--live：只跑今天一个交易日，收盘后退出（云端定时任务用）")
    mode.add_argument("--replay", action="store_true", help="回放（默认本地模拟数据，0 配额）")
    mode.add_argument("--check", action="store_true", help="免费检查 API Key 与剩余配额")
    mode.add_argument("--premarket", action="store_true", help="盘前研究工作流：生成多头/空头/等待观察名单")
    mode.add_argument("--notify-test", action="store_true", help="向 config.json 里的 discord_webhook_url 发一条测试消息")
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
        if args.notify_test:
            n = DiscordNotifier(cfg.discord_webhook_url)
            if not n.enabled:
                log.error("config.json 里没有有效的 discord_webhook_url（应以 https://discord.com/api/webhooks/ 开头）")
                return 2
            ok = n.send("✅ 夜航社 GEX 模拟盘：Discord 通知测试成功。开盘后这里会推送开仓、平仓和收盘复盘。")
            log.info("Discord 测试消息%s", "已发送，请到频道查看" if ok else "发送失败，见上方日志")
            return 0 if ok else 1
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
        run_live(cfg, one_day=args.one_day)
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
