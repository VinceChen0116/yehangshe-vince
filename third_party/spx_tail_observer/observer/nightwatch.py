"""Read-only Nightwatch source. Manual start, no new scheduling or market data files.

Only the worker queries the provider. Opening/closing a dashboard does not
start/stop requests. Historical aggregates and current quotes remain distinct.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import exp
from pathlib import Path
from random import uniform
import re
from statistics import median
import time

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .clock import NY
from .engine import Engine, number
from .flow_stream import FLOW_URL, IDLE_LIMIT, TOKEN_PATH, StreamError, receive_flow
from .tail_journal import TailJournal, nearest_level

ROOT = "https://api.yehangshe.com"
INDEX_ROOT = "https://api.massive.com"
INDEX_PATH = "/v3/snapshot/indices"
INDEX_INTERVAL = 5
CHAIN_PATH = "/v1/options/chain-snapshot/SPX"
GEX_PATH = "/v1/derived/dealer-gex/SPX/live"
FLOW_PATH = "/v1/flow/trades"
CONTRACT = re.compile(r"^(?:O:)?(SPXW)(\d{6})([CP])(\d{8})$")
QUOTE_AGE_LIMIT = 45
FUTURE_TOLERANCE = 2
FLOW_LOOKBACK = 600
FLOW_PAGE_SIZE = 25
FLOW_POLL_INTERVAL = 5
FLOW_HEAD_REFRESH = 20
FLOW_MAX_PENDING_RANGES = 8
# Conservative display heuristics in SPX index points (not option prices).
# This rolling observation is separate from the original one-time 15:10 butterfly setup.
PIN_WINDOW, PIN_MIN_SPAN, PIN_MIN_SAMPLES = 300, 180, 7
PIN_MAX_GAP, PIN_MIN_CROSS_SPAN, PIN_LAST_CROSS_AGE = 45, 60, 90
PIN_MAX_RANGE, PIN_RADIUS, PIN_CURRENT_RADIUS = 3.5, 2.5, 2.0
PIN_SIDE_BAND, PIN_MIN_CROSSES = .5, 3
# SPX points and seconds of verified source samples, not chart bars.
PATH_WINDOW, PATH_MIN_SPAN, PATH_MIN_SAMPLES = 150, 75, 6
PATH_STEP, PATH_MOVE, PATH_WEIGHT = .5, 3., 25
PATH_EFFICIENCY, PATH_EDGE_FRACTION = .55, .25
# The proxy is a relative price path, not a substitute for index prints or GEX.
PARITY_QUOTE_AGE, PARITY_SYNC, PARITY_MIN_PAIRS, PARITY_MAX_WIDTH = 20, 5, 3, 1.5
PARITY_MAX_DISAGREEMENT, PARITY_MAX_INTERVAL_GAP = 1., .6
PARITY_WATCH_SPAN, PARITY_WATCH_MAX_GAP = 50, 20
TURN_LEG, TURN_RECOVERY, TURN_LEG_SECONDS, TURN_MAX_AGE = 3., .5, 15, 60
HOLD_BUFFER, HOLD_SECONDS = .5, 30
VOL_WINDOW, VOL_BASE_WINDOWS = 60, 3
VOL_MIN_SAMPLE, VOL_MIN_BASE, VOL_MIN_RATIO = 50, 10, 1.5
# Reference selection only: never filter the observed tape or change its CALL/PUT score.
REF_MAX_DISTANCE = 5.
VERSION = "0.2.18"
TAIL_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "tail-health.log"
TAIL_LOG_LIMIT = 256 * 1024


def stamp(value):
    """Require an explicit timezone; never turn a request time into quote time."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def contract(value, day):
    match = CONTRACT.fullmatch(value or "")
    if not match:
        return None
    _, expiry, right, strike = match.groups()
    try:
        if datetime.strptime(expiry, "%y%m%d").date().isoformat() != day:
            return None
    except ValueError:
        return None
    strike = int(strike) / 1000
    return (strike, right) if strike > 0 else None


def symbol(key, day):
    expiry = datetime.strptime(day, "%Y-%m-%d").strftime("%y%m%d")
    return f"SPXW{expiry}{key[1]}{round(key[0]*1000):08d}"


@dataclass
class Result:
    status: int
    data: object
    meta: dict
    headers: dict


class ProviderError(Exception):
    def __init__(self, code, retry=10, detail="", status=None):
        self.code, self.retry, self.detail, self.status = code, retry, detail, status
        super().__init__(code)


class Client:
    """Single in-flight request to the official host; only a token exchange may POST."""
    def __init__(self, key, max_units=3000, transport=None):
        self.http = httpx.AsyncClient(base_url=ROOT, headers={"Authorization": "Bearer " + key},
                                      follow_redirects=False, timeout=httpx.Timeout(12, connect=10),
                                      transport=transport)
        self.max_units = max_units
        self.units = 0
        self.requests = 0
        self.remaining = None
        self.limit = None
        self.capabilities = {}
        self.etags = {}
        self.pause_until = 0
        self.next_send = 0
        self.lock = asyncio.Lock()
        self.last_ok = 0

    async def close(self):
        await self.http.aclose()

    async def get(self, path, params=None, command=None):
        if path == TOKEN_PATH:
            raise ProviderError("INVALID_PATH", 3600)
        return await self._request(path, params, command)

    async def flow_token(self):
        result = await self._request(TOKEN_PATH)
        data = result.data
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]+", token) or data.get("flow_url") != FLOW_URL:
            raise ProviderError("INVALID_STREAM_TOKEN", 30)
        return token

    async def _request(self, path, params=None, command=None):
        if not path.startswith("/v1/") or ".." in path or "?" in path:
            raise ProviderError("INVALID_PATH", 3600)
        exchange = path == TOKEN_PATH
        cost = 0 if exchange or path == "/v1/discover" else self.capabilities.get(command, {}).get("weight", 1)
        if path == "/v1/options/quotes":
            cost = len(set(params["contracts"].split(",")))
        async with self.lock:
            if self.units + cost > self.max_units:
                raise ProviderError("LOCAL_BUDGET", 3600)
            if cost and self.remaining is not None and self.remaining < cost + 100:
                raise ProviderError("QUOTA_RESERVE", 3600)
            now = time.time()
            if now < self.pause_until:
                raise ProviderError("RATE_BACKOFF", self.pause_until-now)
            await asyncio.sleep(max(0, self.next_send-time.monotonic()))
            # Only the fixed, unpaginated Live GEX URL is revalidated.
            conditional = path == GEX_PATH and not params
            etag = self.etags.get(path) if conditional else None
            try:
                request = (self.http.post(path) if exchange else
                           self.http.get(path, params=params, headers={"If-None-Match": etag} if etag else {}))
                response = await asyncio.wait_for(request, 12)
            except (httpx.HTTPError, TimeoutError):
                raise ProviderError("NETWORK", 10) from None
            self.requests += 1
            self.next_send = time.monotonic() + 1.3
            status = response.status_code
            if status == 304:
                self.last_ok = time.time()
                return Result(304, None, {}, dict(response.headers))
            try:
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                if not exchange or status == 200:
                    raise ProviderError("INVALID_RESPONSE", 30, status=status) from None
                body = {}
            if status != 200:
                error = body.get("error") or {}
                if not isinstance(error, dict):
                    error = {}
                code = str(error.get("code") or f"HTTP_{status}")
                retry = number(response.headers.get("Retry-After")) or number(error.get("retry_after_seconds")) or 10
                retry = min(3600, max(2, retry))
                if status in (401, 402):
                    retry = 3600
                if status == 403:
                    retry = 300
                if status == 429:
                    self.pause_until = time.time() + retry
                detail = "" if exchange else str(error.get("message") or "")[:350]
                auth = self.http.headers.get("Authorization", "")
                if auth.startswith("Bearer ") and auth[7:]:
                    detail = detail.replace(auth[7:], "[REDACTED]")
                raise ProviderError(code if re.fullmatch(r"[A-Za-z0-9_:-]{1,90}", code) else f"HTTP_{status}", retry, detail, status)
            data, meta = body.get("data"), body.get("_meta") or {}
            if path == "/v1/discover":
                data = data if isinstance(data, dict) else body
                self.capabilities = {r["command"]: r for r in data.get("capabilities_detail", [])
                                     if isinstance(r, dict) and isinstance(r.get("command"), str)}
                quota = data.get("quota") or {}
                self.remaining = number(quota.get("monthly_remaining"))
                self.limit = number(quota.get("monthly_limit"))
            if path == "/v1/options/quotes":
                cost = len((data or {}).get("quotes", []))
            self.units += cost
            balance = number(response.headers.get("X-Quota-Remaining"))
            if balance is not None:
                # API balance headers represent the balance before this response's charge.
                self.remaining = max(0, balance-cost)
            limit = number(response.headers.get("X-Quota-Limit"))
            if limit is not None:
                self.limit = limit
            if conditional:
                if response.headers.get("ETag"):
                    self.etags[path] = response.headers["ETag"]
                else:
                    self.etags.pop(path, None)
            self.last_ok = time.time()
            return Result(200, data, meta, dict(response.headers))


