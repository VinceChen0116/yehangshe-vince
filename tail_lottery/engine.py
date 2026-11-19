"""调度与快照：按时间表轮询夜航社 → 更新内存状态 → 各模块独立计算（互不拖累）→ 生成页面快照 / 推 Discord。

时间表（美东，交易日）：
  14:58 起开始拉数据（15:00、15:10 的 GEX 帧给钉住蝴蝶用；strike_bias 在 15:30 需要之前 600 秒的成交）
  链快照（行权价 / 合约代码 / OI）每 10 分钟一次（1 配额）——盘中落后约 13 分钟，所以不用它的报价和 IV
  实时报价：每 30 秒拉离现货最近 3 个行权价的 Call/Put（6 配额）算现价；
            15:25 起（以及 15:00、15:10 两帧）每分钟拉现货上下各 10 档（40 配额）算 GEX / Skew / Charm
  1 分钟成交桶每分钟一次（summary 1 + top_contracts 1–3）；大单每分钟一次（通常 1）
  16:01 停止拉取，推一条收盘总结
  预计每天约 2,000 次配额。剩余配额 < QUOTA_FLOOR 时停止拉取。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import traceback

from config import et_datetime, hhmm_to_minutes, is_trading_day, minutes_of_day, session_close_hhmm, to_et

from . import modules as M
from . import playbook as P
from .source import SourceError
from .store import Store

log = logging.getLogger("tail.engine")

START_HHMM = "14:58"
BASE_CHAIN_POLL_S = 600         # 链快照只取行权价 / OI，10 分钟一次足够
QUOTE_POLL_S = 30
SPOT_STRIKES = 3                # 现价轮：离现货最近 3 个行权价（6 个报价）
LIVE_STRIKES_EACH_SIDE = 10     # 完整轮：现货上下各 10 档（40 个报价）
FULL_FROM = "15:25"             # 完整轮开始时间（之前只在 15:00、15:10 各做一次，给钉住蝴蝶）
FULL_FRAMES = ("15:00", "15:10")
FLOW_POLL_S = 60
FLOW_POLL_OFFSET_S = 8          # 每分钟第 8 秒拉上一分钟的桶
BUCKET_GIVE_UP_MIN = 20         # 一分钟的桶 20 分钟后仍没有才跳过（11-19 实测 0DTE 桶晚到约 8 分钟，4 分钟会误判缺失）
BACKFILL_MIN = 15               # 启动时最多回补 15 分钟成交桶
FLOW_STALE_MIN = 5              # 最新成交桶比现在早 5 分钟以上 → 成交数据延迟
QUOTA_FLOOR = 3000              # 与 tape_live 一致：剩余配额低于此值停止拉取
BACKOFF = (5, 10, 20, 40, 60)   # 失败后的重试间隔（秒）
PUSH_CONFIRM_ROUNDS = 2         # 倾向连续 2 次计算一致才推送，避免来回跳


def et_fmt(t: dt.datetime) -> str:
    return to_et(t).strftime("%H:%M:%S")


class Engine:
    def __init__(self, source, notifier=None, clock=None, push_enabled=True):
        self.source = source
        self.n = notifier
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.push_enabled = push_enabled
        self.store = Store()
        self.lock = threading.Lock()
        self.snapshot: dict = {}
        self.errors: dict = {}
        self.last_chain_ok: dt.datetime | None = None    # 最近一次成功拿到实时报价
        self.last_quote_at: dt.datetime | None = None    # 最近一轮报价里最新的 quoted_at
        self.next_chain: dt.datetime | None = None       # 下次拉实时报价
        self.base_chain = None                            # 链快照（行权价 / OI）
        self.next_base: dt.datetime | None = None
        self.last_full_min: dt.datetime | None = None
        self.next_flow: dt.datetime | None = None
        self.next_bucket: dt.datetime | None = None
        self.fail_n = 0
        self.conn = "未连接"
        self.missing_buckets = 0
        self.stopped_reason = ""
        self.pin = None
        self.pin_checked = False
        self.last_verdict_pushed = None
        self.pending_verdict = (None, 0)
        self.closing_sent = False
        self.degrade_sent = set()
        self.playbook: dict = {}
        self.quota_start = None
        self.version = 0            # 每次有新数据（期权链 / 成交桶）+1；推送确认按数据轮次计，不按秒
        self.pushed_version = -1

    # ------------------------------------------------------------------ 时间
    def _session(self, now):
        et = to_et(now)
        d = et.date()
        if not is_trading_day(d):
            return d, None, "休市（非交易日）"
        close_hhmm = session_close_hhmm(d)
        if close_hhmm != "16:00":
            return d, None, f"休市（提前收盘日 {close_hhmm}，尾盘窗口不适用）"
        return d, et_datetime(d, "16:00").astimezone(dt.timezone.utc), ""

    # ------------------------------------------------------------------ 主循环
    def step(self) -> None:
        """每秒调用一次。"""
        now = self.clock()
        day, close, closed_reason = self._session(now)
        if self.store.session != day:
            self.store.reset(day)
            self._new_day()
        now_min = minutes_of_day(to_et(now))
        active = not closed_reason and hhmm_to_minutes(START_HHMM) <= now_min <= 16 * 60 and not self.stopped_reason
        if active:
            self._poll(now, day, close)
        self._compute(now, day, close, closed_reason, active)
        if not closed_reason and now_min >= 16 * 60 + 1 and not self.closing_sent:
            self.closing_sent = True
            self._push_close()

    def _new_day(self):
        self.next_chain = self.next_flow = self.next_bucket = self.next_base = None
        self.base_chain = None
        self.last_full_min = None
        self.missing_buckets = 0
        self.pin, self.pin_checked = None, False
        self.last_verdict_pushed, self.pending_verdict = None, (None, 0)
        self.closing_sent = False
        self.degrade_sent = set()
        self.stopped_reason = ""

    def _fail(self, now, what, e):
        delay = BACKOFF[min(self.fail_n, len(BACKOFF) - 1)]
        self.fail_n += 1
        self.conn = f"断开（{what}失败：{e}；{delay} 秒后重试）"
        log.warning("%s 失败：%s（%d 秒后重试）", what, e, delay)
        return now + dt.timedelta(seconds=delay)

    def _poll(self, now, day, close):
        from yhs_client import QuotaExhausted
        try:
            q = self.source.quota_remaining
            if self.quota_start is None and q is not None:
                self.quota_start = q
            if q is not None and q < QUOTA_FLOOR:
                self.stopped_reason = f"配额剩余 {q} < {QUOTA_FLOOR}，停止拉取"
                self._push_text(self.stopped_reason, key="quota")
                return
            if self.next_base is None or now >= self.next_base:
                try:
                    self.base_chain = self.source.fetch_chain(day, now)
                    self.next_base = now + dt.timedelta(seconds=BASE_CHAIN_POLL_S)
                except SourceError as e:
                    self.next_base = self._fail(now, "链快照", e)
            if self.base_chain is not None and (self.next_chain is None or now >= self.next_chain):
                try:
                    self._poll_quotes(now, close)
                    self.fail_n, self.conn = 0, "已连接"
                    self.next_chain = now + dt.timedelta(seconds=QUOTE_POLL_S)
                except SourceError as e:
                    self.next_chain = self._fail(now, "实时报价", e)
            if self.next_flow is None or now >= self.next_flow:
                try:
                    self._poll_flow(now, day)
                    m0 = now.replace(second=0, microsecond=0)
                    self.next_flow = m0 + dt.timedelta(seconds=FLOW_POLL_S + FLOW_POLL_OFFSET_S)
                except SourceError as e:
                    self.next_flow = self._fail(now, "成交", e)
        except QuotaExhausted as e:
            self.stopped_reason = str(e)
            self._push_text(f"配额熔断：{e}", key="quota")

    def _full_due(self, now):
        et = to_et(now)
        m = now.replace(second=0, microsecond=0)
        if self.last_full_min == m:
            return False
        if self.store.chain is None or minutes_of_day(et) >= hhmm_to_minutes(FULL_FROM):
            return True
        return et.strftime("%H:%M") in FULL_FRAMES

    def _poll_quotes(self, now, close):
        base = self.base_chain
        T = M.years_to_close(now, close)
        hint = self.store.spot or base.underlying_price
        if hint is None:
            hint = M.parity_spot(base, T)[0]
        if hint is None:
            raise SourceError("链快照里没有可用的行权价")
        if self._full_due(now):
            syms = M.window_symbols(base, hint, LIVE_STRIKES_EACH_SIDE)
            q = self.source.fetch_quotes(syms, now)
            self._note_quote_time(q)
            snap, spot, _ = M.live_snapshot(base, q, now, hint, T, LIVE_STRIKES_EACH_SIDE)
            self.last_full_min = now.replace(second=0, microsecond=0)
            self.last_chain_ok = now
            if spot is not None:
                self.store.add_spot(now, spot, estimated=True)
            self._on_chain(now, close, snap)
            return
        # 现价轮：只拉离现货最近的几个行权价，更新现价和现用期权链里这几个合约的报价
        syms = [c.symbol for c in M.nearest_contracts(base.contracts, hint, SPOT_STRIKES)]
        q = self.source.fetch_quotes(syms, now)
        self._note_quote_time(q)
        snap, spot, _ = M.live_snapshot(base, q, now, hint, T, nearest=SPOT_STRIKES)
        self.last_chain_ok = now
        if spot is not None:
            self.store.add_spot(now, spot, estimated=True)
        cur = self.store.chain
        if cur is not None:
            fresh = {c.symbol: c for c in snap.contracts}
            for c in cur.contracts:
                f = fresh.get(c.symbol)
                if f is not None:
                    c.bid, c.ask, c.quoted_at = f.bid, f.ask, f.quoted_at

    def _note_quote_time(self, q):
        ts = [v[2] for v in q.values() if v and v[2] is not None]
        if ts:
            self.last_quote_at = max(ts)

    def _on_chain(self, now, close, snap):
        self.store.chain = snap
        self.version += 1
        T = M.years_to_close(now, close)
        try:
            g = M.run_gex(snap, self.store.spot) if self.store.spot else {"available": False, "reason": "没有现价"}
            self.errors.pop("gex", None)
        except Exception as e:   # 模块异常只影响自己
            g = {"available": False, "error": True, "reason": f"计算失败：{e}"}
            self.errors["gex"] = traceback.format_exc(limit=3)
        self.store.add_gex(now, g)
        self.gex = g
        skew = charm = None
        if self.store.spot:
            try:
                skew = M.run_skew(snap, self.store.spot, T, now)
            except Exception as e:
                skew = {"available": False, "error": True, "reason": f"计算失败：{e}"}
                self.errors["skew"] = traceback.format_exc(limit=3)
            try:
                charm = M.run_charm(snap, self.store.spot, now, close, to_et(now))
            except Exception as e:
                charm = {"available": False, "error": True, "reason": f"计算失败：{e}"}
                self.errors["charm"] = traceback.format_exc(limit=3)
        self.store.add_debug(now, skew, charm)

    def _poll_flow(self, now, day):
        m_now = now.replace(second=0, microsecond=0)
        if self.next_bucket is None:
            start = et_datetime(day, START_HHMM).astimezone(dt.timezone.utc) - dt.timedelta(minutes=BACKFILL_MIN - 2)
            self.next_bucket = max(start, m_now - dt.timedelta(minutes=BACKFILL_MIN))
        while self.next_bucket < m_now:   # 只拉已结束的分钟
            b = self.source.fetch_bucket(day, self.next_bucket, day)
            if not b.present:
                if (m_now - self.next_bucket).total_seconds() >= BUCKET_GIVE_UP_MIN * 60:
                    self.missing_buckets += 1
                    self.next_bucket += dt.timedelta(minutes=1)
                    continue
                break   # 还没生成，下一轮再来
            self.store.add_bucket(b)
            self.version += 1
            self.next_bucket += dt.timedelta(minutes=1)
        new = self.source.fetch_big_prints(day, day, self.store.seen_prints)
        self.store.add_big_prints(new, now)

    # ------------------------------------------------------------------ 计算与快照
    def _blockers(self, now):
        out = []
        if self.stopped_reason:
            out.append(self.stopped_reason)
        if self.base_chain is None:
            out.append("没有链快照（行权价 / OI）")
        if self.last_chain_ok is None or (now - self.last_chain_ok).total_seconds() > M.CHAIN_STALE_S:
            out.append("行情中断（实时报价超过 120 秒没有成功拉取）")
        qa = self.last_quote_at
        if qa is not None and (now - qa).total_seconds() > M.CHAIN_STALE_S:
            out.append(f"实时报价过期（{to_et(qa).strftime('%H:%M:%S')}）")
        if self.store.spot is None:
            out.append("无法估算现货")
        last_b = next(reversed(self.store.buckets)) if self.store.buckets else None
        if last_b is None or (now - last_b).total_seconds() > FLOW_STALE_MIN * 60 + 60:
            out.append("成交数据延迟（最新 1 分钟桶超过 5 分钟前）")
        return out

    def _flow_rows(self, since):
        rows = []
        from .direction import split_bucket_row
        for b in self.store.buckets_since(since):
            for r in b.rows:
                buy, sell, unk, _, _ = split_bucket_row(r, b.known_ratio(r.right))
                rows.append({"strike": r.strike, "right": r.right, "contracts": r.contracts, "buy": buy, "sell": sell})
        return rows

    def _compute(self, now, day, close, closed_reason, active):
        st = self.store
        now_et = to_et(now)
        blockers = [] if closed_reason else self._blockers(now)
        snap = {"generated_at": et_fmt(now), "session": day.isoformat(), "closed": closed_reason,
                "status": self._status(now, close, closed_reason, blockers, active), "modules": {}}
        # Feed
        try:
            M.run_feed(st, now, et_fmt)
            snap["modules"]["feed"] = {"signals": [self._sig_json(s) for s in reversed(st.signals)]}
        except Exception as e:
            snap["modules"]["feed"] = {"error": f"计算失败：{e}"}
            log.exception("Feed 计算失败")
        # Strike Leader
        try:
            snap["modules"]["leader"] = {"windows": M.run_leader(st, now)}
        except Exception as e:
            snap["modules"]["leader"] = {"error": f"计算失败：{e}"}
            log.exception("Strike Leader 计算失败")
        g = getattr(self, "gex", None) or {"available": False, "reason": "尚无数据"}
        snap["modules"]["gex"] = g
        # 受影响模块变灰
        stale = [b for b in blockers if "行情中断" in b or "过期" in b or "现货" in b]
        flow_stale = [b for b in blockers if "成交数据" in b]
        snap["modules"]["feed"]["stale"] = "；".join(flow_stale + stale)
        snap["modules"]["leader"]["stale"] = "；".join(flow_stale)
        snap["modules"]["gex"] = dict(g, stale="；".join(stale))
        # Playbook
        try:
            win_start = et_datetime(day, "15:30").astimezone(dt.timezone.utc)
            day_vol = {k: d.contracts for k, d in st.day.items()}
            mids = {(c.strike, c.right): c.mid for c in (st.chain.contracts if st.chain else [])}
            inp = {"now": now, "now_min": minutes_of_day(now_et), "window_start": win_start, "spot": st.spot,
                   "spots": list(st.spot_series), "flow_600": self._flow_rows(now - dt.timedelta(seconds=P.STRIKE_LOOKBACK_S + 60)),
                   "flow_300": self._flow_rows(now - dt.timedelta(seconds=300 + 60)), "gex": g, "skew": st.skew,
                   "charm": st.charm, "day_volume": day_vol, "mids": mids,
                   "blockers": blockers if not closed_reason else [closed_reason]}
            pb = P.evaluate(inp)
        except Exception as e:
            pb = {"verdict": P.STAND_BY, "label": "计算失败", "reason": str(e), "legs": [], "disclaimer": P.DISCLAIMER}
            log.exception("Playbook 计算失败")
        self.playbook = pb
        snap["modules"]["playbook"] = pb
        self._run_log(now, pb, snap["status"], active)
        # 钉住蝴蝶（15:10 判定一次）
        if not closed_reason and not self.pin_checked and minutes_of_day(now_et) >= 15 * 60 + 10 and active:
            self.pin_checked = True
            self._check_pin(day)
        snap["modules"]["pin"] = self.pin
        if active and self.version != self.pushed_version:
            self.pushed_version = self.version
            self._maybe_push(pb, now)
        with self.lock:
            self.snapshot = snap

    def _run_log(self, now, pb, status, active):
        """运行日志（不记逐笔行情）：倾向变化 + 每 5 分钟一行健康状况，供收盘后写运行报告。"""
        if not active:
            return
        key = (pb.get("verdict"), pb.get("call_score"), pb.get("put_score"), pb.get("reason"))
        if key != getattr(self, "_last_log_key", None):
            self._last_log_key = key
            legs = "；".join(f"{l['name']}={'弃权' if l.get('abstain') else (l.get('side') or '-') + str(l.get('score'))}"
                            for l in pb.get("legs", []))
            log.info("【倾向】%s CALL %s / PUT %s%s %s | %s", pb.get("label"), pb.get("call_score"), pb.get("put_score"),
                     " 证据分歧" if pb.get("conflict") else "", pb.get("reason") or "", legs)
        m = now.replace(second=0, microsecond=0)
        if to_et(now).minute % 5 == 0 and getattr(self, "_last_health", None) != m:
            self._last_health = m
            qa = self.last_quote_at
            lag = None if qa is None else int((now - qa).total_seconds())
            last_b = next(reversed(self.store.buckets)) if self.store.buckets else None
            blag = None if last_b is None else int((now - last_b).total_seconds())
            log.info("【健康】现价 %s 报价延迟 %ss 成交桶延迟 %ss 现用合约 %s 方向已知 %s 信号 %d 配额剩 %s 今日用 %s 降级 %s",
                     status.get("spot"), lag, blag, status.get("contracts"), status.get("direction_known_share"),
                     len(self.store.signals), status.get("quota_remaining"), status.get("quota_used_today"),
                     "；".join(status.get("degraded") or []) or "无")

    def _check_pin(self, day):
        t0 = et_datetime(day, "15:00").astimezone(dt.timezone.utc)
        t1 = et_datetime(day, "15:10").astimezone(dt.timezone.utc)
        g0, g1 = self.store.gex_at(t0), self.store.gex_at(t1)
        spots = self.store.spots_between(t0 - dt.timedelta(seconds=45), t1 + dt.timedelta(seconds=45))
        s0 = min(spots, key=lambda x: abs((x[0] - t0).total_seconds()))[1] if spots else None
        quotes = {(c.strike, c.right): c for c in (self.store.chain.contracts if self.store.chain else [])}
        try:
            res, why = P.pin_butterfly(g0, g1, s0, spots, quotes)
        except Exception as e:
            res, why = None, f"计算失败：{e}"
        log.info("钉住蝴蝶判定：%s", res or why)
        if res:
            self.pin = res
            self._push_text(f"🦋 钉住蝴蝶提示：{res['structure']}，估算净支出 ${res['est_debit']:.2f}"
                            f"（两墙中点 {res['mid_point']:g}）· {P.DISCLAIMER}", key="pin")

    def _status(self, now, close, closed_reason, blockers, active):
        st = self.store
        cap = self.source.capabilities
        share = st.direction_known_share()
        q = self.source.quota_remaining
        degrade = list(blockers)
        if self.base_chain is not None and self.base_chain.notice and "older than" not in self.base_chain.notice:
            degrade.append("服务端提示：" + self.base_chain.notice[:80])
        for k in ("skew", "charm"):
            v = getattr(st, k)
            if active and v is not None and not v.get("available"):
                degrade.append(f"{k.capitalize()} 不可用：{v.get('reason', '')}")
        if self.base_chain is not None and self.base_chain.contracts and all(c.oi is None for c in self.base_chain.contracts):
            degrade.append("OI 未到齐")
        if self.missing_buckets:
            degrade.append(f"缺失 1 分钟成交桶 {self.missing_buckets} 个")
        for name, tb in self.errors.items():
            degrade.append(f"{name} 计算失败")
        return {
            "broker": cap["name"], "connection": "休市" if closed_reason else (self.conn if active else "待命（未到拉取时间）"),
            "realtime": "轮询（实时报价 30 秒 / 1 分钟、成交 1 分钟），非逐笔推送",
            "contracts": len(st.chain.contracts) if st.chain else 0,
            "contracts_limit": f"现货上下各 {LIVE_STRIKES_EACH_SIDE} 档（按配额计费）",
            "spot": None if st.spot is None else round(st.spot, 2),
            "spot_at": et_fmt(st.spot_at) if st.spot_at else None,
            "spot_note": "估算现货（期权平价反推）",
            "vix": None,
            "countdown_s": None if close is None else max(0, int((close - now).total_seconds())),
            "direction_known_share": None if share is None else round(share, 3),
            "direction_source": "数据商给出（夜航社 at_ask / at_bid）；单合约按同侧已知占比估算",
            "quota_remaining": q, "quota_used_today": (self.quota_start - q) if (self.quota_start and q is not None) else None,
            "queue_dropped": 0,
            "degraded": degrade,
            "oi_as_of": to_et(self.base_chain.oi_as_of).strftime("%m-%d %H:%M") if self.base_chain and self.base_chain.oi_as_of else None,
        }

    @staticmethod
    def _sig_json(s):
        d = dict(s)
        d.pop("t", None)
        d.pop("last_t", None)
        return d

    # ------------------------------------------------------------------ Discord
    def _push_text(self, msg, key=None):
        if key and key in self.degrade_sent:
            return
        if key:
            self.degrade_sent.add(key)
        if self.n and self.push_enabled:
            self.n.send(f"🎰【尾盘彩票】{msg}")

    def _maybe_push(self, pb, now):
        v = pb.get("verdict")
        if v in (None, P.STAND_BY) and self.last_verdict_pushed is None:
            return
        pv, cnt = self.pending_verdict
        cnt = cnt + 1 if pv == v else 1
        self.pending_verdict = (v, cnt)
        if cnt < PUSH_CONFIRM_ROUNDS or v == self.last_verdict_pushed:
            return
        self.last_verdict_pushed = v
        if self.n and self.push_enabled:
            self.n.send(embeds=[playbook_embed(pb, self.store, now)])

    def _push_close(self):
        pb = self.playbook or {}
        q = self.source.quota_remaining
        used = (self.quota_start - q) if (self.quota_start and q is not None) else None
        share = self.store.direction_known_share()
        self._push_text(f"收盘。最后倾向：{pb.get('label', '—')}（CALL {pb.get('call_score', 0):g} / PUT {pb.get('put_score', 0):g}）；"
                        f"今日 Feed 信号 {len(self.store.signals)} 条；方向已知占比 "
                        f"{'—' if share is None else f'{share:.0%}'}；今日用配额 {used if used is not None else '—'}，剩余 {q} · {P.DISCLAIMER}",
                        key="close")


COLORS = {P.CALL: 0x2ECC71, P.PUT: 0xE74C3C, P.NEUTRAL: 0x95A5A6, P.STAND_BY: 0x7F8C8D}


def playbook_embed(pb, store, now):
    lines = []
    for l in pb.get("legs", []):
        if l.get("abstain"):
            lines.append(f"· {l['name']}：弃权（{l['abstain']}）")
        else:
            who = l["side"] or "不选边"
            lines.append(f"· {l['name']}：{who} {l['score']:g} 分 — {l['note']}")
    ref = pb.get("reference")
    ref_s = (f"参考合约：SPXW {ref['strike']:g}{ref['right']}（带内当日量 {ref['day_volume']:,} 张，现价 "
             f"{'—' if ref.get('mid') is None else '$%.2f' % ref['mid']}）") if ref else ""
    spot = "—" if store.spot is None else f"{store.spot:,.2f}（估算）"
    desc = (f"**{pb.get('label')}** · CALL {pb.get('call_score', 0):g} / PUT {pb.get('put_score', 0):g}"
            + (" · ⚠️ 证据分歧" if pb.get("conflict") else "") + f"\nSPX {spot} · {et_fmt(now)} ET\n"
            + ("\n".join(lines))[:3000] + ("\n" + ref_s if ref_s else ""))
    return {"title": f"🎰【尾盘彩票】{pb.get('label')} · {P.DISCLAIMER}", "color": COLORS.get(pb.get("verdict"), 0x7F8C8D),
            "description": desc}


def run_forever(engine, stop_event=None, exit_after_close=False):
    while not (stop_event and stop_event.is_set()):
        if exit_after_close and engine.closing_sent:
            log.info("收盘总结已推送，退出（--exit-after-close）")
            return
        try:
            engine.step()
        except Exception:
            log.exception("主循环异常（继续运行）")
        time.sleep(1.0)
