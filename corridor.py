"""
corridor.py —— 盘前「加速通道」形态：扫描、记录、次日验证、回测
================================================================
形态（看涨，看跌为镜像）：
    天花板 C  ++++++   显著正 GEX（目标）
              ------   ┐
              ------   │ 通道：以负 GEX 为主（做市商顺势加速）
     现价 →   ------   ┘
    地板 F    ++++++   显著正 GEX，现价略高于它（≤ 4%）
  价格站在地板上方，一旦上行，中间的负 GEX 会推着价格冲向天花板。例：IREN 2026-10-09 收盘，10/16 到期，
  地板 34(+0.44M)、通道 35–38 全负、天花板 39(+0.52M)、现价 35.21。

Heatmap 只在常规时段更新，收盘后一直停在最后一个快照，所以"盘前"状态 = 上一交易日收盘快照，前一晚扫描即可。

用法：
  python3 corridor.py --scan                 # 用当前快照扫描下一交易日的候选（每只 1 次配额），记录并推送 Discord
  python3 corridor.py --evaluate             # 用已存档的日内数据验证此前候选（0 配额）
  python3 corridor.py --backtest             # 对存档里的每个交易日 P，用 P 收盘快照找候选，再用 P 的下一交易日验证
  python3 corridor.py --scan --no-discord    # 不推送
记录：backtests/corridor/candidates.csv（每行一个候选，验证后补全结果列）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import sys

from archive import HEATMAP_HISTORY_PATH, load_archived_heatmap, rows_to_frames
from config import SystemConfig, et_datetime, next_trading_day, setup_logging
from yhs_client import QuotaExhausted, YHSApiError, YHSClient

log = logging.getLogger("yhs.corridor")
OUT_DIR = os.path.join("backtests", "corridor")
CSV_PATH = os.path.join(OUT_DIR, "candidates.csv")

FIELDS = ["session", "next_session", "ticker", "expiration", "direction", "spot", "base", "base_gex", "target",
          "target_gex", "dist_pct", "room_pct", "corridor_neg_gex", "corridor_pos_gex", "neg_share",
          "corridor_strikes", "max_abs_gex", "score", "source",
          # 次日验证（方向化：看涨为正、看跌取反；百分比）
          "open_ret", "ret_30m", "ret_60m", "close_ret", "mfe", "mae", "first_hour_ok",
          "hit_target", "broke_base", "first_event", "minutes_to_target"]


# =============================================================================
# 形态识别
# =============================================================================
def find_corridor(cells: list, spot: float, cfg: SystemConfig) -> dict | None:
    """cells：同一到期的 [{strike_usd, net_dealer_gex_usd}]。返回得分最高的看涨/看跌通道，或 None。"""
    pts = sorted(((float(c["strike_usd"]), float(c["net_dealer_gex_usd"])) for c in cells
                  if c.get("strike_usd") is not None and c.get("net_dealer_gex_usd") is not None), key=lambda x: x[0])
    if len(pts) < 4 or not spot:
        return None
    mx = max(abs(g) for _, g in pts) or 0.0
    if mx <= 0:
        return None
    sig = cfg.corridor_node_min_rel * mx
    out = []
    for direction in ("UP", "DOWN"):
        if direction == "UP":
            bases = [(k, g) for k, g in pts if k <= spot and g >= sig]
            if not bases:
                continue
            base = max(bases, key=lambda x: x[0])                      # 现价下方最近的显著正节点 = 地板
            tgt = next(((k, g) for k, g in pts if k > spot and g >= sig), None)   # 上方第一个显著正节点 = 天花板
            dist = (spot - base[0]) / spot
        else:
            bases = [(k, g) for k, g in pts if k >= spot and g >= sig]
            if not bases:
                continue
            base = min(bases, key=lambda x: x[0])                      # 现价上方最近的显著正节点 = 天花板
            tgt = next(((k, g) for k, g in reversed(pts) if k < spot and g >= sig), None)  # 下方第一个显著正节点 = 地板
            dist = (base[0] - spot) / spot
        if tgt is None or dist > cfg.corridor_floor_max_dist_pct:
            continue
        lo, hi = sorted((base[0], tgt[0]))
        corr = [(k, g) for k, g in pts if lo < k < hi]
        neg = sum(-g for _, g in corr if g < 0)
        pos = sum(g for _, g in corr if g > 0)
        if len(corr) < 2 or neg <= 0 or neg / (neg + pos) < cfg.corridor_neg_share_min:
            continue
        if not any(-g >= sig for _, g in corr):
            continue
        room = abs(tgt[0] - spot) / spot
        if not (cfg.corridor_min_room_pct <= room <= cfg.corridor_max_room_pct):
            continue
        # 质量分（0–1）：通道负 GEX 厚度、地板强度、目标强度、贴近地板、空间适中（3%–12% 最佳）
        room_s = 1.0 if 0.03 <= room <= 0.12 else max(0.0, 1 - (0.03 - room) / 0.01) if room < 0.03 \
            else max(0.0, 1 - (room - 0.12) / 0.13)
        score = (0.30 * min(neg / mx, 2.0) / 2.0 + 0.20 * base[1] / mx + 0.15 * tgt[1] / mx
                 + 0.20 * (1 - dist / cfg.corridor_floor_max_dist_pct) + 0.15 * room_s)
        out.append({"direction": direction, "spot": round(spot, 4), "base": base[0], "base_gex": round(base[1]),
                    "target": tgt[0], "target_gex": round(tgt[1]), "dist_pct": round(dist * 100, 3),
                    "room_pct": round(room * 100, 3), "corridor_neg_gex": round(-neg), "corridor_pos_gex": round(pos),
                    "neg_share": round(neg / (neg + pos), 3),
                    "corridor_strikes": " ".join(f"{k:g}" for k, _ in corr), "max_abs_gex": round(mx),
                    "score": round(score, 3)})
    return max(out, key=lambda x: x["score"]) if out else None


def scan_expirations(listed: list, next_session: dt.date) -> list:
    """要看的到期：下一交易日起最近的到期 + 最近的周五到期（若不同）。"""
    exps = sorted(dt.date.fromisoformat(e) for e in listed if e and dt.date.fromisoformat(e) >= next_session)
    if not exps:
        return []
    out = [exps[0]]
    fri = next((e for e in exps if e.weekday() == 4), None)
    if fri and fri != exps[0]:
        out.append(fri)
    return [e.isoformat() for e in out]


def best_for_ticker(ticker: str, session: dt.date, nxt: dt.date, spot: float, cells_by_exp: dict,
                    cfg: SystemConfig, source: str) -> dict | None:
    best = None
    for exp, cells in cells_by_exp.items():
        c = find_corridor(cells, spot, cfg)
        if c and (best is None or c["score"] > best["score"]):
            best = dict(c, expiration=exp)
    if best:
        best.update(session=session.isoformat(), next_session=nxt.isoformat(), ticker=ticker, source=source)
    return best


# =============================================================================
# 数据：当前快照 / 历史收盘快照 / 次日日内现货
# =============================================================================
def universe(cfg: SystemConfig) -> list:
    with open(os.path.join(cfg.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
        return json.load(f)["tickers"]


def close_cells(client, cfg: SystemConfig, ticker: str, session: dt.date, exp: str):
    """某交易日收盘时某到期的 (spot, cells)；返回 None 表示该到期不存在/无数据。存档优先，否则只拉收盘前 10 分钟（1 次配额）并缓存。"""
    rows = load_archived_heatmap(cfg, session, ticker, exp)
    if rows is None:
        cp = os.path.join(cfg.data_dir, "cache", "heatmap_close", session.isoformat(), ticker, f"{exp}.json")
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                rows = json.load(f)
        else:
            payload = client.get(HEATMAP_HISTORY_PATH.format(ticker=ticker), {
                "expiration": exp, "from": et_datetime(session, "15:45").isoformat(),
                "to": et_datetime(session, "16:00").isoformat(), "limit": 24})
            rows = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), list) else []
            os.makedirs(os.path.dirname(cp), exist_ok=True)
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(rows, f)
    rows = sorted((r for r in rows if r.get("cells")), key=lambda r: r.get("minute_at") or "")
    if not rows:
        return None
    last = rows[-1]
    return float(last["spot_usd"]), [{"strike_usd": c["strike_usd"], "net_dealer_gex_usd": c["net_dealer_gex_usd"]}
                                     for c in last["cells"]]


def session_spots(cfg: SystemConfig, ticker: str, day: dt.date) -> list:
    """某交易日的 5 分钟现货序列 [(美东时间, spot)]，取该标的当日任一存档到期文件（0 配额）。"""
    d = os.path.join(cfg.data_dir, "archive", "heatmap", day.isoformat(), ticker)
    if not os.path.isdir(d):
        return []
    for fn in sorted(os.listdir(d)):
        rows = load_archived_heatmap(cfg, day, ticker, fn[:-5])
        if rows:
            frames = rows_to_frames(ticker, day, fn[:-5], rows)
            return sorted((t, float(f["data"]["spot_usd"])) for t, f in frames.items() if f["data"]["spot_usd"])
    return []


def evaluate(cand: dict, spots: list, buffer_pct: float = 0.005) -> dict:
    """用次日现货验证：开盘、30/60 分钟、收盘方向化收益；是否先到目标 / 先破地板（看跌为天花板）。
    破位需穿过基准再多 buffer_pct（看涨：低于 地板×(1−buffer)；看跌：高于 天花板×(1+buffer)）。"""
    if not spots:
        return {}
    sgn = 1 if cand["direction"] == "UP" else -1
    prev = float(cand["spot"])
    target = float(cand["target"])
    base = float(cand["base"]) * (1 - sgn * buffer_pct)
    t0 = spots[0][0]

    def ret(px):
        return round(sgn * (px / prev - 1) * 100, 3)

    def at(minutes):
        pts = [s for t, s in spots if (t - t0).total_seconds() <= minutes * 60]
        return ret(pts[-1]) if pts else None
    moves = [ret(s) for _, s in spots]
    first, mins = "none", None
    for t, s in spots:
        hit_t = s >= target if sgn > 0 else s <= target
        broke = s < base if sgn > 0 else s > base
        if hit_t or broke:
            first, mins = ("target" if hit_t else "broke_base"), int((t - t0).total_seconds() // 60)
            break
    r60 = at(60)
    return {"open_ret": ret(spots[0][1]), "ret_30m": at(30), "ret_60m": r60, "close_ret": ret(spots[-1][1]),
            "mfe": max(moves), "mae": min(moves), "first_hour_ok": int(r60 is not None and r60 > 0),
            "hit_target": int(any((s >= target if sgn > 0 else s <= target) for _, s in spots)),
            "broke_base": int(any((s < base if sgn > 0 else s > base) for _, s in spots)),
            "first_event": first, "minutes_to_target": mins if first == "target" else ""}


# =============================================================================
# 记录
# =============================================================================
def load_csv() -> list:
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_csv(rows: list) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["session"], -float(r["score"] or 0)))
    with open(CSV_PATH + ".tmp", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(CSV_PATH + ".tmp", CSV_PATH)


def upsert(rows: list, new: list) -> list:
    key = lambda r: (r["session"], r["ticker"])
    idx = {key(r): i for i, r in enumerate(rows)}
    for n in new:
        n = {k: ("" if v is None else v) for k, v in n.items()}
        if key(n) in idx:
            rows[idx[key(n)]].update(n)
        else:
            idx[key(n)] = len(rows)
            rows.append(n)
    return rows


def evaluate_pending(cfg: SystemConfig, rows: list) -> list:
    """给还没有结果、且次日已存档的候选补上验证结果。返回本次新验证的行。"""
    done = []
    for r in rows:
        if r.get("first_event"):
            continue
        spots = session_spots(cfg, r["ticker"], dt.date.fromisoformat(r["next_session"]))
        if spots:
            r.update({k: ("" if v is None else v) for k, v in evaluate(r, spots, cfg.corridor_break_buffer_pct).items()})
            done.append(r)
    return done


def summarize(rows: list) -> dict:
    ev = [r for r in rows if r.get("first_event")]
    if not ev:
        return {"n": 0}
    f = lambda k: [float(r[k]) for r in ev if r.get(k) not in ("", None)]
    n = len(ev)
    return {"n": n, "first_hour_ok": sum(int(r["first_hour_ok"]) for r in ev),
            "hit_target": sum(int(r["hit_target"]) for r in ev),
            "target_first": sum(r["first_event"] == "target" for r in ev),
            "broke_first": sum(r["first_event"] == "broke_base" for r in ev),
            "avg_60m": sum(f("ret_60m")) / max(1, len(f("ret_60m"))),
            "avg_close": sum(f("close_ret")) / max(1, len(f("close_ret"))),
            "avg_mfe": sum(f("mfe")) / max(1, len(f("mfe")))}


# =============================================================================
# 运行模式
# =============================================================================
def run_scan(cfg: SystemConfig, client) -> tuple:
    """当前快照 = 最近一个交易日的收盘状态，扫描下一交易日的候选。"""
    found, session = [], None
    for t in universe(cfg):
        try:
            p = client.get(f"/v1/derived/heatmap/{t}/snapshot")
        except QuotaExhausted:
            raise
        except YHSApiError as e:
            log.warning("%s 快照失败：%s", t, e)
            continue
        d = p.get("data") or {}
        if not d.get("spot_usd") or not d.get("session_date_et"):
            continue
        session = dt.date.fromisoformat(d["session_date_et"])
        nxt = next_trading_day(session)
        exps = scan_expirations(d.get("expirations") or [], nxt)
        by_exp = {e: [c for c in d.get("cells") or [] if c.get("expiration") == e] for e in exps}
        c = best_for_ticker(t, session, nxt, float(d["spot_usd"]), by_exp, cfg, "snapshot")
        if c:
            found.append(c)
            log.info("候选 %s %s 现价 %.2f 基准 %g → 目标 %g（%+.1f%%，到期 %s，分 %.2f）", t, c["direction"], c["spot"],
                     c["base"], c["target"], c["room_pct"] * (1 if c["direction"] == "UP" else -1), c["expiration"], c["score"])
    return session, found


def run_backtest(cfg: SystemConfig, client) -> list:
    """对存档里每个交易日 P（其下一交易日 S 也已存档），用 P 收盘快照找候选、用 S 验证。"""
    root = os.path.join(cfg.data_dir, "archive", "heatmap")
    days = sorted(d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "_manifest.json")))
    out = []
    for p_str in days:
        P = dt.date.fromisoformat(p_str)
        S = next_trading_day(P)
        if S.isoformat() not in days:
            continue
        log.info("===== 回测：%s 收盘 → %s =====", P, S)
        for t in universe(cfg):
            by_exp, spot = {}, None
            # 最近到期（S 起逐个交易日探测）+ 最近周五
            d, nearest = S, None
            for _ in range(10):
                cc = close_cells(client, cfg, t, P, d.isoformat())
                if cc:
                    nearest = d
                    spot, by_exp[d.isoformat()] = cc[0], cc[1]
                    break
                d = next_trading_day(d)
            fri = S + dt.timedelta(days=(4 - S.weekday()) % 7)
            if nearest and fri != nearest and fri > nearest:
                cc = close_cells(client, cfg, t, P, fri.isoformat())
                if cc:
                    by_exp[fri.isoformat()] = cc[1]
            if not by_exp:
                continue
            c = best_for_ticker(t, P, S, spot, by_exp, cfg, "archive")
            if c:
                c.update(evaluate(c, session_spots(cfg, t, S), cfg.corridor_break_buffer_pct))
                out.append(c)
    return out


def post_discord(cfg: SystemConfig, session, found: list, evaluated: list, all_rows: list) -> None:
    from notify import from_config
    n = from_config(cfg)
    if not n:
        return
    if evaluated:
        s = summarize(evaluated)
        lines = [f"`{r['ticker']:<5}{'↑' if r['direction'] == 'UP' else '↓'} 首小时 {float(r['ret_60m'] or 0):+.1f}%  "
                 f"收盘 {float(r['close_ret'] or 0):+.1f}%  {'🎯到达目标' if r['first_event'] == 'target' else ('💥先破基准' if r['first_event'] == 'broke_base' else '—')}`"
                 for r in sorted(evaluated, key=lambda r: -float(r['score']))][:20]
        n.send(embeds=[{"title": f"🔍 昨晚加速通道候选验证（{evaluated[0]['next_session']}）：{s['n']} 个",
                        "color": 0x95A5A6,
                        "description": f"首小时方向正确 **{s['first_hour_ok']}/{s['n']}**｜当天到达目标 **{s['hit_target']}/{s['n']}**"
                                       f"（先到目标 {s['target_first']}、先破基准 {s['broke_first']}）\n"
                                       f"平均：首小时 {s['avg_60m']:+.2f}%｜收盘 {s['avg_close']:+.2f}%｜最大顺行 {s['avg_mfe']:+.2f}%\n"
                                       + "\n".join(lines)}])
    if session is None:
        return
    top = sorted(found, key=lambda c: -c["score"])[:cfg.corridor_top_n]
    nxt = top[0]["next_session"] if top else next_trading_day(session).isoformat()
    if not top:
        n.send(f"🛫 盘前加速通道（{nxt}）：今晚没有符合条件的标的。")
        return
    lines = []
    for c in top:
        up = c["direction"] == "UP"
        lines.append(f"**{c['ticker']}** {'↑' if up else '↓'} 现价 {c['spot']:.2f}｜{'地板' if up else '天花板'} {c['base']:g}"
                     f"（{c['base_gex'] / 1e6:+.2f}M）｜通道 {c['corridor_strikes'].split()[0]}–{c['corridor_strikes'].split()[-1]} 负GEX"
                     f"｜目标 {c['target']:g}（{c['room_pct'] * (1 if up else -1):+.1f}%）｜到期 {c['expiration'][5:]}｜分 {c['score']:.2f}")
    n.send(embeds=[{"title": f"🛫 盘前加速通道（{nxt} 开盘）：{len(found)} 个候选，前 {len(top)}",
                    "color": 0x9B59B6, "description": "\n".join(lines)[:4000],
                    "footer": {"text": f"基于 {session} 收盘 Heatmap · 地板/天花板 = 显著正 GEX，通道 = 负 GEX 为主 · 仅供研究"}}])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="盘前加速通道形态")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true")
    mode.add_argument("--evaluate", action="store_true")
    mode.add_argument("--backtest", action="store_true")
    ap.add_argument("--no-discord", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--log-file")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(args.config)
    setup_logging(cfg.log_level, args.log_file)
    client = YHSClient(cfg)
    rows = load_csv()

    if args.backtest:
        res = run_backtest(cfg, client)
        rows = upsert(rows, res)
        save_csv(rows)
        s = summarize(res)
        log.info("回测：%d 个候选，%s", len(res), s)
        log.info(client.quota_summary())
        return 0

    evaluated = evaluate_pending(cfg, rows)
    session, found = (None, [])
    if args.scan:
        session, found = run_scan(cfg, client)
        rows = upsert(rows, found)
    save_csv(rows)
    log.info("验证 %d 个历史候选；扫描到 %d 个新候选。%s", len(evaluated), len(found), client.quota_summary())
    if not args.no_discord:
        post_discord(cfg, session, found, evaluated, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
