"""Deterministic synthetic sampling feed. Always labelled DEMO, never broker data."""
from datetime import datetime
from math import sin
from .clock import NY
from .mathmodel import bsm


class Demo:
    def __init__(self, engine):
        self.engine = engine
        self.start = datetime(2026, 11, 13, 15, 50, tzinfo=NY).timestamp()
        self.clock = self.start
        self.step = 0
        self.volumes = {}

    def event(self, kind, ts, **fields):
        self.engine.handle({"kind": kind, "ts": ts, **fields})

    def bootstrap(self):
        self.event("status", self.start-80, connected=True, message="演示假行情 · 未连接券商")
        self.event("chain", self.start-80, date="2026-11-13", strikes=list(range(7455, 7656, 5)))
        self.event("spot_type", self.start-80, value=1)
        for i in range(-80, 1):
            self.advance(self.start+i, i)

    def advance(self, ts=None, step=None):
        if ts is None:
            self.step += 1
            ts, step = self.start+self.step, self.step
        self.clock = ts
        spot = 7553.8 + .8*sin(step/18) + .25*sin(step/5)
        self.event("spot", ts, value=spot)
        removed, added, _ = self.engine.window.update(spot, self.engine.strikes, ts)
        for key in removed:
            self.event("unsubscribed", ts, key=key)
        for key in added:
            self.event("subscribed", ts, key=key)
            self.event("md_type", ts, key=key, value=1)
            self.event("oi", ts, key=key, value=2200+int(1000/(1+abs(key[0]-7555)/5)),
                       date="2026-11-12", date_verified=True)
            self.volumes.setdefault(key, 1000)
            self.event("volume", ts, key=key, value=self.volumes[key])
        remaining = max(11, datetime(2026, 11, 13, 16, tzinfo=NY).timestamp()-ts)
        for key in sorted(self.engine.window.active):
            sigma = .19 + (.004 if key[1] == "P" else 0)
            g = bsm(spot, key[0], remaining, sigma, key[1])
            mid = max(.025, round(g["price"], 2))
            bid, ask = max(.01, round(mid-.025, 2)), max(.02, round(mid+.025, 2))
            self.event("quote", ts, key=key, field="bid", value=bid)
            self.event("quote", ts, key=key, field="ask", value=ask)
            self.event("greeks", ts, key=key, iv=sigma, delta=g["delta"], gamma=g["gamma"])
            if step % 3 == 0:
                burst = key[1] == "C" and key[0] in {7555, 7560} and step >= -5
                qty = 65+(step % 11) if burst else 3
                self.event("quote", ts, key=key, field="last", value=ask if burst or step % 2 == 0 else bid)
                self.volumes[key] += qty
                self.event("volume", ts, key=key, value=self.volumes[key])

