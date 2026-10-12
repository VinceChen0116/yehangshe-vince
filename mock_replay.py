"""
mock_replay.py —— 全交易日（09:30–16:00 ET，78 个 5 分钟步进）模拟回放器
=====================================================================
做法：在本机 127.0.0.1 启动一个"仿夜航社"HTTP 服务器，返回与真实 API 同构的 JSON
（data/_meta 信封、X-Quota-* / X-RateLimit-* 响应头、首次查询返回 202 materializing），
然后用 **真实的 YHSClient** 去请求它。这样客户端的 202 重试、配额头解析、熔断逻辑与
detector / paper_trader 的全部判断都在同一条链路上被验证，零 API 配额消耗。

内置剧本（每只标的覆盖一条逻辑分支）：
  NVDA  跳板 + Call 大单 → 拉升触发 +150% 硬顶止盈
  TSLA  悬崖 + Put 大单（行内无量/OI → 合约补查）→ 下跌后反抽，追踪止盈出场
  AAPL  跳板 + Call 大单 → 跌破地板，-35% 硬止损
  MSFT  跳板 + Call 大单，但合约当日量 < OI（疑似平仓）→ Flow 拒绝
  AMD   跳板结构，但大单权利金仅 $30k → Flow 拒绝
  META  跳板结构，正节点被反复测试 >3 次后才出现大单 → 节点疲劳拒绝
  GOOGL 悬崖结构 + Put 大单，但 PCR 偏多 → 方向过滤拒绝
  SPY   白名单 ETF，尾盘悬崖 + Put 大单 → 持有至 16:00 收盘强平
  QQQ   白名单 ETF，全天无结构
  SOFI  量能异动但股价 < $20 → 首次 Heatmap 后移出标的池
  另含 SPX/VIX/NDX 指数、XLF/IWM/ARKK/SMH 等非白名单 ETF 与 80+ 只常规标的，用于验证阶段1 剔除。

运行：python mock_replay.py      或     python main.py --replay
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import random
import threading
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config import SystemConfig, et_datetime, poll_interval_min, setup_logging
from detector import HEATMAP_PATH, SignalDetector, UniverseFilter
from paper_trader import PaperTrader, SessionEngine, bs_price_greeks, occ_symbol
from yhs_client import QuotaExhausted, YHSApiError, YHSClient

log = logging.getLogger("yhs.replay")

REPLAY_DATE = dt.date(2026, 10, 9)          # 周五（夏令时，ET = UTC-4）
EXP_NEAR = "2026-10-16"
EXP_FAR = "2026-10-23"
STEPS = 78


def _seed(*parts) -> int:
    """跨进程稳定的随机种子（内置 hash() 对字符串有随机化）。"""
    return zlib.crc32(repr(parts).encode())


# =============================================================================
# 剧本
# =============================================================================
@dataclass
class FlowPrint:
    minute: int            # 距 09:30 的分钟数
    right: str
    side: str
    premium: float
    strike: float
    contracts: int
    volume: int | None
    oi: int | None


@dataclass
class Scenario:
    ticker: str
    keyframes: list[tuple[int, float]]      # (距 09:30 分钟, 价格)
    pos_strike: float
    neg_strike: float
    call_vol: float
    put_vol: float
    oi_total: float
    oi_change_pct: float
    flows: list[FlowPrint] = field(default_factory=list)
    node_shift: list[tuple[int, float, float]] = field(default_factory=list)  # (分钟, 新正节点, 新负节点)
    noise: float = 0.0002
    oscillate: list[float] | None = None    # 若给出则按 5 分钟循环取价（用于疲劳剧本）

    def spot(self, minute: int, rng: random.Random) -> float:
        if self.oscillate:
            base = self.oscillate[(minute // 5) % len(self.oscillate)]
        else:
            kf = self.keyframes
            if minute <= kf[0][0]:
                base = kf[0][1]
            elif minute >= kf[-1][0]:
                base = kf[-1][1]
            else:
                for (m0, p0), (m1, p1) in zip(kf, kf[1:]):
                    if m0 <= minute <= m1:
                        base = p0 + (p1 - p0) * (minute - m0) / (m1 - m0)
                        break
        return round(base * (1 + rng.uniform(-self.noise, self.noise)), 2)

    def nodes(self, minute: int) -> tuple[float, float]:
        pos, neg = self.pos_strike, self.neg_strike
        for m, p, n in self.node_shift:
            if minute >= m:
                pos, neg = p, n
        return pos, neg


def build_scenarios() -> dict[str, Scenario]:
    S = {}
    S["NVDA"] = Scenario("NVDA", [(0, 186.2), (25, 186.6), (60, 189.5), (150, 197.0), (240, 193.0), (390, 192.0)],
                         185.0, 195.0, 2_400_000, 980_000, 3_900_000, 0.072,
                         [FlowPrint(27, "C", "ask", 182_000, 195.0, 1500, 52_000, 31_000)])
    S["TSLA"] = Scenario("TSLA", [(0, 437.6), (55, 437.0), (115, 426.0), (150, 422.5), (215, 436.5), (390, 435.0)],
                         440.0, 425.0, 900_000, 1_650_000, 3_100_000, 0.081,
                         [FlowPrint(57, "P", "ask", 96_000, 425.0, 300, None, None)])
    S["AAPL"] = Scenario("AAPL", [(0, 253.2), (12, 253.4), (90, 246.5), (390, 247.0)],
                         252.5, 260.0, 1_200_000, 540_000, 2_300_000, 0.055,
                         [FlowPrint(8, "C", "ask", 71_000, 260.0, 700, 18_000, 9_500)])
    S["MSFT"] = Scenario("MSFT", [(0, 512.5), (390, 516.0)],
                         510.0, 525.0, 600_000, 260_000, 1_100_000, 0.06,
                         [FlowPrint(40, "C", "ask", 140_000, 525.0, 400, 2_100, 14_800)])
    S["AMD"] = Scenario("AMD", [(0, 231.0), (390, 233.0)],
                        230.0, 240.0, 1_500_000, 610_000, 2_700_000, 0.09,
                        [FlowPrint(35, "C", "ask", 30_000, 240.0, 120, 9_000, 4_000)])
    S["META"] = Scenario("META", [(0, 716.0)], 715.0, 735.0, 700_000, 300_000, 1_000_000, 0.07,
                         [FlowPrint(176, "C", "ask", 250_000, 735.0, 300, 6_000, 2_500)],
                         oscillate=[715.6, 718.0, 722.0, 724.0, 721.0, 717.5], noise=0.00005)
    S["GOOGL"] = Scenario("GOOGL", [(0, 244.0), (390, 241.0)],
                          245.0, 238.0, 1_100_000, 480_000, 2_100_000, 0.065,
                          [FlowPrint(20, "P", "ask", 88_000, 238.0, 400, 7_000, 3_000)])
    S["SPY"] = Scenario("SPY", [(0, 674.0), (300, 672.5), (310, 667.4), (360, 664.8), (390, 664.2)],
                        655.0, 645.0, 4_200_000, 7_400_000, 15_000_000, 0.03,
                        [FlowPrint(308, "P", "ask", 410_000, 660.0, 2000, 160_000, 90_000)],
                        node_shift=[(300, 668.0, 660.0)])
    S["QQQ"] = Scenario("QQQ", [(0, 601.0), (120, 603.0), (240, 600.0), (390, 602.0)],
                        590.0, 585.0, 1_800_000, 3_300_000, 6_500_000, 0.06)
    S["SOFI"] = Scenario("SOFI", [(0, 14.2), (390, 14.6)], 14.0, 15.0, 1_400_000, 300_000, 2_000_000, 0.12)
    return S


def build_volume_rank(scen: dict[str, Scenario], rng: random.Random) -> list[dict]:
    rows = []

    def oi_block(cur, chg):
        prev = cur / (1 + chg)
        return {"current": round(cur), "previous": round(prev), "change": round(cur - prev), "change_pct": round(chg, 4)}

    def row(t, cv, pv, oi, chg):
        return {"ticker": t, "total_volume": round(cv + pv), "call_volume": round(cv), "put_volume": round(pv),
                "open_interest": {"total": oi_block(oi, chg), "call": oi_block(oi * 0.55, chg),
                                  "put": oi_block(oi * 0.45, chg)}}

    for s in scen.values():
        rows.append(row(s.ticker, s.call_vol, s.put_vol, s.oi_total, s.oi_change_pct))
    # 指数 / 非白名单 ETF（应被剔除）
    for t, v in [("SPX", 9e6), ("VIX", 3e6), ("NDX", 6e5), ("XLF", 9e5), ("IWM", 2.5e6), ("ARKK", 7e5),
                 ("SMH", 8e5), ("TLT", 1.2e6), ("SOXL", 1.1e6), ("TQQQ", 1.3e6), ("UVXY", 6e5)]:
        rows.append(row(t, v * 0.5, v * 0.5, v * 2.0, 0.01))
    # 常规标的：PCR 中性、量能一般（应被量能/PCR 初筛剔除）
    for i in range(110):
        t = f"TK{i:03d}"
        v = rng.uniform(1.5e5, 8e5)
        rows.append(row(t, v * 0.5, v * 0.5, v * rng.uniform(2.0, 4.0), rng.uniform(-0.03, 0.03)))
    rows.sort(key=lambda r: -r["total_volume"])
    for i, r in enumerate(rows[:150], 1):
        r["rank"] = i
    return rows[:150]


# =============================================================================
# 仿 YHS 服务器
# =============================================================================
class MockMarket:
    def __init__(self, seed: int = 20261009):
        self.rng = random.Random(seed)
        self.scen = build_scenarios()
        self.rank_rows = build_volume_rank(self.scen, self.rng)
        self.now: dt.datetime = et_datetime(REPLAY_DATE, "09:30")
        self.quota_remaining = 100_000
        self.rpm_window: list[float] = []
        self.materialized: set[str] = set()
        self.lock = threading.Lock()
        self._spot_cache: dict[tuple[str, int], float] = {}

    def minute(self) -> int:
        return int((self.now - et_datetime(REPLAY_DATE, "09:30")).total_seconds() // 60)

    def spot(self, t: str, minute: int | None = None) -> float:
        m = self.minute() if minute is None else minute
        key = (t, m)
        if key not in self._spot_cache:
            self._spot_cache[key] = self.scen[t].spot(m, random.Random(_seed(*key)))
        return self._spot_cache[key]

    # ---------------------------------------------------------------- 端点
    def heatmap(self, t: str) -> dict:
        s = self.scen.get(t)
        if s is None:
            return None
        m = self.minute()
        spot = self.spot(t, m)
        pos, neg = s.nodes(m)
        r = random.Random(_seed(t, m // 30))
        step = 0.5 if spot < 50 else (2.5 if spot < 300 else 5.0)
        cells = [{"strike_usd": pos, "expiration": EXP_NEAR, "net_dealer_gex_usd": round(2.4e8 * r.uniform(0.95, 1.05))},
                 {"strike_usd": neg, "expiration": EXP_NEAR, "net_dealer_gex_usd": -round(2.1e8 * r.uniform(0.95, 1.05))}]
        for k in range(1, 6):   # 次级节点（量级较小）
            cells.append({"strike_usd": round(pos - k * step, 2), "expiration": EXP_NEAR,
                          "net_dealer_gex_usd": round(4e7 * r.uniform(0.3, 1.0))})
            cells.append({"strike_usd": round(neg + (k if neg > pos else -k) * step * 2, 2), "expiration": EXP_NEAR,
                          "net_dealer_gex_usd": -round(3e7 * r.uniform(0.3, 1.0))})
        # 远月节点：方向相反、量级更大，用于验证"只看近周到期"
        cells.append({"strike_usd": round(spot * 1.08 / step) * step, "expiration": EXP_FAR, "net_dealer_gex_usd": 9e8})
        cells.append({"strike_usd": round(spot * 0.92 / step) * step, "expiration": EXP_FAR, "net_dealer_gex_usd": -9e8})
        gen = self.now.astimezone(dt.timezone.utc)
        return {"data": {"ticker": t, "generated_at": gen.isoformat().replace("+00:00", "Z"),
                         "session_date_et": REPLAY_DATE.isoformat(), "spot_usd": spot,
                         "expirations": [EXP_NEAR, EXP_FAR], "cells": cells}}

    def flow_trades(self, q: dict) -> dict:
        tickers = set(q.get("tickers", [""])[0].split(","))
        minp = float(q.get("min_premium_usd", ["10000"])[0])
        sides = set(filter(None, q.get("sides", [""])[0].split(",")))
        rights = set(filter(None, q.get("rights", [""])[0].split(",")))
        now_m = self.minute()
        rows = []
        for t in tickers:
            s = self.scen.get(t)
            if not s:
                continue
            for f in s.flows:
                if f.minute > now_m or f.premium < minp:
                    continue
                if sides and f.side not in sides:
                    continue
                if rights and f.right not in rights:
                    continue
                ts = (et_datetime(REPLAY_DATE, "09:30") + dt.timedelta(minutes=f.minute)).astimezone(dt.timezone.utc)
                row = {"occurred_at": ts.isoformat().replace("+00:00", "Z"), "ticker": t,
                       "contract_symbol": occ_symbol(t, EXP_NEAR, f.right, f.strike), "right": f.right,
                       "side": f.side, "strike_usd": f.strike, "expiration": EXP_NEAR,
                       "premium_usd": f.premium, "contracts": f.contracts, "print_count": 3}
                if f.volume is not None:
                    row["volume"] = f.volume
                    row["open_interest"] = f.oi
                rows.append(row)
        rows.sort(key=lambda r: r["occurred_at"], reverse=True)
        return {"data": rows, "_meta": {"next_cursor": None}}

    def contract_lookup(self, sym: str) -> dict:
        for s in self.scen.values():
            for f in s.flows:
                if occ_symbol(s.ticker, EXP_NEAR, f.right, f.strike) == sym:
                    vol = f.volume if f.volume is not None else int(f.contracts * 30)
                    oi = f.oi if f.oi is not None else int(f.contracts * 18)
                    return {"data": {"contract_symbol": sym, "volume": vol, "open_interest": oi}}
        return None

    def chain(self, t: str, expiration: str) -> dict:
        s = self.scen.get(t)
        if s is None:
            return None
        spot = self.spot(t)
        T = ((dt.date.fromisoformat(expiration) - REPLAY_DATE).days + (390 - self.minute()) / 390) / 365
        step = 0.5 if spot < 50 else (2.5 if spot < 300 else 5.0)
        center = round(spot / step) * step
        rows = []
        for k in range(-20, 21):
            K = round(center + k * step, 2)
            for right in ("C", "P"):
                px, d, g = bs_price_greeks(spot, K, T, 0.04, 0.42, right == "C")
                spr = max(0.02, px * 0.03)
                rows.append({"contract_symbol": occ_symbol(t, expiration, right, K), "right": right, "strike": K,
                             "expiration": expiration, "bid": round(px - spr / 2, 2), "ask": round(px + spr / 2, 2),
                             "delta": round(d if right == "C" else -d, 4), "gamma": round(g, 5), "iv": 0.42})
        return {"data": {"ticker": t, "expiration": expiration, "underlying_price": spot, "rows": rows},
                "_meta": {"next_cursor": None}}


def _make_handler(market: MockMarket):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音
            pass

        def _send(self, status: int, obj: dict, billable: bool):
            body = json.dumps(obj).encode()
            with market.lock:
                if billable and status == 200:
                    market.quota_remaining -= 1
                q = market.quota_remaining
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-RateLimit-Limit", "60")
            self.send_header("X-RateLimit-Remaining", "55")
            self.send_header("X-RateLimit-Reset", str(int(dt.datetime.now().timestamp()) + 60))
            self.send_header("X-Quota-Limit", "100000")
            self.send_header("X-Quota-Remaining", str(q))
            self.send_header("X-Quota-Reset-At", "2026-11-01T00:00:00Z")
            if status == 202:
                self.send_header("Retry-After", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            p = u.path
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(401, {"error": {"code": "UNAUTHENTICATED", "tags": [], "message": "missing key",
                                                  "docs_url": "https://docs.yehangshe.com/errors", "request_id": None}}, False)
            if p in ("/v1/discover", "/v1/health"):
                return self._send(200, {"data": {"quota": {"monthly_remaining": market.quota_remaining}}}, False)
            if p == "/v1/market/options-volume-rank":
                return self._send(200, {"data": market.rank_rows, "_meta": {"cache_hit": True}}, True)
            parts = p.strip("/").split("/")
            if p.startswith("/v1/derived/heatmap/") and p.endswith("/snapshot"):
                t = parts[3]
                if t not in market.materialized:      # 首次查询：202 数据准备中（免费）
                    market.materialized.add(t)
                    return self._send(202, {"data": None, "_meta": {"status": "materializing",
                                                                    "retry_after_seconds": 0.01,
                                                                    "hint": "Data is being prepared; retry shortly."}}, False)
                obj = market.heatmap(t)
                if obj is None:
                    return self._send(404, {"error": {"code": "NOT_FOUND", "tags": [], "message": t,
                                                      "docs_url": "", "request_id": None}}, False)
                return self._send(200, obj, True)
            if p == "/v1/flow/trades":
                return self._send(200, market.flow_trades(q), True)
            if p == "/v1/flow/contracts/lookup":
                obj = market.contract_lookup(q.get("contract_symbol", [""])[0])
                if obj is None:
                    return self._send(404, {"error": {"code": "NOT_FOUND", "tags": [], "message": "contract",
                                                      "docs_url": "", "request_id": None}}, False)
                return self._send(200, obj, True)
            if p.startswith("/v1/options/chain-snapshot/"):
                obj = market.chain(parts[3], q.get("expiration", [EXP_NEAR])[0])
                if obj is None:
                    return self._send(404, {"error": {"code": "NOT_FOUND", "tags": [], "message": "chain",
                                                      "docs_url": "", "request_id": None}}, False)
                return self._send(200, obj, True)
            return self._send(404, {"error": {"code": "NOT_FOUND", "tags": [], "message": p,
                                              "docs_url": "", "request_id": None}}, False)
    return H


# =============================================================================
# 回放主循环
# =============================================================================
def run_mock_replay(cfg: SystemConfig | None = None, csv_path: str | None = None) -> dict:
    cfg = cfg or SystemConfig()
    # 本地回放：关闭真实限频间隔以秒级跑完（实盘时恢复 1.2s / 50 RPM）
    cfg.min_request_interval_s = 0.0
    cfg.safe_rpm = 10_000
    cfg.backoff_base_s = 0.01
    cfg.data_dir = os.path.join(cfg.data_dir, "mock")
    market = MockMarket()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(market))
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    cfg.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    cfg.api_key = cfg.api_key or "sk_mock_replay_key"

    client = YHSClient(cfg)
    try:
        log.info("=" * 78)
        log.info("模拟回放开始：%s，09:30–16:00 ET，共 %d 个 5 分钟步进（仿 YHS 服务器 %s）",
                 REPLAY_DATE, STEPS, cfg.base_url)
        log.info("=" * 78)
        uf = UniverseFilter(cfg)
        universe = uf.build(client, REPLAY_DATE, force=True)
        detector = SignalDetector(cfg, universe)
        trader = PaperTrader(cfg)
        engine = SessionEngine(cfg, client, detector, trader)
        pool = [e.ticker for e in universe]
        last_poll: dict[str, dt.datetime] = {}
        heatmap_calls = 0

        for i in range(STEPS):
            now = et_datetime(REPLAY_DATE, "09:30") + dt.timedelta(minutes=5 * i)
            market.now = now
            interval = poll_interval_min(cfg, now)
            for t in list(pool):
                if t in detector.rejected_spot:
                    pool.remove(t)
                    continue
                has_pos = trader.open_for(t) is not None and cfg.poll_open_positions_fast
                lp = last_poll.get(t)
                if not has_pos and lp is not None and (now - lp).total_seconds() < interval * 60:
                    continue
                try:
                    payload = client.get(HEATMAP_PATH.format(ticker=t))
                except QuotaExhausted as e:
                    log.error("配额熔断：%s", e)
                    raise
                except YHSApiError as e:
                    log.warning("%s Heatmap 拉取失败：%s", t, e)
                    continue
                heatmap_calls += 1
                last_poll[t] = now
                engine.on_snapshot(t, payload, now)
        engine.end_of_day(et_datetime(REPLAY_DATE, "16:00"))
        trader.print_report()
        path = trader.export_trades_csv(csv_path or cfg.trades_csv)
        log.info("检测统计：%s", detector.stats)
        log.info("Heatmap 拉取 %d 次；%s", heatmap_calls, client.quota_summary())
        out = trader.stats()
        out["csv"] = path
        out["billed_calls"] = client.billed_calls
        out["detector"] = detector.stats
        return out
    finally:
        server.shutdown()


if __name__ == "__main__":
    setup_logging("INFO")
    run_mock_replay()
