"""
signal_log.py —— 记录所有信号（排序研究第 1 步）
================================================
只读本地 Heatmap 存档，0 配额。对每只标的每个 5 分钟快照：
  * 按回测同样的规则选到期（有 0DTE 用 0DTE，否则最近到期；受 expiry_max_trading_dte 限制）；
  * 识别跳板/悬崖结构（与 detector.detect_structure 完全相同），出现即记一行——
    不管是否会被持仓上限、冷却期、疲劳度挡掉，这样结果不受"谁先占到仓位"的影响；
  * 记录候选排序特征 + 用之后的现货价格算出的后续表现（方向化：看涨为正、看跌取反）。

后续表现（只用现货，5 分钟粒度）：
  fwd_15/30/60     之后 15/30/60 分钟的方向化收益 %
  mfe_60/mae_60    60 分钟内最大顺行 / 最大逆行 %
  outcome          先到目标（负节点）= target，先跌破支撑（正节点被反向穿越）= stop，收盘前都没有 = none
  minutes_to_outcome

用法：
  python3 signal_log.py                       # 全部存档日期 × Heatmap 大样本标的池
  python3 signal_log.py --universe config     # 只用 config.archive_tickers
  python3 signal_log.py --report-only         # 不重新扫描，只根据已有 CSV 出报告
输出：backtests/signals/signals.csv、backtests/signals/report.md
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys

from archive import load_archived_heatmap, rows_to_frames
from config import KNOWN_ETFS, SystemConfig, expiry_candidates, hhmm_to_minutes, minutes_of_day, trading_dte
from detector import NodeFatigueTracker, detect_structure, parse_heatmap

OUT_DIR = os.path.join("backtests", "signals")
FWD_STEPS = {"fwd_15": 3, "fwd_30": 6, "fwd_60": 12}

FIELDS = ["day", "time", "ticker", "is_etf", "structure", "right", "expiration", "trading_dte", "spot",
          "pos_strike", "neg_strike", "dist_pos_pct", "room_pct", "room_vol", "pos_gex", "neg_gex",
          "pos_share", "neg_share", "pos_neg_ratio", "total_abs_gex", "tests", "age", "is_new",
          "session", "in_entry_window",
          "fwd_15", "fwd_30", "fwd_60", "mfe_60", "mae_60", "outcome", "minutes_to_outcome"]


def session_of(t: dt.datetime) -> str:
    m = minutes_of_day(t)
    if m < hhmm_to_minutes("11:30"):
        return "早盘"
    if m < hhmm_to_minutes("14:30"):
        return "午间"
    return "尾盘"


def pick_frames(cfg: SystemConfig, day: dt.date, ticker: str) -> tuple[str | None, dict]:
    """与历史回放相同的到期选择，但只读存档：遇到没存档的候选到期就停止（不调用 API）。"""
    for exp in expiry_candidates(day, cfg.expiry_max_trading_dte, cfg.expiry_search_trading_days):
        rows = load_archived_heatmap(cfg, day, ticker, exp)
        if rows is None:
            return None, {}
        if rows:
            return exp, rows_to_frames(ticker, day, exp, rows)
    return None, {}


def directional(ret: float, right: str) -> float:
    return ret if right == "C" else -ret


def scan_ticker_day(cfg: SystemConfig, day: dt.date, ticker: str) -> list[dict]:
    exp, frames = pick_frames(cfg, day, ticker)
    if not frames:
        return []
    times = sorted(frames)
    spots = [float(frames[t]["data"]["spot_usd"] or 0) for t in times]
    fatigue = NodeFatigueTracker(cfg)
    out, prev_struct, age = [], None, 0
    entry_lo, entry_hi = hhmm_to_minutes(cfg.no_new_entries_before), hhmm_to_minutes(cfg.no_new_entries_after)
    for i, t in enumerate(times):
        snap = parse_heatmap(frames[t], cfg, day)
        if snap is None or snap.spot <= 0:
            prev_struct, age = None, 0
            continue
        tests = fatigue.update(ticker, snap.king_call_strike, snap.spot)
        st = detect_structure(snap, cfg)
        if st is None:
            prev_struct, age = None, 0
            continue
        key = (st.structure, st.pos_strike, st.neg_strike)
        age = age + 1 if key == prev_struct else 1
        prev_struct = key

        cells = [c for c in frames[t]["data"]["cells"] if c.get("net_dealer_gex_usd") is not None]
        total = sum(abs(float(c["net_dealer_gex_usd"])) for c in cells) or float("nan")
        # 已实现波动：当日此前 5 分钟收益的标准差，换算成 1 小时
        past = [s for s in spots[:i + 1] if s > 0]
        rets = [math.log(b / a) for a, b in zip(past, past[1:])]
        vol_1h = (math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(12) * 100) if len(rets) >= 6 else None
        room = abs(st.neg_strike - snap.spot) / snap.spot * 100

        row = {
            "day": day.isoformat(), "time": t.strftime("%H:%M"), "ticker": ticker,
            "is_etf": int(ticker in KNOWN_ETFS), "structure": st.structure, "right": st.right,
            "expiration": exp, "trading_dte": trading_dte(dt.date.fromisoformat(exp), day), "spot": snap.spot,
            "pos_strike": st.pos_strike, "neg_strike": st.neg_strike,
            "dist_pos_pct": round(st.distance_pct * 100, 4), "room_pct": round(room, 4),
            "room_vol": round(room / vol_1h, 3) if vol_1h else None,
            "pos_gex": snap.king_call_gex, "neg_gex": snap.king_put_gex,
            "pos_share": round(snap.king_call_gex / total, 4), "neg_share": round(abs(snap.king_put_gex) / total, 4),
            "pos_neg_ratio": round(snap.king_call_gex / abs(snap.king_put_gex), 4) if snap.king_put_gex else None,
            "total_abs_gex": round(total), "tests": tests, "age": age, "is_new": int(age == 1),
            "session": session_of(t), "in_entry_window": int(entry_lo <= minutes_of_day(t) < entry_hi),
        }
        # 后续表现（方向化）
        for name, k in FWD_STEPS.items():
            j = i + k
            row[name] = round(directional((spots[j] / snap.spot - 1) * 100, st.right), 4) \
                if j < len(spots) and spots[j] > 0 else None
        window = [s for s in spots[i + 1:i + 13] if s > 0]
        moves = [directional((s / snap.spot - 1) * 100, st.right) for s in window]
        row["mfe_60"] = round(max(moves), 4) if moves else None
        row["mae_60"] = round(min(moves), 4) if moves else None
        outcome, mins = "none", None
        for j in range(i + 1, len(spots)):
            s = spots[j]
            if s <= 0:
                continue
            hit_target = s >= st.neg_strike if st.right == "C" else s <= st.neg_strike
            hit_stop = s < st.pos_strike if st.right == "C" else s > st.pos_strike
            if hit_target or hit_stop:
                outcome = "target" if hit_target else "stop"
                mins = int((times[j] - t).total_seconds() // 60)
                break
        row["outcome"], row["minutes_to_outcome"] = outcome, mins
        out.append(row)
    return out


# =============================================================================
# 报告：每个特征分档，看哪一档后续表现更好
# =============================================================================
FEATURES = [("dist_pos_pct", "现货距正节点 %"), ("room_pct", "到目标（负节点）空间 %"),
            ("room_vol", "目标空间 / 1 小时波动"), ("pos_share", "正节点 GEX 占比"), ("neg_share", "负节点 GEX 占比"),
            ("pos_neg_ratio", "正/负节点 GEX 比"), ("total_abs_gex", "全链 |GEX| 总量"), ("tests", "节点测试次数"),
            ("age", "结构已持续根数"), ("trading_dte", "到期 DTE")]
CATS = [("session", "时段"), ("structure", "结构"), ("is_etf", "ETF")]


def _f(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    f30 = [x for x in (_f(r["fwd_30"]) for r in rows) if x is not None]
    mfe = [x for x in (_f(r["mfe_60"]) for r in rows) if x is not None]
    tgt = sum(r["outcome"] == "target" for r in rows)
    stp = sum(r["outcome"] == "stop" for r in rows)
    return {"n": n, "tickers": len({r["ticker"] for r in rows}),
            "fwd30": sum(f30) / len(f30) if f30 else None,
            "win30": 100 * sum(x > 0 for x in f30) / len(f30) if f30 else None,
            "mfe60": sum(mfe) / len(mfe) if mfe else None,
            "target_pct": 100 * tgt / n if n else None, "stop_pct": 100 * stp / n if n else None,
            "t_minus_s": 100 * (tgt - stp) / n if n else None}


def fmt(v, nd=3, pct=False):
    if v is None:
        return "—"
    return f"{v:+.{nd}f}" if not pct else f"{v:.0f}%"


def table(groups: list[tuple[str, list[dict]]]) -> list[str]:
    lines = ["| 档位 | 信号数 | 标的数 | 30 分钟方向收益均值 % | 30 分钟胜率 | 60 分钟 MFE 均值 % | 先到目标 | 先破支撑 | 目标−破位 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for label, rs in groups:
        s = stats(rs)
        lines.append(f"| {label} | {s['n']} | {s['tickers']} | {fmt(s['fwd30'])} | {fmt(s['win30'], pct=True)} | "
                     f"{fmt(s['mfe60'])} | {fmt(s['target_pct'], pct=True)} | {fmt(s['stop_pct'], pct=True)} | "
                     f"{fmt(s['t_minus_s'], 0)} |")
    return lines


def quantile_groups(rows: list[dict], key: str, q: int = 5) -> list[tuple[str, list[dict]]]:
    vals = sorted(((_f(r[key]), r) for r in rows if _f(r[key]) is not None), key=lambda x: x[0])
    if len(vals) < q * 5:
        return []
    distinct = sorted({v for v, _ in vals})
    if len(distinct) <= q:   # 离散特征（如测试次数、DTE）直接按取值分组
        return [(f"= {d:g}", [r for v, r in vals if v == d]) for d in distinct]
    out = []
    for k in range(q):
        part = vals[k * len(vals) // q:(k + 1) * len(vals) // q]
        if part:
            out.append((f"Q{k + 1}（{part[0][0]:.3g} ~ {part[-1][0]:.3g}）", [r for _, r in part]))
    return out


def write_report(rows: list[dict], path: str, meta: dict) -> None:
    new = [r for r in rows if str(r["is_new"]) == "1"]
    tradable = [r for r in new if str(r["in_entry_window"]) == "1"]
    lines = ["# 信号记录报告（排序研究第 1 步）", "",
             f"- 生成时间：{meta['generated_at']}；日期：{', '.join(meta['dates'])}；标的池：{meta['universe']}（{meta['n_tickers']} 只，"
             f"有可用存档 {meta['n_with_data']} 只）",
             f"- 到期规则：{meta['expiry_rule']}；数据：只用本地存档的现货与 GEX（0 配额），5 分钟粒度",
             f"- 结构出现的快照共 {len(rows)} 个；**新结构事件（结构刚形成的那一根）{len(new)} 个**，"
             f"其中在开仓时间窗（{meta['entry_window']}）内 {len(tradable)} 个。以下统计均基于开仓时间窗内的新结构事件。",
             "- 指标均已方向化（看涨信号用上涨为正，看跌信号用下跌为正）。「先到目标」= 现货先到负节点；「先破支撑」= 现货先反向穿越正节点。",
             "- ⚠️ 样本只有几天，同一天的信号高度相关（大盘一起动），分档差异很可能是噪音，只用于建立假设。", "",
             "## 总体", ""] + table([("全部", tradable)]) + [""]
    for key, label in CATS:
        order = {"早盘": 0, "午间": 1, "尾盘": 2}
        vals = sorted({str(r[key]) for r in tradable}, key=lambda v: (order.get(v, 9), v))
        lines += [f"## 按{label}", ""] + table([(v, [r for r in tradable if str(r[key]) == v]) for v in vals]) + [""]
    lines += ["## 按日期", ""] + table([(d, [r for r in tradable if r["day"] == d]) for d in meta["dates"]]) + [""]
    for key, label in FEATURES:
        g = quantile_groups(tradable, key)
        if g:
            lines += [f"## {label}（`{key}`）", ""] + table(g) + [""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="记录所有信号与后续表现（0 配额）")
    ap.add_argument("--universe", choices=["config", "heatmap"], default="heatmap")
    ap.add_argument("--dates", help="逗号分隔，默认全部已存档日期")
    ap.add_argument("--config", help="JSON 配置文件")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(args.config)
    os.makedirs(OUT_DIR, exist_ok=True)
    csv_path, meta_path = os.path.join(OUT_DIR, "signals.csv"), os.path.join(OUT_DIR, "meta.json")

    if args.report_only:
        with open(csv_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    else:
        if args.universe == "heatmap":
            with open(os.path.join(cfg.data_dir, "archive", "universe", "heatmap_filtered.json"), encoding="utf-8") as f:
                tickers = json.load(f)["tickers"]
        else:
            tickers = list(cfg.archive_tickers)
        root = os.path.join(cfg.data_dir, "archive", "heatmap")
        dates = args.dates.split(",") if args.dates else sorted(
            d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "_manifest.json")))
        rows, with_data = [], set()
        for d in dates:
            day = dt.date.fromisoformat(d)
            for t in tickers:
                rs = scan_ticker_day(cfg, day, t)
                if rs or pick_frames(cfg, day, t)[1]:
                    with_data.add(t)
                rows += rs
            print(f"{d}: 累计 {len(rows)} 行")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        cap = cfg.expiry_max_trading_dte
        meta = {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "dates": dates,
                "universe": args.universe, "n_tickers": len(tickers), "n_with_data": len(with_data),
                "expiry_rule": "0DTE 否则最近到期" if cap is None else f"0DTE 否则 ≤{cap} DTE",
                "entry_window": f"{cfg.no_new_entries_before}–{cfg.no_new_entries_after}"}
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        rows = [{k: ("" if v is None else v) for k, v in r.items()} for r in rows]
    write_report(rows, os.path.join(OUT_DIR, "report.md"), meta)
    print(f"已写出 {csv_path}（{len(rows)} 行）与 {os.path.join(OUT_DIR, 'report.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
