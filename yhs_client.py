"""
yhs_client.py —— 夜航社 Data API 网络客户端（纯标准库）
=====================================================
职责：
  * 50 RPM 滑动窗口限频 + 串行请求间隔 ≥ 1.2s + 最大在途并发 3
  * 202（数据准备中，免费）：按 _meta.retry_after_seconds / 指数退避重试直至 200
  * 429：按 Retry-After 退避；QUOTA_EXHAUSTED 直接熔断
  * 503 READ_MODEL_UNAVAILABLE：按 retry_after_seconds 有限重试
  * 404 NOT_FOUND / RANGE_TOO_DEEP：不重试，抛出 YHSApiError（按 error.code 分支）
  * 每次响应解析 X-Quota-Remaining / X-RateLimit-Remaining / X-RateLimit-Reset，实现熔断
  * 统计：本进程计费调用（200）与免费调用次数，按端点分类
"""
from __future__ import annotations

import collections
import http.client
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import SystemConfig

log = logging.getLogger("yhs.client")


class YHSApiError(Exception):
    def __init__(self, status: int, code: str, message: str, path: str = ""):
        super().__init__(f"HTTP {status} {code}: {message} ({path})")
        self.status = status
        self.code = code
        self.message = message
        self.path = path


class QuotaExhausted(YHSApiError):
    def __init__(self, message: str, path: str = ""):
        super().__init__(429, "QUOTA_EXHAUSTED", message, path)


