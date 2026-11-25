"""IBKR TWS/Gateway adapter. Only contract discovery and market-data requests.

The network decoder invokes callbacks which only enqueue events. A separate
worker issues paced requests; the application loop owns all calculations.
"""
from collections import deque
from datetime import datetime, timezone
from queue import Empty, Queue
import threading
import time

from .clock import Calendar, NY


class IBKRAdapter:
    def __init__(self, config, emit, calendar=None):
        self.cfg, self.emit = config, emit
        self.calendar = calendar or Calendar()
        self.client = None
        self.commands = Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.next_id = 1000
        self.contexts = {}
        self.active = {}
        self.pending = set()
        self.ready = False
        self.session_date = None
        self.chains = set()
        self.discovered = False
        self.next_send = 0.
        self.lock = threading.RLock()
        self.quota_failures = 0

    def event(self, kind, **fields):
        self.emit({"kind": kind, "ts": time.time(), **fields})

    def start(self):
        self.thread = threading.Thread(target=self._worker, daemon=True, name="ibkr-control")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.client is not None:
            self.client.disconnect()
        if self.thread:
            self.thread.join(timeout=3)

    def command(self, action, key=None):
        self.commands.put((action, key))

    def _id(self, context):
        with self.lock:
            self.next_id += 1
            self.contexts[self.next_id] = context
            return self.next_id

    def _call(self, name, *args):
        # All outbound calls, including contract-detail requests, share one limiter.
        wait = self.next_send-time.monotonic()
        if wait > 0:
            self.stop_event.wait(wait)
        if self.stop_event.is_set():
            return
        getattr(self.client, name)(*args)
        self.next_send = time.monotonic()+1/self.cfg.request_rate

    def _clear(self):
        with self.lock:
            self.contexts.clear()
            self.active.clear()
            self.pending.clear()
        self.chains.clear()
        self.discovered = False
        self.ready = False
        while True:
            try:
                self.commands.get_nowait()
            except Empty:
                break

    def _worker(self):
        try:
            from ibapi.client import EClient
            from ibapi.wrapper import EWrapper
            from ibapi.contract import Contract
        except ImportError:
            self.event("status", connected=False, message="缺少 ibapi；请先安装 requirements.txt")
            return
        self.Contract = Contract
        owner = self

        class Bridge(EWrapper, EClient):
            def __init__(self):
                EClient.__init__(self, self)

            def nextValidId(self, orderId):
                owner.event("control_ready")  # ID is ignored; no order APIs are used.

            def connectionClosed(self):
                owner.event("status", connected=False, message="IBKR 连接关闭；等待重连")

            def error(self, *args):
                # Newer SDKs add errorTime. Keep account/session identifiers out of logs.
                if len(args) < 3:
                    return
                req_id = args[0]
                code = args[2] if len(args) >= 4 and isinstance(args[2], int) else args[1]
                owner.event("control_error", req_id=req_id, code=code)

            def contractDetails(self, reqId, details):
                c = details.contract
                owner.event("control_contract", req_id=reqId, con_id=c.conId,
                            symbol=c.symbol, right=c.right, strike=c.strike,
                            trading_class=c.tradingClass,
                            expiry=c.lastTradeDateOrContractMonth[:8], multiplier=c.multiplier)

            def contractDetailsEnd(self, reqId):
                owner.event("control_contract_end", req_id=reqId)

            def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId,
                                                  tradingClass, multiplier, expirations, strikes):
                owner.event("control_chain", req_id=reqId, exchange=exchange,
                            trading_class=tradingClass, multiplier=multiplier,
                            expirations=list(expirations), strikes=list(strikes))

            def securityDefinitionOptionParameterEnd(self, reqId):
                owner.event("control_chain_end", req_id=reqId)

            def tickPrice(self, reqId, tickType, price, attrib):
                owner.event("control_price", req_id=reqId, tick_type=tickType, value=price)

            def tickSize(self, reqId, tickType, size):
                owner.event("control_size", req_id=reqId, tick_type=tickType, value=float(size))

            def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol, delta,
                                      optPrice, pvDividend, gamma, vega, theta, undPrice):
                if tickType == 13:
                    owner.event("control_greeks", req_id=reqId, iv=impliedVol, delta=delta, gamma=gamma)

            def marketDataType(self, reqId, marketDataType):
                owner.event("control_type", req_id=reqId, value=marketDataType)

        backoff = 1
        while not self.stop_event.is_set():
            now = datetime.now(timezone.utc)
            session = self.calendar.session(now)
            if not session["open"] or not session["open"] <= now < session["close"]:
                self.event("status", connected=False, message="休市 / 非 RTH；接口保持待命")
                self.stop_event.wait(15)
                continue
            self.session_date = session["date"]
            self._clear()
            try:
                self.client = Bridge()
                self.client.connect(self.cfg.ib_host, self.cfg.ib_port, self.cfg.ib_client_id)
                if not self.client.isConnected():
                    raise ConnectionError("gateway unavailable")
                reader = threading.Thread(target=self.client.run, daemon=True, name="ibkr-reader")
                reader.start()
                connected_at = time.monotonic()
                while self.client.isConnected() and not self.stop_event.is_set():
                    current = datetime.now(timezone.utc)
                    if current >= session["close"] or current.astimezone(NY).date().isoformat() != self.session_date:
                        break
                    if not self.ready and time.monotonic()-connected_at > 20:
                        raise TimeoutError("IBKR handshake timeout")
                    try:
                        action, payload = self.commands.get(timeout=.1)
                    except Empty:
                        continue
                    self._execute(action, payload)
                    if self.ready:
                        backoff = 1
            except Exception:
                self.event("status", connected=False, message="IBKR 未就绪；检查网关、端口、API 设置与行情权限")
            finally:
                if self.client is not None:
                    self.client.disconnect()
                self.event("status", connected=False,
                           message="IBKR 接口已暂停；检查配置后重启" if self.stop_event.is_set() else "IBKR 未连接；自动退避重连")
            self.stop_event.wait(backoff)
            backoff = min(30, backoff*2)

    def _execute(self, action, payload):
        if action == "ready":
            self.ready = True
            self._call("reqMarketDataType", 1)
            self.event("status", connected=True, message="IBKR 已连接；等待实时行情确认")
            c = self.Contract()
            c.symbol, c.secType, c.exchange, c.currency = "SPX", "IND", "CBOE", "USD"
            self._call("reqContractDetails", self._id({"kind": "underlying"}), c)
            if self.cfg.vix_enabled:
                v = self.Contract()
                v.symbol, v.secType, v.exchange, v.currency = "VIX", "IND", "CBOE", "USD"
                self._call("reqMktData", self._id({"kind": "vix"}), v, "", False, False, [])
        elif action == "underlying_found":
            con_id = payload
            c = self.Contract()
            c.conId, c.symbol, c.secType, c.exchange, c.currency = con_id, "SPX", "IND", "CBOE", "USD"
            self._call("reqMktData", self._id({"kind": "spot"}), c, "", False, False, [])
            self._call("reqSecDefOptParams", self._id({"kind": "chain"}), "SPX", "", "IND", con_id)
        elif action == "subscribe":
            key = payload
            with self.lock:
                if key in self.active or key in self.pending:
                    return
                self.pending.add(key)
            c = self.Contract()
            c.symbol, c.secType, c.exchange, c.currency = "SPX", "OPT", "SMART", "USD"
            c.lastTradeDateOrContractMonth = self.session_date.replace("-", "")
            c.tradingClass, c.multiplier = "SPXW", "100"
            c.strike, c.right = key
            self._call("reqContractDetails", self._id({"kind": "option_resolve", "key": key}), c)
        elif action == "resolved":
            key, con_id = payload
            with self.lock:
                self.pending.discard(key)
                if key in self.active:
                    return
                c = self.Contract()
                c.conId, c.symbol, c.secType, c.exchange, c.currency = con_id, "SPX", "OPT", "SMART", "USD"
                req_id = self._id({"kind": "option", "key": key})
                self.active[key] = req_id
            self.event("subscribed", key=key)
            self._call("reqMktData", req_id, c, "100,101", False, False, [])
        elif action == "unsubscribe":
            key = payload
            with self.lock:
                req_id = self.active.pop(key, None)
                self.pending.discard(key)
                # Remove all pending request contexts too, so late replies cannot resubscribe.
                for request, context in list(self.contexts.items()):
                    if context.get("key") == key:
                        del self.contexts[request]
            if req_id is not None:
                self._call("cancelMktData", req_id)
            self.event("unsubscribed", key=key)

    def dispatch(self, event):
        """Called only by the application calculation loop."""
        kind, ts = event["kind"], event["ts"]
        if kind == "control_ready":
            self.command("ready")
            return []
        if kind == "control_error":
            code = event["code"]
            if code in {2104, 2106, 2107, 2108, 2158}:
                return []
            if code in {1100, 1101, 1102, 1300, 2110}:
                if self.client:
                    self.client.disconnect()  # Reconnect rebuilds every volume baseline.
                return [{"kind": "status", "ts": ts, "connected": False, "message": f"行情链路重建（{code}）"}]
            message = {354: "缺少实时行情权限", 10167: "收到延迟行情；不输出方向", 10089: "API 行情权限不足",
                       10090: "部分行情权限缺失", 101: "行情订阅额度不足", 200: "合约未能确认"}.get(code, "接口报告错误；详见官方错误码")
            req_id = event.get("req_id")
            with self.lock:
                context = self.contexts.get(req_id, {})
            events = [{"kind": "error", "ts": ts, "code": code, "message": message}]
            if code in {354, 10167, 10089, 10090}:
                if context.get("kind") == "spot":
                    events.append({"kind": "spot_type", "ts": ts, "value": 3})
                elif "key" in context:
                    events.append({"kind": "md_type", "ts": ts, "key": context["key"], "value": 3})
            if code == 101:
                self.quota_failures += 1
                reduced = max(10, int(self.cfg.market_lines*.75))
                events.append({"kind": "capacity", "ts": ts, "market_lines": reduced})
                self.cfg.market_lines = reduced
                if self.quota_failures >= 3:
                    self.stop_event.set()
                    message = "订阅额度连续不足；已停止重试，请释放行情线或调低 IB_MARKET_LINES 后重启"
                else:
                    message = f"订阅额度不足；降低本程序预算至 {self.cfg.active_budget} 条后重连"
                events.append({"kind": "status", "ts": ts, "connected": False, "message": message})
                if self.client:
                    self.client.disconnect()
            return events
        with self.lock:
            context = dict(self.contexts.get(event.get("req_id"), {}))
        if not context:
            return []
        target = context["kind"]
        if kind == "control_contract" and target == "underlying":
            if not self.discovered and event["symbol"] == "SPX":
                self.discovered = True
                self.command("underlying_found", event["con_id"])
        elif kind == "control_contract" and target == "option_resolve":
            key = context["key"]
            if (event["trading_class"] == "SPXW" and event["expiry"] == self.session_date.replace("-", "")
                    and event["right"] == key[1] and abs(event["strike"]-key[0]) < 1e-6 and event["multiplier"] == "100"):
                self.command("resolved", (key, event["con_id"]))
        elif kind == "control_chain":
            if (event["trading_class"] == "SPXW" and event["multiplier"] == "100"
                    and self.session_date.replace("-", "") in event["expirations"]):
                self.chains.update(float(k) for k in event["strikes"] if k > 0)
        elif kind == "control_chain_end":
            return [{"kind": "chain", "ts": ts, "date": self.session_date, "strikes": sorted(self.chains)}]
        elif kind == "control_type":
            if target == "spot":
                return [{"kind": "spot_type", "ts": ts, "value": event["value"]}]
            if target == "option":
                return [{"kind": "md_type", "ts": ts, "key": context["key"], "value": event["value"]}]
        elif kind == "control_price":
            tick = event["tick_type"]
            if target in {"spot", "vix"} and tick == 4:
                return [{"kind": target, "ts": ts, "value": event["value"]}]
            if target == "option" and tick in {1, 2, 4}:
                return [{"kind": "quote", "ts": ts, "key": context["key"],
                         "field": {1: "bid", 2: "ask", 4: "last"}[tick], "value": event["value"]}]
        elif kind == "control_size" and target == "option":
            key, tick = context["key"], event["tick_type"]
            if tick == 8:
                return [{"kind": "volume", "ts": ts, "key": key, "value": event["value"]}]
            if tick == (27 if key[1] == "C" else 28):
                date = self.calendar.session(datetime.fromtimestamp(ts, timezone.utc))["previous"]
                return [{"kind": "oi", "ts": ts, "key": key, "value": event["value"],
                         "date": date, "date_verified": False}]
        elif kind == "control_greeks" and target == "option":
            return [{"kind": "greeks", "ts": ts, "key": context["key"],
                     "iv": event["iv"], "delta": event["delta"], "gamma": event["gamma"]}]
        return []
