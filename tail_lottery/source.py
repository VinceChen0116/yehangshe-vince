"""行情适配层：夜航社（YHS）适配器 + 假行情源。计算模块只依赖本文件的数据结构，不碰 HTTP。

夜航社是 REST 轮询 + 配额计费，不是券商那种推送；所以"统一事件"在这里落成三种轮询快照：
  ChainSnapshot   ← GET /v1/options/chain-snapshot/SPX?expiration=<今天>      1 次配额 / 次（limit=1000 一页拿齐 ATM 附近约 300 个合约）
                    **盘中落后约 13 分钟（2026-11-18 实测）**，只用来取行权价、合约代码和 OI
  实时报价        ← GET /v1/options/quotes?contracts=…（≤50 个）                每个报价 1 次配额；延迟约 0 秒
                    现价、IV、gamma、delta 都由实时报价本地计算
  FlowBucket      ← GET /v1/flow/tickers/SPX/bucket  window=1m                  summary 1 次 + top_contracts 每页 1 次
  BigPrint        ← GET /v1/flow/trades  tickers=SPX expirations=<今天> 大单    每页 1 次
能力声明见 CAPABILITIES；README 的"能力核对表"逐条写了出处和实测情况。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

log = logging.getLogger("tail.source")

# 夜航社的能力声明（说明 4.1 节）。值的来源见 tail_lottery/README.md「能力核对表」。
CAPABILITIES = {
    "name": "夜航社 YHS",
    "trade_ticks": True,         # /v1/flow/trades 逐笔聚合（同一时刻同一合约的多笔会合并成一条 aggregate）
    "aggressor_side": True,      # side: at_ask / above_ask / at_bid / below_bid / mid ...（数据商按 NBBO 标注）
    "greeks": True,              # chain-snapshot 带 IV / delta / gamma / charm
    "open_interest": True,       # chain-snapshot 带 OI（open_interest_as_of = 当日 11:30 UTC 前后，即前一交易日收盘 OI）
    "index_quote": False,        # 没有分钟级 SPX 现价（/stocks/quote 实测延迟约 14 分钟）→ 平价反推，标"估算现货"
    "max_subscriptions": None,   # 无订阅概念；受配额与 60 RPM 限制
    "subscribe_rate": None,
    "quote_delivery": "polling", # 轮询快照：每次拉取都是全量最新值
}

BUY_SIDES = {"at_ask", "above_ask", "A", "AA"}
SELL_SIDES = {"at_bid", "below_bid", "B", "BB"}


@dataclass
class Contract:
    symbol: str
    strike: float
    right: str                       # "C" / "P"
    bid: float | None = None
    ask: float | None = None
    quoted_at: dt.datetime | None = None
    oi: int | None = None
    iv: float | None = None          # 小数（0.25 = 25%）
    delta: float | None = None
    gamma: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return 0.5 * (self.bid + self.ask)

    @property
    def tradable(self) -> bool:
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask >= self.bid


@dataclass
class ChainSnapshot:
    fetched_at: dt.datetime                  # 本地收到时刻（UTC）
    expiration: dt.date
    quote_as_of: dt.datetime | None
    greeks_as_of: dt.datetime | None
    oi_as_of: dt.datetime | None
    underlying_price: float | None
    underlying_as_of: dt.datetime | None
    contracts: list[Contract] = field(default_factory=list)
    notice: str = ""                         # 服务端提示（例如"数据可能过期"）


@dataclass
class ContractFlow:
    """某 1 分钟桶内一个合约的成交汇总。net_premium = ask 侧权利金 − bid 侧权利金（已用 summary 核对）。"""
    symbol: str
    strike: float
    right: str
    contracts: float
    gross_premium: float
    net_premium: float
    surge_multiple: float | None = None


@dataclass
class FlowBucket:
    start: dt.datetime                       # 桶开始时刻（UTC）
    present: bool                            # 服务端是否已有这一分钟的数据
    summary: dict = field(default_factory=dict)   # zero_dte 汇总：call/put 的 ask/bid 权利金等
    rows: list[ContractFlow] = field(default_factory=list)
    truncated: bool = False                  # top_contracts 翻页到上限仍未取完

    def known_ratio(self, right: str) -> float:
        """该侧（C/P）方向已知的权利金占比 = (ask + bid) / 总权利金。"""
        s = self.summary or {}
        k = "call" if right == "C" else "put"
        gross = float(s.get(f"{k}_premium_usd") or 0)
        known = float(s.get(f"{k}_ask_premium_usd") or 0) + float(s.get(f"{k}_bid_premium_usd") or 0)
        if gross <= 0:
            return 0.0
        return max(0.0, min(1.0, known / gross))


@dataclass
class BigPrint:
    agg_id: str
    at: dt.datetime
    symbol: str
    strike: float
    right: str
    contracts: float
    premium: float
    price: float | None
    bid: float | None
    ask: float | None
    side: str | None


class SourceError(Exception):
    pass


# ---------------------------------------------------------------------------- 解析工具
def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _ts(v):
    from config import parse_iso
    return parse_iso(v)


def _strip_o(sym: str) -> str:
    return sym[2:] if sym and sym.startswith("O:") else sym


def parse_chain(payload: dict, fetched_at: dt.datetime, expiration: dt.date) -> ChainSnapshot:
    d = (payload or {}).get("data") or {}
    meta = (payload or {}).get("_meta") or {}
    cs = []
    for x in d.get("contracts") or []:
        k, r = _f(x.get("strike_usd")), x.get("right")
        if k is None or r not in ("C", "P"):
            continue
        oi = x.get("open_interest")
        cs.append(Contract(symbol=_strip_o(x.get("contract_symbol") or ""), strike=k, right=r,
                           bid=_f(x.get("bid_usd")), ask=_f(x.get("ask_usd")), quoted_at=_ts(x.get("quoted_at")),
                           oi=None if oi is None else int(oi), iv=_f(x.get("implied_vol_pct")),
                           delta=_f(x.get("delta")), gamma=_f(x.get("gamma"))))
    return ChainSnapshot(fetched_at=fetched_at, expiration=expiration, quote_as_of=_ts(d.get("quote_as_of")),
                         greeks_as_of=_ts(d.get("greeks_as_of")), oi_as_of=_ts(d.get("open_interest_as_of")),
                         underlying_price=_f(d.get("underlying_price_usd")), underlying_as_of=_ts(d.get("underlying_as_of")),
                         contracts=cs, notice=str(meta.get("notice") or ""))


def parse_bucket_rows(payload: dict, expiration: dt.date) -> list[ContractFlow]:
    d = (payload or {}).get("data") or {}
    out = []
    for x in d.get("rows") or []:
        if x.get("expiration") != expiration.isoformat():
            continue
        k, r = _f(x.get("strike_usd")), x.get("right")
        n, g = _f(x.get("flow_contracts")), _f(x.get("gross_premium_usd"))
        if k is None or r not in ("C", "P") or n is None or g is None:
            continue
        out.append(ContractFlow(symbol=_strip_o(x.get("contract_symbol") or ""), strike=k, right=r, contracts=n,
                                gross_premium=g, net_premium=_f(x.get("net_premium_usd")) or 0.0,
                                surge_multiple=_f(x.get("surge_multiple"))))
    return out


def parse_big_prints(payload: dict, expiration: dt.date) -> list[BigPrint]:
    out = []
    for x in ((payload or {}).get("data") or []):
        if x.get("expiration") != expiration.isoformat() or x.get("right") not in ("C", "P"):
            continue
        at = _ts(x.get("occurred_at"))
        k, n, p = _f(x.get("min_strike_usd")), _f(x.get("contracts")), _f(x.get("premium_usd"))
        if at is None or k is None or n is None or p is None:
            continue
        out.append(BigPrint(agg_id=str(x.get("aggregate_id")), at=at, symbol=_strip_o(x.get("contract_symbol") or ""),
                            strike=k, right=x["right"], contracts=n, premium=p,
                            price=_f(x.get("avg_fill_usd")), bid=_f(x.get("nbbo_bid_usd")), ask=_f(x.get("nbbo_ask_usd")),
                            side=x.get("side")))
    return out


# ---------------------------------------------------------------------------- 夜航社适配器
class YHSSource:
    """只读：只调用 GET 行情接口。"""
    CHAIN_PATH = "/v1/options/chain-snapshot/SPX"
    BUCKET_PATH = "/v1/flow/tickers/SPX/bucket"
    TRADES_PATH = "/v1/flow/trades"
    TOP_PAGES_MAX = 3          # 每分钟 top_contracts 最多翻 3 页（每页 100 个合约），够覆盖 ±1% 带
    BIG_PAGES_MAX = 4
    BIG_MIN_PREMIUM = 50_000   # 大单下限与 Feed 的 large_ask_trade 一致；≥50 张但 < $5 万的单子拿不到（见 README 局限）

    capabilities = CAPABILITIES

    def __init__(self, client):
        self.client = client

    @property
    def quota_remaining(self):
        return self.client.quota_remaining

    def _get(self, path, params):
        from yhs_client import YHSApiError
        try:
            return self.client.get(path, params)
        except YHSApiError as e:
            raise SourceError(str(e))

    def fetch_chain(self, expiration: dt.date, now: dt.datetime) -> ChainSnapshot:
        p = self._get(self.CHAIN_PATH, {"expiration": expiration.isoformat(), "limit": 1000})
        return parse_chain(p, now, expiration)

    QUOTES_PATH = "/v1/options/quotes"
    QUOTES_MAX = 50

    def fetch_quotes(self, symbols, now: dt.datetime) -> dict:
        """实时报价（2026-11-18 盘中实测延迟约 0 秒；chain-snapshot 盘中落后约 13 分钟）。每个返回的报价 1 配额。
        返回 {合约代码: (bid, ask, quoted_at)}。"""
        out = {}
        syms = [x for x in symbols if x]
        for i in range(0, len(syms), self.QUOTES_MAX):
            p = self._get(self.QUOTES_PATH, {"contracts": ",".join(syms[i:i + self.QUOTES_MAX])})
            for x in (((p or {}).get("data") or {}).get("quotes") or []):
                out[_strip_o(x.get("contract_symbol") or "")] = (_f(x.get("bid_usd")), _f(x.get("ask_usd")),
                                                                  _ts(x.get("quoted_at")))
        return out

    def fetch_bucket(self, session: dt.date, start: dt.datetime, expiration: dt.date) -> FlowBucket:
        micros = int(start.timestamp() * 1_000_000)
        base = {"session_date": session.isoformat(), "window": "1m", "bucket_start_micros": micros}
        s = self._get(self.BUCKET_PATH, dict(base, section="summary"))
        z = ((s or {}).get("data") or {}).get("zero_dte") or {}
        b = FlowBucket(start=start, present=bool(z.get("present")), summary=z)
        if not b.present:
            return b
        cursor = None
        for _ in range(self.TOP_PAGES_MAX):
            p = self._get(self.BUCKET_PATH, dict(base, section="top_contracts", cohort="zero_dte", limit=100, cursor=cursor))
            b.rows.extend(parse_bucket_rows(p, expiration))
            cursor = ((p or {}).get("data") or {}).get("next_cursor")
            if not cursor:
                break
        else:
            b.truncated = bool(cursor)
        return b

    def fetch_big_prints(self, session: dt.date, expiration: dt.date, seen: set) -> list[BigPrint]:
        """按时间倒序翻页，翻到已见过的 aggregate 为止。"""
        out, cursor = [], None
        for _ in range(self.BIG_PAGES_MAX):
            p = self._get(self.TRADES_PATH, {"session_date": session.isoformat(), "kind": "order", "tickers": "SPXW",   # 实测：tickers=SPX 加 expirations 返回空，SPXW 才行
                                             "expirations": expiration.isoformat(), "min_premium_usd": self.BIG_MIN_PREMIUM,
                                             "sort_by": "occurredAt", "direction": "desc", "limit": 25, "cursor": cursor})
            rows = parse_big_prints(p, expiration)
            new = [r for r in rows if r.agg_id not in seen]
            out.extend(new)
            cursor = (p or {}).get("_meta", {}).get("next_cursor") or (p or {}).get("next_cursor")
            if len(new) < len(rows) or not cursor:
                break
        return out


# ---------------------------------------------------------------------------- 假行情源
class FakeSource:
    """按脚本回放的测试替身。script(now) → dict，可含 chain / bucket / big_prints / error。

    - chain: ChainSnapshot 或 None
    - buckets: {bucket_start(UTC datetime): FlowBucket}
    - big_prints: list[BigPrint]（按 agg_id 去重）
    - error: 非空时本次调用抛 SourceError（模拟断线）
    """
    capabilities = dict(CAPABILITIES, name="假行情源")

    def __init__(self, chain_fn=None, buckets=None, big_prints=None, error_fn=None):
        self.chain_fn = chain_fn
        self.buckets = buckets or {}
        self.big_prints = big_prints or []
        self.error_fn = error_fn
        self.calls = 0
        self.quote_calls = []
        self.quota_remaining = 99_999

    def _maybe_fail(self, now):
        self.calls += 1
        if self.error_fn and self.error_fn(now):
            raise SourceError("模拟断线")

    def fetch_chain(self, expiration, now):
        self._maybe_fail(now)
        snap = self.chain_fn(now) if self.chain_fn else None
        if snap is None:
            raise SourceError("无数据")
        return snap

    def fetch_quotes(self, symbols, now):
        """默认用 chain_fn(now) 的报价当实时报价；quote_calls 记录每次要了几个合约（核对配额用）。"""
        self._maybe_fail(now)
        snap = self.chain_fn(now) if self.chain_fn else None
        if snap is None:
            raise SourceError("无数据")
        want = set(symbols)
        self.quote_calls.append(len(want))
        return {c.symbol: (c.bid, c.ask, c.quoted_at) for c in snap.contracts if c.symbol in want}

    def fetch_bucket(self, session, start, expiration):
        self._maybe_fail(start)
        return self.buckets.get(start) or FlowBucket(start=start, present=False)

    def fetch_big_prints(self, session, expiration, seen):
        return [b for b in self.big_prints if b.agg_id not in seen]
