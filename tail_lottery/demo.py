"""合成行情：测试与 --demo 用。用 BSM 按给定现价路径生成期权链，按脚本生成 1 分钟成交桶。不碰网络、不花配额。"""
from __future__ import annotations

import datetime as dt
import math
import random
import time

from config import et_datetime

from . import bsm
from .source import BigPrint, ChainSnapshot, Contract, ContractFlow, FakeSource, FlowBucket

DEMO_DAY = dt.date(2026, 11, 17)   # 周二，正常 16:00 收盘


def make_chain(now, spot, close, expiration, iv=0.20, skew=0.0, strikes_each=40, step=5.0, oi_fn=None,
               quote_age_s=2.0, greeks_age_s=2.0, spread=0.10):
    """iv 平值波动率；skew 每 1% 虚值的 IV 斜率（负 = Put 贵）。"""
    T = max(1.0, (close - now).total_seconds()) / bsm.YEAR_S
    k0 = round(spot / step) * step
    cs = []
    for i in range(-strikes_each, strikes_each + 1):
        k = k0 + i * step
        for r in ("C", "P"):
            m = (k - spot) / spot * 100
            vol = max(0.05, iv + skew * m)
            px = bsm.price(spot, k, T, vol, r)
            bid = max(0.0, round(px - spread / 2, 2))
            ask = round(max(px + spread / 2, 0.05), 2)
            oi = oi_fn(k, r) if oi_fn else 1000
            cs.append(Contract(symbol=f"SPXW{expiration:%y%m%d}{r}{int(k * 1000):08d}", strike=k, right=r, bid=bid, ask=ask,
                               quoted_at=now - dt.timedelta(seconds=quote_age_s), oi=oi, iv=vol,
                               delta=bsm.delta(spot, k, T, vol, r), gamma=bsm.gamma(spot, k, T, vol)))
    return ChainSnapshot(fetched_at=now, expiration=expiration, quote_as_of=now - dt.timedelta(seconds=quote_age_s),
                         greeks_as_of=now - dt.timedelta(seconds=greeks_age_s), oi_as_of=now - dt.timedelta(hours=8),
                         underlying_price=spot, underlying_as_of=now - dt.timedelta(minutes=15), contracts=cs)


def make_bucket(start, rows, call_known=0.9, put_known=0.9):
    """rows: [(strike, right, contracts, gross, net)]"""
    cr = [ContractFlow(symbol=f"X{k}{r}", strike=k, right=r, contracts=n, gross_premium=g, net_premium=net)
          for k, r, n, g, net in rows]
    cg = sum(x.gross_premium for x in cr if x.right == "C")
    pg = sum(x.gross_premium for x in cr if x.right == "P")
    cn = sum(x.net_premium for x in cr if x.right == "C")
    pn = sum(x.net_premium for x in cr if x.right == "P")
    s = {"present": True, "gross_premium_usd": cg + pg, "call_premium_usd": cg, "put_premium_usd": pg,
         "call_ask_premium_usd": (call_known * cg + cn) / 2, "call_bid_premium_usd": (call_known * cg - cn) / 2,
         "put_ask_premium_usd": (put_known * pg + pn) / 2, "put_bid_premium_usd": (put_known * pg - pn) / 2}
    return FlowBucket(start=start, present=True, summary=s, rows=cr)


def demo_engine(speed=10.0, seed=7, clock=None):
    """15:25 ET 起的模拟时钟（speed 倍速）；现价先上涨再在 7330 附近来回，Call 侧成交偏多。"""
    from .engine import Engine
    rnd = random.Random(seed)
    close = et_datetime(DEMO_DAY, "16:00").astimezone(dt.timezone.utc)
    t_start = et_datetime(DEMO_DAY, "14:58").astimezone(dt.timezone.utc)
    t_real0 = time.time()
    t_demo0 = et_datetime(DEMO_DAY, "15:25").astimezone(dt.timezone.utc)

    def real_clock():
        return t_demo0 + dt.timedelta(seconds=(time.time() - t_real0) * speed)

    clock = clock or real_clock

    def spot_at(t):
        m = (t - t_start).total_seconds() / 60
        return 7310 + 20 * math.tanh(m / 20) + 3 * math.sin(m / 3)

    def oi_fn(k, r):   # Call 持仓集中在 7340，Put 集中在 7315
        c = 7340 if r == "C" else 7315
        return int((3000 if r == "C" else 4000) * math.exp(-((k - c) / 20) ** 2) + 200)

    buckets = {}
    t = t_start - dt.timedelta(minutes=13)
    while t < close:
        s = spot_at(t)
        rows = []
        for i in range(-12, 13):
            k = round(s / 5) * 5 + 5 * i
            for r in ("C", "P"):
                n = max(1, int(rnd.expovariate(1 / (60 if r == "C" else 40)) * math.exp(-abs(i) / 6)))
                if rnd.random() < 0.01:
                    n *= 12
                px = max(0.1, bsm.price(s, k, max(60.0, (close - t).total_seconds()) / bsm.YEAR_S, 0.2, r))
                g = n * px * 100
                net = g * (rnd.uniform(0.0, 0.5) if r == "C" else rnd.uniform(-0.3, 0.3))
                rows.append((k, r, n, g, net))
        buckets[t] = make_bucket(t, rows)
        t += dt.timedelta(minutes=1)

    def chain_fn(now):
        if now < t_start:
            return None
        return make_chain(now, spot_at(now), close, DEMO_DAY, oi_fn=oi_fn, skew=-0.004)

    class DemoSource(FakeSource):
        def fetch_bucket(self, session, start, expiration):
            if start >= clock().replace(second=0, microsecond=0):
                return FlowBucket(start=start, present=False)
            return super().fetch_bucket(session, start, expiration)

        def fetch_big_prints(self, session, expiration, seen):
            now = clock()
            out = []
            if rnd.random() < 0.05:
                s = spot_at(now)
                k = round(s / 5) * 5 + 5
                out.append(BigPrint(agg_id=f"demo-{now.timestamp()}", at=now, symbol="", strike=k, right="C",
                                    contracts=80, premium=80 * 150, price=1.5, bid=1.4, ask=1.5, side="at_ask"))
            return out

    src = DemoSource(chain_fn=chain_fn, buckets=buckets)
    src.capabilities = dict(src.capabilities, name="假行情源（演示）")
    return Engine(src, notifier=None, clock=clock, push_enabled=False)
