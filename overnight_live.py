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
  5. 每晚最多 10 笔，按 15:00 后同方向大单权利金合计从大到小排
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

from archive import fetch_heatmap_rows, live_universe, rows_to_frames
from config import (SystemConfig, et_datetime, et_now, expiry_candidates, is_trading_day, next_trading_day,
                    parse_iso, prev_trading_day, setup_logging, to_et)
from detector import FLOW_TRADES_PATH, detect_structure, extract_rows, parse_heatmap
from main import QuoteBook
from notify import from_config
from paper_trader import occ_symbol
from yhs_client import YHSClient

log = logging.getLogger("yhs.overnight_live")
LIVE_DIR = "live"
MAX_POSITIONS = 10
BUDGET, BUDGET_BAND = 1000.0, 0.25   # 每笔目标投入 $1,000，允许 $750–$1,250（2026-10-16 用户定）


def size_contracts(price: float) -> int:
    """按每笔 $1,000（±25%）算张数：取落在 $750–$1,250 内、最接近 $1,000 的张数；单张就超过 $1,250 返回 0（不开）。"""
    per = price * 100
    if per <= 0:
        return 0
    lo, hi = BUDGET * (1 - BUDGET_BAND), BUDGET * (1 + BUDGET_BAND)
    best = 0
    for n in range(1, int(hi // per) + 1):
        if lo <= n * per <= hi and (not best or abs(n * per - BUDGET) < abs(best * per - BUDGET)):
            best = n
    return best
FLOW_MIN_PREMIUM = 50000
FLOW_SINCE = "15:00"   # 只看尾盘的同向大单
MAX_SPREAD = 0.25      # (ask - bid) / 中间价 超过 25% 不开：一买一卖先亏掉价差
PRESCAN_START, OPEN_DEADLINE = "15:15", "15:57"
BAR1, BAR2 = "15:40", "15:45"   # 两根确认用的 Heatmap；标记 T 的段约 T+5:10 才可用
SCAN1_AT, SCAN2_AT = dt.timedelta(hours=15, minutes=45, seconds=15), dt.timedelta(hours=15, minutes=50, seconds=15)
FLOW_UNTIL = "15:50"   # 大单统计截止（与回测一致）
SELL_START, SELL_DEADLINE = "09:35", "09:45"
FIELDS = ["position_id", "open_date", "ticker", "structure", "node_mode", "right", "contract", "expiration", "strike", "qty",
          "spot", "pos_node", "neg_node", "dist_pct", "flow_n", "flow_usd", "flow_max", "entry_time", "entry_bid",
          "entry_ask", "entry_price", "exit_time", "exit_bid", "exit_price", "exit_note", "pnl_usd", "roi_pct", "status"]


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
    exps, early, daily = {}, [], daily_expiry_tickers(cfg)
    for t in tickers:
        exp, raw = pick_expiry(client, t, day, predicted_expiry(t, day, daily))
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
    log.info("【隔夜·候选】结构 + 大单都满足 %d 个：%s", len(cands), "、".join(c[1] for c in cands) or "无")
    rows, quotes = [], QuoteBook(client)
    for usd, t, exp, snap, st, mode, fl in cands:
        if len(rows) >= MAX_POSITIONS:
            break
        K = first_otm(snap, st.right)
        if K is None:
            continue
        sym = occ_symbol(t, exp, st.right, K)
        q = quotes.get(sym)
        if not q or q["ask"] <= 0 or q["bid"] <= 0:
            log.warning("【隔夜·放弃】%s %s 没有报价或 bid 为 0，不开", t, sym)
            continue
        if q["ask"] < cfg.min_option_price:
            continue
        spread = (q["ask"] - q["bid"]) / ((q["ask"] + q["bid"]) / 2)
        if spread > MAX_SPREAD:
            log.warning("【隔夜·放弃】%s %s 买卖价差 %.0f%%（bid %.2f / ask %.2f）> %.0f%%，不开", t, sym, spread * 100,
                        q["bid"], q["ask"], MAX_SPREAD * 100)
            continue
        qty = size_contracts(q["ask"])
        if not qty:
            log.warning("【隔夜·放弃】%s %s 单张 $%.0f 超过每笔上限 $%.0f，不开", t, sym, q["ask"] * 100, BUDGET * (1 + BUDGET_BAND))
            continue
        rows.append({"position_id": f"ON{day.strftime('%Y%m%d')}-{len(rows) + 1:02d}", "open_date": day.isoformat(), "qty": qty,
                     "ticker": t, "structure": st.structure, "node_mode": mode, "right": st.right, "contract": sym,
                     "expiration": exp, "strike": K, "spot": round(snap.spot, 2), "pos_node": st.pos_strike,
                     "neg_node": st.neg_strike, "dist_pct": round(st.distance_pct * 100, 2), "flow_n": fl["n"],
                     "flow_usd": fl["usd"], "flow_max": fl["max"], "entry_time": et_now().strftime("%H:%M"),
                     "entry_bid": q["bid"], "entry_ask": q["ask"], "entry_price": q["ask"], "status": "OPEN"})
        log.info("【隔夜·开仓】%s %s %s × %d 张 | ask %.2f（bid %.2f）投入 $%.0f | 大单 %d 笔 $%.0fK", rows[-1]["position_id"],
                 t, sym, qty, q["ask"], q["bid"], q["ask"] * 100 * qty, fl["n"], fl["usd"] / 1e3)
    if not dry:
        write_ledger(day, rows)
    if notifier and not dry:
        lines = [f"{'🟢 Call' if r['right'] == 'C' else '🔴 Put'} **{r['ticker']}** {r['strike']:g}{r['right']} {r['expiration'][5:]}"
                 f" ×{r['qty']} @ ${r['entry_price']:.2f}（${r['entry_price'] * 100 * r['qty']:,.0f}）| {'局部' if r['node_mode'] == 'local' else '全局'}"
                 f"{'跳板' if r['structure'] == 'SPRINGBOARD' else '悬崖'} {r['pos_node']:g}→{r['neg_node']:g}"
                 f" | 大单 {r['flow_n']} 笔 ${r['flow_usd'] / 1e3:,.0f}K" for r in rows]
        notifier.send(embeds=[{"title": f"🌙 {day} 隔夜单开仓 {len(rows)} 笔（次日 09:35–09:45 跳空平仓）",
                               "color": 0x5865F2, "description": "\n".join(lines) or "今晚没有同时满足结构 + 大单的标的，不开仓",
                               "footer": {"text": "隔夜模拟盘 · 每笔 1 张 · 按实时 ask 买入 · 与日内账户分开记账"}}])
    log.info("【隔夜】开仓 %d 笔；配额剩余 %s", len(rows), client.quota_remaining)
    return 0


def do_close(cfg, client, notifier, day: dt.date, dry: bool) -> int:
    pday = prev_trading_day(day)
    if not os.path.exists(ledger_path(pday)):
        log.info("【隔夜·平仓】%s 没有隔夜仓（%s 不存在）", day, ledger_path(pday))
        return 0
    with open(ledger_path(pday), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    open_rows = [r for r in rows if r["status"] == "OPEN"]
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
                          "status": "CLOSED"})
                open_rows.remove(r)
                log.info("【隔夜·平仓】%s %s %s | 买 %.2f → 卖 %.2f | %+.1f%% $%+.0f", r["position_id"], r["ticker"],
                         r["contract"], entry, bid, float(r["roi_pct"]), float(r["pnl_usd"]))
        if open_rows:
            time.sleep(60)
    if not dry:
        write_ledger(pday, rows)
    total = sum(float(r["pnl_usd"] or 0) for r in rows)
    if notifier and not dry:
        lines = [f"{'✅' if float(r['pnl_usd']) > 0 else '❌'} **{r['ticker']}** {float(r['strike']):g}{r['right']}"
                 f" ×{r.get('qty') or 1} ${float(r['entry_price']):.2f} → ${float(r['exit_price']):.2f}（{float(r['roi_pct']):+.0f}%，"
                 f"${float(r['pnl_usd']):+,.0f}）{r['exit_time']}" for r in rows]
        notifier.send(embeds=[{"title": f"☀️ {pday} 隔夜单平仓：{len(rows)} 笔，合计 ${total:+,.0f}",
                               "color": 0x2ECC71 if total >= 0 else 0xE74C3C, "description": "\n".join(lines),
                               "footer": {"text": "隔夜模拟盘 · 按实时 bid 卖出 · 09:35–09:45"}}])
    log.info("【隔夜】%s 的隔夜仓已全部平仓，合计 $%+.0f；配额剩余 %s", pday, total, client.quota_remaining)
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