class NightwatchEngine(Engine):
    def __init__(self, config, calendar=None):
        super().__init__(config, calendar)
        self.started_at = time.time()
        self.focus = set()
        self.native = None
        self.native_input_age = None
        self.native_received = 0
        self.pin_samples = deque(maxlen=512)
        self.path_samples = deque(maxlen=256)
        self.tail_journal = TailJournal()
        self.index_samples = deque(maxlen=256)
        self.parity_samples = deque(maxlen=256)
        self.parity_note = "等待同一行权价的实时 Call／Put 双边报价"
        self.index_note = "等待独立 SPX 现价" if config.massive_spot_key else "未启用独立 SPX 现价"
        self.aggregate_seen = {}
        self.flow_rows = 0
        self.flow_polls = 0
        self.last_flow_ok = 0
        self.flow_gap = False
        self.flow_pending_pages = 0
        self.flow_pending_ranges = 0
        self.flow_covered_at = None
        self.flow_resync_at = None
        self.flow_transport = "rest"
        self.flow_stream_note = None
        self.last_quote_ok = 0
        self.gex_checked_at = 0
        self.gex_http_status = None
        self.quote_unavailable = []
        self.quote_results = {}
        self.provider_errors = {}
        self.api_info = {}
        self.quote_delivery = "polling"

    def wanted(self):
        return self.focus

    def flow_vote_supported(self, trades, right):
        # Multiple updates of one aggregate are still one underlying order.
        return len({t["aggregate_id"] for k,t in trades if k[1] == right and t.get("aggregate_id")}) >= 2

    def playbook_reference(self, now, right):
        """Select by distance only; the latest ask is a separate price annotation."""
        return self.near_reference(now, right)[0]

    def near_reference(self, now, right):
        if not self.spot:
            return None, "等待有效 SPX 上下文价"
        levels = sorted({k for k,r in self.focus if r == right})
        if not levels:
            return None, "未取得近价合约"
        sign = 1 if right == "C" else -1
        # If equidistant, choose the nearer-to-intrinsic (ITM) side of the pair.
        # Do not skip this strike to find a cheaper or tighter-spread contract.
        k = min(levels, key=lambda level: (abs(level-self.spot), sign*level))
        if abs(k-self.spot) > REF_MAX_DISTANCE:
            return None, f"最近已跟踪的行权价也超过 {REF_MAX_DISTANCE:g} 点"
        if not self.quote_fresh((k, right), now):
            return None, f"SPXW {k:,.0f} {'CALL' if right == 'C' else 'PUT'} · 近价报价待更新"
        q = self.option((k, right))
        ceiling = self.cfg.feed_max_ask
        return {"right": right, "strike": k, "bid": q.bid, "ask": q.ask,
                "offset": round(k-self.spot, 2), "quoted_at": min(q.bid_at, q.ask_at),
                "price_state": "ABOVE_LIMIT" if q.ask > ceiling else "QUOTED", "ask_ceiling": ceiling,
                "basis": "最近行权价", "note": "仅跟踪当前卖价；参考上限不参与方向评分。"}, None

    def roll_day(self, ts):
        previous = self.day
        super().roll_day(ts)
        if previous and previous != self.day:
            self.focus.clear()
            self.native = None
            self.pin_samples.clear()
            self.path_samples.clear()
            self.tail_journal = TailJournal()
            self.index_samples.clear()
            self.parity_samples.clear()
            self.parity_note = "等待同一行权价的实时 Call／Put 双边报价"
            if self.cfg.massive_spot_key:
                self.index_note = "等待独立 SPX 现价"
            self.aggregate_seen.clear()
            self.quote_results.clear()
            self.flow_rows = self.flow_polls = 0
            self.last_flow_ok = self.last_quote_ok = 0
            self.flow_gap = False
            self.flow_pending_pages = 0
            self.flow_pending_ranges = 0
            self.flow_covered_at = None
            self.flow_resync_at = None
            self.gex_checked_at, self.gex_http_status = 0, None

    def apply_gex(self, data, meta, now):
        source = stamp(data.get("snapshot_at"))
        price = number(data.get("spot_usd"))
        if data.get("session_date_et") != self.day or source is None or source > now+FUTURE_TOLERANCE:
            raise ProviderError("INVALID_GEX_TIME", 15)
        self.native, self.native_received = data, now
        self.native_input_age = number(meta.get("data_freshness_seconds"))
        self.record_tail_sample(source, price, data, now)
        # This is a structure-context spot, explicitly labeled as such in the UI.
        if price is not None and price > 0 and data.get("state") == "fresh":
            self.spot, self.spot_at, self.spot_type = price, source, 1
            self.spot_history.append((source, price))
            if self.strikes:
                self.apply_chain([], now)
        self.connected = True
        self.status_note = "已接通夜航社"

    def record_tail_sample(self, ts, price, data, received):
        # Only new upstream GEX frames qualify, never web refreshes or HTTP 304s.
        clock = datetime.fromtimestamp(ts, NY)
        total = number((data.get("summary") or {}).get("total_gex_usd"))
        age = self.native_input_age
        history = self.pin_samples
        if (clock.date().isoformat() != self.day or clock.hour != 15 or clock.minute < 30
                or data.get("state") != "fresh" or not price or price <= 0
                or not 0 <= received-ts <= QUOTE_AGE_LIMIT
                or age is None or not 0 <= age <= QUOTE_AGE_LIMIT):
            self.path_samples.clear()
            history.clear()
            return
        path = self.path_samples
        if path and (ts < path[-1][0] or ts-path[-1][0] > QUOTE_AGE_LIMIT):
            path.clear()
        if path and ts == path[-1][0]:
            path.pop()
        path.append((ts, price))
        while path and path[0][0] < ts-PATH_WINDOW:
            path.popleft()
        if total is None or total <= 0:
            history.clear()
            return
        nodes = frozenset(k for row in (data.get("strikes") or []) if isinstance(row, dict)
                          and (k := number(row.get("strike_usd"))) is not None and k > 0
                          and (number(row.get("net_gex_usd")) or 0) > 0)
        if not nodes:
            history.clear()
            return
        if history and (ts < history[-1][0] or ts-history[-1][0] > PIN_MAX_GAP):
            history.clear()
        if history and ts == history[-1][0]:
            history.pop()  # correction to the same source frame; not another price crossing
        history.append((ts, price, nodes))
        while history and history[0][0] < ts-PIN_WINDOW:
            history.popleft()

    def pin_observation(self, frame, now, inside):
        def status(state, note):
            return {"state": state, "strike": None, "note": note}

        if not inside:
            return status("OUTSIDE", "美东 15:30 后开始观察")
        gex = frame["gex"]
        if not frame["healthy"] or not gex["available"] or self.native_input_age is None or "gex" in self.provider_errors:
            return status("WAIT", "等待有效报价、GEX 和完整的成交分页")
        if gex["regime"] != "POSITIVE":
            return status("WATCH", "未满足正净 GEX 条件")
        samples = [s for s in self.pin_samples if now-PIN_WINDOW <= s[0] <= now]
        if not samples or now-samples[-1][0] > PIN_MAX_GAP or self.spot_at != samples[-1][0]:
            return status("WAIT", "等待新的有效价格与正 GEX 节点")
        span = samples[-1][0]-samples[0][0]
        if len(samples) < PIN_MIN_SAMPLES or span < PIN_MIN_SPAN:
            return status("BUILDING", f"有效观察 {int(span)} 秒；至少需 {PIN_MIN_SPAN} 秒")
        prices = [s[1] for s in samples]
        lo, hi, last = min(prices), max(prices), prices[-1]
        if hi-lo > PIN_MAX_RANGE:
            return status("WATCH", "价格区间已超出钉盘观察带")
        stable = samples[0][2].intersection(*(s[2] for s in samples[1:]))
        for k in sorted(stable, key=lambda level: (abs(last-level), level)):
            if abs(last-k) > PIN_CURRENT_RADIUS or max(abs(hi-k), abs(lo-k)) > PIN_RADIUS:
                continue
            prev, crossings = 0, []
            for ts, price, _ in samples:
                side = 1 if price >= k+PIN_SIDE_BAND else -1 if price <= k-PIN_SIDE_BAND else 0
                if side:
                    if prev and side != prev:
                        crossings.append(ts)
                    prev = side
            if (len(crossings) >= PIN_MIN_CROSSES and crossings[-1]-crossings[0] >= PIN_MIN_CROSS_SPAN
                    and now-crossings[-1] <= PIN_LAST_CROSS_AGE):
                return {"state": "ACTIVE", "strike": k, "low": lo, "high": hi,
                        "crosses": len(crossings), "span_seconds": round(span),
                        "note": f"观察区间 {lo:,.2f}–{hi:,.2f}，穿过中心 {len(crossings)} 次。"}
        return status("WATCH", "未见价格在同一正 GEX 节点两侧反复来回")

    def tail_price_pattern(self, now, gex):
        if "gex" in self.provider_errors:
            return None
        return self.price_pattern(self.recent_price_path(self.path_samples, now, self.spot_at), gex.get("top", ()))

    @staticmethod
    def recent_price_path(samples, now, latest=None, *, min_span=PATH_MIN_SPAN):
        path = [(t, p) for t, p in samples if now-PATH_WINDOW <= t <= now]
        if (not path or now-path[-1][0] > QUOTE_AGE_LIMIT or (latest is not None and path[-1][0] != latest)
                or len(path) < PATH_MIN_SAMPLES or path[-1][0]-path[0][0] < min_span):
            return []
        return path

    def apply_index_spot(self, payload, now):
        def reject(note):
            self.index_samples.clear()
            self.index_note = note
            return False

        rows = payload.get("results") if isinstance(payload, dict) and payload.get("status") == "OK" else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return reject("未收到有效的 Massive SPX 响应")
        row = rows[0]
        if row.get("ticker") != "I:SPX" or row.get("error"):
            return reject("未取得独立 SPX 现价；请检查行情权限")
        if row.get("timeframe") != "REAL-TIME":
            return reject("独立 SPX 未确认实时；延迟行情不参与判断")
        if row.get("market_status") != "regular_trading":
            return reject("独立 SPX 未确认处于正常交易时段")
        raw, value = row.get("last_updated"), row.get("value")
        price = number(value) if type(value) in (float, int) else None
        if type(raw) is not int or not price or price <= 0:
            return reject("独立 SPX 缺有效价格或源时间")
        if not (now-QUOTE_AGE_LIMIT)*1e9 <= raw <= now*1e9:
            return reject("独立 SPX 源时间过期或超前")
        ts = raw/1e9
        at = datetime.fromtimestamp(ts, NY)
        if at.date() != datetime.fromtimestamp(now, NY).date() or at.hour != 15 or at.minute < 30:
            return reject("独立 SPX 源时间尚未进入本日尾盘")
        history = self.index_samples
        if history and ts < history[-1][0]:
            return reject("独立 SPX 源时间回退，等待重建样本")
        if history and ts-history[-1][0] > QUOTE_AGE_LIMIT:
            history.clear()
        if history and ts == history[-1][0]:
            history.pop()
        history.append((ts, price))
        while history and history[0][0] < ts-PATH_WINDOW:
            history.popleft()
        self.index_note = "独立 SPX 实时现价"
        return True

    @staticmethod
    def price_pattern(path, nodes=()):
        if not path:
            return None
        spot = path[-1][1]
        total_span = path[-1][0]-path[0][0]
        # Side-normalization keeps the tests and thresholds identical for both directions.
        candidates = []
        for sign, side in ((1,"CALL"),(-1,"PUT")):
            ps = [p*sign for _,p in path]
            end, peak = ps[-1], max(ps)
            # A real turn needs an inbound leg and multiple outward observations.
            # A single wick back through a node is not enough for a V/A label.
            pivot = ps.index(min(ps))
            if 1 < pivot < len(ps)-2:
                pre = max(ps[:pivot])
                origin = max(i for i in range(pivot) if ps[i] == pre)
                inbound, outbound = pre-ps[pivot], end-ps[pivot]
                age = path[-1][0]-path[pivot][0]
                if (pivot-origin >= 2 and path[pivot][0]-path[origin][0] >= TURN_LEG_SECONDS
                        and TURN_LEG_SECONDS <= age <= TURN_MAX_AGE and inbound >= TURN_LEG
                        and outbound >= max(TURN_LEG, TURN_RECOVERY*inbound)
                        and ps[-2] >= ps[pivot]+PATH_STEP and end >= ps[-2]+PATH_STEP/2
                        and max(ps[pivot:])-end <= PATH_STEP*2):
                    candidates.append((2,path[pivot][0],{"kind":"REVERSAL","side":side,"weight":PATH_WEIGHT,
                        "reason":"先下探，收回后继续上推" if sign==1 else "先冲高，收回后继续下压"}))
            net = end-ps[0]
            if net < PATH_MOVE or peak-end > max(PATH_STEP*2,PATH_EDGE_FRACTION*(peak-min(ps))):
                continue
            bins = [[],[],[]]
            for (t,_),p in zip(path,ps):
                bins[min(2,int(3*(t-path[0][0])/total_span))].append(p)
            travel = sum(abs(b-a) for a,b in zip(ps,ps[1:]))
            if (all(len(part)>=2 for part in bins) and travel and net/travel >= PATH_EFFICIENCY
                    and all(min(b)-min(a) >= PATH_STEP and max(b)-max(a) >= PATH_STEP
                            for a,b in zip(bins,bins[1:]))):
                candidates.append((1,0,{"kind":"CONTINUATION","side":side,"weight":PATH_WEIGHT,
                    "reason":"连续三段高低点均抬高" if sign==1 else "连续三段高低点均下移"}))
                continue
            # Also recognize a sustained break without requiring a retest or a staircase.
            for node in sorted((row["strike"] for row in nodes if abs(row["strike"]-spot)<=10),
                               key=lambda k:abs(k-spot)):
                level = node*sign
                wrong = [i for i,p in enumerate(ps) if p < level+HOLD_BUFFER]
                if not wrong:
                    continue  # No crossing observed inside the window.
                first = wrong[-1]+1
                if (len(ps)-first >= 3 and min(ps[:first]) <= level-HOLD_BUFFER
                        and path[-1][0]-path[first][0] >= HOLD_SECONDS):
                    candidates.append((1,0,{"kind":"CONTINUATION","side":side,"weight":PATH_WEIGHT,
                        "node":node,"reason":"突破近端节点后持续站稳" if sign==1 else "跌破近端节点后持续压住"}))
                    break
        if not candidates:
            return None
        candidates.sort(key=lambda item:(item[0],item[1]),reverse=True)
        if len(candidates)>1 and candidates[0][:2]==candidates[1][:2] and candidates[0][2]["side"]!=candidates[1][2]["side"]:
            return None
        return candidates[0][2]

    def record_quote_proxy(self, now):
        def reject(note):
            self.parity_samples.clear()
            self.parity_note = note

        at = datetime.fromtimestamp(now, NY)
        if at.hour != 15 or at.minute < 30:
            return
        session = self.session(now)
        if not session["verified"] or not session["close"] or session["close"].timestamp() <= now:
            return
        rate = number(self.cfg.rate)
        if rate is None or abs(rate)>1:
            return reject("利率参数无效，未计算期权隐含价格")
        factor = exp(rate*(session["close"].timestamp()-now)/(365.25*86400))
        pairs = []
        for k in sorted({k for k,_ in self.focus}):
            keys = ((k,"C"),(k,"P"))
            if not all(key in self.focus and self.quote_fresh(key,now) for key in keys):
                continue
            c,p = map(self.option,keys)
            oldest,newest = sorted((c.bid_at,p.bid_at))
            if now-oldest>PARITY_QUOTE_AGE or newest>now or newest-oldest>PARITY_SYNC:
                continue
            low,high = k+(c.bid-p.ask)*factor,k+(c.ask-p.bid)*factor
            if high-low <= PARITY_MAX_WIDTH:
                pairs.append((oldest,newest,low,high))
        if pairs:
            newest = max(row[1] for row in pairs)
            pairs = [row for row in pairs if newest-row[0] <= PARITY_SYNC]
        if len(pairs)<PARITY_MIN_PAIRS:
            return reject("不足 3 组新鲜、同步且价差合格的期权报价")
        low,high = max(row[2] for row in pairs),min(row[3] for row in pairs)
        mids = [(row[2]+row[3])/2 for row in pairs]
        if max(mids)-min(mids)>PARITY_MAX_DISAGREEMENT or low-high>PARITY_MAX_INTERVAL_GAP:
            return reject("各行权价的期权隐含价格分歧过大")
        ts = min(row[0] for row in pairs)
        samples = self.parity_samples
        if samples and ts<samples[-1][0]:
            return reject("期权报价源时间回退，等待重建样本")
        if samples and ts-samples[-1][0]>QUOTE_AGE_LIMIT:
            samples.clear()
        if samples and ts==samples[-1][0]:
            samples.pop()
        samples.append((ts,(low+high)/2 if low<=high else median(mids)))
        while samples and samples[0][0]<ts-PATH_WINDOW:
            samples.popleft()
        self.parity_note = f"期权隐含价格 · {len(pairs)} 组同步报价"

    def tail_volume(self, now, inside, healthy):
        # Compare only full equal-length windows, dated by the last completely
        # traversed head. Never count an unfinished minute as an empty minute.
        waiting = {"state":"WAIT","ratio":None}
        if not inside or not healthy or self.flow_gap or self.flow_covered_at is None or not self.spot:
            return waiting
        end = int(min(now,self.flow_covered_at)//VOL_WINDOW)*VOL_WINDOW
        start = end - (VOL_BASE_WINDOWS+1)*VOL_WINDOW
        opened = datetime.fromtimestamp(now, NY).replace(hour=15,minute=30,second=0,microsecond=0).timestamp()
        if start < max(self.started_at,opened) or now-self.flow_covered_at > QUOTE_AGE_LIMIT:
            return waiting
        volumes = [0.]*(VOL_BASE_WINDOWS+1)
        for (k,_),q in self.options.items():
            if abs(k/self.spot-1) > .01:
                continue
            for t in q.trades:
                # Coverage is verified in the provider's occurred_at order.
                flow_at = t.get("flow_at", t["ts"])
                if start <= flow_at < end and t["ts"] < end:
                    volumes[int((flow_at-start)//VOL_WINDOW)] += t["size"]
        reference = median(volumes[:-1])
        if min(volumes[:-1]) <= 0 or reference < VOL_MIN_BASE:
            return waiting
        ratio = volumes[-1]/reference
        return {"state":"EXPANDING" if ratio>=VOL_MIN_RATIO and volumes[-1]>=VOL_MIN_SAMPLE else "NORMAL",
                "ratio":round(ratio,2),"contracts":volumes[-1],"baseline":reference,"through":end}

    def apply_chain(self, rows, now):
        strikes = set()
        for row in rows:
            key = contract(row.get("contract_symbol"), self.day)
            if key is None:
                continue
            strikes.add(key[0])
            q = self.option(key)
            oi = number(row.get("open_interest"))
            if oi is not None and oi >= 0:
                q.oi = oi
                q.oi_date = self.session(now)["previous"]
                q.oi_date_verified = False
            # Native chain Greeks may be stale. Never stamp them with request time.
            qt = stamp(row.get("quoted_at"))
            iv, delta, gamma = (number(row.get(k)) for k in ("implied_vol_pct", "delta", "gamma"))
            if qt is not None and iv and 0 < iv <= 5 and delta is not None and abs(delta) <= 1 and gamma is not None and gamma >= 0:
                q.greeks, q.greek_at = {"iv": iv, "delta": delta, "gamma": gamma}, qt
        if strikes:
            self.strikes = sorted(set(self.strikes) | strikes)
            self.chain_date = self.day
        if self.spot and self.strikes:
            near = sorted(self.strikes, key=lambda k: (abs(k-self.spot), k))[:5]
            new = {(k, r) for k in near for r in ("C", "P")}
            self.focus = new
            self.window.active = {k: None for k in new}
            for key in new:
                self.option(key).active = True

    def apply_quotes(self, data, now):
        returned = set()
        for row in data.get("quotes", []):
            key = contract(row.get("contract_symbol"), self.day)
            if key is None or key not in self.focus:
                continue
            source = stamp(row.get("quoted_at"))
            bid, ask = number(row.get("bid_usd")), number(row.get("ask_usd"))
            q = self.option(key)
            returned.add(key)
            if source is None:
                self.quote_results[key] = {"status": "invalid_time", "reason": "接口报价时间缺失或没有时区"}
                q.md_type = 0
                continue
            if source > now+FUTURE_TOLERANCE:
                self.quote_results[key] = {"status": "clock_ahead", "reason": f"最新报价到达时领先本机 {source-now:.1f} 秒；请核对系统时间",
                                           "rejected_source_at": source}
                q.md_type = 0
                continue
            if bid is None or ask is None or not 0 < bid <= ask:
                self.quote_results[key] = {"status": "invalid_quote", "reason": "本轮没有有效双边报价，或买卖价倒挂"}
                q.md_type = 0
                continue
            q.active = True
            q.bid, q.ask, q.bid_at, q.ask_at = bid, ask, source, source
            self.quote_results[key] = {"status": "ok", "reason": ""}
            q.md_type = int(self.quote_fresh(key, now))
        # Missing/one-sided quotes never silently inherit the old quote's status.
        for key in self.focus-returned:
            self.option(key).md_type = 0
            self.quote_results[key] = {"status": "unavailable", "reason": "本轮接口未返回该合约的有效报价；显示的价格可能是上次有效报价"}
        self.quote_unavailable = [r.get("contract_symbol") for r in data.get("unavailable", [])]
        self.last_quote_ok = now
        self.record_quote_proxy(now)

    def quote_fresh(self, key, now):
        q = self.option(key)
        age = now-max(q.bid_at, q.ask_at)
        # Use the same 2s tolerance at admission and display; never revive an
        # explicitly missing/invalid response merely because older prices exist.
        return (self.quote_results.get(key, {}).get("status") == "ok" and q.active
                and q.valid_quote() and -FUTURE_TOLERANCE <= age <= QUOTE_AGE_LIMIT)

    def quote_diagnostic(self, key, now):
        result = self.quote_results.get(key, {"status": "waiting", "reason": "尚未收到本轮重点合约报价"})
        status, reason = result["status"], result["reason"]
        if status == "ok":
            age = now-max(self.option(key).bid_at, self.option(key).ask_at)
            if age > QUOTE_AGE_LIMIT:
                status, reason = "stale", f"源报价已超过 {QUOTE_AGE_LIMIT} 秒"
            elif age < -FUTURE_TOLERANCE:
                status, reason = "clock_ahead", "本机时间回退或源时间异常，请核对系统时间"
        labels = {"ok": "有效", "waiting": "等待报价", "unavailable": "本轮无报价",
                  "invalid_time": "时间无效", "invalid_quote": "报价无效",
                  "clock_ahead": "时钟偏差", "stale": "报价过期"}
        return {"status": status, "label": labels[status], "reason": reason,
                "rejected_source_at": result.get("rejected_source_at")}

    def apply_flow(self, rows, now, *, include_backfill=False, count_poll=True):
        out_of_order = set()
        for row in rows:
            self.flow_rows += 1
            if row.get("kind") != "order":
                continue  # Packages cannot be treated as single-leg directional buys.
            key = contract(row.get("contract_symbol"), self.day)
            source = stamp(row.get("occurred_at"))
            # The streaming schema has a five-minute bucket boundary that can
            # lie in the future. Both sources use the event's time in stream mode.
            end = source if self.cfg.nightwatch_flow_stream else stamp(row.get("bucket_end_at")) or source
            size, premium = number(row.get("contracts")), number(row.get("premium_usd"))
            identity = row.get("aggregate_id")
            if key is None or source is None or end is None or not identity or size is None or size <= 0 or premium is None or premium < 0:
                continue
            if end > now+FUTURE_TOLERANCE or now-end > (FLOW_LOOKBACK if include_backfill else 60):
                continue
            old = self.aggregate_seen.get(identity)
            if old:
                delta, amount = size-old[0], premium-old[1]
                if delta <= 0 or amount <= 0:
                    continue
            else:
                if source < self.started_at:
                    self.aggregate_seen[identity] = (size, premium, end)
                    continue  # Existing orders establish a baseline; no startup burst.
                delta, amount = size, premium
            bid, ask = number(row.get("nbbo_bid_usd")), number(row.get("nbbo_ask_usd"))
            if bid is None or ask is None or not 0 < bid <= ask:
                continue
            self.aggregate_seen[identity] = (size, premium, end)
            q = self.option(key)
            if q.first_observed is None:
                q.first_observed = now
            # Classify against this aggregate's NBBO, never a later polled quote.
            price = amount/delta/100
            if bid <= price <= ask and ask > bid:
                pos = (price-bid)/(ask-bid)
                side = "BUY" if pos >= .8 else "SELL" if pos <= .2 else "UNKNOWN"
                quality = 1 if side != "UNKNOWN" else .7
            else:
                side, quality = "UNKNOWN", .5
            trade = {"ts": end, "flow_at": source, "size": delta, "price": price, "premium": amount, "aggregate_id": identity,
                     "side": side, "quality": quality, "sampled": True}
            if q.trades and end < q.trades[-1]["ts"]:
                out_of_order.add(key)
            q.trades.append(trade)
            d = self.daily[key]
            d["volume"] += delta
            d["premium"] += amount
            d[{"BUY": "buy", "SELL": "sell", "UNKNOWN": "unknown"}[side]] += delta
            current_quote = q.bid, q.ask
            q.bid, q.ask = bid, ask
            if now-end <= 60 and self.spot and 0 <= now-self.spot_at <= 45:
                # Superclass rules operate on aggregated observations, not raw prints.
                self.detect(key, end, trade, "聚合订单增量；按该订单 NBBO 估算方向，非原始逐笔")
            q.bid, q.ask = current_quote
        for key in out_of_order:
            q = self.option(key)
            q.trades = deque(sorted(q.trades, key=lambda t: t["ts"]))
        self.aggregate_seen = {k: v for k, v in self.aggregate_seen.items() if v[2] >= now-900}
        self.flow_polls += int(count_poll)
        self.last_flow_ok = now

    def analytics(self, q, now, remaining):
        if now-max(q.bid_at, q.ask_at) > 45:
            return None
        return super().analytics(q, now, remaining)

    def detect(self, key, ts, trade, reason):
        """For a sampled aggregate feed, only report observed large ask orders.

        Incomplete pages are not a sound basis for raw 1/5/10-second spike ratios.
        Keep those ratios unavailable rather than giving sampling noise a score.
        """
        q = self.option(key)
        if not q.valid_quote() or not self.cfg.feed_min_ask <= q.ask <= self.cfg.feed_max_ask:
            return
        if not self.spot or (q.right == "C" and q.strike < self.spot) or (q.right == "P" and q.strike > self.spot):
            return
        if trade["side"] != "BUY" or (trade["size"] < self.cfg.large_contracts and trade["premium"] < self.cfg.large_premium):
            return
        recent = [t for t in q.trades if ts-10 < t["ts"] <= ts]
        volume = sum(t["size"] for t in recent)
        ratio = sum(t["size"] for t in recent if t["side"] == "BUY")/volume if volume else None
        previous = next((s for s in reversed(self.signals) if s["key"] == key and 0 <= ts-s["last_ts"] <= 10), None)
        if previous:
            previous.update(last_ts=ts, price=trade["price"], bid=q.bid, ask=q.ask, ask_buy=ratio)
            previous["size"] += trade["size"]
            previous["premium"] += trade["premium"]
            previous["updates"] += 1
        else:
            self.signal_seq += 1
            self.signals.append({"id": self.signal_seq, "key": key, "strike": q.strike, "right": q.right,
                                 "ts": ts, "last_ts": ts, "types": ["large_ask_trade"], "updates": 1,
                                 "size": trade["size"], "premium": trade["premium"], "price": trade["price"],
                                 "bid": q.bid, "ask": q.ask, "quality": trade["quality"], "note": reason,
                                 "ask_buy": ratio, "volume_multiple": None, "premium_multiple": None,
                                 "sampled": True, "trigger_context": self.tail_journal.for_order(ts, q.right)})

    def model_modules(self, now):
        _, skew, charm, remaining = super().model_modules(now)
        data = self.native or {}
        source_at = stamp(data.get("snapshot_at"))
        age = now-source_at if source_at is not None else None
        input_age = (self.native_input_age + max(0, now-self.native_received)
                     if self.native_input_age is not None else None)
        reason = None
        if not data:
            reason = "尚未收到 Live GEX"
        elif data.get("session_date_et") != self.day:
            reason = "Live GEX 不属于当前美东交易日"
        elif age is None:
            reason = "Live GEX 没有可核验的源时间"
        elif age < -FUTURE_TOLERANCE:
            reason = f"GEX 源时间领先本机 {-age:.1f} 秒，请核对系统时间"
        elif age > QUOTE_AGE_LIMIT:
            reason = f"GEX 快照已 {age:.0f} 秒，超过 {QUOTE_AGE_LIMIT} 秒门槛"
        elif data.get("state") != "fresh":
            reason = "平台 GEX 状态为 " + str(data.get("state") or "未知")
        elif input_age is not None and input_age > QUOTE_AGE_LIMIT:
            reason = (f"GEX 内容已 {input_age:.0f} 秒，超过 {QUOTE_AGE_LIMIT} 秒门槛"
                      f"（收到时 {self.native_input_age:.0f} 秒，收包后又过了 {max(0, now-self.native_received):.0f} 秒）")
        available = reason is None
        summary = data.get("summary") or {}
        rows = [{"strike": r["strike_usd"], "net": r["net_gex_usd"], "call": None, "put": None}
                for r in data.get("strikes", []) if number(r.get("strike_usd")) and number(r.get("net_gex_usd")) is not None]
        total = number(summary.get("total_gex_usd")) if available else None
        gex = {"available": available, "coverage": None, "rows": rows if available else [],
               "total": total, "regime": "POSITIVE" if total is not None and total > 0 else "NEGATIVE" if total is not None and total < 0 else "UNAVAILABLE",
               "top": sorted(rows, key=lambda r: abs(r["net"]), reverse=True)[:3] if available else [],
               "call_wall": summary.get("call_wall_strike_usd"), "put_wall": summary.get("put_wall_strike_usd"),
               "scope": "夜航社 Live Dealer GEX · Top 节点 · 无逐节点 C/P 拆分", "native": True,
               "oi_date": None, "oi_date_inferred": False,
               "reason": reason, "source_at": source_at, "source_age_seconds": age,
               "input_age_seconds": input_age, "content_age_seconds": input_age,
               "content_age_on_arrival_seconds": self.native_input_age if data else None,
               "received_at": self.native_received if data else None,
               "checked_at": self.gex_checked_at or None, "http_status": self.gex_http_status,
               "upstream_state": data.get("state")}
        # The small quote set does not satisfy the original +/-10-strike Charm coverage.
        charm.update(available=False, direction="NEUTRAL", flow_dollars_per_minute=None,
                     reason="试跑版仅跟价10个合约；不满足原版 Charm 的完整窗口要求")
        return gex, skew, charm, remaining

    def near_flow(self, frame):
        """Read-only tape summary; never feeds the playbook or aggregate detector."""
        now, api = frame["time"], frame["api"]
        anchor = frame.get("candidate_spot")
        if not anchor and frame["gex"]["available"] and self.spot and 0 <= now-self.spot_at <= QUOTE_AGE_LIMIT:
            anchor = {"source": "gex", "value": self.spot, "at": self.spot_at}
        if not frame["market_open"]:
            return {"state": "OUTSIDE"}
        if not frame["connected"] or not api["flow_fresh"] or not anchor:
            return {"state": "WAIT", "reason": "等待有效参考价" if not anchor else "等待新鲜成交样本"}
        buckets = {r: {"volume": 0., "buy": 0., "sell": 0., "unknown": 0.} for r in ("C", "P")}
        for (strike, right), q in self.options.items():
            if right not in buckets or abs(strike-anchor["value"]) > 10:
                continue
            row = buckets[right]
            for trade in q.trades:
                if now-300 <= trade.get("flow_at", trade["ts"]) <= now and trade["ts"] <= now:
                    row["volume"] += trade["size"]
                    kind = {"BUY": "buy", "SELL": "sell"}.get(trade["side"], "unknown")
                    row[kind] += trade["premium"]
        return {"state": "PARTIAL" if api["flow_gap"] else "RECENT", "anchor": anchor,
                "covered_at": self.flow_covered_at, "sides": buckets}

    def snapshot(self, now=None, leader_seconds=300, leader_sort="premium"):
        now = time.time() if now is None else now
        for key in self.focus:
            q = self.option(key)
            q.md_type = int(self.quote_fresh(key, now))
        result = super().snapshot(now, leader_seconds, leader_sort)
        book = result["playbook"]
        coverage_age = max(0, now-self.flow_covered_at) if self.flow_covered_at is not None else None
        coverage_stale = coverage_age is not None and coverage_age > QUOTE_AGE_LIMIT
        resync_pending = self.flow_resync_at is not None and now <= self.flow_resync_at + FLOW_LOOKBACK + FUTURE_TOLERANCE
        gap = self.flow_gap or coverage_stale or resync_pending
        flow_age = max(0, now-self.last_flow_ok) if self.last_flow_ok else None
        flow_fresh = flow_age is not None and flow_age <= QUOTE_AGE_LIMIT
        # Preserve only the independently validated price shape. Leave the
        # combined score and reference under the original Flow coverage gate.
        pattern = book["pattern"]
        price_preview = None
        if (gap and flow_fresh and pattern and result["healthy"] and result["gex"]["available"]
                and self.pin_observation(result, now, book["in_window"])["state"] != "ACTIVE"):
            price_preview = {"side": pattern["side"], "label": self.price_pattern_label(pattern),
                             "reason": pattern["reason"], "source": "gex", "unconfirmed": ["成交未确认"]}
        if not flow_fresh or gap:
            result["healthy"] = False
            book.update(direction="STAND_BY", reference=None, reason="成交查询未就绪、超过45秒未更新或本轮覆盖有缺口")
        if not result["gex"]["available"]:
            result["healthy"] = False
            book.update(direction="STAND_BY", reference=None, reason=result["gex"]["reason"])
        result["warnings"] = [w.replace("窗口移动中；等待旧合约退订释放额度", "重点合约正在重新选择")
                               for w in result["warnings"]]
        result["warnings"].extend(self.provider_errors.values())
        gap_reasons = []
        if resync_pending:
            gap_reasons.append("Flow 源要求重新同步；近 10 分钟覆盖暂未确认")
        if self.flow_pending_pages:
            extra = f" · {self.flow_pending_ranges} 段待补" if self.flow_pending_ranges > 1 else ""
            gap_reasons.append(f"成交分页追补中 · 最近段优先 · 待补段已读 {self.flow_pending_pages} 页" + extra)
        elif self.flow_gap:
            gap_reasons.append("推送已连接；等待核对交接缺口" if self.flow_transport == "stream" else "本轮成交覆盖不足；等待重新查询")
        elif coverage_stale:
            gap_reasons.append("旧页已补齐；正在核对最新一段成交")
        result["warnings"].extend(gap_reasons)
        et = datetime.fromtimestamp(now, NY)
        in_window = bool(result["market_open"] and et.hour == 15 and et.minute >= 30 and result["remaining"] > 0)
        blockers = []
        if not self.connected:
            blockers.append("行情源尚未连接")
        if not result["market_open"]:
            blockers.append("当前不在美股正常交易时段")
        elif not in_window:
            blockers.append("尚未进入美东15:30至收盘的方向观察窗口")
        if result["quote_coverage"] < .5:
            blockers.append("有效期权报价不足50%")
        if not result["gex"]["available"]:
            blockers.append(result["gex"]["reason"])
        if not flow_fresh:
            delivery = "成交推送" if self.flow_transport == "stream" else "成交查询"
            blockers.append("等待首轮成交数据" if flow_age is None else f"{delivery}已 {flow_age:.0f} 秒未更新")
        blockers.extend(gap_reasons)
        if book["direction"] == "STAND_BY" and not blockers:
            blockers.append(book["reason"] or "等待有效观察条件")
        book.update(in_window=in_window, blockers=blockers, price_preview=price_preview,
                    pin=self.pin_observation(result, now, in_window))
        if in_window:
            gex_ok = result["gex"]["available"]
            index_latest = self.fresh_price_sample(self.index_samples, now, QUOTE_AGE_LIMIT)
            parity_latest = (self.fresh_price_sample(self.parity_samples, now, PARITY_QUOTE_AGE)
                             if self.connected and "quotes" not in self.provider_errors else None)
            spot, spot_source = parity_latest,"parity"
            if index_latest:
                spot, spot_source = index_latest,"massive"
            elif gex_ok and self.spot and 0<=now-self.spot_at<=QUOTE_AGE_LIMIT:
                spot, spot_source = (self.spot_at,self.spot),"gex"
            if spot:
                result["candidate_spot"] = {"at": spot[0], "value": spot[1], "source": spot_source}
            if gex_ok:
                note = (self.price_pattern_label(pattern) if pattern else
                        "尚未形成符合条件的顺势或 V／A 形" if self.recent_price_path(self.path_samples, now, self.spot_at)
                        else "采集价格样本中或源价格已过期")
                price_status = "GEX 上下文价 · " + note
            else:
                source, name, samples = None, None, None
                if index_latest:
                    source,name,samples = "massive","独立 SPX",self.index_samples
                elif parity_latest:
                    source,name,samples = "parity","期权隐含价格",self.parity_samples
                if samples:
                    path = self.recent_price_path(samples,now)
                    inferred = self.price_pattern(path)  # Without GEX nodes.
                    early = False
                    if source == "parity" and not path:
                        # Show an early observation only while the standard window is
                        # still building, never after a full window fails the shape test.
                        watch = self.recent_price_path(samples,now,min_span=PARITY_WATCH_SPAN)
                        if watch and all(b[0]-a[0] <= PARITY_WATCH_MAX_GAP for a,b in zip(watch,watch[1:])):
                            inferred = self.price_pattern(watch)
                            early = bool(inferred)
                    ts,price = samples[-1]
                    label = self.price_pattern_label(inferred) if inferred else None
                    observation = (label + (" · 观察中" if early else "") if label else
                                   "尚未形成符合条件的顺势或 V／A 形" if path else "采集价格样本中")
                    price_status = f"{name} {price:,.2f} · {datetime.fromtimestamp(ts, NY):%H:%M:%S} ET · {observation}"
                    if inferred:
                        warnings = ["GEX 未确认"] + (["成交未确认"] if gap or not flow_fresh else [])
                        book["price_preview"] = {"side": inferred["side"], "label": label,
                                                 "reason": inferred["reason"], "source": source, "unconfirmed": warnings,
                                                 "tier": "watch" if early else "preview"}
                else:
                    proxy_note = ("期权报价请求异常" if "quotes" in self.provider_errors else
                                  "期权隐含价格已过期" if self.parity_samples else self.parity_note)
                    price_status = ("独立 SPX 已过期" if self.index_samples else self.index_note)+"；"+proxy_note
            if self.flow_covered_at is None:
                flow_status = "尚无连续核对进度"
            else:
                verified_to = datetime.fromtimestamp(self.flow_covered_at, NY).strftime("%H:%M:%S")
                flow_status = f"连续核对至 {verified_to} ET"
            if not flow_fresh or gap:
                flow_status += " · 近 10 分钟未确认"
            else:
                flow_status += " · 近期已确认"
            book["data_status"] = {"price": price_status, "gex": "内容有效" if gex_ok else result["gex"]["reason"],
                                   "flow": flow_status}
        volume = self.tail_volume(now, in_window, result["healthy"])
        if book["pin"]["state"] == "ACTIVE":
            book.update(reference=None, reference_note="疑似pin盘")
        elif book["direction"] in {"CALL","PUT"} and book["reference"] is None:
            _, book["reference_note"] = self.near_reference(now, "C" if book["direction"] == "CALL" else "P")
        side = book["direction"]
        label = None
        if book["pin"]["state"] == "ACTIVE":
            state = "PIN"
        elif side in {"CALL","PUT"} and pattern and pattern["side"] == side:
            state = pattern["kind"]
            label = self.price_pattern_label(pattern)
        else:
            state = side if side in {"STAND_BY","NEUTRAL"} else "BIAS"
        book.update(regime={"state":state,"label":label}, volume=volume)
        result["api"] = {**self.api_info, "flow_polls": self.flow_polls, "flow_rows": self.flow_rows,
                         "flow_transport": self.flow_transport, "flow_stream_note": self.flow_stream_note,
                         "last_flow_ok": self.last_flow_ok, "last_quote_ok": self.last_quote_ok,
                         "flow_age_seconds": flow_age, "flow_fresh": flow_fresh,
                         "freshness_limit_seconds": QUOTE_AGE_LIMIT,
                         "feed_rules": {"min_ask": self.cfg.feed_min_ask, "max_ask": self.cfg.feed_max_ask,
                                        "large_contracts": self.cfg.large_contracts, "large_premium": self.cfg.large_premium},
                         "quote_unavailable": len(self.quote_unavailable), "focus": [symbol(k, self.day) for k in sorted(self.focus)],
                         "manual_start": True, "flow_gap": gap,
                         "flow_pending_pages": self.flow_pending_pages,
                         "flow_pending_ranges": self.flow_pending_ranges,
                         "flow_coverage_age_seconds": coverage_age,
                         "data_connection_ready": bool(self.connected and result["gex"]["available"]
                                                       and result["quote_coverage"] >= .5
                                                       and self.last_flow_ok and now-self.last_flow_ok <= 45),
                         "quotes": [{"symbol": symbol(k, self.day), "strike": k[0], "right": k[1],
                                     "bid": self.option(k).bid, "ask": self.option(k).ask,
                                     "quoted_at": max(self.option(k).bid_at, self.option(k).ask_at),
                                     "fresh": self.option(k).md_type == 1,
                                     **self.quote_diagnostic(k, now)}
                                    for k in sorted(self.focus)]}
        result["spot_label"] = "SPX · Live GEX 上下文价"
        result["tail_history"] = list(self.tail_journal.changes)
        result["nearest_key_level"], _ = nearest_level(result)
        result["near_flow"] = self.near_flow(result)
        result["signals"].sort(key=lambda row: (row["last_ts"], row["id"]), reverse=True)
        return result

    @staticmethod
    def fresh_price_sample(samples, now, max_age):
        return samples[-1] if samples and 0<=now-samples[-1][0]<=max_age else None

    @staticmethod
    def price_pattern_label(pattern):
        if pattern["kind"] == "CONTINUATION":
            return "顺势延续"
        return "V形反转" if pattern["side"] == "CALL" else "A形反转"


class FlowSweep:
    """One descending interval. The first-page time dates its coverage;
    completing or absorbing a page must never timestamp it with a later wall clock.
    """
    def __init__(self, now, lower):
        self.head_at = now
        self.lower = lower
        self.high = lower
        self.pages = 0
        self.cursor = None
        self.cursors = set()
        self.last_time = None

    def visit(self, times, cursor):
        if times and self.last_time is not None and times[0] > self.last_time:
            raise ProviderError("FLOW_PAGINATION_ORDER", 5, "成交分页顺序改变，将从第一页重新追补")
        if cursor is not None and cursor in self.cursors:
            raise ProviderError("FLOW_PAGINATION_INCOMPLETE", 5, "成交分页重复，将从第一页重新追补")
        if not self.pages and times:
            self.high = max(self.lower, times[0])
        self.pages += 1
        if times:
            self.last_time = times[-1]
        self.cursor = cursor
        if cursor is not None:
            self.cursors.add(cursor)
        # Equality is NOT overlap: more IDs with that same time can be on the next page.
        return cursor is None or bool(times and times[-1] < self.lower)


class Service:
    def __init__(self, cfg, engine, client, *, run_log=None):
        self.cfg, self.engine, self.client = cfg, engine, client
        self.ready = False
        self.live = True
        self.due = {}
        self.flow_mark = engine.started_at
        self.flow_backlog = deque()
        self.flow_head_requested = None
        self.flow_newest_seen = None
        self.flow_can_probe = False
        self.flow_head_failed = False
        self.stream_since = self.stream_last = None
        self.stream_proven = self.stream_repair = False
        self.chain_cursor = None
        self.chain_rows = []
        self.day = None
        self.blocked = False
        self.health_trace = deque(maxlen=380)
        self.run_log = run_log

    @property
    def flow_sweep(self):
        # The queue is in time order; the newest missing range gets the next page.
        return self.flow_backlog[-1] if self.flow_backlog else None

    def commit_flow(self, sweep):
        self.flow_mark = sweep.high
        self.engine.flow_covered_at = sweep.head_at

    def reset_flow_work(self):
        # Keep the last *committed* mark; no queued cursor proves a missing interval.
        self.flow_backlog.clear()
        self.flow_can_probe = False
        self.flow_head_requested = None
        self.flow_head_failed = True
        self.engine.flow_pending_pages = self.engine.flow_pending_ranges = 0
        self.engine.flow_gap = True

    def stream_event(self, day, kind, rows, now):
        e = self.engine
        if self.blocked:
            raise StreamError("采集已停止", stop=True)
        if self.day != day or e.day != day:
            return
        if kind in {"subscribed", "resync"}:
            self.stream_since = now
            self.stream_proven = False
            e.flow_transport = "stream"
            e.flow_stream_note = "推送已订阅 · 正在核对交接缺口"
            e.flow_gap = True
            self.due["flow"] = 0
            if kind == "resync":
                # A rebuild can remove rows, which aggregate upserts cannot undo.
                # Don't certify a scoring window that overlaps the old view.
                e.flow_resync_at = now
                self.stream_repair = True
        if self.stream_since is None:
            return
        if kind == "flow":
            accepted = [r for r in rows if r.get("session_date") == day and r.get("kind") == "order"]
            for row in accepted:
                source = stamp(row.get("occurred_at"))
                identity, sym = row.get("aggregate_id"), row.get("contract_symbol")
                if (source is None or source > now + FUTURE_TOLERANCE or not isinstance(identity, str)
                        or not identity or not isinstance(sym, str) or not contract(sym, day)):
                    raise StreamError("成交时间或合约无法核对")
            if accepted:
                e.apply_flow(accepted, now, include_backfill=True, count_poll=False)
        self.stream_last = now
        e.last_flow_ok = max(e.last_flow_ok, now)
        if self.stream_proven:
            e.flow_covered_at = max(e.flow_covered_at or 0, now)

    def stream_covers(self, now):
        if self.stream_repair or self.stream_last is None or now-self.stream_last > IDLE_LIMIT:
            return False
        if self.stream_proven:
            return True
        e = self.engine
        stitched = not e.flow_gap and e.flow_covered_at is not None and e.flow_covered_at >= self.stream_since
        if not stitched and self.stream_last-self.stream_since < FLOW_LOOKBACK:
            return False
        # Run in the HTTP worker, never clear a cursor while it has a request in flight.
        self.flow_backlog.clear()
        self.flow_head_failed = False
        e.flow_pending_pages = e.flow_pending_ranges = 0
        self.stream_proven = True
        e.flow_gap = False
        e.flow_covered_at = max(e.flow_covered_at or 0, self.stream_last)
        e.flow_stream_note = "Flow 实时推送 · REST 仅用于回补"
        return True

    def stream_lost(self, note):
        if self.stream_proven:
            self.engine.flow_gap = True
            self.stream_repair = True
        self.stream_since = self.stream_last = None
        self.stream_proven = False
        self.engine.flow_transport = "rest"
        self.engine.flow_stream_note = note

    async def stream_worker(self):
        delay = 2
        while True:
            e = self.engine
            now = time.time()
            session = e.session(now)
            if (not self.ready or self.day != e.day or not session["verified"] or not session["open"]
                    or not session["open"].timestamp() <= now < session["close"].timestamp()):
                await asyncio.sleep(1)
                continue
            if self.blocked:
                return
            day = self.day
            e.flow_stream_note = "正在连接推送 · 当前使用 REST"
            try:
                async with asyncio.timeout(session["close"].timestamp()-now):
                    token = await self.client.flow_token()
                    await receive_flow(token, day, lambda kind, rows: self.stream_event(day, kind, rows, time.time()))
            except ProviderError as error:
                reason = {401: "Key 未通过推送鉴权", 402: "推送需要有效订阅", 403: "账户或 Key 暂无推送权限"}.get(error.status)
                if reason:
                    self.stream_lost(reason + " · 继续 REST")
                    return
                self.stream_lost("推送凭据暂未就绪 · 继续 REST")
                delay = max(delay, error.retry)
            except StreamError as error:
                if self.stream_since is not None and time.time()-self.stream_since >= IDLE_LIMIT:
                    delay = 2
                self.stream_lost(error.code + " · 继续 REST")
                if error.stop:
                    return
            except TimeoutError:
                self.stream_lost("正常交易时段已结束")
                delay = 2
            if self.blocked:
                return
            await asyncio.sleep(delay + uniform(0, .5))
            delay = max(2, min(60, delay*2))

    def sync_info(self):
        self.engine.api_info = {"units": self.client.units, "budget": self.client.max_units,
                                "remaining": self.client.remaining, "monthly_limit": self.client.limit,
                                "requests": self.client.requests, "last_ok": self.client.last_ok}

    def record_health(self, frame):
        if not frame["playbook"]["in_window"]:
            return
        ts = frame["time"]
        if self.health_trace and 0 <= ts-self.health_trace[-1]["time"] < 5:
            return
        gex, api = frame["gex"], frame["api"]
        self.health_trace.append({
            "time": ts, "time_et": frame["time_et"], "direction": frame["playbook"]["direction"],
            "blockers": frame["playbook"]["blockers"], "quote_coverage": frame["quote_coverage"],
            "gex_checked_at": gex.get("checked_at"), "gex_http_status": gex.get("http_status"),
            "gex_request_error": self.engine.provider_errors.get("gex"),
            "gex_source_age_seconds": gex.get("source_age_seconds"),
            "gex_content_age_on_arrival_seconds": gex.get("content_age_on_arrival_seconds"),
            "gex_content_age_seconds": gex.get("content_age_seconds"),
            "flow_age_seconds": api["flow_age_seconds"], "flow_gap": api["flow_gap"],
            "flow_pending_pages": api["flow_pending_pages"], "flow_coverage_age_seconds": api["flow_coverage_age_seconds"],
            "flow_pending_ranges": api["flow_pending_ranges"],
        })
        if self.run_log is not None:
            # Persist only allowlisted connection/quality diagnostics. Never dump
            # a frame, provider error text, quotes, scores, or direction values.
            self.write_run_health({
                "event": "tail_data_check", "time_et": frame["time_et"], "version": VERSION,
                "connected": bool(frame["connected"]), "data_ready": bool(frame["healthy"]),
                "quote_coverage": frame["quote_coverage"],
                "gex_available": bool(gex.get("available")),
                "gex_state_fresh": gex.get("upstream_state") == "fresh",
                "gex_last_http": gex.get("http_status"),
                "gex_request_failed": "gex" in self.engine.provider_errors,
                "gex_snapshot_age_s": gex.get("source_age_seconds"),
                "gex_age_on_arrival_s": gex.get("content_age_on_arrival_seconds"),
                "gex_content_age_s": gex.get("content_age_seconds"),
                "flow_age_s": api["flow_age_seconds"], "flow_gap": api["flow_gap"],
                "flow_pages_in_progress": api["flow_pending_pages"],
                "flow_pending_ranges": api["flow_pending_ranges"],
                "flow_coverage_age_s": api["flow_coverage_age_seconds"],
                "flow_request_failed": "flow" in self.engine.provider_errors,
                "chain_request_failed": "chain" in self.engine.provider_errors,
                "worker_failed": "worker" in self.engine.provider_errors,
            })

    def write_run_health(self, status):
        path = self.run_log
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(status, separators=(",", ":"), allow_nan=False)+"\n"
            if path.exists() and path.stat().st_size+len(line) > TAIL_LOG_LIMIT:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(line)
        except (OSError, ValueError):
            self.engine.provider_errors["run_log"] = "本地尾盘诊断日志写入失败；采集和方向规则不受影响"
        else:
            self.engine.provider_errors.pop("run_log", None)

    def log_lifecycle(self, event):
        self.write_run_health({"event": event, "version": VERSION,
                               "time_et": datetime.now(NY).strftime("%Y-%m-%d %H:%M:%S ET")})

    async def step(self, now=None):
        now = time.time() if now is None else now
        e = self.engine
        e.now = now
        e.roll_day(now)
        session = e.session(now)
        if not session["verified"] or not session["open"] or not session["open"].timestamp() <= now < session["close"].timestamp():
            e.status_note = "休市；正常交易时段才取行情（尚未添加尾盘定时）"
            self.sync_info()
            return
        if not self.cfg.nightwatch_key:
            e.status_note = "未设置 NIGHTWATCH_API_KEY；请在终端启动并输入"
            return
        if self.day != e.day:
            self.day = e.day
            self.chain_cursor, self.chain_rows = None, []
            self.flow_mark = max(e.started_at, session["open"].timestamp())
            self.reset_flow_work()
            self.flow_newest_seen = None
            self.due.clear()
        if self.blocked:
            self.sync_info()
            return
        if self.stream_repair:
            self.reset_flow_work()
            self.due["flow"] = 0
            self.stream_repair = False
        pending = {"discover": 3600} if not self.ready else {"gex": 10, "quotes": 10, "chain": 60, "flow": FLOW_POLL_INTERVAL}
        while pending:
            # Re-evaluate deadlines after every HTTP call. Do not start another
            # paginated call when a GEX check is already due.
            wall = time.time()
            current = e.session(wall)
            if not current["open"] or not current["open"].timestamp() <= wall < current["close"].timestamp():
                break
            if self.ready and "gex" not in pending and self.due.get("gex") and wall >= self.due["gex"]:
                break  # GEX has had its turn and is due again; yield to the next pass.
            job = next((name for name in pending if wall >= self.due.get(name, 0)
                        and (name != "quotes" or e.focus)), None)
            if job is None:
                break
            interval = pending.pop(job)
            if job == "flow" and self.stream_covers(wall):
                continue
            try:
                await self.run_job(job, wall)
                e.provider_errors.pop(job, None)
                more = (job == "chain" and self.chain_cursor) or (job == "flow" and self.flow_sweep)
                finished = time.time()
                if job == "flow" and not self.stream_covers(finished) and self.stream_since is not None:
                    # A request already in flight at subscription time can
                    # finish after the ACK. Its old head doesn't bridge the gap.
                    e.flow_gap = True
                if job == "flow" and not more and e.flow_covered_at is not None:
                    # If the traversal took a while, there may already be new
                    # pages. Schedule from the head's query, not the last page.
                    self.due[job] = max(finished, e.flow_covered_at + interval)
                else:
                    self.due[job] = finished + (0 if more else interval)
            except ProviderError as error:
                names = {"discover": "权限", "gex": "Live GEX", "chain": "期权链", "quotes": "最新报价", "flow": "成交"}
                cursor_mismatch = (job == "chain" and self.chain_cursor and error.code == "VALIDATION_ERROR"
                                   and "cursor" in error.detail.casefold() and "restart pagination" in error.detail.casefold())
                if cursor_mismatch:
                    self.chain_cursor, self.chain_rows = None, []
                    e.provider_errors[job] = "期权链分页已过期；稍后从第一页自动重查"
                    self.due[job] = time.time()+max(5, error.retry)
                else:
                    e.provider_errors[job] = f"{names[job]}：{error.code}" + (f" · {error.detail}" if error.detail else "")
                    self.due[job] = time.time()+error.retry
                if job == "flow":
                    e.flow_gap = True
                if error.code in {"LOCAL_BUDGET", "QUOTA_RESERVE", "QUOTA_EXHAUSTED", "UNAUTHORIZED",
                                  "INVALID_API_KEY", "AUTH_INVALID", "HTTP_401", "SUBSCRIPTION_REQUIRED"}:
                    self.blocked = True
                    e.connected = False
                    e.status_note = f"已停止取行情：{error.code}"
                    break
            except (KeyError, ValueError, TypeError, IndexError, AttributeError):
                if job == "flow":
                    e.flow_gap = True
                e.provider_errors[job] = f"{job}：响应字段未通过校验，模块待命"
                self.due[job] = time.time()+30
            self.sync_info()

    async def run_job(self, job, now):
        e, c = self.engine, self.client
        if job == "discover":
            await c.get("/v1/discover")
            needed = ("options.quotes", "options.chain_snapshot", "flow.trades")
            if any(c.capabilities.get(k, {}).get("available") is not True for k in needed):
                raise ProviderError("REQUIRED_CAPABILITY_MISSING", 300)
            if c.capabilities.get("derived.dealer_gex_live", {}).get("available") is not True:
                raise ProviderError("LIVE_GEX_CAPABILITY_REQUIRED", 300)
            self.ready = True
            e.connected = True
            e.status_note = "权限已确认，开始手动试跑"
        elif job == "gex":
            r = await c.get(GEX_PATH, command="derived.dealer_gex_live")
            e.gex_checked_at, e.gex_http_status = time.time(), r.status
            if r.status == 200:
                e.apply_gex(r.data, r.meta, e.gex_checked_at)
        elif job == "chain":
            params = {"expiration": e.day, "limit": 200}
            if self.chain_cursor:
                params["cursor"] = self.chain_cursor
            r = await c.get(CHAIN_PATH, params, "options.chain_snapshot")
            data = r.data
            if not isinstance(data, dict) or data.get("expiration") != e.day or not isinstance(data.get("contracts"), list):
                raise ProviderError("INVALID_CHAIN", 30)
            if not self.chain_cursor:
                self.chain_rows = []
            self.chain_rows.extend(data["contracts"])
            self.chain_cursor = r.meta.get("next_cursor")
            e.apply_chain(self.chain_rows, time.time())
        elif job == "quotes":
            if not e.focus:
                return
            params = {"contracts": ",".join(symbol(k, e.day) for k in sorted(e.focus))}
            r = await c.get("/v1/options/quotes", params, "options.quotes")
            e.apply_quotes(r.data, time.time())
        elif job == "flow":
            await self.flow_page(now)

    async def flow_page(self, now):
        e = self.engine
        pending = self.flow_backlog
        # The oldest missing orders cannot affect today's rolling direction once
        # they are outside its window. Keep all cursor ranges touching the window,
        # including a boundary tie; do not move the verified head on a discard.
        floor = now - FLOW_LOOKBACK
        while pending and pending[0].high < floor:
            pending.popleft()
        if pending:
            oldest = pending[0]
            oldest.lower = max(oldest.lower, floor)
            # An already read row strictly before the new boundary proves this
            # part of the window, even if an older cursor is still available.
            if oldest.last_time is not None and oldest.last_time < oldest.lower:
                self.commit_flow(pending.popleft())
        # Guarantee at least one successful continuation between live probes.
        # At capacity keep the active cursor instead of starting that interval over.
        # Once a REST head overlaps an acknowledged live stream, finish that
        # finite interval. New orders come from the socket, not more REST heads.
        bridging = (self.stream_since is not None and self.flow_head_requested is not None
                    and self.flow_head_requested >= self.stream_since and not self.flow_head_failed)
        probe = bool(not bridging and pending and len(pending) < FLOW_MAX_PENDING_RANGES
                     and self.flow_can_probe and self.flow_head_requested is not None
                     and now - self.flow_head_requested >= FLOW_HEAD_REFRESH)
        head = not pending or probe
        if head:
            sweep = FlowSweep(now, pending[-1].high if pending else max(self.flow_mark, floor))
            self.flow_head_requested = now
            self.flow_can_probe = False
            self.flow_head_failed = True  # cleared only after accepting a valid first page
        else:
            sweep = self.flow_sweep
        day = e.day
        params = {"tickers": "SPXW", "session_date": day, "expirations": day, "kind": "order", "limit": FLOW_PAGE_SIZE,
                  "sort_by": "occurredAt", "direction": "desc"}
        if not head:
            params["cursor"] = sweep.cursor
        # Exact same filter, sort, and ET day for every cursor. Still just one
        # vendor request per turn, after any due quotes/GEX and under the shared budget.
        try:
            r = await self.client.get(FLOW_PATH, params, "flow.trades")
        except ProviderError as error:
            e.flow_gap = True
            if not head and error.code == "VALIDATION_ERROR" and "cursor" in error.detail.casefold():
                self.reset_flow_work()
                raise ProviderError(error.code, error.retry, "成交游标已过期；从第一页重新追补") from None
            raise
        received = time.time()
        if e.day != day:
            return
        rows = r.data
        if not isinstance(rows, list) or not isinstance(r.meta, dict):
            self.reset_flow_work()
            raise ProviderError("INVALID_FLOW", 15)
        times = [stamp(row.get("occurred_at")) if isinstance(row, dict) else None for row in rows]
        if any(t is None or t > received+FUTURE_TOLERANCE for t in times) or times != sorted(times, reverse=True):
            self.reset_flow_work()
            raise ProviderError("INVALID_FLOW_TIMESTAMPS", 15)
        if head and self.flow_newest_seen is not None and (not times or times[0] < self.flow_newest_seen):
            # An empty/older first page cannot verify newer activity already observed today.
            self.reset_flow_work()
            raise ProviderError("FLOW_HEAD_BEHIND", 5, "成交首页落后于已见订单，等待重新核对")
        if head and times:
            self.flow_newest_seen = times[0]
        cursor = r.meta.get("next_cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            self.reset_flow_work()
            raise ProviderError("INVALID_FLOW_CURSOR", 15)
        if r.meta.get("truncated", False) is not False:
            # Source truncation is a coverage hole even if it supplies a cursor.
            e.apply_flow(list(reversed(rows)), received, include_backfill=True, count_poll=head)
            self.reset_flow_work()
            raise ProviderError("FLOW_SOURCE_TRUNCATED", 10, "成交源标记数据不全；等待完整分页")
        if "next_cursor" not in r.meta and len(rows) >= FLOW_PAGE_SIZE and times[-1] >= sweep.lower:
            self.reset_flow_work()
            raise ProviderError("INVALID_FLOW_CURSOR", 10, "满页缺少分页标记，等待重新核对")
        try:
            complete = sweep.visit(times, cursor)
        except ProviderError:
            self.reset_flow_work()
            raise
        e.apply_flow(list(reversed(rows)), received, include_backfill=True, count_poll=head)
        if head:
            self.flow_head_failed = False
        else:
            self.flow_can_probe = True

        if complete:
            if not head:
                pending.pop()
            if pending:
                # The completed newer interval crosses the previous range's
                # upper boundary. Stitch it onto that range, but its older gap
                # and cursor still have to be resolved before declaring coverage.
                pending[-1].high, pending[-1].head_at = sweep.high, sweep.head_at
            else:
                self.commit_flow(sweep)
        elif head:
            pending.append(sweep)
        e.flow_pending_pages = sum(segment.pages for segment in pending)
        e.flow_pending_ranges = len(pending)
        e.flow_gap = bool(pending) or self.flow_head_failed


async def poll_index_spot(http, engine):
    try:
        response = await http.get(INDEX_PATH, params={"ticker": "I:SPX"})
        if response.is_success:
            engine.apply_index_spot(response.json(), time.time())
            return INDEX_INTERVAL if engine.index_samples else 15
        engine.index_note = ("独立 SPX 权限未确认" if response.status_code in (401, 403) else
                             f"独立 SPX 请求未就绪（HTTP {response.status_code}）")
        delay = 60 if response.status_code in (401, 403, 429) else 15
        if response.status_code == 429:
            delay = min(3600, max(delay, number(response.headers.get("Retry-After")) or 0))
    except (httpx.HTTPError, ValueError):
        engine.index_note, delay = "独立 SPX 请求暂未返回有效数据", 15
    engine.index_samples.clear()
    return delay


def create_nightwatch_app(cfg, client=None, engine=None, *, run_log=TAIL_LOG_PATH):
    e = engine or NightwatchEngine(cfg)
    client = client or Client(cfg.nightwatch_key, cfg.nightwatch_max_units)
    service = Service(cfg, e, client, run_log=run_log)

    async def index_worker():
        async with httpx.AsyncClient(base_url=INDEX_ROOT, headers={"Authorization": "Bearer " + cfg.massive_spot_key},
                                     timeout=5, follow_redirects=False) as http:
            while True:
                now = time.time()
                at = datetime.fromtimestamp(now, NY)
                session = e.session(now)
                inside = (at.hour == 15 and at.minute >= 30 and session["verified"] and session["open"]
                          and session["open"].timestamp() <= now < session["close"].timestamp())
                await asyncio.sleep(await poll_index_spot(http, e) if inside else INDEX_INTERVAL)

    async def worker():
        while True:
            try:
                await service.step()
                frame = e.snapshot(time.time())
                e.tail_journal.observe(frame)
                service.record_health(frame)  # No provider request or notification.
                e.provider_errors.pop("worker", None)
            except Exception as error:
                # Never stringify exceptions containing a request/configuration with credentials.
                e.provider_errors["worker"] = "计算循环暂停：" + type(error).__name__
                e.connected = False
            # A queued page gets another turn promptly. The Client retains
            # its shared pacing, backoff and quota caps for every HTTP call.
            more = service.flow_sweep and not service.blocked and time.time() >= service.due.get("flow", 0)
            await asyncio.sleep(.15 if more else 1)

    @asynccontextmanager
    async def lifespan(app):
        service.log_lifecycle("started")
        tasks = [asyncio.create_task(worker())]
        if cfg.massive_spot_key:
            tasks.append(asyncio.create_task(index_worker()))
        if cfg.nightwatch_flow_stream:
            tasks.append(asyncio.create_task(service.stream_worker()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            service.log_lifecycle("stopped")
            await client.close()

    app = FastAPI(title="SPX Tail Observer · Nightwatch manual trial", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.state.engine, app.state.service, app.state.provider = e, service, client

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent/"static"/"index.html")

    @app.get("/api/snapshot")
    async def snapshot(window: int = Query(300, ge=0, le=900), sort: str = "premium"):
        return e.snapshot(time.time(), window if window in {0, 60, 300, 900} else 300, sort)

    @app.get("/api/debug")
    async def debug():
        return {"source": "nightwatch", "version": VERSION, "demo": False, "series": list(e.debug_history),
                "gex_snapshots": list(e.gex_history), "api": e.api_info, "tail_health": list(service.health_trace)}

    @app.get("/health")
    async def health():
        return {"service": "up", "version": VERSION, "mode": "nightwatch", "read_only": True,
                "broker_connected": False, "api_authenticated": service.ready,
                "connected": e.connected, "manual_start": True, "flow_transport": e.flow_transport,
                "flow_stream_note": e.flow_stream_note}

    return app
