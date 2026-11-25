from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from uuid import uuid4

from .clock import Calendar, NY
from .config import Config
from .mathmodel import bsm, implied, charm_per_minute
from .subscriptions import Window


def number(value):
    try:
        v = float(value)
        return v if isfinite(v) and abs(v) < 1e100 else None
    except (TypeError, ValueError, OverflowError):
        return None


def contract_id(key):
    return f"{key[0]:g}{key[1]}"


def node_reclaims(history, node):
    """Recognize a completed excursion back to the current side of the node."""
    prices = [p for _, p in sorted(dict(history).items())]
    if len(prices) < 3 or prices[-1] == node:
        return None, None
    side = "CALL" if prices[-1] > node else "PUT"
    sign = 1 if side == "CALL" else -1
    distance = [(p-node)*sign for p in prices]
    # Ignore the most recent run on the origin side; then inspect the touch/cross.
    i = len(distance)-2
    while i >= 0 and distance[i] > 0:
        i -= 1
    if i < 0:
        return None, None
    depth = 0
    while i >= 0 and distance[i] <= 0:
        depth = max(depth, -distance[i])
        i -= 1
    if i < 0:
        return None, None  # No evidence price ever started on this side.
    return (side if depth >= 3 else None), side


@dataclass
class Option:
    strike: float
    right: str
    active: bool = False
    generation: int = 0
    bid: float | None = None
    ask: float | None = None
    bid_at: float = 0
    ask_at: float = 0
    last: float | None = None
    last_at: float = 0
    last_generation: int = -1
    cumulative: float | None = None
    oi: float | None = None
    oi_date: str | None = None
    oi_date_verified: bool = False
    greeks: dict = field(default_factory=dict)
    greek_at: float = 0
    md_type: int = 0
    first_observed: float | None = None
    trades: deque = field(default_factory=deque)

    def valid_quote(self):
        return self.bid is not None and self.ask is not None and 0 < self.bid <= self.ask


