"""
tape_live.py —— 跟大单（dennis-tape 式）模拟实盘（2026-10-19 用户定，与日内 / 隔夜分开记账）
=================================================================================
每个交易日 09:25 启动，09:30–16:00 每分钟一轮：
  1. 大单：全市场 Ask 侧（A/AA）单笔 ≥$15 万，按时间倒序翻到上一轮看过的位置为止；
     候选 = 到期 2–75 天、价外 0–8%、当日量 > OI（tape_archive.is_candidate）
  2. 大单后 5 分钟（等下一根 Heatmap）拉该标的实时 Heatmap 快照（含全部到期，1 次配额），过滤 HW：
       该到期 |GEX| 前 4 大节点里的负节点 = 行权价，且该到期主导墙在交易方向一侧（Call 在上方 / Put 在下方）
       + 所有到期按行权价合计的主导墙是「负、在现价上方」（与 dennis 的「主导墙 −上」口径一致，25 笔对上 18 笔）
     同一合约已开过仓的不再开；没通过的合约，后面再来的大单会重新判断
  3. 开仓：按实时 ask 买，bid 为 0 / 没报价 / 买卖价差 > 中间价 25% 不开；每笔约 $1,800（至少 1 张，不设单张上限）；
     每天最多 5 笔新仓、同时最多 10 笔；15:50 以后不开
  4. 持仓（可跨日）：每分钟实时报价，按中间价判断、按 bid 卖：
       −22% 止损；涨到 +30% 后追踪，回撤到 max(+30%, 峰值 × 0.9) 卖；最多持有 8 个交易日、到期前一天 15:50 时间离场
  5. 配额：剩余 < QUOTA_FLOOR 时停止开新仓（持仓照常管理），并发 Discord
记账：live/tape_trades.csv（全部仓位，开仓写入、平仓补全）；live/tape_state_<日期>.json（已看过的大单，重启不重复）
     调用到的实时 Heatmap 快照存 yhs_data/archive/heatmap_live/<日期>/<标的>/<HHMM>.json（之后验证用，0 额外配额）
用法：python3 tape_live.py [--dry-run] [--log-file F]
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import logging
import os
import sys
import time

from archive import _write_json, archive_root
from config import SystemConfig, et_datetime, et_now, is_trading_day, next_trading_day, parse_iso, setup_logging, to_et
from detector import FLOW_TRADES_PATH, HEATMAP_PATH, extract_rows
from main import QuoteBook
from notify import GREEN, RED, BLUE, ORANGE, from_config
from tape_archive import MIN_PREMIUM, is_candidate, parse_occ
from yhs_client import QuotaExhausted, YHSApiError, YHSClient

log = logging.getLogger("yhs.tape_live")
LIVE_DIR = "live"
LEDGER = os.path.join(LIVE_DIR, "tape_trades.csv")
TAG = "🎯【跟单】"
LAG_MIN = 5
LAST_ENTRY = "15:50"
MAX_NEW_PER_DAY = 5
MAX_OPEN = 10
MAX_SPREAD = 0.25
BUDGET = 1800.0
STOP, TRAIL_ON, TRAIL = -0.22, 0.30, 0.10
MAX_HOLD = 8
QUOTA_FLOOR = 3000
SNAP_TTL_S = 240
FIELDS = ["position_id", "open_date", "ticker", "right", "contract", "expiration", "strike", "qty", "flow_time", "flow_usd",
          "flow_side", "spot", "strike_rank", "exp_wall", "agg_wall", "entry_time", "entry_bid", "entry_ask", "entry_price",
          "entry_mid", "peak_mid", "status", "exit_time", "exit_bid", "exit_price", "exit_reason", "pnl_usd", "roi_pct"]


def size(price: float) -> int:
    """每笔约 $1,800，至少 1 张，不设单张上限（2026-10-19 用户定）。"""
    per = price * 100
    return max(1, int(round(BUDGET / per))) if per > 0 else 0


def read_ledger() -> list:
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_ledger(rows: list) -> None:
    os.makedirs(LIVE_DIR, exist_ok=True)
    tmp = f"{LEDGER}.{os.getpid()}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, LEDGER)


def trading_days_held(open_day: dt.date, today: dt.date) -> int:
    n, d = 0, open_day
    while d < today:
        d = next_trading_day(d)
        n += 1
    return n


class Tape:
    def __init__(self, cfg, client, notifier, day: dt.date, dry: bool):
        self.cfg, self.client, self.day, self.dry = cfg, client, day, dry
        self.n = notifier.tagged(TAG) if notifier else None
        self.quotes = QuoteBook(client)
        self.rows = read_ledger()
        self.state_fp = os.path.join(LIVE_DIR, f"tape_state_{day}.json")
        st = {}
        if os.path.exists(self.state_fp):
            with open(self.state_fp, encoding="utf-8") as f:
                st = json.load(f)
        self.seen = set(st.get("seen") or [])
        self.newest = st.get("newest")
        self.pending = []                  # [(到期判断时刻, 大单)]
        self.snaps = {}                    # 标的 → (拉取时刻, payload)
        self.halted = False

    # ------------------------------------------------------------------ 状态
    def save(self):
        if self.dry:
            return
        write_ledger(self.rows)
        _write_json(self.state_fp, {"seen": sorted(self.seen), "newest": self.newest})

    def open_rows(self):
        return [r for r in self.rows if r["status"] == "OPEN"]

    def opened_today(self):
        return [r for r in self.rows if r["open_date"] == self.day.isoformat()]

    def send(self, title, color, fields=None, desc=""):
        if self.n and not self.dry:
            self.n.send(embeds=[{"title": self.n._t(title), "color": color, "description": desc, "fields": fields or []}])

    # ------------------------------------------------------------------ 大单
    def poll_flow(self):
        new, cursor, newest = [], None, None
        for _ in range(20):
            p = {"kind": "order", "min_premium_usd": MIN_PREMIUM, "sides": "A,AA", "session_date": self.day.isoformat(),
                 "sort_by": "occurredAt", "direction": "desc", "limit": 25, "cursor": cursor}
            payload = self.client.get(FLOW_TRADES_PATH, p)
            rows = extract_rows(payload)
            stop = False
            for x in rows:
                key = f'{x.get("occurred_at")}|{x.get("contract_symbol")}|{x.get("premium_usd")}'
                newest = newest or x.get("occurred_at")
                if self.newest and str(x.get("occurred_at")) < self.newest:
                    stop = True
                    break
                if key in self.seen:
                    continue
                self.seen.add(key)
                new.append(x)
            cursor = (payload.get("_meta") or {}).get("next_cursor") if isinstance(payload, dict) else None
            if stop or not cursor or not self.newest:
                break   # 第一轮只看最新一页（启动 / 重启时不追溯开盘以来的全部大单）
        if newest and (not self.newest or newest > self.newest):
            self.newest = newest
        traded = {r["contract"] for r in self.rows}
        for x in new:
            core = str(x.get("contract_symbol")).split(":")[-1]
            if not is_candidate(x) or core in traded:
                continue
            when = to_et(parse_iso(x["occurred_at"]))
            self.pending.append((when + dt.timedelta(minutes=LAG_MIN), x))
            log.info("【跟单·候选】%s %s $%.0fK（%s）→ %s 判断", when.strftime("%H:%M"), core,
                     float(x.get("premium_usd") or 0) / 1e3, x.get("side"), (when + dt.timedelta(minutes=LAG_MIN)).strftime("%H:%M"))

    # ------------------------------------------------------------------ Heatmap
    def snapshot(self, ticker):
        now = et_now()
        hit = self.snaps.get(ticker)
        if hit and (now - hit[0]).total_seconds() < SNAP_TTL_S:
            return hit[1]
        try:
            p = self.client.get(HEATMAP_PATH.format(ticker=ticker))
        except QuotaExhausted:
            raise
        except YHSApiError as e:
            log.info("【跟单】%s 没有 Heatmap：%s", ticker, e)
            p = None
        d = p.get("data") if isinstance(p, dict) else None
        self.snaps[ticker] = (now, d)
        if d and not self.dry:
            _write_json(os.path.join(archive_root(self.cfg), "heatmap_live", self.day.isoformat(), ticker,
                                     f"{now.strftime('%H%M')}.json"), d)
        return d

    def features(self, x):
        t, exp, right, K = parse_occ(x["contract_symbol"])
        d = self.snapshot(t)
        if not d or not d.get("spot_usd"):
            return None
        spot = float(d["spot_usd"])
        cells = [(c.get("expiration"), float(c["strike_usd"]), float(c["net_dealer_gex_usd"])) for c in d.get("cells") or []
                 if c.get("net_dealer_gex_usd") is not None]
        ec = sorted([(k, g) for e, k, g in cells if e == exp], key=lambda c: -abs(c[1]))
        if not ec:
            return None
        strikes = [k for k, _ in ec]
        rank = strikes.index(K) + 1 if K in strikes else 99
        gk = dict(ec).get(K)
        dk, dg = ec[0]
        agg = collections.defaultdict(float)
        for _, k, g in cells:
            agg[k] += g
        ak, ag = max(agg.items(), key=lambda kv: abs(kv[1]))
        return {"ticker": t, "exp": exp, "right": right, "strike": K, "spot": spot, "rank": rank,
                "sign": None if gk is None else ("+" if gk > 0 else "-"),
                "aligned": (dk > spot) if right == "C" else (dk < spot),
                "exp_wall": f'{"+" if dg > 0 else "-"}{"上" if dk > spot else "下"}',
                "agg_wall": f'{"+" if ag > 0 else "-"}{"上" if ak > spot else "下"}'}

    # ------------------------------------------------------------------ 开仓
    def try_entries(self):
        now = et_now()
        due = [p for p in self.pending if p[0] <= now]
        self.pending = [p for p in self.pending if p[0] > now]
        for _, x in sorted(due, key=lambda p: p[1]["occurred_at"]):
            core = str(x["contract_symbol"]).split(":")[-1]
            if any(r["contract"] == core for r in self.rows):
                continue
            if now > et_datetime(self.day, LAST_ENTRY) or self.halted:
                continue
            if len(self.opened_today()) >= MAX_NEW_PER_DAY or len(self.open_rows()) >= MAX_OPEN:
                log.info("【跟单·跳过】%s 今日已开 %d 笔 / 持仓 %d 笔（上限 %d / %d）", core, len(self.opened_today()),
                         len(self.open_rows()), MAX_NEW_PER_DAY, MAX_OPEN)
                continue
            f = self.features(x)
            ok = f and f["rank"] <= 4 and f["sign"] == "-" and f["aligned"] and f["agg_wall"] == "-上"
            if not ok:
                log.info("【跟单·不符】%s %s", core, f and f"行权价排名 {f['rank']}{f['sign'] or ''} 该到期主导墙 {f['exp_wall']} "
                         f"{'同向' if f['aligned'] else '反向'} 合计主导墙 {f['agg_wall']}" or "无 Heatmap")
                continue
            q = self.quotes.get(core)
            if not q or q["ask"] <= 0 or q["bid"] <= 0 or q["ask"] < self.cfg.min_option_price:
                log.info("【跟单·放弃】%s 没有报价或 bid 为 0", core)
                continue
            mid = (q["ask"] + q["bid"]) / 2
            if (q["ask"] - q["bid"]) / mid > MAX_SPREAD:
                log.info("【跟单·放弃】%s 价差 %.0f%%（bid %.2f / ask %.2f）", core, (q["ask"] - q["bid"]) / mid * 100, q["bid"], q["ask"])
                continue
            qty = size(q["ask"])
            when = to_et(parse_iso(x["occurred_at"]))
            row = {"position_id": f"TP{self.day.strftime('%Y%m%d')}-{len(self.opened_today()) + 1:02d}",
                   "open_date": self.day.isoformat(), "ticker": f["ticker"], "right": f["right"], "contract": core,
                   "expiration": f["exp"], "strike": f["strike"], "qty": qty, "flow_time": when.strftime("%H:%M"),
                   "flow_usd": round(float(x.get("premium_usd") or 0)), "flow_side": x.get("side"), "spot": round(f["spot"], 2),
                   "strike_rank": f["rank"], "exp_wall": f["exp_wall"], "agg_wall": f["agg_wall"],
                   "entry_time": et_now().strftime("%Y-%m-%d %H:%M"), "entry_bid": q["bid"], "entry_ask": q["ask"],
                   "entry_price": q["ask"], "entry_mid": round(mid, 4), "peak_mid": round(mid, 4), "status": "OPEN"}
            self.rows.append(row)
            self.save()
            log.info("【跟单·开仓】%s %s × %d 张 ask %.2f（bid %.2f）投入 $%.0f | 大单 %s $%.0fK", row["position_id"], core, qty,
                     q["ask"], q["bid"], q["ask"] * 100 * qty, row["flow_time"], row["flow_usd"] / 1e3)
            self.send(f"开仓 {f['ticker']} {f['strike']:g}{f['right']} {f['exp'][5:]}", BLUE, [
                {"name": "成交", "value": f"ask ${q['ask']:.2f} × {qty} 张 = ${q['ask'] * 100 * qty:,.0f}（bid {q['bid']:.2f}）", "inline": False},
                {"name": "触发大单", "value": f"{row['flow_time']} ${row['flow_usd'] / 1e3:,.0f}K（{row['flow_side']}）", "inline": True},
                {"name": "Heatmap", "value": f"行权价排第 {f['rank']}（负节点），该到期主导墙 {f['exp_wall']}，合计主导墙 {f['agg_wall']}", "inline": False},
                {"name": "离场", "value": f"−22% 止损 / +30% 起追踪 10% / 最多 {MAX_HOLD} 个交易日", "inline": False}])

    # ------------------------------------------------------------------ 持仓
    def manage(self, final=False):
        pos = self.open_rows()
        if not pos:
            return
        now = et_now()
        self.quotes.new_round([r["contract"] for r in pos])
        changed = False
        for r in pos:
            q = self.quotes.cache.get(r["contract"])
            if not q or q["ask"] <= 0:
                continue
            mid = (q["ask"] + q["bid"]) / 2 if q["bid"] > 0 else q["ask"] / 2
            em = float(r["entry_mid"])
            peak = max(float(r["peak_mid"]), mid)
            if peak > float(r["peak_mid"]):
                r["peak_mid"] = round(peak, 4)
                changed = True
            held = trading_days_held(dt.date.fromisoformat(r["open_date"]), self.day)
            last_day = dt.date.fromisoformat(r["expiration"]) - dt.timedelta(days=1)
            reason = None
            if mid / em - 1 <= STOP:
                reason = "hard_stop"
            elif peak / em - 1 >= TRAIL_ON and mid <= max(em * (1 + TRAIL_ON), peak * (1 - TRAIL)):
                reason = "trailing_stop"
            elif (held + 1 >= MAX_HOLD or self.day >= last_day) and now >= et_datetime(self.day, "15:50"):
                reason = "time_exit"
            if not reason or (q["bid"] <= 0 and not final):
                continue
            bid = q["bid"]
            ep, qty = float(r["entry_price"]), int(r["qty"])
            r.update({"status": "CLOSED", "exit_time": now.strftime("%Y-%m-%d %H:%M"), "exit_bid": bid, "exit_price": bid,
                      "exit_reason": reason, "pnl_usd": round((bid - ep) * 100 * qty, 2), "roi_pct": round((bid / ep - 1) * 100, 1)})
            changed = True
            log.info("【跟单·平仓】%s %s %s bid %.2f（%+.1f%%）$%+.0f", r["position_id"], r["contract"], reason, bid,
                     float(r["roi_pct"]), float(r["pnl_usd"]))
            cn = {"hard_stop": "止损", "trailing_stop": "追踪止盈", "time_exit": "时间离场"}[reason]
            self.send(f"平仓 {r['ticker']} {float(r['strike']):g}{r['right']} {r['expiration'][5:]}：{cn} {float(r['roi_pct']):+.1f}%",
                      GREEN if float(r["pnl_usd"]) > 0 else RED,
                      desc=f"买 ${ep:.2f} → 卖 bid ${bid:.2f} × {qty} 张，${float(r['pnl_usd']):+,.0f}（{r['open_date']} 开，峰值中间价 "
                           f"{(peak / em - 1) * 100:+.0f}%）")
        if changed:
            self.save()

    # ------------------------------------------------------------------ 收盘
    def summary(self):
        today = [r for r in self.rows if (r.get("exit_time") or "").startswith(self.day.isoformat())]
        pos = self.open_rows()
        tot = sum(float(r["pnl_usd"]) for r in today)
        allc = [r for r in self.rows if r["status"] == "CLOSED"]
        lines = [f"{'✅' if float(r['pnl_usd']) > 0 else '❌'} {r['ticker']} {float(r['strike']):g}{r['right']} "
                 f"{r['exit_reason']} {float(r['roi_pct']):+.1f}% ${float(r['pnl_usd']):+,.0f}" for r in today]
        self.send(f"{self.day} 收盘：今日开 {len(self.opened_today())} 笔，平 {len(today)} 笔 ${tot:+,.0f}", ORANGE, [
            {"name": "今日平仓", "value": "\n".join(lines) or "无", "inline": False},
            {"name": f"持仓 {len(pos)} 笔", "value": "\n".join(f"{r['ticker']} {float(r['strike']):g}{r['right']} {r['expiration'][5:]}"
                                                               f"（{r['open_date'][5:]} 开，买 {float(r['entry_price']):.2f}）" for r in pos) or "无", "inline": False},
            {"name": "累计", "value": f"已平 {len(allc)} 笔，胜率 {100 * sum(float(r['pnl_usd']) > 0 for r in allc) / max(1, len(allc)):.0f}%，"
                                      f"${sum(float(r['pnl_usd']) for r in allc):+,.0f}；配额剩余 {self.client.quota_remaining}", "inline": False}])

    def check_quota(self):
        q = self.client.quota_remaining
        if q is not None and q < QUOTA_FLOOR and not self.halted:
            self.halted = True
            log.warning("【跟单】配额剩余 %d < %d，停止开新仓（持仓照常管理）", q, QUOTA_FLOOR)
            self.send(f"配额剩余 {q} < {QUOTA_FLOOR}：停止开新仓，持仓照常管理", RED)

    def run(self):
        start, end = et_datetime(self.day, "09:30"), et_datetime(self.day, "16:00")
        while et_now() < start:
            time.sleep(min(30, max(1, (start - et_now()).total_seconds())))
        self.send(f"{self.day} 启动：持仓 {len(self.open_rows())} 笔", BLUE)
        while et_now() < end:
            t0 = et_now()
            try:
                self.poll_flow()
                self.try_entries()
                self.manage()
                self.check_quota()
                self.save()
            except QuotaExhausted as e:
                log.critical("配额熔断：%s", e)
                self.send(f"配额熔断：{e}", RED)
                break
            except Exception as e:   # 单轮出错不退出，下一分钟重试
                log.exception("【跟单】本轮出错：%s", e)
            nxt = t0.replace(second=15, microsecond=0) + dt.timedelta(minutes=1)
            time.sleep(max(1, (nxt - et_now()).total_seconds()))
        try:
            self.manage(final=True)
        except QuotaExhausted:
            pass
        self.save()
        self.summary()
        log.info("【跟单】收盘；配额剩余 %s", self.client.quota_remaining)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跟大单模拟实盘")
    ap.add_argument("--dry-run", action="store_true", help="不写账本、不发 Discord")
    ap.add_argument("--log-file")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    setup_logging(cfg.log_level, args.log_file)
    day = et_now().date()
    if not is_trading_day(day):
        log.info("今天 %s 不是交易日，退出", day)
        return 0
    client = YHSClient(cfg)
    Tape(cfg, client, from_config(cfg), day, args.dry_run).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