class YHSClient:
    """线程安全的夜航社 REST 客户端。"""

    FREE_PATHS = ("/v1/discover", "/v1/health", "/v1/openapi.json")

    def __init__(self, cfg: SystemConfig):
        self.cfg = cfg
        self._throttle_lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(cfg.max_in_flight)
        self._last_send = 0.0
        self._window: collections.deque[float] = collections.deque()
        # 实时余量（来自响应头）
        self.quota_limit: int | None = None
        self.quota_remaining: int | None = None
        self.quota_reset_at: str | None = None
        self.rate_remaining: int | None = None
        self.rate_reset_epoch: float | None = None
        # 统计
        self.billed_calls = 0
        self.free_calls = 0
        self.billed_by_endpoint: collections.Counter[str] = collections.Counter()

    # ------------------------------------------------------------------ 限频
    def _throttle(self) -> None:
        with self._throttle_lock:
            while True:
                now = time.monotonic()
                while self._window and now - self._window[0] >= 60.0:
                    self._window.popleft()
                waits = [self._last_send + self.cfg.min_request_interval_s - now]
                if len(self._window) >= self.cfg.safe_rpm:
                    waits.append(self._window[0] + 60.0 - now + 0.05)
                if (self.rate_remaining is not None and self.rate_remaining <= self.cfg.rate_remaining_floor
                        and self.rate_reset_epoch):
                    w = self.rate_reset_epoch - time.time() + 0.5
                    if w > 0:
                        waits.append(min(w, 61.0))
                    self.rate_remaining = None  # 等待一次后重新以响应头为准
                wait = max(waits)
                if wait <= 0:
                    break
                if wait > 2:
                    log.debug("限频保护：等待 %.1fs", wait)
                time.sleep(wait)
            t = time.monotonic()
            self._last_send = t
            self._window.append(t)

    # ------------------------------------------------------------------ 响应头
    def _parse_headers(self, headers) -> None:
        def _int(name):
            v = headers.get(name)
            try:
                return int(float(v)) if v is not None else None
            except ValueError:
                return None

        q = _int("X-Quota-Remaining")
        if q is not None:
            self.quota_remaining = q
        ql = _int("X-Quota-Limit")
        if ql is not None:
            self.quota_limit = ql
        if headers.get("X-Quota-Reset-At"):
            self.quota_reset_at = headers.get("X-Quota-Reset-At")
        r = _int("X-RateLimit-Remaining")
        if r is not None:
            self.rate_remaining = r
        rs = _int("X-RateLimit-Reset")
        if rs is not None:
            self.rate_reset_epoch = float(rs)

    def _circuit_check(self, path: str) -> None:
        if path in self.FREE_PATHS:
            return
        if self.quota_remaining is not None and self.quota_remaining <= self.cfg.quota_reserve:
            raise QuotaExhausted(
                f"配额熔断：剩余 {self.quota_remaining} ≤ 保留线 {self.cfg.quota_reserve}，停止计费请求", path)

    def _backoff(self, attempt: int, hinted: float | None = None) -> float:
        if hinted is not None and hinted > 0:
            base = hinted * (1.5 ** max(0, attempt - 1))
        else:
            base = self.cfg.backoff_base_s * (2 ** attempt)
        return min(self.cfg.backoff_cap_s, base) + random.uniform(0, 0.3)

    # ------------------------------------------------------------------ 主请求
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET 请求，返回已解析的 JSON（200）。失败抛 YHSApiError / QuotaExhausted。"""
        if not self.cfg.api_key and path not in ("/v1/health", "/v1/openapi.json"):
            raise YHSApiError(401, "NO_API_KEY", "未配置 API Key（环境变量 YHS_API_KEY 或 --config）", path)
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        qs = urllib.parse.urlencode(clean, safe=",")
        url = self.cfg.base_url.rstrip("/") + path + (("?" + qs) if qs else "")
        headers = {"Accept": "application/json", "User-Agent": "yhs-gex-scanner/1.0 (python-urllib)"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        n202 = n503 = nnet = n429 = 0
        while True:
            self._circuit_check(path)
            self._throttle()
            req = urllib.request.Request(url, headers=headers, method="GET")
            status, body, hdrs = None, b"", {}
            try:
                with self._sem:
                    with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_s) as resp:
                        status = resp.status
                        hdrs = resp.headers
                        body = resp.read()
            except urllib.error.HTTPError as e:
                status, hdrs = e.code, e.headers
                try:
                    body = e.read()
                except Exception:  # noqa: BLE001
                    body = b""
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                    http.client.HTTPException) as e:   # 含 IncompleteRead：响应读到一半连接断开
                nnet += 1
                if nnet > self.cfg.max_network_retries:
                    raise YHSApiError(0, "NETWORK_ERROR", str(e), path) from e
                w = self._backoff(nnet)
                log.warning("网络异常 %s：%s，%.1fs 后第 %d 次重试", path, e, w, nnet)
                time.sleep(w)
                continue

            if hdrs:
                self._parse_headers(hdrs)
            payload = self._decode(body)

            if status == 200:
                if path in self.FREE_PATHS:
                    self.free_calls += 1
                else:
                    self.billed_calls += 1
                    self.billed_by_endpoint[self._endpoint_key(path)] += 1
                log.debug("200 %s | 配额剩余=%s RPM剩余=%s", path, self.quota_remaining, self.rate_remaining)
                return payload

            self.free_calls += 1
            err = (payload or {}).get("error") if isinstance(payload, dict) else None
            code = (err or {}).get("code", "") if isinstance(err, dict) else ""
            msg = (err or {}).get("message", "") if isinstance(err, dict) else ""

            if status == 202:
                n202 += 1
                if n202 > self.cfg.max_202_retries:
                    raise YHSApiError(202, "MATERIALIZING_TIMEOUT", "数据准备超时，放弃本轮", path)
                meta = (payload or {}).get("_meta", {}) if isinstance(payload, dict) else {}
                hint = meta.get("retry_after_seconds") or _float(hdrs.get("Retry-After") if hdrs else None)
                w = self._backoff(n202, hint)
                log.info("202 数据准备中 %s，%.1fs 后免费重试（第 %d 轮）", path, w, n202)
                time.sleep(w)
                continue

            if status == 429:
                if code == "QUOTA_EXHAUSTED":
                    raise QuotaExhausted(msg or "月配额已耗尽", path)
                n429 += 1
                if n429 > 5:
                    raise YHSApiError(429, code or "RATE_LIMITED", msg, path)
                ra = _float(hdrs.get("Retry-After") if hdrs else None) or self._backoff(n429)
                log.warning("429 %s（%s），按 Retry-After 等待 %.1fs", path, code or "RATE_LIMITED", ra)
                time.sleep(ra + 0.2)
                continue

            if status in (500, 502, 503, 504):
                if "recently failed" in (msg or "").lower():
                    # 服务端已确认该 key 物化失败（如合约不存在），重试只会白等
                    raise YHSApiError(status, code or "UNAVAILABLE", msg, path)
                n503 += 1
                if n503 > self.cfg.max_503_retries:
                    raise YHSApiError(status, code or "UNAVAILABLE", msg or "服务暂不可用，多轮重试无进展", path)
                hint = (err or {}).get("retry_after_seconds") if isinstance(err, dict) else None
                w = self._backoff(n503, _float(hint))
                log.warning("%d %s（%s），%.1fs 后重试", status, path, code or "SERVER_ERROR", w)
                time.sleep(w)
                continue

            # 400/401/402/403/404 等：不重试
            raise YHSApiError(status or 0, code or f"HTTP_{status}", msg or "请求失败", path)

    @staticmethod
    def _decode(body: bytes):
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"_raw": body[:500].decode("utf-8", "replace")}

    @staticmethod
    def _endpoint_key(path: str) -> str:
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[1] in ("derived",):
            return f"{parts[1]}/{parts[2]}/{parts[-1]}"
        if len(parts) >= 3:
            return f"{parts[1]}/{parts[2]}"
        return path

    # ------------------------------------------------------------------ 免费端点
    def discover(self) -> dict:
        return self.get("/v1/discover")

    def health(self) -> dict:
        return self.get("/v1/health")

    def quota_summary(self) -> str:
        return (f"本进程计费调用 {self.billed_calls} 次，免费调用 {self.free_calls} 次；"
                f"服务端剩余配额 {self.quota_remaining if self.quota_remaining is not None else '未知'}"
                f" / {self.quota_limit or self.cfg.monthly_quota}；按端点：{dict(self.billed_by_endpoint)}")


def _float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
