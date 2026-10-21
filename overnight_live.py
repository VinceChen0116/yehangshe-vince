"""
overnight_live.py —— 隔夜期权模拟实盘（2026-10-15 用户定的规则，与日内实盘分开记账）
=====================================================================================
尾盘开仓（--open，14:55 启动；15:15 预扫，15:45:15 扫描 1，15:50:15 扫描 2 确认，15:50–15:57 买入）：
  1. 标的：212 只存档池 + 最近一个交易日的日内实盘监控名单（含 LITE / TSM / MU 这类高价票）
  2. 结构：标记 15:40、15:45 的两个 5 分钟 Heatmap 快照（到期 = 今天之后最近一个有数据的到期，能过夜）都识别出同一结构，
     局部节点（node_mode="local"）或全局节点（最大正 / 负 GEX）任一成立即可：跳板 → 买 Call，悬崖 → 买 Put
  3. Flow 大单确认（必须）：今天 15:00–15:50该标的有同方向（Call / Put）的主动买入大单（成交在 Ask 或 Ask 以上），单笔权利金 ≥ $5 万
  4. 合约：同一到期的第一档虚值（接近平值），每笔约 $1,000（$750–$1,250，单张超 $1,250 不开）；按实时报价的 ask 买入，bid 为 0、没有报价、
     或买卖价差超过中间价 25% 的不开
  5. 每晚最多 10 笔、同板块最多 3 笔（sectors.py），按 15:00 后同方向大单权利金合计从大到小排
次日出场（--close，约 09:15 启动）：
  09:35 起每分钟查实时报价，bid > 0 就按 bid 卖出；09:45 还没卖掉的按当时 bid 记（没有报价按 0 记）。夜里不设止损。
记账：live/overnight_<开仓日>.csv（开仓时写入，次日平仓后补全出场字段）；开 / 平仓都发 Discord。
用法：python3 overnight_live.py --open | --close [--dry-run] [--now HH:MM]
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.request

from archive import fetch_heatmap_rows, live_universe, rows_to_frames
from config import (SystemConfig, et_datetime, et_now, expiry_candidates, is_trading_day, next_trading_day,
                    parse_iso, prev_trading_day, setup_logging, to_et)
from detector import FLOW_TRADES_PATH, detect_structure, extract_rows, parse_heatmap
from main import QuoteBook
from notify import from_config
from paper_trader import occ_symbol
from sectors import sector_of
from yhs_client import YHSClient

log = logging.getLogger("yhs.overnight_live")
LIVE_DIR = "live"
MAX_POSITIONS = 10
MAX_PER_SECTOR = 3   # 同板块每晚最多 3 笔（2026-10-16 用户定，板块见 sectors.py）
BUDGET, BUDGET_BAND = 1000.0, 0.25   # 每笔目标投入 $1,000，允许 $750–$1,250（2026-10-16 用户定）
SAME_COMPANY = (("GOOG", "GOOGL"),)   # 同一家公司的不同股票类别：方向冲突就都不开（2026-10-20 用户定）


def node_metrics(snap, st) -> dict:
    """开仓时的结构指标（只记录，见 FIELDS 注释）。"""
    cells, spot = list(snap.cells), snap.spot
    tot = snap.total_abs_gex or sum(abs(g) for _, g in cells) or 1.0
    pos_max = max((g for _, g in cells if g > 0), default=0.0) or 1.0
    up = st.structure == "SPRINGBOARD"
    win = [(k, g) for k, g in cells if (spot < k <= spot * 1.01 if up else spot * 0.99 <= k < spot)]
    node_g = dict(cells).get(st.pos_strike, 0.0)
    near = [(k, g) for k, g in cells if abs(k / spot - 1) <= 0.02]
    m = max(near, key=lambda c: abs(c[1]), default=None)
    side = ("本身" if m and m[0] == st.pos_strike else
            "前方" if m and ((m[0] > spot) if up else (m[0] < spot)) else "背后")
    abs_max = max((abs(g) for _, g in cells), default=1.0) or 1.0
    return {"node_ratio": round(node_g / pos_max, 2),
            "target_pct": round(abs(st.neg_strike - spot) / spot * 100, 2),
            "fuel_neg_pct": round(-sum(g for _, g in win if g < 0) / tot * 100, 1),
            "fuel_pos_pct": round(sum(g for _, g in win if g > 0) / tot * 100, 1),
            "block_ratio": round(max((g for k, g in win if g > 0 and k != st.pos_strike), default=0.0) / pos_max, 2),
            "magnet": f"{m[0]:g}/{'正' if m[1] > 0 else '负'}/{side}/{abs(m[1]) / abs_max:.2f}" if m else ""}


def path_reason(snap, st) -> str:
    """天花板 / 踏板（不含）→ 深渊 / 加速区（含）这段路径的检查，局部 / 全局模式都用；不通过返回原因，通过返回 ""。"""
    cells = dict(snap.cells)
    tot = snap.total_abs_gex or sum(abs(g) for g in cells.values()) or 1.0
    up = st.structure == "SPRINGBOARD"
    path = [(k, g) for k, g in snap.cells if (st.pos_strike < k <= st.neg_strike if up else st.neg_strike <= k < st.pos_strike)]
    node, tgt = cells.get(st.pos_strike, 0.0), cells.get(st.neg_strike, 0.0)
    neg = [-g for _, g in path if g < 0]
    pos = [(k, g) for k, g in path if g > 0]
    if abs(tgt) < PATH_TARGET_MIN * tot:
        return f"目标 {st.neg_strike:g} 太弱（{abs(tgt) / tot * 100:.1f}% < {PATH_TARGET_MIN * 100:.0f}%）"
    wall = max(pos, key=lambda x: x[1], default=None)
    if wall and node > 0 and wall[1] > PATH_BLOCK_MAX * node:
        return f"路上 {wall[0]:g} 正节点挡路（{wall[1] / node:.0%} × 天花板/踏板）"
    if sum(neg) < PATH_NEG_POS * sum(g for _, g in pos) or len(neg) <= len(pos):
        return f"路径燃料不足（负 {sum(neg) / 1e6:.2f}M/{len(neg)} 个 vs 正 {sum(g for _, g in pos) / 1e6:.2f}M/{len(pos)} 个）"
    return ""


def confirm_bar_ok(st, prev_spot: float, spot: float) -> tuple[bool, str]:
    """再确认（15:50 那根）：现价往反方向走不超过 CONFIRM_TOL、且没越过天花板 / 踏板才算确认。返回 (是否通过, 说明)。
    悬崖（买 Put）：反方向 = 往上，越过 = 现价 > 天花板；跳板（买 Call）：反方向 = 往下，越过 = 现价 < 踏板。"""
    if st.structure == "CLIFF":
        if spot > st.pos_strike:
            return False, f"现价 {spot:.2f} 已越过天花板 {st.pos_strike:g}"
        if spot > prev_spot * (1 + CONFIRM_TOL):
            return False, f"现价 {prev_spot:.2f}→{spot:.2f} 往上（反向）{(spot / prev_spot - 1) * 100:.2f}%"
    else:
        if spot < st.pos_strike:
            return False, f"现价 {spot:.2f} 已跌破踏板 {st.pos_strike:g}"
        if spot < prev_spot * (1 - CONFIRM_TOL):
            return False, f"现价 {prev_spot:.2f}→{spot:.2f} 往下（反向）{(1 - spot / prev_spot) * 100:.2f}%"
    return True, ""


EARN_DIR = os.path.join("yhs_data", "cache", "earnings")


def earnings_on(day: dt.date) -> dict | None:
    """Nasdaq 财报日历 {代码: time-pre-market / time-after-hours / time-not-supplied}，缓存在 EARN_DIR；拉不到返回 None。"""
    fp = os.path.join(EARN_DIR, f"{day}.json")
    if not os.path.exists(fp):
        try:
            req = urllib.request.Request(f"https://api.nasdaq.com/api/calendar/earnings?date={day}",
                                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = (json.load(r).get("data") or {}).get("rows") or []
        except Exception as e:
            log.warning("【隔夜·财报】%s 财报日历拉取失败：%s", day, e)
            return None
        os.makedirs(EARN_DIR, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump({x["symbol"].replace(".", "").replace("/", ""): x.get("time") for x in rows}, f, indent=0)
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def earnings_reason(ticker: str, day: dt.date) -> str:
    """隔夜跨财报：开仓日盘后 / 时间未注明，或次日（及中间休市日）盘前 / 时间未注明发财报 → 返回说明；否则 ""。
    财报后 IV 骤降，买方多空都亏（2026-10-21 用户定：财报夜不开）。日历拉不到时不拦（日志有警告）。"""
    nxt = next_trading_day(day)
    t = ticker.replace(".", "")
    d = day
    while d <= nxt:
        cal = earnings_on(d)
        tm = (cal or {}).get(t)
        if tm and ((d == day and tm != "time-pre-market") or (d == nxt and tm != "time-after-hours") or day < d < nxt):
            return f"财报夜（{d:%m/%d} {'盘后' if tm == 'time-after-hours' else '盘前' if tm == 'time-pre-market' else '时间未注明'}）"
        d += dt.timedelta(days=1)
    return ""


def drop_conflicts(cands: list, ticker, right) -> tuple[list, list]:
    """同一家公司（SAME_COMPANY）的候选一个看涨一个看跌 = 方向不明，全部去掉。返回 (保留, 去掉的代码)。
    ticker / right：从候选元组取代码与 C/P 的函数（实盘与回测的元组结构不同）。"""
    dropped = set()
    for group in SAME_COMPANY:
        rights = {right(c) for c in cands if ticker(c) in group}
        if len(rights) > 1:
            dropped |= set(group)
    return [c for c in cands if ticker(c) not in dropped], sorted(dropped & {ticker(c) for c in cands})


def size_contracts(price: float) -> int:
    """按每笔 $1,000（±25%）算张数：取落在 $750–$1,250 内、最接近 $1,000 的张数；单张就超过 $1,250 返回 0（不开）。
    单张 $625–$750 时 1 张不够下限、2 张超上限，开 1 张（2026-10-19 用户定）。"""
    per = price * 100
    if per <= 0:
        return 0
    lo, hi = BUDGET * (1 - BUDGET_BAND), BUDGET * (1 + BUDGET_BAND)
    best = 0
    for n in range(1, int(hi // per) + 1):
        if lo <= n * per <= hi and (not best or abs(n * per - BUDGET) < abs(best * per - BUDGET)):
            best = n
    if not best and per <= hi:
        best = 1
    return best
FLOW_MIN_PREMIUM = 50000
FLOW_SINCE = "15:00"   # 只看尾盘的同向大单
MAX_SPREAD = 0.25      # (ask - bid) / 中间价 超过 25% 不开：一买一卖先亏掉价差
PRESCAN_START, OPEN_DEADLINE = "15:15", "15:58"   # 10/20 起 15:50 那根再确认后才买，截止放宽到 15:58
BAR1, BAR2 = "15:40", "15:45"   # 两根确认用的 Heatmap；标记 T 的段约 T+5:10 才可用
SCAN1_AT, SCAN2_AT = dt.timedelta(hours=15, minutes=45, seconds=15), dt.timedelta(hours=15, minutes=50, seconds=15)
BAR3, SCAN3_AT = "15:50", dt.timedelta(hours=15, minutes=55, seconds=15)   # 再确认那根（数据约 15:55），确认后约 15:56 买
PATH_TARGET_MIN = 0.03   # 深渊 / 加速区本身 |GEX| ≥ 该到期 |GEX| 合计 3%（与"显著正节点"同一标准）
PATH_BLOCK_MAX = 0.5     # 路径上任何正节点 ≤ 天花板 / 踏板的 50%（否则是挡路的墙）
PATH_NEG_POS = 2.0       # 路径上负 GEX 合计 ≥ 2 × 正 GEX 合计，且负节点个数 > 正节点个数（2026-10-21 用户定）
CONFIRM_TOL = 0.0005   # 再确认：反向不超过现价 0.05% 且没越过天花板 / 踏板（2026-10-20 用户定）
FLOW_UNTIL = "15:50"   # 大单统计截止（与回测一致）
SELL_START, SELL_DEADLINE = "09:35", "09:45"
FIELDS = ["position_id", "open_date", "ticker", "structure", "node_mode", "right", "contract", "expiration", "strike", "qty",
          "spot", "pos_node", "neg_node", "dist_pct", "flow_n", "flow_usd", "flow_max", "entry_time", "entry_bid",
          "entry_ask", "entry_price", "exit_time", "exit_bid", "exit_price", "exit_note", "pnl_usd", "roi_pct", "status",
          "shadow_reason", "node_ratio", "target_pct", "fuel_neg_pct", "fuel_pos_pct", "block_ratio", "magnet", "spot_1550"]
# 结构指标（10/21 起只记录、不参与开仓，积累一周后再定门槛；均按标记 15:45 那根计算，占比 = 占该到期 |GEX| 合计 %）：
#   node_ratio   天花板 / 踏板 GEX ÷ 该到期最大正节点 GEX（1 = 就是最大正节点）
#   target_pct   深渊 / 加速区离现价 %
#   fuel_neg_pct / fuel_pos_pct  现价往目标方向 1% 内负 GEX 合计 / 正 GEX 合计占比（燃料）
#   block_ratio  同一窗口内（不含天花板 / 踏板本身）最大正节点 ÷ 最大正节点（路上的阻挡）
#   magnet       现价 ±2% 内 |GEX| 最大的节点：行权价/正负/位置（本身 / 前方 / 背后）/ 占最大 |GEX| 的比
#   spot_1550    标记 15:50 那根的现价（再确认用）
# status：OPEN / CLOSED = 真实（模拟）仓；SHADOW_OPEN / SHADOW_CLOSED = 影子仓：没有新规则时本会开、被规则删掉的单子，
# 同样按实时 ask 记买入、次日按 bid 平，只用来检验规则（不计入隔夜盈亏），shadow_reason 写被哪条规则删掉


def ledger_path(day: dt.date) -> str:
    return os.path.join(LIVE_DIR, f"overnight_{day}.csv")


def write_ledger(day: dt.date, rows: list) -> None:
    os.makedirs(LIVE_DIR, exist_ok=True)
    tmp = ledger_path(day) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, ledger_path(day))


def wait_until(t: dt.datetime) -> None:
    while et_now() < t:
        time.sleep(min(30, max(1, (t - et_now()).total_seconds())))


def universe(cfg, day: dt.date) -> list:
    with open(os.path.join(cfg.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
        tickers = list(json.load(f)["tickers"])
    d = day
    for _ in range(5):   # 当天的日内实盘日志收盘后才推送：用最近一个有日志的交易日名单
        extra = live_universe(d)
        if extra:
            tickers += [t for t in extra if t not in tickers]
            break
        d = prev_trading_day(d)
    return tickers


def daily_expiry_tickers(cfg) -> set:
    """有每日到期的标的：最近一个存档日（次日不是周五）里，次日到期的存档文件非空。"""
    root = os.path.join(cfg.data_dir, "archive", "heatmap")
    for d in sorted(os.listdir(root), reverse=True):
        try:
            day = dt.date.fromisoformat(d)
        except ValueError:
            continue
        nxt = next_trading_day(day)
        if nxt.weekday() == 4:
            continue
        out = set()
        for t in os.listdir(os.path.join(root, d)):
            fp = os.path.join(root, d, t, f"{nxt}.json")
            if os.path.exists(fp) and os.path.getsize(fp) > 1000:
                out.add(t)
        return out
    return set()


def predicted_expiry(ticker: str, day: dt.date, daily: set) -> str:
    """能过夜的最近到期：有每日到期的用下一个交易日，其余用下一个周五（周五休市则用前一个交易日）。"""
    if ticker in daily:
        return next_trading_day(day).isoformat()
    d = day + dt.timedelta(days=1)
    while d.weekday() != 4:
        d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d.isoformat()


def pick_expiry(client, ticker: str, day: dt.date, first: str | None = None):
    """今天之后最近一个有 Heatmap 的到期（预扫用，只拉 15:00 之后的几根）；先试推算的到期。返回 (到期, 行) 或 (None, [])。"""
    cands = expiry_candidates(day, None, 6)[1:]
    if first in cands:
        cands = [first] + [e for e in cands if e != first]
    for exp in cands:
        try:
            raw = fetch_heatmap_rows(client, ticker, exp, day, max_pages=1, start="15:00")
        except Exception as e:   # 该到期没有数据：换下一个
            log.debug("%s %s Heatmap 获取失败：%s", ticker, exp, e)
            raw = None
        if raw:
            return exp, raw
    return None, []


def structure_at(cfg, lcfg, ticker: str, day: dt.date, exp: str, raw: list, label: str):
    """标记为 label 的那根 Heatmap 上的结构（局部节点优先，其次全局节点）。返回 (快照, 结构, 模式) 或 None。"""
    frames = rows_to_frames(ticker, day, exp, raw)
    k = next((t for t in frames if t.strftime("%H:%M") == label), None)
    snap = parse_heatmap(frames[k], cfg, day) if k else None
    if snap is None:
        return None
    loc = detect_structure(snap, lcfg)
    st = loc or detect_structure(snap, cfg)
    return (snap, st, "local" if loc else "king") if st else None


def find_structure(cfg, lcfg, ticker: str, day: dt.date, exp: str, raw: list, now: dt.datetime):
    """最近两个已可用快照（标记 T 的段在 T+5 才可用）都识别出同一结构（局部节点优先，其次全局节点）。"""
    frames = rows_to_frames(ticker, day, exp, raw)
    ready = [t for t in sorted(frames) if t + dt.timedelta(minutes=5) <= now][-2:]
    if len(ready) < 2:
        return None
    found = []
    for t in ready:
        snap = parse_heatmap(frames[t], cfg, day)
        if snap is None:
            return None
        loc = detect_structure(snap, lcfg)
        found.append((snap, loc or detect_structure(snap, cfg), "local" if loc else "king"))
    (_, a, _), (snap, b, mode) = found
    if a and b and a.structure == b.structure:
        return exp, snap, b, mode
    return None


def flow_check(client, ticker: str, right: str, day: dt.date) -> dict:
    """今天 FLOW_SINCE（15:00）之后该标的同方向 Ask 侧单笔 ≥ $5 万 的大单（2026-10-15 用户定：其余时段的大单参考价值不大）。"""
    since, until = et_datetime(day, FLOW_SINCE), et_datetime(day, FLOW_UNTIL)
    trades, cursor = [], None
    for _ in range(20):   # 从最新往前翻，翻到 15:00 之前就停
        p = {"tickers": ticker, "kind": "order", "min_premium_usd": FLOW_MIN_PREMIUM, "sides": "A,AA", "rights": right,
             "session_date": day.isoformat(), "sort_by": "occurredAt", "direction": "desc", "limit": 25, "cursor": cursor}
        payload = client.get(FLOW_TRADES_PATH, p)
        page = extract_rows(payload)
        recent = [t for t in page if to_et(parse_iso(t["occurred_at"])) >= since]
        trades += [t for t in recent if to_et(parse_iso(t["occurred_at"])) <= until]
        cursor = (payload.get("_meta") or {}).get("next_cursor") if isinstance(payload, dict) else None
        if not cursor or len(recent) < len(page):
            break
    big = max(trades, key=lambda t: float(t.get("premium_usd") or 0), default=None)
    return {"n": len(trades), "usd": round(sum(float(t.get("premium_usd") or 0) for t in trades)),
            "max": (f'{str(big.get("contract_symbol", "")).split(":")[-1]} ${float(big["premium_usd"]) / 1e3:.0f}K '
                    f'{to_et(parse_iso(big["occurred_at"])).strftime("%H:%M")}') if big else ""}


def first_otm(snap, right: str):
    side = [k for k in snap.strikes if (k > snap.spot if right == "C" else k < snap.spot)]
    return (min(side) if right == "C" else max(side)) if side else None


def do_open(cfg, client, notifier, day: dt.date, dry: bool, only=None) -> int:
    lcfg = copy.copy(cfg)
    lcfg.node_mode = "local"
    wait_until(et_datetime(day, PRESCAN_START))
    tickers = only or universe(cfg, day)
    # 预扫：确定每只能过夜的到期日，顺便看 15:00 后有没有结构（有的排在正式扫描前面）
    log.info("【隔夜·预扫】%s 确定 %d 只的过夜到期日", et_now().strftime("%H:%M"), len(tickers))
    exps, early = {}, []
    for t in tickers:
        exp, raw = pick_expiry(client, t, day)   # 开仓日之后最近一个有挂牌（有 Heatmap）的到期；当天到期不看（10/20 用户定）
        if exp:
            exps[t] = exp
            if find_structure(cfg, lcfg, t, day, exp, raw, et_now()):
                early.append(t)
    order = early + [t for t in exps if t not in early]
    log.info("【隔夜·预扫】完成：%d 只有过夜到期，其中 %d 只预扫时已有结构：%s", len(exps), len(early), "、".join(early))
    # 第一段（15:45:15 起）：扫全部，15:40 那根有结构的进候选（若扫到时 15:45 那根也已出炉，就直接两根一起确认）
    base = dt.datetime.combine(day, dt.time(0), tzinfo=et_datetime(day, "12:00").tzinfo)
    wait_until(base + SCAN1_AT)
    log.info("【隔夜·扫描1】%s 扫 %d 只，看 %s 那根", et_now().strftime("%H:%M:%S"), len(order), BAR1)
    pending, confirmed = {}, []
    for t in order:
        if not dry and et_now() >= et_datetime(day, OPEN_DEADLINE):
            log.warning("【隔夜·扫描】到 %s 还没扫完，剩余标的放弃", OPEN_DEADLINE)
            break
        try:
            raw = fetch_heatmap_rows(client, t, exps[t], day, max_pages=1, start="15:25")
        except Exception as e:
            log.info("%s Heatmap 获取失败：%s", t, e)
            continue
        a1 = structure_at(cfg, lcfg, t, day, exps[t], raw, BAR1)
        if a1 is None:
            continue
        a2 = structure_at(cfg, lcfg, t, day, exps[t], raw, BAR2)
        if a2 is not None:
            if a2[1].structure == a1[1].structure:
                confirmed.append((t, a2))
        else:
            pending[t] = a1
    log.info("【隔夜·扫描1】完成：%s 有结构的 %d 只待 %s 确认，已确认 %d 只", BAR1, len(pending), BAR2, len(confirmed))
    # 第二段（15:50:15 起）：候选重拉一次，15:45 那根结构相同才算
    if pending:
        wait_until(base + SCAN2_AT)
        for t, a1 in pending.items():
            if not dry and et_now() >= et_datetime(day, OPEN_DEADLINE):
                break
            try:
                raw = fetch_heatmap_rows(client, t, exps[t], day, max_pages=1, start="15:25")
            except Exception as e:
                log.info("%s Heatmap 获取失败：%s", t, e)
                continue
            a2 = structure_at(cfg, lcfg, t, day, exps[t], raw, BAR2)
            if a2 and a2[1].structure == a1[1].structure:
                confirmed.append((t, a2))
    log.info("【隔夜·确认】%s/%s 两根都成立 %d 只：%s", BAR1, BAR2, len(confirmed), "、".join(t for t, _ in confirmed))
    cands = []
    for t, (snap, st, mode) in confirmed:
        fl = flow_check(client, t, st.right, day)
        log.info("【隔夜·结构】%s %s（%s）现货 %.2f 节点 %.2f→%.2f 距 %.2f%% | 15:00–%s 同向大单 %d 笔 $%.0fK",
                 t, st.structure, mode, snap.spot, st.pos_strike, st.neg_strike, st.distance_pct * 100, FLOW_UNTIL,
                 fl["n"], fl["usd"] / 1e3)
        if fl["n"]:
            cands.append((fl["usd"], t, exps[t], snap, st, mode, fl))
    cands.sort(key=lambda x: -x[0])
    all_cands = list(cands)   # 没有新规则时的候选：影子仓（被规则删掉的单子）从这里选
    reasons = {c[1]: earnings_reason(c[1], day) or path_reason(c[3], c[4]) for c in cands}
    reasons = {t: r for t, r in reasons.items() if r}
    for t, r in reasons.items():
        log.info("【隔夜·放弃】%s %s，不开", t, r)
    cands = [c for c in cands if c[1] not in reasons]
    cands, dropped = drop_conflicts(cands, lambda c: c[1], lambda c: c[4].right)
    reasons.update({t: "同一家公司方向相反" for t in dropped})
    s1550 = {}   # 标记 15:50 那根的现价（记录用）
    if dropped:
        log.info("【隔夜·放弃】%s 是同一家公司、方向相反，都不开", "、".join(dropped))
    # 再确认（15:55:15 起看标记 15:50 的那根）：反向超过 0.05% 或越过天花板 / 踏板就不开
    if cands:
        wait_until(base + SCAN3_AT)
        kept = []
        for c in cands:
            t, exp, snap, st = c[1], c[2], c[3], c[4]
            s3 = None
            try:
                frames = rows_to_frames(t, day, exp, fetch_heatmap_rows(client, t, exp, day, max_pages=1, start="15:25"))
                k = next((k for k in frames if k.strftime("%H:%M") == BAR3), None)
                s3 = parse_heatmap(frames[k], cfg, day) if k else None
            except Exception as e:
                log.info("%s Heatmap 获取失败：%s", t, e)
            ok, why = confirm_bar_ok(st, snap.spot, s3.spot) if s3 else (False, f"{BAR3} 那根拿不到")
            if s3:
                s1550[t] = round(s3.spot, 2)
            if ok:
                kept.append(c)
            else:
                reasons[t] = f"{BAR3} 再确认未通过：{why}"
                log.info("【隔夜·再确认】%s 不开：%s", t, why)
        cands = kept
    log.info("【隔夜·候选】结构 + 大单 + %s 再确认都满足 %d 个：%s", BAR3, len(cands), "、".join(c[1] for c in cands) or "无")
    quotes = QuoteBook(client)

    def pick(clist, prefix: str, verbose: bool) -> list:
        """按排序依次过板块 / 报价 / 价差 / 张数，最多 MAX_POSITIONS 笔。"""
        out, sec_n = [], {}
        say = log.info if verbose else log.debug
        warn = log.warning if verbose else log.debug
        for usd, t, exp, snap, st, mode, fl in clist:
            if len(out) >= MAX_POSITIONS:
                break
            sec = sector_of(t)
            if sec_n.get(sec, 0) >= MAX_PER_SECTOR:
                say("【隔夜·跳过】%s 同板块（%s）已 %d 笔", t, sec, MAX_PER_SECTOR)
                continue
            K = first_otm(snap, st.right)
            if K is None:
                continue
            sym = occ_symbol(t, exp, st.right, K)
            q = quotes.get(sym)
            if not q or q["ask"] <= 0 or q["bid"] <= 0:
                warn("【隔夜·放弃】%s %s 没有报价或 bid 为 0，不开", t, sym)
                continue
            if q["ask"] < cfg.min_option_price:
                continue
            spread = (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2)
            if spread > MAX_SPREAD:
                warn("【隔夜·放弃】%s %s 买卖价差 %.0f%%（bid %.2f / ask %.2f）> %.0f%%，不开", t, sym, spread * 100,
                     q["bid"], q["ask"], MAX_SPREAD * 100)
                continue
            qty = size_contracts(q["ask"])
            if not qty:
                warn("【隔夜·放弃】%s %s 单张 $%.0f 超过每笔上限 $%.0f，不开", t, sym, q["ask"] * 100, BUDGET * (1 + BUDGET_BAND))
                continue
            out.append({"position_id": f"{prefix}{day.strftime('%Y%m%d')}-{len(out) + 1:02d}", "open_date": day.isoformat(),
                        "qty": qty, "ticker": t, "structure": st.structure, "node_mode": mode, "right": st.right,
                        "contract": sym, "expiration": exp, "strike": K, "spot": round(snap.spot, 2),
                        "pos_node": st.pos_strike, "neg_node": st.neg_strike, "dist_pct": round(st.distance_pct * 100, 2),
                        "flow_n": fl["n"], "flow_usd": fl["usd"], "flow_max": fl["max"],
                        "entry_time": et_now().strftime("%H:%M"), "entry_bid": q["bid"], "entry_ask": q["ask"],
                        "entry_price": q["ask"], "status": "OPEN", "spot_1550": s1550.get(t, ""),
                        **node_metrics(snap, st)})
            sec_n[sec] = sec_n.get(sec, 0) + 1
            say("【隔夜·开仓】%s %s %s × %d 张 | ask %.2f（bid %.2f）投入 $%.0f | 大单 %d 笔 $%.0fK", out[-1]["position_id"],
                t, sym, qty, q["ask"], q["bid"], q["ask"] * 100 * qty, fl["n"], fl["usd"] / 1e3)
        return out

    rows = pick(cands, "ON", True)
    # 影子仓：没有"同公司冲突 / 15:50 再确认"两条规则时本会开、现在被删掉的单子（同样按此刻 ask 记，次日按 bid 平）
    real = {r["ticker"] for r in rows}
    shadow = [r for r in pick(all_cands, "SH", False) if r["ticker"] not in real]
    for r in shadow:
        r.update({"status": "SHADOW_OPEN", "shadow_reason": reasons.get(r["ticker"], "")})
        log.info("【隔夜·影子】%s %s × %d 张 | ask %.2f | 被删原因：%s", r["ticker"], r["contract"], r["qty"],
                 r["entry_price"], r["shadow_reason"] or "（名额顺序变化）")
    if not dry:
        write_ledger(day, rows + shadow)
    if notifier and not dry:
        lines = [f"{'🟢 Call' if r['right'] == 'C' else '🔴 Put'} **{r['ticker']}** {r['strike']:g}{r['right']} {r['expiration'][5:]}"
                 f" ×{r['qty']} @ ${r['entry_price']:.2f}（${r['entry_price'] * 100 * r['qty']:,.0f}）| {'局部' if r['node_mode'] == 'local' else '全局'}"
                 f"{'跳板' if r['structure'] == 'SPRINGBOARD' else '悬崖'} {r['pos_node']:g}→{r['neg_node']:g}"
                 f" | 大单 {r['flow_n']} 笔 ${r['flow_usd'] / 1e3:,.0f}K" for r in rows]
        if shadow:
            lines += ["", f"👻 被规则删掉的 {len(shadow)} 笔（影子仓，次日同样平仓对照，不计盈亏）："]
            lines += [f"· {r['ticker']} {r['strike']:g}{r['right']} ×{r['qty']} @ ${r['entry_price']:.2f} — {r['shadow_reason']}" for r in shadow]
        notifier.send(embeds=[{"title": f"🌙 {day} 隔夜单开仓 {len(rows)} 笔（次日 09:35–09:45 跳空平仓）",
                               "color": 0x5865F2, "description": "\n".join(lines) or "今晚没有同时满足结构 + 大单 + 再确认的标的，不开仓",
                               "footer": {"text": "隔夜模拟盘 · 按实时 ask 买入 · 与日内账户分开记账"}}])
    log.info("【隔夜】开仓 %d 笔；配额剩余 %s", len(rows), client.quota_remaining)
    return 0


def do_close(cfg, client, notifier, day: dt.date, dry: bool) -> int:
    pday = prev_trading_day(day)
    if not os.path.exists(ledger_path(pday)):
        log.info("【隔夜·平仓】%s 没有隔夜仓（%s 不存在）", day, ledger_path(pday))
        return 0
    with open(ledger_path(pday), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    open_rows = [r for r in rows if r["status"] in ("OPEN", "SHADOW_OPEN")]
    if not open_rows:
        return 0
    wait_until(et_datetime(day, SELL_START))
    deadline = et_datetime(day, SELL_DEADLINE)
    quotes = QuoteBook(client)
    while open_rows:
        last = et_now() >= deadline
        quotes.new_round([r["contract"] for r in open_rows])
        for r in list(open_rows):
            q = quotes.get(r["contract"])
            bid = q["bid"] if q else 0.0
            if bid > 0 or last:
                entry = float(r["entry_price"])
                r.update({"exit_time": et_now().strftime("%H:%M"), "exit_bid": bid, "exit_price": bid,
                          "exit_note": "" if bid > 0 else ("09:45 仍无报价，按 0 记" if not q else "09:45 bid 为 0"),
                          "pnl_usd": round((bid - entry) * 100 * int(r.get("qty") or 1), 2), "roi_pct": round((bid / entry - 1) * 100, 1),
                          "status": "SHADOW_CLOSED" if r["status"] == "SHADOW_OPEN" else "CLOSED"})
                open_rows.remove(r)
                log.info("【隔夜·%s】%s %s %s | 买 %.2f → 卖 %.2f | %+.1f%% $%+.0f",
                         "影子平仓" if r["status"] == "SHADOW_CLOSED" else "平仓", r["position_id"], r["ticker"],
                         r["contract"], entry, bid, float(r["roi_pct"]), float(r["pnl_usd"]))
        if open_rows:
            time.sleep(60)
    if not dry:
        write_ledger(pday, rows)
    real = [r for r in rows if not r["status"].startswith("SHADOW")]
    shadow = [r for r in rows if r["status"].startswith("SHADOW")]
    total = sum(float(r["pnl_usd"] or 0) for r in real)
    stotal = sum(float(r["pnl_usd"] or 0) for r in shadow)
    if notifier and not dry:
        fmt = lambda r: (f"{'✅' if float(r['pnl_usd']) > 0 else '❌'} **{r['ticker']}** {float(r['strike']):g}{r['right']}"
                         f" ×{r.get('qty') or 1} ${float(r['entry_price']):.2f} → ${float(r['exit_price']):.2f}（{float(r['roi_pct']):+.0f}%，"
                         f"${float(r['pnl_usd']):+,.0f}）{r['exit_time']}")
        lines = [fmt(r) for r in real]
        if shadow:
            lines += ["", f"👻 被规则删掉的 {len(shadow)} 笔（影子仓，不计盈亏）：合计 ${stotal:+,.0f}"]
            lines += [fmt(r) + f" — {r.get('shadow_reason') or ''}" for r in shadow]
        notifier.send(embeds=[{"title": f"☀️ {pday} 隔夜单平仓：{len(real)} 笔，合计 ${total:+,.0f}",
                               "color": 0x2ECC71 if total >= 0 else 0xE74C3C, "description": "\n".join(lines),
                               "footer": {"text": "隔夜模拟盘 · 按实时 bid 卖出 · 09:35–09:45"}}])
    log.info("【隔夜】%s 的隔夜仓已全部平仓，合计 $%+.0f；影子仓 %d 笔合计 $%+.0f；配额剩余 %s", pday, total, len(shadow),
             stotal, client.quota_remaining)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="隔夜期权模拟实盘")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--open", action="store_true")
    g.add_argument("--close", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="不写账本、不发 Discord")
    ap.add_argument("--log-file")
    ap.add_argument("--tickers", help="只扫这些标的（测试用）")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    setup_logging(cfg.log_level, args.log_file)
    day = et_now().date()
    if not is_trading_day(day):
        log.info("%s 休市", day)
        return 0
    client = YHSClient(cfg)
    notifier = from_config(cfg)
    if args.open:
        return do_open(cfg, client, notifier, day, args.dry_run, args.tickers.split(",") if args.tickers else None)
    return do_close(cfg, client, notifier, day, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
