"""
flow_stream.py —— 夜航社 Flow WebSocket 大单采集（只记录、不参与交易；0 配额）
=============================================================================
用户 11/25 定：盘中把全市场同向主动大单存下来，用于 Flow 研究（例如"逼空开仓前 15 分钟有没有同向大单"），
不再只靠开仓前那一次 REST 查询；Flow 历史接口最多回查 90 天，自己存的永久保留。

* 通道：POST /v1/stream/token（weight 0）换 10 分钟短期 token → wss://stream.yehangshe.com/v1/flow/stream
  （子协议 "dataplus-v1, <token>"，必须 permessage-deflate）。每个账户同一入口只能 1 条连接（另一个仓库不用这个通道）。
* 订阅：单腿订单（kind=order）、Ask 侧（A / AA）、权利金 ≥ $50,000（与实盘 Flow 判定口径一致）。不限标的（标的过滤最多 50 只）。
* 心跳：服务端每 25 秒发 {"type":"ping"}，必须回 {"type":"pong"}（应用层 JSON，不是协议层 ping）。
* 同一 aggregate_id 会被整行替换推送多次：原样追加到 yhs_data/flow_stream/<日期>.jsonl（每行 = 收到时刻 + 原始行），
  收盘后 compact() 只保留每个 aggregate_id 的最后一行，写成 live/flow_stream_<日期>.jsonl.gz（随实盘日志一起提交）。
* 断线：按关闭码退避重连（每次重新换 token、重新订阅）；4409（账户已有连接）/ 401·402·403 停止，不抢线。
  断线期间的数据不补（推送不回放历史），在 gaps 里记下断线区间。
* 慢消费者保护：读取与写盘分开（asyncio 队列 + 单独写线程），避免 2 秒内没读完被 4008 断开。

用法：python3 flow_stream.py --until 16:00            # 前台单独跑（测试用）
     实盘：main.py --live 会在后台线程里自动启动（config.flow_stream_record，默认开）。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gzip
import json
import logging
import os
import queue
import random
import subprocess
import sys
import threading
import urllib.error
import urllib.request

from config import SystemConfig, et_now

log = logging.getLogger("yhs.flow_stream")

TOKEN_PATH = "/v1/stream/token"
FILTER = {"kind": "order", "sides": ["A", "AA"], "min_premium_usd": 50000}
STOP_CODES = {4409, 4003}          # 账户已有活跃连接 / 权限失效：不再重连
RAW_DIR = os.path.join("yhs_data", "flow_stream")


class FatalStreamError(RuntimeError):
    pass


def _ensure_websockets():
    try:
        import websockets  # noqa: F401
        return True
    except ImportError:
        log.info("【Flow 采集】未安装 websockets，尝试 pip 安装")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websockets"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            import websockets  # noqa: F401
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("【Flow 采集】websockets 安装失败，今天不采集：%s", e)
            return False


def fetch_token(cfg: SystemConfig) -> dict:
    """换短期 token（0 配额）。不打印 token。401/402/403 视为致命（Key 或权限问题）。"""
    req = urllib.request.Request(cfg.base_url.rstrip("/") + TOKEN_PATH, data=b"", method="POST",
                                 headers={"Authorization": f"Bearer {cfg.api_key}", "Accept": "application/json",
                                          "User-Agent": "yhs-gex-scanner/1.0 (flow-stream)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["data"]
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        if e.code in (401, 402, 403):
            raise FatalStreamError(f"stream/token HTTP {e.code}: {body}") from e
        raise RuntimeError(f"stream/token HTTP {e.code}: {body}") from e


class FlowRecorder:
    def __init__(self, cfg: SystemConfig, day: dt.date | None = None, until_hhmm: str = "16:00"):
        self.cfg = cfg
        self.day = day or et_now().date()
        self.until = until_hhmm
        os.makedirs(RAW_DIR, exist_ok=True)
        self.raw_path = os.path.join(RAW_DIR, f"{self.day}.jsonl")
        self.q: queue.Queue = queue.Queue()
        self.rows_in = 0
        self.resyncs = 0
        self.gaps: list[list[str]] = []
        self.status = "未启动"
        self._stop = threading.Event()

    # ------------------------------------------------------------ 写盘线程
    def _writer(self):
        with open(self.raw_path, "a", encoding="utf-8") as f:
            while True:
                item = self.q.get()
                if item is None:
                    break
                f.write(item)
                if self.q.empty():
                    f.flush()

    def _past_until(self) -> bool:
        return et_now().strftime("%H:%M") >= self.until

    # ------------------------------------------------------------ 连接循环
    async def _session(self, websockets) -> int | None:
        tok = await asyncio.to_thread(fetch_token, self.cfg)
        url = tok.get("flow_url") or "wss://stream.yehangshe.com/v1/flow/stream"
        async with websockets.connect(url, subprotocols=["dataplus-v1", tok["token"]], compression="deflate",
                                      ping_interval=None, max_size=2 ** 24, open_timeout=30) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "ping":
                    await ws.send('{"type":"pong"}')
                elif t == "hello":
                    await ws.send(json.dumps({"type": "subscribe", "id": "rec", "channel": "flow", "filter": FILTER}))
                elif t == "subscribed":
                    self.status = "已订阅"
                    log.info("【Flow 采集】已订阅：单腿 Ask 侧 ≥ $50,000")
                elif t == "flow":
                    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
                    rows = msg.get("rows") or []
                    self.rows_in += len(rows)
                    self.q.put("".join(json.dumps({"recv": now, "row": r}, ensure_ascii=False) + "\n" for r in rows))
                elif t == "resync":
                    self.resyncs += 1
                elif t in ("subscribe_rejected", "error"):
                    log.warning("【Flow 采集】控制消息异常：%s", msg)
                    if t == "subscribe_rejected":
                        raise FatalStreamError(f"订阅被拒：{msg.get('reason')}")
                if self._past_until() or self._stop.is_set():
                    await ws.close(1000)
                    return 1000
        return None

    async def _run(self):
        import websockets
        backoff = 1.0
        while not self._past_until() and not self._stop.is_set():
            code = None
            t0 = et_now()
            try:
                code = await self._session(websockets)
                if code == 1000:
                    break
            except FatalStreamError as e:
                log.warning("【Flow 采集】停止：%s", e)
                self.status = f"停止：{e}"
                return
            except websockets.exceptions.ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                log.warning("【Flow 采集】连接关闭 code=%s reason=%s", code, e.rcvd.reason if e.rcvd else "")
                if code in STOP_CODES:
                    self.status = f"停止：关闭码 {code}（4409 = 同账户已有连接）"
                    log.warning("【Flow 采集】%s", self.status)
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("【Flow 采集】连接异常：%s: %s", type(e).__name__, str(e)[:200])
            self.gaps.append([t0.strftime("%H:%M:%S"), et_now().strftime("%H:%M:%S"), str(code)])
            if et_now() - t0 > dt.timedelta(minutes=2):
                backoff = 1.0
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    def run(self):
        """阻塞运行到 until（美东），然后写压缩文件。"""
        if not _ensure_websockets():
            self.status = "未安装 websockets"
            return None
        w = threading.Thread(target=self._writer, daemon=True)
        w.start()
        self.status = "连接中"
        try:
            asyncio.run(self._run())
        finally:
            self.q.put(None)
            w.join(timeout=30)
        return self.compact()

    def stop(self):
        self._stop.set()

    def compact(self) -> str | None:
        """同一 aggregate_id 只留最后一行 → live/flow_stream_<日期>.jsonl.gz。"""
        if not os.path.exists(self.raw_path):
            return None
        last: dict[str, dict] = {}
        with open(self.raw_path, encoding="utf-8") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                r = o.get("row") or {}
                last[r.get("aggregate_id") or ln] = {**r, "first_recv": last.get(r.get("aggregate_id"), {}).get("first_recv") or o.get("recv"),
                                                     "last_recv": o.get("recv")}
        os.makedirs("live", exist_ok=True)
        out = os.path.join("live", f"flow_stream_{self.day}.jsonl.gz")
        rows = sorted(last.values(), key=lambda r: r.get("occurred_at") or "")
        with gzip.open(out, "wt", encoding="utf-8") as g:
            g.write(json.dumps({"_meta": {"day": str(self.day), "filter": FILTER, "rows": len(rows), "updates": self.rows_in,
                                          "resyncs": self.resyncs, "gaps_et": self.gaps, "status": self.status}},
                               ensure_ascii=False) + "\n")
            for r in rows:
                g.write(json.dumps(r, ensure_ascii=False) + "\n")
        log.info("【Flow 采集】%s：%d 笔大单（收到 %d 次更新，resync %d 次，断线 %d 次）→ %s",
                 self.day, len(rows), self.rows_in, self.resyncs, len(self.gaps), out)
        return out


def start_background(cfg: SystemConfig, until_hhmm: str = "16:00") -> FlowRecorder | None:
    """实盘用：后台线程跑采集，任何异常只记日志，不影响交易。"""
    if not cfg.api_key:
        return None
    rec = FlowRecorder(cfg, until_hhmm=until_hhmm)

    def _go():
        try:
            rec.run()
        except Exception as e:  # noqa: BLE001
            log.warning("【Flow 采集】线程异常退出：%s", e)
    rec.thread = threading.Thread(target=_go, name="flow-stream", daemon=True)
    rec.thread.start()
    return rec


def finish_background(rec: FlowRecorder | None, timeout_s: float = 120) -> None:
    """收盘时调用：通知停止，等它断开并写好 live/flow_stream_<日期>.jsonl.gz。"""
    if rec is None or getattr(rec, "thread", None) is None:
        return
    rec.stop()
    rec.thread.join(timeout=timeout_s)
    if rec.thread.is_alive():
        log.warning("【Flow 采集】%d 秒内没有结束，直接写压缩文件", timeout_s)
        try:
            rec.compact()
        except Exception as e:  # noqa: BLE001
            log.warning("【Flow 采集】写压缩文件失败：%s", e)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="16:00", help="美东几点停止（默认 16:00）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = SystemConfig.load(None)
    out = FlowRecorder(cfg, until_hhmm=args.until).run()
    print(out or "没有数据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
