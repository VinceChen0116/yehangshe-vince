"""
quote_stream.py —— 夜航社期权实时报价推送（option-quotes 频道；0 配额）
=====================================================================
2026-11-26 用户定：B 账户持仓改用实时推送报价、每 5 秒盯一次（11/27 起，rules_registry: mark_5s）；A / L 仍每分钟 REST。

* 入口：POST /v1/stream/token（weight 0）→ data.url（wss://stream.yehangshe.com/v1/stream），子协议 "dataplus-v1, <token>"，
  permessage-deflate。每个账户这个入口只能 1 条连接（与 Flow 采集的 /v1/flow/stream 是两个入口，互不占用）。
* 订阅：{"type":"subscribe","channel":"option-quotes","contracts":[纯 OCC，无 "O:" 前缀]}，每条连接最多 200 个合约。
  每个合约单独回执 subscribed（mode = live / snapshot；snapshot ≈ 3 秒轮询，只在报价变化时推）。
* 数据：{"type":"data","channel":"option-quotes","rows":[{contract_symbol, quoted_at, bid_usd, ask_usd, mid_usd, bid_size, ask_size}]}
* 心跳：服务端每 25 秒 {"type":"ping"}，必须回 {"type":"pong"}；55 秒无响应会被断开。
* 断线：重连后订阅不保留，按当前持仓重新订阅；4409（同账户已有连接）/ 4003（权限失效）停止，不抢线。
  断线或停止期间 healthy() = False，实盘自动退回每分钟 REST 盯仓（main.py）。

主线程只调用 set_contracts() / get() / healthy()；连接在后台线程的 asyncio 循环里跑。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import threading
import time

from config import SystemConfig, et_now
from flow_stream import FatalStreamError, _ensure_websockets, fetch_token

log = logging.getLogger("yhs.quote_stream")

STOP_CODES = {4409, 4003, 4009}   # 同账户已有连接 / 权限失效 / 当日流量上限：不再重连
MAX_CONTRACTS = 200
STALE_S = 60.0                    # 超过 60 秒没收到任何消息（含 25 秒一次的 ping）→ 视为断线


def core(sym: str) -> str:
    return sym.split(":")[-1].upper()


class QuoteStream:
    def __init__(self, cfg: SystemConfig, until_hhmm: str = "16:00"):
        self.cfg = cfg
        self.until = until_hhmm
        self._lock = threading.Lock()
        self._want: set[str] = set()          # 主线程希望订阅的合约
        self._subscribed: dict[str, str] = {}  # 已确认订阅：合约 → mode
        self._sent: set[str] = set()           # 已发出订阅请求（等回执）
        self._rejected: dict[str, str] = {}    # 合约 → 拒绝原因（退回 REST）
        self.cache: dict[str, dict] = {}       # 合约 → {"bid","ask","mid","quoted_at","recv"}
        self.connected = False
        self.last_msg = 0.0
        self.rows_in = 0
        self.reconnects = 0
        self.status = "未启动"
        self.stopped = False                   # 致命错误后不再重连
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------ 主线程接口
    def set_contracts(self, symbols) -> None:
        want = {core(s) for s in symbols if s}
        if len(want) > MAX_CONTRACTS:
            log.warning("【报价推送】持仓合约 %d 个 > %d，只订阅前 %d 个，其余走 REST", len(want), MAX_CONTRACTS, MAX_CONTRACTS)
            want = set(sorted(want)[:MAX_CONTRACTS])
        with self._lock:
            self._want = want

    def healthy(self) -> bool:
        return self.connected and not self.stopped and (time.monotonic() - self.last_msg) < STALE_S

    def get(self, sym: str):
        """推送来的最新报价（格式与 main.QuoteBook 相同）；没有 / 未订阅 / 不健康时返回 None。"""
        if not self.healthy():
            return None
        c = core(sym)
        with self._lock:
            if c not in self._subscribed:
                return None
            q = self.cache.get(c)
        return dict(q) if q else None

    # ------------------------------------------------------------ 消息处理（纯逻辑，便于测试）
    def handle(self, msg: dict) -> list[dict]:
        """处理一条服务端消息；返回需要发回的消息（pong）。"""
        self.last_msg = time.monotonic()
        t = msg.get("type")
        out: list[dict] = []
        if t == "ping":
            out.append({"type": "pong"})
        elif t == "hello":
            self.connected = True
            self.status = "已连接"
        elif t == "subscribed":
            c = core(msg.get("contract") or msg.get("contract_symbol") or "")
            cs = [core(x) for x in (msg.get("contracts") or ([c] if c else []))]
            with self._lock:
                for x in cs:
                    self._subscribed[x] = msg.get("mode") or "live"
                    self._sent.discard(x)
                    self._rejected.pop(x, None)
        elif t == "unsubscribed":
            c = core(msg.get("contract") or msg.get("contract_symbol") or "")
            cs = [core(x) for x in (msg.get("contracts") or ([c] if c else []))]
            with self._lock:
                for x in cs:
                    self._subscribed.pop(x, None)
                    self._sent.discard(x)
                    self.cache.pop(x, None)
        elif t == "subscribe_rejected":
            c = core(msg.get("contract") or msg.get("contract_symbol") or "")
            cs = [core(x) for x in (msg.get("contracts") or ([c] if c else []))]
            with self._lock:
                for x in cs:
                    self._rejected[x] = str(msg.get("reason"))
                    self._sent.discard(x)
            log.warning("【报价推送】订阅被拒 %s：%s", ",".join(cs) or "?", msg.get("reason"))
        elif t == "data" and msg.get("channel") == "option-quotes":
            now = time.monotonic()
            with self._lock:
                for r in msg.get("rows") or []:
                    c = core(r.get("contract_symbol") or "")
                    bid, ask, mid = r.get("bid_usd"), r.get("ask_usd"), r.get("mid_usd")
                    if not c or ask is None or float(ask) <= 0:
                        self.cache.pop(c, None)   # 没有卖价 = 无有效报价，不能用旧值盯仓
                        continue
                    bid = float(bid or 0.0)
                    ask = float(ask)
                    self.cache[c] = {"bid": bid, "ask": ask, "mid": float(mid) if mid else (bid + ask) / 2,
                                     "quoted_at": r.get("quoted_at"), "recv": now}
                    self.rows_in += 1
        elif t == "error":
            log.warning("【报价推送】服务端错误：%s", msg)
        return out

    def _diff(self) -> tuple[list[str], list[str]]:
        with self._lock:
            have = set(self._subscribed) | self._sent
            add = sorted(c for c in self._want - have if c not in self._rejected)
            drop = sorted(c for c in set(self._subscribed) if c not in self._want)
            self._sent |= set(add)
            for c in drop:
                self._subscribed.pop(c, None)
                self.cache.pop(c, None)
        return add, drop

    # ------------------------------------------------------------ 连接循环
    def _past_until(self) -> bool:
        return et_now().strftime("%H:%M") >= self.until

    async def _session(self, websockets) -> int | None:
        tok = await asyncio.to_thread(fetch_token, self.cfg)
        url = tok.get("url") or "wss://stream.yehangshe.com/v1/stream"
        async with websockets.connect(url, subprotocols=["dataplus-v1", tok["token"]], compression="deflate",
                                      ping_interval=None, max_size=2 ** 24, open_timeout=30) as ws:
            with self._lock:   # 新连接：订阅全部重来
                self._subscribed.clear()
                self._sent.clear()
                self.cache.clear()

            async def pump():
                n = 0
                while True:
                    await asyncio.sleep(0.3)
                    if not self.connected:
                        continue
                    add, drop = self._diff()
                    for i in range(0, len(add), MAX_CONTRACTS):
                        n += 1
                        await ws.send(json.dumps({"type": "subscribe", "id": f"s{n}", "channel": "option-quotes",
                                                  "contracts": add[i:i + MAX_CONTRACTS]}))
                    if drop:
                        n += 1
                        await ws.send(json.dumps({"type": "unsubscribe", "id": f"u{n}", "channel": "option-quotes",
                                                  "contracts": drop}))
                    if self._past_until() or self._stop.is_set():
                        await ws.close(1000)
                        return

            task = asyncio.create_task(pump())
            try:
                async for raw in ws:
                    for reply in self.handle(json.loads(raw)):
                        await ws.send(json.dumps(reply))
            finally:
                task.cancel()
                self.connected = False
        return 1000 if (self._past_until() or self._stop.is_set()) else None

    async def _run(self):
        import websockets
        backoff = 1.0
        while not self._past_until() and not self._stop.is_set():
            t0 = time.monotonic()
            try:
                if await self._session(websockets) == 1000:
                    break
            except FatalStreamError as e:
                self.stopped, self.status = True, f"停止：{e}"
                log.warning("【报价推送】%s", self.status)
                return
            except websockets.exceptions.ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                log.warning("【报价推送】连接关闭 code=%s reason=%s", code, e.rcvd.reason if e.rcvd else "")
                if code in STOP_CODES:
                    self.stopped = True
                    self.status = f"停止：关闭码 {code}" + ("（同账户已有另一条 /v1/stream 连接）" if code == 4409 else "")
                    log.warning("【报价推送】%s", self.status)
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("【报价推送】连接异常：%s: %s", type(e).__name__, str(e)[:200])
            finally:
                self.connected = False
            self.reconnects += 1
            if time.monotonic() - t0 > 120:
                backoff = 1.0
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 30)

    def run(self) -> None:
        if not _ensure_websockets():
            self.stopped, self.status = True, "未安装 websockets"
            return
        self.status = "连接中"
        asyncio.run(self._run())

    def stop(self) -> None:
        self._stop.set()


def start_background(cfg: SystemConfig, until_hhmm: str = "16:00") -> QuoteStream | None:
    if not cfg.api_key:
        return None
    qs = QuoteStream(cfg, until_hhmm=until_hhmm)

    def _go():
        try:
            qs.run()
        except Exception as e:  # noqa: BLE001
            qs.stopped, qs.status = True, f"线程异常：{e}"
            log.warning("【报价推送】线程异常退出：%s", e)
    qs.thread = threading.Thread(target=_go, name="quote-stream", daemon=True)
    qs.thread.start()
    return qs


def stop_background(qs: QuoteStream | None, timeout_s: float = 10) -> None:
    if qs is None or qs.thread is None:
        return
    qs.stop()
    qs.thread.join(timeout=timeout_s)
    log.info("【报价推送】结束：收到 %d 条报价，重连 %d 次，状态 %s", qs.rows_in, qs.reconnects, qs.status)


if __name__ == "__main__":   # 手动连通性测试：python3 quote_stream.py SPXW261127C07200000 [秒数]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    qs = start_background(SystemConfig.load(None), until_hhmm="23:59")
    qs.set_contracts(sys.argv[1:2] or ["SPXW261127C07200000"])
    for _ in range(int(sys.argv[2]) if len(sys.argv) > 2 else 20):
        time.sleep(1)
        print(dt.datetime.now().strftime("%H:%M:%S"), qs.status, "healthy" if qs.healthy() else "-",
              dict(qs._subscribed), qs.get(sys.argv[1] if len(sys.argv) > 1 else "SPXW261127C07200000"))
    stop_background(qs)
