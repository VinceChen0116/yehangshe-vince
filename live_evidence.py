"""
live_evidence.py —— 每日实盘证据记录（0 配额：只读实盘交易明细 + 当天 Heatmap 收盘存档）
=====================================================================================
1. 实盘开平仓逐笔（live/trades_<日期>*.csv）。
2. 逼空 B / C / 燃料三条规则当天拦下的信号：用存档把 B / C / 燃料关掉再判一遍，多出来的信号就是被拦下的；
   再看信号之后的现价（Heatmap 现价，5 分钟一根）先碰到 Call 墙（目标）还是先跌破 −0.8% 现货止损，或收盘都没碰到。
   每只票每个规则 30 分钟内只记第一次。
3. 候选规则（还没加）在实盘单上的触发情况：A 悬崖当天已跌 >1%、悬崖当天已涨 >1%（逆势）、逼空当天涨 >5%。
结果追加到 docs/LIVE_EVIDENCE.md。用法：python3 live_evidence.py [--date 2026-10-21]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import logging
import os
import sys

import baseline as B
from archive import live_universe
from baseline_frames import merged_frames
from config import SystemConfig, et_now

OUT = os.path.join("docs", "LIVE_EVIDENCE.md")


def day_frames(cfg, day, tk):  # noqa: D401
    j = B.BaselineJudge(cfg)
    out = []
    for t, p in merged_frames(cfg, day, tk).items():
        f, _, _ = j.observe(tk, p, t, day)
        if f is not None:
            out.append(f)
    return out


def blocked_by(j, tk, snap, now):
    """三条全开时没有逼空信号、三条全关就有逼空信号（含评分 ≥55、衰竭拦截）→ 返回 (拦它的规则, 信号, 全开时的原因)。
    "拦它的规则" = 单独打开就能拦住它的那几条（可能不止一条）。"""
    sig = j.evaluate(tk, snap, now)
    if sig is not None and sig.cand.structure_type == "SHORT_GAMMA_VACUUM_SQUEEZE":
        return None
    why_on = j.last_reason.get(tk, "")
    saved = dict(B.SQ_FILTERS)
    try:
        B.SQ_FILTERS.update({k: False for k in B.SQ_FILTERS})
        s2 = j.evaluate(tk, snap, now)
        if s2 is None or s2.cand.structure_type != "SHORT_GAMMA_VACUUM_SQUEEZE":
            return None
        who = []
        for name in ("B", "C", "fuel"):
            B.SQ_FILTERS.update({k: (k == name) for k in B.SQ_FILTERS})
            s3 = j.evaluate(tk, snap, now)
            if s3 is None or s3.cand.structure_type != "SHORT_GAMMA_VACUUM_SQUEEZE":
                who.append(name)
    finally:
        B.SQ_FILTERS.update(saved)
    seg = [x for x in why_on.split("；") if x.startswith("逼空")]
    return "+".join(who) or "?", s2, (seg[0] if seg else why_on)


def outcome(frames, i, cand, stop_pct=0.008):
    S = frames[i].spot
    stop, tgt = S * (1 - stop_pct), cand.target_strike
    for f in frames[i + 1:]:
        if f.spot >= tgt:
            return f"先到目标 {tgt:g}（{(f.t + dt.timedelta(minutes=5)).strftime('%H:%M')}）"
        if f.spot <= stop:
            return f"先破止损 {stop:.2f}（{(f.t + dt.timedelta(minutes=5)).strftime('%H:%M')}）"
    best = max((f.spot for f in frames[i + 1:]), default=S)
    return f"收盘都没碰到（最高 {(best / S - 1) * 100:+.2f}%，收盘 {(frames[-1].spot / S - 1) * 100:+.2f}%）"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args(argv)
    logging.disable(logging.CRITICAL)
    day = dt.date.fromisoformat(args.date) if args.date else et_now().date()
    cfg = SystemConfig.load(None)
    rows = []
    for fp in sorted(glob.glob(f"live/trades_{day}*.csv")):
        rows += list(csv.DictReader(open(fp, encoding="utf-8-sig")))
    if not os.path.exists(f"yhs_data/archive/heatmap/{day}"):
        print(f"{day} 没有 Heatmap 存档"); return 1
    L = [f"\n## {day}\n"]
    pnl = sum(float(r["realized_pnl"]) for r in rows)
    L.append(f"实盘 {len(rows)} 笔，合计 ${pnl:+,.2f}" + (f"（胜率 {sum(1 for r in rows if float(r['realized_pnl']) > 0) / len(rows) * 100:.0f}%）" if rows else "") + "\n")
    if rows:
        L.append("| 开仓 | 交易 | 张数 | 平仓 | 原因 | 盈亏 | 候选规则触发 |\n|---|---|---|---|---|---|---|")
        fcache = {}
        for r in rows:
            tk, st = r["ticker"], r["bl_structure"]
            fr = fcache.setdefault(tk, day_frames(cfg, day, tk))
            et = dt.datetime.fromisoformat(r["entry_time"]); lab = ((et.hour * 60 + et.minute) // 5) * 5 - 5
            hist = [x for x in fr if x.t.hour * 60 + x.t.minute <= lab]
            notes = []
            if hist:
                d = B.context(hist).intraday_ret
                if st.startswith("CLIFF") and d < -0.01:
                    notes.append(f"A：悬崖当天已跌 {d * 100:.1f}%")
                if st.startswith("CLIFF") and d > 0.01:
                    notes.append(f"逆势：悬崖当天已涨 {d * 100:+.1f}%")
                if st.startswith("SHORT") and d > 0.05:
                    notes.append(f"逼空当天已涨 {d * 100:+.1f}%")
            L.append(f"| {r['entry_time'][11:16]} | {tk} {float(r['strike']):g}{'C' if r['action'] == 'BUY_CALL' else 'P'} {B.STRUCT_CN.get(st, st)[:2]} | {r.get('bl_contracts') or 1} | "
                     f"{r['exit_time'][11:16]} | {r['exit_reason']} | {float(r['realized_pnl']):+.0f} | {'；'.join(notes) or '—'} |")
    # 被 B / C / 燃料拦下的逼空信号
    tickers = live_universe(day) or sorted({r["ticker"] for r in rows})
    seen, blk = {}, []
    for tk in tickers:
        jf = []
        j = B.BaselineJudge(cfg)
        for t, p in merged_frames(cfg, day, tk).items():
            f, snap, _ = j.observe(tk, p, t, day)
            if f is None:
                continue
            jf.append(f)
            now = t + dt.timedelta(minutes=5)
            if not ("09:50" <= now.strftime("%H:%M") < "15:00"):
                continue
            b = blocked_by(j, tk, snap, now)
            if not b:
                continue
            key = (tk, b[0])
            if key in seen and (t - seen[key]).total_seconds() < 1800:
                continue
            seen[key] = t
            blk.append([now.strftime("%H:%M"), tk, b[0], b[2], b[1].cand, len(jf) - 1])
        for x in blk:
            if x[1] == tk and not isinstance(x[4], str):
                x[4] = outcome(jf, x[5], x[4])
    L.append(f"\n逼空 B / C / 燃料拦下的信号 {len(blk)} 个（三条全关才会出的逼空信号；「规则」列是单独打开就能拦住它的那几条；之后只看 Heatmap 现价，止损按 −0.8%，不含期权棘轮）：\n")
    if blk:
        L.append("| 数据时刻 | 标的 | 规则 | 拦下原因 | 之后 |\n|---|---|---|---|---|")
        for x in sorted(blk):
            L.append(f"| {x[0]} | {x[1]} | {x[2]} | {x[3].replace('逼空：', '')[:90]} | {x[4]} |")
        for name in ("B", "C", "fuel"):
            g = [x for x in blk if name in x[2].split("+")]
            if g:
                L.append(f"\n* {name}：{len(g)} 个，先到目标 {sum('先到目标' in x[4] for x in g)}、先破止损 {sum('先破止损' in x[4] for x in g)}、都没碰到 {sum('都没碰到' in x[4] for x in g)}")
    text = "\n".join(L) + "\n"
    if not os.path.exists(OUT):
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("# baseline 实盘证据记录（每日收盘后自动追加，live_evidence.py）\n\n"
                    "用途：按用户要求，候选规则与已上线规则都用实盘数据检验。时间 = 数据实际时刻（美东）。\n")
    old = open(OUT, encoding="utf-8").read()
    if f"\n## {day}\n" in old:
        old = old[:old.index(f"\n## {day}\n")]
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(old)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
