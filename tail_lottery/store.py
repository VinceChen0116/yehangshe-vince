"""内存状态（说明第 6 节）。不落盘；进程重启从零开始。

| 数据 | 保留 |
| 最新期权链（报价 / IV / Greeks / OI） | 只留最新一份 |
| 分钟成交桶（每合约成交量 / 权利金 / 估算方向） | 最近 15 分钟 |
| 每合约当日累计 | 当日，跨日清零（从本进程开始拉取时算起） |
| 大单 | 最近 15 分钟 |
| Feed 信号 | 最近 200 条 |
| 现货序列（每次拉链一个点，约 30 秒） | 最近 90 分钟 |
| GEX 快照 | 最近 60 分钟，每分钟一份 |
| Skew / Charm | 最新一份 + 最近 10 分钟每 10 秒一份（实际每次拉链一份，约 30 秒） |
"""
from __future__ import annotations

import collections
import datetime as dt
from dataclasses import dataclass, field

from .direction import split_bucket_row

FLOW_KEEP = dt.timedelta(minutes=15)
SPOT_KEEP = dt.timedelta(minutes=90)
GEX_KEEP = dt.timedelta(minutes=60)
DEBUG_KEEP = dt.timedelta(minutes=10)
SIGNALS_MAX = 200


@dataclass
class DayTotal:
    contracts: float = 0.0
    buy: float = 0.0
    sell: float = 0.0
    unknown: float = 0.0
    premium: float = 0.0
    buy_premium: float = 0.0
    sell_premium: float = 0.0


@dataclass
class Store:
    session: dt.date | None = None
    chain: object = None                                       # 最新 ChainSnapshot
    spot: float | None = None
    spot_at: dt.datetime | None = None
    spot_estimated: bool = True
    buckets: "collections.OrderedDict" = field(default_factory=collections.OrderedDict)   # start → FlowBucket
    day: dict = field(default_factory=dict)                    # (strike, right) → DayTotal
    day_known_premium: float = 0.0
    day_gross_premium: float = 0.0
    big_prints: list = field(default_factory=list)
    seen_prints: set = field(default_factory=set)
    signals: "collections.deque" = field(default_factory=lambda: collections.deque(maxlen=SIGNALS_MAX))
    spot_series: "collections.deque" = field(default_factory=collections.deque)   # (t, spot)
    gex_frames: "collections.OrderedDict" = field(default_factory=collections.OrderedDict)  # 分钟 → GEX 结果
    skew: dict | None = None
    charm: dict | None = None
    debug_series: "collections.deque" = field(default_factory=collections.deque)  # (t, skew, charm)

    def reset(self, session: dt.date) -> None:
        self.__init__()
        self.session = session

    # ------------------------------------------------------------------ 写入
    def add_spot(self, t: dt.datetime, s: float, estimated: bool) -> None:
        self.spot, self.spot_at, self.spot_estimated = s, t, estimated
        self.spot_series.append((t, s))
        while self.spot_series and t - self.spot_series[0][0] > SPOT_KEEP:
            self.spot_series.popleft()

    def add_bucket(self, b) -> None:
        if b.start in self.buckets:
            return
        self.buckets[b.start] = b
        for row in b.rows:
            buy, sell, unk, bp, sp = split_bucket_row(row, b.known_ratio(row.right))
            d = self.day.setdefault((row.strike, row.right), DayTotal())
            d.contracts += row.contracts
            d.buy += buy
            d.sell += sell
            d.unknown += unk
            d.premium += row.gross_premium
            d.buy_premium += bp
            d.sell_premium += sp
        s = b.summary or {}
        self.day_gross_premium += float(s.get("gross_premium_usd") or 0)
        self.day_known_premium += sum(float(s.get(k) or 0) for k in (
            "call_ask_premium_usd", "call_bid_premium_usd", "put_ask_premium_usd", "put_bid_premium_usd"))
        while self.buckets and b.start - next(iter(self.buckets)) >= FLOW_KEEP:
            self.buckets.popitem(last=False)

    def add_big_prints(self, prints, now: dt.datetime) -> list:
        new = [p for p in prints if p.agg_id not in self.seen_prints]
        for p in new:
            self.seen_prints.add(p.agg_id)
        self.big_prints.extend(new)
        self.big_prints = [p for p in self.big_prints if now - p.at < FLOW_KEEP]
        return new

    def add_gex(self, t: dt.datetime, g: dict) -> None:
        key = t.replace(second=0, microsecond=0)
        self.gex_frames[key] = g
        while self.gex_frames and key - next(iter(self.gex_frames)) > GEX_KEEP:
            self.gex_frames.popitem(last=False)

    def add_debug(self, t: dt.datetime, skew: dict | None, charm: dict | None) -> None:
        self.skew, self.charm = skew, charm
        self.debug_series.append((t, skew, charm))
        while self.debug_series and t - self.debug_series[0][0] > DEBUG_KEEP:
            self.debug_series.popleft()

    # ------------------------------------------------------------------ 读取
    def direction_known_share(self) -> float | None:
        if self.day_gross_premium <= 0:
            return None
        return self.day_known_premium / self.day_gross_premium

    def buckets_since(self, t0: dt.datetime) -> list:
        return [b for s, b in self.buckets.items() if s >= t0]

    def gex_at(self, t: dt.datetime, tol_min: int = 2):
        """取 t 时刻（±tol 分钟内最近的一帧）的 GEX 快照。"""
        best, bd = None, None
        for k, v in self.gex_frames.items():
            d = abs((k - t).total_seconds())
            if d <= tol_min * 60 and (bd is None or d < bd):
                best, bd = v, d
        return best

    def spots_between(self, t0: dt.datetime, t1: dt.datetime) -> list:
        return [(t, s) for t, s in self.spot_series if t0 <= t <= t1]