class Engine:
    """Single-loop event reducer. All timestamps are UTC epoch seconds internally."""
    def __init__(self, config=None, calendar=None):
        self.cfg = config or Config()
        self.cfg.validate()
        self.calendar = calendar or Calendar(demo=self.cfg.source == "demo")
        self.window = Window(self.cfg.option_budget, self.cfg.actual_n, self.cfg.retirement_seconds)
        self.options = {}
        self.strikes = []
        self.spot = None
        self.spot_at = 0
        self.spot_type = 0
        self.vix = None
        self.connected = False
        self.status_note = "等待行情源"
        self.run_id = uuid4().hex
        self.day = None
        self.chain_date = None
        self.signals = deque(maxlen=200)
        self.signal_seq = 0
        self.daily = defaultdict(lambda: {"volume": 0., "premium": 0., "buy": 0., "sell": 0., "unknown": 0.})
        self.spot_history = deque()
        self.gex_history = deque()
        self.debug_history = deque()
        self.last_minute = -1
        self.last_debug = -1
        self.now = 0
        self.dropped = 0
        self.gap_until = 0
        self.incomplete = False
        self.errors = deque(maxlen=8)
        self.quote_delivery = "on_change"

    def option(self, key):
        key = (float(key[0]), key[1])
        if key not in self.options:
            self.options[key] = Option(*key)
        return self.options[key]

    def session(self, ts=None):
        return self.calendar.session(datetime.fromtimestamp(ts if ts is not None else self.now, timezone.utc))

    def roll_day(self, ts):
        day = datetime.fromtimestamp(ts, NY).date().isoformat()
        if self.day is None:
            self.day = day
        elif self.day != day:
            self.day = day
            self.chain_date = None
            self.strikes = []
            self.options.clear()
            self.daily.clear()
            self.signals.clear()
            self.spot_history.clear()
            self.gex_history.clear()
            self.debug_history.clear()
            self.window.reset()
            self.spot, self.spot_at, self.spot_type = None, 0, 0
            self.incomplete = False

    def transport_reset(self, reason):
        self.connected = False
        self.status_note = reason
        self.spot_type = 0
        self.incomplete = True
        for option in self.options.values():
            option.cumulative = None
            option.last = None
            option.last_generation = -1
            option.bid = option.ask = None
            option.greeks = {}
            option.first_observed = None
            option.trades.clear()
            option.md_type = 0
            option.active = False
        self.window.reset()
        self.signals.clear()

    def data_gap(self, count, ts):
        self.dropped += count
        self.gap_until = ts + 60
        self.incomplete = True
        for q in self.options.values():
            q.cumulative = None
            q.first_observed = None
            q.trades.clear()
        self.signals.clear()

    def handle(self, event):
        ts = float(event["ts"])
        self.now = max(self.now, ts)
        self.roll_day(ts)
        kind = event["kind"]
        if kind == "capacity":
            self.cfg.market_lines = int(event["market_lines"])
            self.window.budget = self.cfg.option_budget
            self.window.n = self.cfg.actual_n
            return
        if kind == "status":
            if not event.get("connected", False):
                self.transport_reset(event.get("message", "行情连接中断"))
            else:
                self.connected = True
                self.status_note = event.get("message", "已连接")
            return
        if kind == "error":
            self.errors.append({"code": event.get("code"), "message": event.get("message", "接口错误")})
            return
        if kind == "chain":
            if event.get("date") == self.day:
                self.strikes = sorted({float(k) for k in event["strikes"] if float(k) > 0})
                self.chain_date = event["date"]
            return
        if kind == "spot_type":
            self.spot_type = int(event["value"])
            return
        if kind == "spot":
            value = number(event.get("value"))
            if value and value > 0:
                self.spot, self.spot_at = value, ts
                self.spot_history.append((ts, value))
            return
        if kind == "vix":
            self.vix = number(event.get("value"))
            return
        if "key" not in event:
            return
        key = (float(event["key"][0]), event["key"][1])
        q = self.option(key)
        if kind == "subscribed":
            q.active = True
            q.generation += 1
            q.cumulative = None
            q.last = None
            q.last_generation = -1
            q.bid = q.ask = None
            q.greeks = {}
            q.md_type = 0
            q.first_observed = ts
            return
        if kind == "unsubscribed":
            q.active = False
            q.cumulative = None
            return
        if not q.active:
            return
        if kind == "md_type":
            q.md_type = int(event["value"])
        elif kind == "quote":
            field_name = event.get("field")
            v = number(event.get("value"))
            if field_name in {"bid", "ask", "last"} and v is not None and v >= 0:
                setattr(q, field_name, v)
                setattr(q, field_name + "_at", ts)
                if field_name == "last":
                    q.last_generation = q.generation
        elif kind == "greeks":
            vals = {f: number(event.get(f)) for f in ("iv", "delta", "gamma")}
            if (vals["iv"] is not None and 0 < vals["iv"] <= 5 and
                    vals["delta"] is not None and abs(vals["delta"]) <= 1 and
                    vals["gamma"] is not None and vals["gamma"] >= 0):
                q.greeks, q.greek_at = vals, ts
        elif kind == "oi":
            v = number(event.get("value"))
            if v is not None and v >= 0:
                q.oi, q.oi_date = v, event.get("date")
                q.oi_date_verified = event.get("date_verified", False)
        elif kind == "volume":
            value = number(event.get("value"))
            if value is None or value < 0:
                return
            old, q.cumulative = q.cumulative, value
            if old is None:
                q.first_observed = ts
                return  # Subscription/reconnect first value is ONLY a baseline.
            if value < old:
                q.first_observed = ts
                q.trades.clear()
                self.incomplete = True
                return
            if value > old:
                self.record_trade(key, ts, value-old, sampled=True)
        elif kind == "trade":
            self.record_trade(key, ts, number(event.get("size")) or 0,
                              price=number(event.get("price")), side=event.get("side"), sampled=False)

    def classify(self, q, ts, price=None, supplied_side=None):
        if not q.valid_quote():
            return None
        if self.quote_delivery == "streaming" and abs(q.bid_at-q.ask_at) > 1:
            return None
        if supplied_side in {"BUY", "SELL"} and price is not None and price > 0:
            return price, supplied_side, 1.0, "数据源主动方向（定义须核实）"
        has_last = q.last is not None and q.last_generation == q.generation
        used = price if price is not None else (q.last if has_last else None)
        age = 0 if price is not None else ts-q.last_at
        if used is None or used <= 0 or not q.bid <= used <= q.ask:
            return (q.bid+q.ask)/2, "UNKNOWN", .5, "缺少匹配成交价；以中间价估算金额"
        spread = q.ask-q.bid
        pos = (used-q.bid)/spread if spread > 0 else .5
        side = "BUY" if pos >= .8 else "SELL" if pos <= .2 else "UNKNOWN"
        confidence = (1.0 if side != "UNKNOWN" else .7) if age <= 1 else (.8 if side != "UNKNOWN" else .6)
        return used, side, confidence, "报价规则估算；质量系数不代表正确率"

    def record_trade(self, key, ts, size, price=None, side=None, sampled=True):
        q = self.option(key)
        if size <= 0 or not self.connected or q.md_type != 1 or self.spot_type != 1:
            return
        session = self.session(ts)
        if not session["open"] or not session["open"].timestamp() <= ts < session["close"].timestamp():
            return
        if not self.spot or ts-self.spot_at > self.cfg.spot_timeout or ts < self.gap_until:
            return
        classified = self.classify(q, ts, price, side)
        if classified is None:
            return
        price, direction, confidence, reason = classified
        trade = {"ts": ts, "size": size, "price": price, "side": direction,
                 "premium": price*size*100, "quality": confidence, "sampled": sampled}
        q.trades.append(trade)
        d = self.daily[key]
        d["volume"] += size
        d["premium"] += trade["premium"]
        d[{"BUY": "buy", "SELL": "sell", "UNKNOWN": "unknown"}[direction]] += size
        self.detect(key, ts, trade, reason)

    def detect(self, key, ts, trade, reason):
        q = self.option(key)
        if not (self.cfg.feed_min_ask <= q.ask <= self.cfg.feed_max_ask):
            return
        if (q.right == "C" and q.strike < self.spot) or (q.right == "P" and q.strike > self.spot):
            return
        factors = []
        for window in (1, 5, 10):
            current = [x for x in q.trades if ts-window < x["ts"] <= ts]
            vol = sum(x["size"] for x in current)
            prem = sum(x["premium"] for x in current)
            buys = sum(x["size"] for x in current if x["side"] == "BUY")
            for baseline in (60, 180, 300):
                if q.first_observed is None or ts-q.first_observed < baseline:
                    continue
                old = [x for x in q.trades if ts-baseline <= x["ts"] <= ts-window]
                expected_v = sum(x["size"] for x in old)*window/(baseline-window)
                expected_p = sum(x["premium"] for x in old)*window/(baseline-window)
                if expected_v >= 1 and expected_p >= 1:
                    factors.append((vol/expected_v, prem/expected_p, buys/vol if vol else 0))
        vx = max((x[0] for x in factors), default=None)
        px = max((x[1] for x in factors), default=None)
        ask_ratio = max((x[2] for x in factors), default=1. if trade["side"] == "BUY" else 0.)
        types = []
        if vx is not None and vx >= self.cfg.spike_multiple:
            types.append("volume_spike")
        if px is not None and px >= self.cfg.spike_multiple:
            types.append("premium_spike")
        if vx is not None and vx >= self.cfg.spike_multiple and ask_ratio >= .6:
            types.append("ask_buy_spike")
        if trade["side"] == "BUY" and (trade["size"] >= self.cfg.large_contracts or trade["premium"] >= self.cfg.large_premium):
            types.append("large_ask_trade")
        if not types:
            return
        previous = next((s for s in reversed(self.signals) if s["key"] == key and ts-s["last_ts"] <= 10), None)
        if previous:
            previous.update(last_ts=ts, price=trade["price"], bid=q.bid, ask=q.ask,
                            quality=min(previous["quality"], trade["quality"]))
            previous["types"] = sorted(set(previous["types"]+types))
            previous["updates"] += 1
            previous["size"] += trade["size"]
            previous["premium"] += trade["premium"]
            previous["volume_multiple"] = max(filter(lambda x: x is not None, [previous["volume_multiple"], vx]), default=None)
            previous["premium_multiple"] = max(filter(lambda x: x is not None, [previous["premium_multiple"], px]), default=None)
            previous["ask_buy"] = ask_ratio
        else:
            self.signal_seq += 1
            self.signals.append({"id": self.signal_seq, "key": key, "strike": q.strike, "right": q.right,
                                 "ts": ts, "last_ts": ts, "types": types, "updates": 1,
                                 "size": trade["size"], "premium": trade["premium"], "price": trade["price"],
                                 "bid": q.bid, "ask": q.ask, "quality": trade["quality"], "note": reason,
                                 "ask_buy": ask_ratio, "volume_multiple": vx, "premium_multiple": px,
                                 "sampled": trade["sampled"]})

    def analytics(self, q, now, remaining):
        if not q.active or q.md_type != 1 or remaining <= 0 or not self.spot:
            return None
        quote_time = max(q.bid_at, q.ask_at)
        vendor = q.greeks if 0 <= now-q.greek_at <= self.cfg.greek_timeout else None
        valid_mid = q.valid_quote() and q.ask > q.bid
        if valid_mid:
            mid = (q.bid+q.ask)/2
            valid_mid = q.ask-q.bid <= 1 and q.ask-q.bid <= .25*mid
        if valid_mid and (vendor is None or quote_time > q.greek_at):
            calculated = implied(self.spot, q.strike, remaining, mid, q.right, self.cfg.rate, self.cfg.dividend)
            if calculated:
                return dict(calculated, source="报价反推 BSM", input_time=quote_time)
        if vendor:
            return dict(vendor, source="券商模型", input_time=q.greek_at)
        return None

    def model_modules(self, now):
        session = self.session(now)
        remaining = session["close"].timestamp()-now if session["close"] else -1
        selected = [self.option(k) for k in self.wanted()]
        computed = {}
        for q in selected:
            try:
                a = self.analytics(q, now, remaining)
                if a:
                    computed[(q.strike, q.right)] = a
            except (ValueError, ArithmeticError):
                continue
        by_strike = defaultdict(lambda: {"call": 0., "put": 0., "valid": 0})
        valid_gex = 0
        oi_unverified = False
        for q in selected:
            a = computed.get((q.strike, q.right))
            if a is None or q.oi is None or q.oi_date != session["previous"]:
                continue
            exposure = a["gamma"]*q.oi*100*self.spot**2*.01
            row = by_strike[q.strike]
            row["call" if q.right == "C" else "put"] += exposure*(1 if q.right == "C" else -1)
            row["valid"] += 1
            valid_gex += 1
            oi_unverified |= not q.oi_date_verified
        coverage = valid_gex/len(selected) if selected else 0
        rows = [{"strike": k, "call": v["call"], "put": v["put"], "net": v["call"]+v["put"],
                 "valid": v["valid"]} for k, v in sorted(by_strike.items())]
        available = coverage >= .2 and bool(rows)
        top = sorted(rows, key=lambda x: abs(x["net"]), reverse=True)[:3] if available else []
        total = sum(x["net"] for x in rows) if available else None
        gex = {"available": available, "coverage": coverage, "rows": rows if available else [],
               "total": total, "regime": ("POSITIVE" if total > 0 else "NEGATIVE" if total < 0 else "ZERO") if total is not None else "UNAVAILABLE",
               "top": top, "call_wall": max(rows, key=lambda x: x["call"])["strike"] if available else None,
               "put_wall": min(rows, key=lambda x: x["put"])["strike"] if available else None,
               "scope": "ATM 订阅窗口内 · Call 正 / Put 负假设", "oi_date": session["previous"],
               "oi_date_inferred": oi_unverified, "reason": None if available else "有效 Gamma＋OI 覆盖不足 20%"}
        iv_coverage = len(computed)/len(selected) if selected else 0
        atm = min(self.strikes, key=lambda k: abs(k-self.spot)) if self.strikes and self.spot else None
        ca, pa = computed.get((atm, "C")), computed.get((atm, "P"))
        calls = [(k, a) for (k, r), a in computed.items() if r == "C" and k >= self.spot]
        puts = [(k, a) for (k, r), a in computed.items() if r == "P" and k <= self.spot]
        c25 = min(calls, key=lambda x: abs(abs(x[1]["delta"])-.25), default=None)
        p25 = min(puts, key=lambda x: abs(abs(x[1]["delta"])-.25), default=None)
        enough_delta = c25 and p25 and all(abs(abs(x[1]["delta"])-.25) <= .15 for x in (c25, p25))
        skew_ok = bool(ca and pa and enough_delta and iv_coverage >= .35)
        skew = {"available": skew_ok, "coverage": iv_coverage,
                "atm_iv": (ca["iv"]+pa["iv"])/2 if ca and pa else None,
                "rr25": c25[1]["iv"]-p25[1]["iv"] if skew_ok else None,
                "reason": None if skew_ok else "IV 覆盖不足，或找不到接近 25Δ 的双侧合约"}
        now_et = datetime.fromtimestamp(now, NY)
        charm_open = now_et.hour == 15 and now_et.minute >= 30 and remaining > 10
        local_keys = sorted({q.strike for q in selected}, key=lambda k: abs(k-self.spot))[:21] if self.spot else []
        local = [q for q in selected if q.strike in local_keys]
        qualified = [q for q in local if (q.strike, q.right) in computed and q.oi is not None and q.oi_date == session["previous"]]
        charm_coverage = len(qualified)/len(local) if local else 0
        dealer_change = 0.
        charm_count = 0
        if charm_open:
            for q in selected:
                a = computed.get((q.strike, q.right))
                if a is None or q.oi is None or q.oi_date != session["previous"]:
                    continue
                value = charm_per_minute(self.spot, q.strike, remaining, a["iv"], q.right, self.cfg.rate, self.cfg.dividend)
                if value is not None:
                    sign = self.cfg.charm_call_sign if q.right == "C" else self.cfg.charm_put_sign
                    dealer_change += value*q.oi*100*self.spot*sign
                    charm_count += 1
        flow = -dealer_change
        charm_ok = charm_open and charm_count > 0
        charm = {"available": charm_ok, "direction": "BUY" if flow > 0 else "SELL" if flow < 0 else "NEUTRAL",
                 "flow_dollars_per_minute": flow if charm_ok else None, "coverage": charm_coverage,
                 "stability": "NORMAL" if charm_coverage >= .7 and remaining >= 120 else "LOW_STABILITY",
                 "position_assumption": "Call 与 Put 均假设做市商净空；与 GEX 是不同情景",
                 "reason": None if charm_ok else "仅 15:30 后且距到期超过 10 秒、OI/IV 有效时计算"}
        return gex, skew, charm, remaining

    def wanted(self):
        if not self.spot or not self.strikes:
            return set()
        i = min(range(len(self.strikes)), key=lambda j: abs(self.strikes[j]-self.spot))
        return {(k, r) for k in self.strikes[max(0, i-self.cfg.actual_n):i+self.cfg.actual_n+1] for r in ("C", "P")}

    def leaders(self, now, seconds=300, sort="premium"):
        rows = []
        for key, q in self.options.items():
            if seconds == 0:
                d = self.daily.get(key, {})
                vol, prem, buy, sell, unknown = (d.get(x, 0.) for x in ("volume", "premium", "buy", "sell", "unknown"))
            else:
                trades = [t for t in q.trades if now-seconds <= t["ts"] <= now]
                vol, prem = sum(t["size"] for t in trades), sum(t["premium"] for t in trades)
                buy = sum(t["size"] for t in trades if t["side"] == "BUY")
                sell = sum(t["size"] for t in trades if t["side"] == "SELL")
                unknown = vol-buy-sell
            if vol <= 0:
                continue
            signal_count = sum(s["key"] == key and (seconds == 0 or s["last_ts"] >= now-seconds) for s in self.signals)
            rows.append({"strike": q.strike, "right": q.right, "volume": vol, "premium": prem,
                         "buy": buy, "sell": sell, "unknown": unknown, "net": buy-sell,
                         "signals": signal_count, "active": any(s["key"] == key and now-s["last_ts"] <= 10 for s in self.signals),
                         "price": (q.bid+q.ask)/2 if q.valid_quote() else None,
                         "distance": q.strike-self.spot if self.spot else None})
        sort = sort if sort in {"premium", "net", "signals"} else "premium"
        return sorted(rows, key=lambda x: x[sort], reverse=True)[:50]

    def tail_price_pattern(self, now, gex):
        """Optional, source-validated observation; no inferred history for other feeds."""
        return None

    def flow_vote_supported(self, trades, right):
        return True

    def playbook(self, now, gex, skew, charm, remaining, healthy):
        et = datetime.fromtimestamp(now, NY)
        in_window = bool(et.hour == 15 and et.minute >= 30 and remaining > 0)
        votes = []

        def vote(name, side=None, points=0, reason="", value=None):
            votes.append({"name": name, "side": side, "points": points, "reason": reason, "value": value})

        trades = [(key, t) for key, q in self.options.items() if self.spot and abs(key[0]/self.spot-1) <= .01
                  for t in q.trades if now-600 <= t.get("flow_at", t["ts"]) <= now and t["ts"] <= now]
        volumes = {r: sum(t["size"] for k, t in trades if k[1] == r) for r in ("C", "P")}
        total = sum(volumes.values())
        if total >= 200:
            bias = (volumes["C"]-volumes["P"])/total
            side = "CALL" if bias >= .15 else "PUT" if bias <= -.15 else None
            if side and not self.flow_vote_supported(trades, side[0]):
                vote("strike_bias", reason="领先侧仅一笔独立聚合单；异动保留，不单独判方向", value=bias)
            else:
                vote("strike_bias", side, 40 if side else 0, "成交量 C/P 偏向；未把 Call 量当作买开证明", bias)
        else:
            vote("strike_bias", reason="近 10 分钟带内观察量不足 200 张")
        if charm["available"] and charm["direction"] != "NEUTRAL":
            vote("charm_hedge_flow", "CALL" if charm["direction"] == "BUY" else "PUT",
                 20 if charm["stability"] == "NORMAL" else 10, "双边净空情景；非真实仓位", charm["flow_dollars_per_minute"])
        else:
            vote("charm_hedge_flow", reason=charm["reason"] or "方向为零")
        near = min(gex["top"], key=lambda r: abs(r["strike"]-self.spot), default=None) if self.spot else None
        window_start = et.replace(hour=15, minute=30, second=0, microsecond=0).timestamp()
        history = sorted({t: p for t, p in self.spot_history if max(now-180, window_start) <= t <= now}.items())
        prices = [p for _, p in history]
        # Define "near" explicitly: <= 10 SPX points; avoid voting on a distant wall.
        node = near["strike"] if near and abs(near["strike"]-self.spot) <= 10 else None
        late = in_window and et.minute >= 50
        if late and node is not None and len(prices) >= 3:
            crossed = (prices[0]-node)*(self.spot-node) < 0
            if crossed:
                vote("key_level", "CALL" if self.spot > node else "PUT", 15, "180 秒窗口两端跨越近端节点", node)
            elif self.spot > node and min(prices[-3:]) > node:
                vote("key_level", "CALL", 15, "最近三个样本守在节点上方", node)
            elif self.spot < node and max(prices[-3:]) < node:
                vote("key_level", "PUT", 15, "最近三个样本压在节点下方", node)
            else:
                vote("key_level", reason="尚无守住/跨越形态", value=node)
        else:
            vote("key_level", reason="未到 15:50、缺价格样本或 10 点内无 Top 节点")
        if skew["available"] and skew["rr25"] <= -.005:
            vote("skew_rr25", "PUT", 10, "原说明中的单侧倾向规则；不是预测概率", skew["rr25"])
        else:
            vote("skew_rr25", reason=skew["reason"] or "RR25 未达到 PUT 阈值", value=skew["rr25"])
        breakout, rejection = node_reclaims(history, node) if node is not None else (None, None)
        vote("breakout_integrity", breakout, 15 if breakout else 0,
             "180 秒内穿出节点至少 3 点后返回起始侧" if breakout else "无满足 3 点穿越的回收形态",
             node if breakout else None)
        if late and rejection:
            points = 15
            reasons = ["触及节点后返回起始侧"]
            if charm["available"] and charm["direction"] == ("BUY" if rejection == "CALL" else "SELL"):
                points += 10
                reasons.append("Charm 同向（重复信息加分）")
            recent = self.atm_flow(now)
            if recent["side"] == rejection:
                points += 10
                reasons.append("ATM 量偏向同向（重复信息加分）")
            vote("rejection_reversal", rejection, points, "；".join(reasons), node)
        else:
            vote("rejection_reversal", reason="未到 15:50 或无拒绝形态")
        pattern = self.tail_price_pattern(now, gex) if in_window and healthy and gex["available"] else None
        if pattern:
            # The same price path may already have earned node/reclaim points.
            # Top up to this pattern's weight, rather than voting for the move twice.
            existing = sum(v["points"] for v in votes if v["side"] == pattern["side"] and v["name"] in
                           {"key_level", "breakout_integrity", "rejection_reversal"})
            vote("price_path", pattern["side"], max(0, pattern["weight"]-existing),
                 pattern["reason"] + ("；已抵扣同向价格结构分" if existing else ""), pattern.get("node"))
        counts = {side: sum(v["points"] for v in votes if v["side"] == side) for side in ("CALL", "PUT")}
        lead = "CALL" if counts["CALL"] > counts["PUT"] else "PUT" if counts["PUT"] > counts["CALL"] else None
        if gex["available"] and gex["regime"] == "NEGATIVE" and lead:
            vote("gex_regime", lead, 15, "负 Gamma 假设只强化已有领先方，不独立选边")
        else:
            vote("gex_regime", reason="正 Gamma：钉住风险" if gex["regime"] == "POSITIVE" else "覆盖不足或尚无领先方")
        atm = self.atm_flow(now)
        vote("atm_flow_5m", reason="只展示，不计分", value=atm)
        counts = {side: sum(v["points"] for v in votes if v["side"] == side) for side in ("CALL", "PUT")}
        direction = ("CALL" if counts["CALL"] > counts["PUT"] else "PUT") if abs(counts["CALL"]-counts["PUT"]) >= self.cfg.decision_margin else "NEUTRAL"
        reason = None
        if not healthy:
            direction, reason = "STAND_BY", "数据未确认实时、连接异常、日历/现货过期或队列有缺口"
        elif not in_window:
            direction, reason = "STAND_BY", "仅美东 15:30 至当日收盘进入观察窗口"
        reference = self.playbook_reference(now, "C" if direction == "CALL" else "P") if direction in {"CALL", "PUT"} else None
        return {"direction": direction, "scores": counts, "evidence": votes, "reason": reason, "pattern": pattern,
                "conflict": counts["CALL"] > 0 and counts["PUT"] > 0, "reference": reference, "in_window": in_window,
                "margin": self.cfg.decision_margin,
                "notice": "仅观察 · 非建议 · 分数不是胜率，证据存在相关性"}

    def playbook_reference(self, now, right):
        if not self.spot:
            return None
        choice = [(q, q.cumulative) for k in self.wanted() if k[1] == right
                  and abs(k[0]/self.spot-1) <= .01
                  and (q := self.option(k)).active and q.md_type == 1 and q.valid_quote()
                  and q.cumulative is not None and q.cumulative > 0]
        return self.reference_row(choice, "当日带内成交量")

    def reference_row(self, choice, basis):
        if not choice:
            return None
        q, volume = max(choice, key=lambda x: (x[1], -abs(x[0].strike-self.spot), -x[0].strike))
        return {"right": q.right, "strike": q.strike, "bid": q.bid, "ask": q.ask,
                "basis": basis, "volume": volume, "note": f"按{basis}排序的同向参考合约，仅供观察"}

    def atm_flow(self, now):
        values = {r: sum(t["size"] for (k, rr), q in self.options.items() if rr == r and self.spot and abs(k-self.spot) <= 10
                         for t in q.trades if now-300 <= t.get("flow_at", t["ts"]) <= now and t["ts"] <= now) for r in ("C", "P")}
        total = sum(values.values())
        diff = (values["C"]-values["P"])/total if total else 0
        return {"call": values["C"], "put": values["P"], "side": "CALL" if total >= 50 and diff >= .1 else "PUT" if total >= 50 and diff <= -.1 else None}

    def trim(self, now):
        while self.spot_history and self.spot_history[0][0] < now-5400:
            self.spot_history.popleft()
        for key, q in list(self.options.items()):
            while q.trades and q.trades[0]["ts"] < now-900:
                q.trades.popleft()
        while self.gex_history and self.gex_history[0]["ts"] < now-3600:
            self.gex_history.popleft()
        while self.debug_history and self.debug_history[0]["ts"] < now-600:
            self.debug_history.popleft()

    def snapshot(self, now=None, leader_seconds=300, leader_sort="premium"):
        now = now if now is not None else self.now
        self.now = max(now, self.now)
        self.roll_day(now)
        self.trim(now)
        session = self.session(now)
        market_open = bool(session["open"] and session["open"].timestamp() <= now < session["close"].timestamp())
        spot_fresh = bool(self.spot and now-self.spot_at <= self.cfg.spot_timeout)
        healthy = self.connected and self.spot_type == 1 and spot_fresh and market_open and now >= self.gap_until
        warnings = []
        if not self.connected:
            warnings.append(self.status_note)
        if not session["verified"]:
            warnings.append(session["note"])
        if not market_open:
            warnings.append("休市 / 正常观察时段之外")
        if not spot_fresh:
            warnings.append("SPX 现价尚未就绪或超过 45 秒未更新")
        if self.spot_type != 1:
            warnings.append("SPX 行情尚未确认实时")
        if now < self.gap_until:
            warnings.append("队列发生丢弃；重建基线期间待命")
        for error in list(self.errors)[-2:]:
            warnings.append(f"IBKR {error['code']}：{error['message']}")
        wanted = self.wanted()
        real_options = sum(self.option(k).active and self.option(k).md_type == 1 and self.option(k).valid_quote() for k in wanted)
        option_realtime = real_options/len(wanted) if wanted else 0
        if option_realtime < .5:
            warnings.append("实时有效期权报价覆盖不足 50%；方向待命")
            healthy = False
        if len(wanted-self.window.active.keys()):
            warnings.append("窗口移动中；等待旧合约退订释放额度")
        if self.incomplete:
            warnings.append("本次观察统计含中断/重订阅缺口；不是完整全日成交")
        try:
            gex, skew, charm, remaining = self.model_modules(now)
        except Exception:
            gex = {"available": False, "rows": [], "top": [], "regime": "UNAVAILABLE", "coverage": 0,
                   "total": None, "reason": "计算失败", "scope": "窗口内估算", "oi_date": None, "oi_date_inferred": True}
            skew = {"available": False, "reason": "计算失败", "rr25": None}
            charm = {"available": False, "reason": "计算失败", "direction": "NEUTRAL"}
            remaining = session["close"].timestamp()-now if session["close"] else -1
        book = self.playbook(now, gex, skew, charm, remaining, healthy)
        if not gex["available"]:
            warnings.append(gex["reason"])
        observed = sum(d["volume"] for d in self.daily.values())
        known = sum(d["buy"]+d["sell"] for d in self.daily.values())
        minute = int(now//60)
        if minute != self.last_minute:
            self.last_minute = minute
            self.gex_history.append({"ts": now, "available": gex["available"], "top": gex["top"], "total": gex["total"]})
        ten = int(now//10)
        if ten != self.last_debug:
            self.last_debug = ten
            self.debug_history.append({"ts": now, "skew": skew, "charm": charm})
        counts = {str(m): {r: sum(s["right"] == r and now-m*60 <= s["last_ts"] <= now for s in self.signals) for r in ("C", "P")} for m in (1, 3, 5, 10)}
        return {"source": self.cfg.source, "demo": self.cfg.source == "demo", "time": now, "run_id": self.run_id,
                "time_et": datetime.fromtimestamp(now, NY).strftime("%Y-%m-%d %H:%M:%S ET"),
                "spot": self.spot, "spot_at": self.spot_at, "vix": self.vix, "remaining": max(0, remaining),
                "connected": self.connected, "healthy": healthy, "status": self.status_note,
                "market_open": market_open, "quote_delivery": self.quote_delivery,
                "active_options": len(self.window.active), "active_lines": len(self.window.active)+1+int(self.cfg.vix_enabled),
                "budget": self.cfg.active_budget, "line_limit": self.cfg.market_lines,
                "desired_options": len(wanted), "quote_coverage": option_realtime,
                "known_direction_ratio": known/observed if observed else None, "observed_volume": observed,
                "dropped": self.dropped, "warnings": warnings, "counts": counts,
                "signals": list(reversed(self.signals)), "leaders": self.leaders(now, leader_seconds, leader_sort),
                "gex": gex, "playbook": book, "errors": list(self.errors)}
