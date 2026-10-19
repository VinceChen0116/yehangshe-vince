"""
tape_filter_study.py —— 跟大单候选的 Heatmap 过滤研究：哪种过滤能把每天几百个候选筛到个位数，同时覆盖 dennis-tape 实际跟的单
用法：python3 tape_filter_study.py --dates 2026-10-14,2026-10-15,2026-10-16 [--dennis 交易记录.csv]
每个候选（同一合约当天第一笔符合条件的大单）取下单时刻已可用的那根 Heatmap（该到期日），计算：
  strike_rank   行权价在该到期 |GEX| 中的排名（1 = 最大）
  strike_sign   行权价节点的正负
  dom_side      主导墙（|GEX| 最大节点）相对现价的方位与正负，如 "-上"
  aligned       主导墙在交易方向一侧（Call 在上方 / Put 在下方）
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import os

from archive import heatmap_path, rows_to_frames
from config import SystemConfig, et_datetime, parse_iso, to_et
from tape_archive import flow_path, is_candidate, parse_occ, tape_heatmap_path


def load_rows(cfg, day, t, exp):
    for fp in (tape_heatmap_path(cfg, day, t, exp), heatmap_path(cfg, day, t, exp)):
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, dict):   # 常规存档格式 {"rows": [...]}
                rows = rows.get("rows") or []
            if rows:
                return rows
    return None


LAG_MIN = 6   # dennis 一般在大单后约 5 分钟开仓 = 等下一根 Heatmap；用下单后 LAG_MIN 分钟内已可用的那根


def features(cfg, day, x, cache):
    t, exp, right, K = parse_occ(x["contract_symbol"])
    key = (t, exp)
    if key not in cache:
        rows = load_rows(cfg, day, t, exp)
        cache[key] = rows_to_frames(t, day, exp, rows) if rows else None
    fr = cache[key]
    if not fr:
        return None
    when = to_et(parse_iso(x["occurred_at"]))
    ready = [k for k in fr if k + dt.timedelta(minutes=5) <= when + dt.timedelta(minutes=LAG_MIN)]
    if not ready:
        return None
    d = fr[max(ready)]["data"]
    spot = d.get("spot_usd")
    cells = [(float(c["strike_usd"]), float(c["net_dealer_gex_usd"])) for c in d.get("cells") or []
             if c.get("expiration") == exp and c.get("net_dealer_gex_usd") is not None]
    if not cells or not spot:
        return None
    ranked = sorted(cells, key=lambda c: -abs(c[1]))
    strikes = [k for k, _ in ranked]
    rank = strikes.index(K) + 1 if K in strikes else 99
    gex_at = dict(cells).get(K)
    dom_k, dom_g = ranked[0]
    dom_side = f'{"+" if dom_g > 0 else "-"}{"上" if dom_k > spot else "下"}'
    aligned = (dom_k > spot) if right == "C" else (dom_k < spot)
    vol, oi = x.get("session_volume") or 0, x.get("open_interest") or 0
    return {"ticker": t, "exp": exp, "right": right, "strike": K, "time": when.strftime("%H:%M"), "hour": when.hour,
            "dte": x.get("min_dte"), "otm": float(x.get("min_moneyness_pct") or 0) * 100,
            "vol_oi": vol / oi if oi else 99.0, "sweep": bool(x.get("has_sweep")), "side": x.get("side"),
            "premium": float(x.get("premium_usd") or 0), "spot": spot, "strike_rank": rank,
            "strike_sign": None if gex_at is None else ("+" if gex_at > 0 else "-"), "dom_side": dom_side,
            "aligned": aligned, "contract": str(x["contract_symbol"]).split(":")[-1]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", required=True)
    ap.add_argument("--dennis", help="dennis-tape 交易记录 CSV（只读，用来核对覆盖率）")
    ap.add_argument("--out", default=os.path.join("backtests", "dennis", "filter_study.json"))
    args = ap.parse_args(argv)
    cfg = SystemConfig.load(None)
    dennis = collections.defaultdict(set)
    if args.dennis:
        with open(args.dennis, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                d = r["开仓时间(ET)"][:10]
                e = dt.date.fromisoformat(r["到期日"])
                occ = f'{r["标的"]}{e.strftime("%y%m%d")}{"C" if r["类型"] == "Call" else "P"}{int(round(float(r["行权价"]) * 1000)):08d}'
                dennis[d].add(occ)
    allf = []
    for ds in args.dates.split(","):
        day = dt.date.fromisoformat(ds)
        with open(flow_path(cfg, day), encoding="utf-8") as f:
            flow = json.load(f)
        best, cache, tot = {}, {}, collections.Counter()
        for x in sorted(flow, key=lambda r: r["occurred_at"]):
            if not is_candidate(x):
                continue
            c = str(x["contract_symbol"]).split(":")[-1]
            tot[c] += float(x.get("premium_usd") or 0)
            if c in best and best[c]["premium"] >= float(x.get("premium_usd") or 0):
                continue
            ft = features(cfg, day, x, cache)
            if ft:   # 每张合约保留当天最大的一笔大单
                ft["day"] = ds
                ft["dennis"] = c in dennis.get(ds, set())
                best[c] = ft
        for c, ft in best.items():
            ft["day_total"] = tot[c]
            allf.append(ft)
    filters = {
        "全部候选（有 Heatmap）": lambda f: True,
        "行权价 = 前3大节点": lambda f: f["strike_rank"] <= 3,
        "行权价 = 前3大负节点": lambda f: f["strike_rank"] <= 3 and f["strike_sign"] == "-",
        "主导墙在交易方向一侧": lambda f: f["aligned"],
        "主导墙为负且在交易方向一侧": lambda f: f["aligned"] and f["dom_side"].startswith("-"),
        "前3大节点 + 主导墙同向": lambda f: f["strike_rank"] <= 3 and f["aligned"],
        "前3大负节点 + 主导墙负且同向": lambda f: f["strike_rank"] <= 3 and f["strike_sign"] == "-" and f["aligned"] and f["dom_side"].startswith("-"),
        "前3大节点 + 主导墙同向 + ≥$50万": lambda f: f["strike_rank"] <= 3 and f["aligned"] and f["premium"] >= 500000,
        "A: 前4大负节点+同向+到期≥14天+价外≥2%": lambda f: f["strike_rank"] <= 4 and f["strike_sign"] == "-" and f["aligned"]
                                                and (f["dte"] or 0) >= 14 and f["otm"] >= 2,
        "B: A + ≥$15万": lambda f: f["strike_rank"] <= 4 and f["strike_sign"] == "-" and f["aligned"]
                                     and (f["dte"] or 0) >= 14 and f["otm"] >= 2 and f["premium"] >= 150000,
        "C: A + 到期≥7天(放宽)": lambda f: f["strike_rank"] <= 4 and f["strike_sign"] == "-" and f["aligned"]
                                         and (f["dte"] or 0) >= 7 and f["otm"] >= 2,
        "D: 前4大负节点+同向+价外≥2%（不限到期）": lambda f: f["strike_rank"] <= 4 and f["strike_sign"] == "-" and f["aligned"] and f["otm"] >= 2,
    }
    days = args.dates.split(",")
    total_dennis = sum(1 for f in allf if f["dennis"])
    print(f"候选（每张合约当天第一笔，且有该到期 Heatmap）：{len(allf)}；其中 dennis 跟了 {total_dennis} 张")
    res = {}
    for name, fn in filters.items():
        g = [f for f in allf if fn(f)]
        per = {d: sum(1 for f in g if f["day"] == d) for d in days}
        cov = sum(1 for f in g if f["dennis"])
        res[name] = {"per_day": per, "dennis_hit": cov}
        print(f"{name:<24} 每天候选 {per} ｜ 覆盖 dennis {cov}/{total_dennis}")
    print("\ndennis 跟的单在候选里的特征：")
    for f in allf:
        if f["dennis"]:
            print(" ", f["day"][5:], f["time"], f["contract"], f"${f['premium'] / 1e3:.0f}K", "行权价排名", f["strike_rank"],
                  f["strike_sign"], "主导墙", f["dom_side"], "同向" if f["aligned"] else "反向")
    import statistics as st
    def desc(g, k):
        v = [f[k] for f in g if f.get(k) is not None]
        return f"中位 {st.median(v):.2f}" if v else "—"
    dn, ot = [f for f in allf if f["dennis"]], [f for f in allf if not f["dennis"]]
    print("\n特征对比（dennis 跟的 vs 没跟的）：")
    for k, lb in (("premium", "最大单金额"), ("day_total", "当天该合约大单合计"), ("vol_oi", "当日量/OI"), ("dte", "到期天数"),
                  ("otm", "价外%"), ("strike_rank", "行权价节点排名"), ("hour", "时段")):
        print(f"  {lb:<16} dennis {desc(dn, k):<16} 其他 {desc(ot, k)}")
    print("  扫单比例        dennis %.0f%%  其他 %.0f%%" % (100 * sum(f["sweep"] for f in dn) / max(1, len(dn)), 100 * sum(f["sweep"] for f in ot) / max(1, len(ot))))
    print("  主导墙同向比例  dennis %.0f%%  其他 %.0f%%" % (100 * sum(f["aligned"] for f in dn) / max(1, len(dn)), 100 * sum(f["aligned"] for f in ot) / max(1, len(ot))))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"filters": res, "n": len(allf), "dennis_n": total_dennis}, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
