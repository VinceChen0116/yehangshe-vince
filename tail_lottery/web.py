"""网页：单页 + 两个只读 JSON 接口。只监听本机（127.0.0.1）。

  GET /               观察台页面（每 2 秒拉一次快照；数据本身约 30 秒 / 1 分钟更新）
  GET /api/snapshot   页面快照
  GET /api/debug      Skew / Charm 最新结果 + 最近 10 分钟序列（核对用，没有页面）
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import et_fmt

log = logging.getLogger("tail.web")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")


def debug_payload(engine) -> dict:
    st = engine.store
    return {"skew": st.skew, "charm": st.charm,
            "series": [{"time": et_fmt(t), "skew": s, "charm": c} for t, s, c in list(st.debug_series)],
            "errors": dict(engine.errors)}


def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(STATIC, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/snapshot":
                with engine.lock:
                    snap = engine.snapshot
                return self._send(200, json.dumps(snap, ensure_ascii=False, default=str).encode("utf-8"),
                                  "application/json; charset=utf-8")
            if path == "/api/debug":
                return self._send(200, json.dumps(debug_payload(engine), ensure_ascii=False, default=str).encode("utf-8"),
                                  "application/json; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def log_message(self, *a):   # 不记访问日志
            pass

    return H


def serve(engine, host="127.0.0.1", port=8765):
    srv = ThreadingHTTPServer((host, port), make_handler(engine))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    log.info("观察台：http://%s:%d", host, port)
    return srv
