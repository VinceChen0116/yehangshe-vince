"""Nightwatch Flow wire protocol. The consumer owns reconciliation and coverage."""
import asyncio
import json
import logging


TOKEN_PATH = "/v1/stream/token"
FLOW_URL = "wss://stream.yehangshe.com/v1/flow/stream"
PROTOCOL = "dataplus-v1"
IDLE_LIMIT = 40  # server sends a JSON ping every 25 seconds
SUBSCRIPTION_ID = "spxw-0dte"


class StreamError(Exception):
    def __init__(self, code, *, stop=False):
        super().__init__(code)
        self.code, self.stop = code, stop


async def receive_flow(token, day, on_event, *, connect=None):
    """Subscribe to one root and expiry. No credentials or frames in diagnostics."""
    try:
        from websockets.asyncio.client import connect as websocket_connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
    except ImportError:
        raise StreamError("依赖未安装", stop=True) from None

    class OfficialConnection(websocket_connect):
        def process_redirect(self, exc):
            return exc  # the token subprotocol must never follow a redirect

    wire_log = logging.getLogger(__name__ + ".wire")
    wire_log.setLevel(logging.WARNING)  # the handshake headers contain a token
    dial = connect or OfficialConnection
    try:
        async with dial(FLOW_URL, subprotocols=[PROTOCOL, token], compression="deflate",
                        ping_interval=None, open_timeout=10, close_timeout=2, logger=wire_log) as ws:
            if ws.subprotocol != PROTOCOL:
                raise StreamError("握手协议不匹配", stop=True)

            async def receive(timeout):
                raw = await asyncio.wait_for(ws.recv(), timeout)
                try:
                    message = json.loads(raw) if isinstance(raw, str) else None
                except ValueError:
                    message = None
                if not isinstance(message, dict):
                    raise StreamError("推送消息无效")
                return message

            if (await receive(10)).get("type") != "hello":
                raise StreamError("未收到握手确认")
            await ws.send(json.dumps({"type": "subscribe", "channel": "flow", "id": SUBSCRIPTION_ID,
                                      "filter": {"tickers": ["SPXW"], "expirations": [day], "kind": "order"}}))
            subscribed = False
            ack_by = asyncio.get_running_loop().time() + 10
            while True:
                timeout = IDLE_LIMIT if subscribed else max(0, ack_by-asyncio.get_running_loop().time())
                msg = await receive(timeout)
                kind = msg.get("type")
                if kind == "ping":
                    await ws.send('{"type":"pong"}')
                    if subscribed:
                        on_event("heartbeat", ())
                elif kind == "error" or (kind == "subscribe_rejected" and msg.get("id") == SUBSCRIPTION_ID):
                    raise StreamError("订阅未获接受", stop=True)
                elif msg.get("channel") == "flow":
                    if kind == "subscribed" and msg.get("id") == SUBSCRIPTION_ID and not subscribed:
                        subscribed = True
                        on_event("subscribed", ())
                    elif subscribed and kind == "resync":
                        on_event("resync", ())
                    elif subscribed and kind == "flow":
                        rows = msg.get("rows")
                        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                            raise StreamError("成交帧无效")
                        on_event("flow", rows)
    except ConnectionClosed as exc:
        code = exc.rcvd.code if exc.rcvd else 1006
        if code == 4409:
            raise StreamError("同账户已有 Flow 连接", stop=True) from None
        if code == 4008:
            raise StreamError("推送消费超时", stop=True) from None
        if code == 1000:
            raise StreamError("推送已正常关闭", stop=True) from None
        raise StreamError(f"连接关闭 {code}") from None
    except InvalidStatus as exc:
        code = exc.response.status_code
        raise StreamError(f"握手 HTTP {code}", stop=code not in {429, 500, 502, 503, 504}) from None
    except (TimeoutError, OSError, InvalidHandshake, ValueError):
        raise StreamError("推送连接或心跳未就绪") from None
