"""实盘三晚逐笔复盘：开仓时节点质量 + 次日开盘价格对节点的反应。数据：存档 Heatmap（缺的从接口拉 09:30 起一页）。"""
import copy, csv, datetime as dt, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from config import SystemConfig
from yhs_client import YHSClient
from archive import load_archived_heatmap, rows_to_frames, fetch_heatmap_rows
from detector import parse_heatmap, detect_structure

cfg = SystemConfig.load(None); lcfg = copy.copy(cfg); lcfg.node_mode = "local"
client = YHSClient(cfg)
D = dt.date

# 10/15 晚：当时（无同板块上限）选出的 10 笔，盈亏来自 10/16 当天的真实价格复算
A = [("QQQ","P",728,706),("AMD","C",567.5,-1125),("SPY","P",772,-328),("TSLA","C",365,-56),("AMZN","C",257.5,-661),
     ("MRVL","C",242.5,-798),("SKHY","C",147,-908),("TSM","C",422.5,-782),("CSCO","C",110,-756),("AMAT","C",432.5,-932)]
nights = [(D(2026,10,15), D(2026,10,16), [(t, r, k, "2026-10-16", p) for t, r, k, p in A], "复算")]
for P, S in ((D(2026,10,16), D(2026,10,19)), (D(2026,10,19), D(2026,10,20))):
    rows = [r for r in csv.DictReader(open(f"live/overnight_{P}.csv")) if not r["status"].startswith("SHADOW")]
    nights.append((P, S, [(r["ticker"], r["right"], float(r["strike"]), r["expiration"], float(r["pnl_usd"])) for r in rows], "实盘"))


def frames(day, t, exp, start="15:00"):
    rows = load_archived_heatmap(cfg, day, t, exp)
    if not rows:
        rows = fetch_heatmap_rows(client, t, exp, day, max_pages=8 if start == "09:30" else 1, start=start)
    return rows_to_frames(t, day, exp, rows or [])


def frame_at(fr, label):
    k = next((k for k in sorted(fr) if k.strftime("%H:%M") == label), None)
    return parse_heatmap(fr[k], cfg, k.date()) if k else None


def spot_open(S, t, exp):
    """次日标记 09:30 那根（数据实际约 09:35，我们开始卖的时刻）的现价；该到期没有就用任一存档到期。"""
    for e in [exp] + sorted(os.listdir(os.path.join(cfg.data_dir, "archive", "heatmap", str(S), t)) if os.path.isdir(os.path.join(cfg.data_dir, "archive", "heatmap", str(S), t)) else []):
        e = e.replace(".json", "")
        try:
            s = frame_at(frames(S, t, e, "09:30"), "09:30")
        except Exception:
            s = None
        if s:
            return s.spot
    return None


out = []
for P, S, trades, src in nights:
    spy_close = frame_at(frames(P, "SPY", str(S)), "15:55")
    spy_open = spot_open(S, "SPY", str(S))
    spy_gap = (spy_open / spy_close.spot - 1) * 100 if spy_close and spy_open else None
    for t, right, K, exp, pnl in trades:
        fr = frames(P, t, exp)
        s45, s55 = frame_at(fr, "15:45"), frame_at(fr, "15:55")
        st = detect_structure(s45, lcfg) or detect_structure(s45, cfg)
        cells = dict(s45.cells)
        pos = sorted([g for g in cells.values() if g > 0], reverse=True)
        tot = s45.total_abs_gex or sum(abs(g) for g in cells.values())
        o = spot_open(S, t, exp)
        row = {"night": f"{P:%m/%d}→{S:%m/%d}", "src": src, "ticker": t, "right": right, "strike": K, "pnl": pnl,
               "spot45": round(s45.spot, 2), "close": round(s55.spot, 2) if s55 else None, "open": round(o, 2) if o else None,
               "spy_gap": round(spy_gap, 2) if spy_gap is not None else None}
        if st:
            pg, ng = cells.get(st.pos_strike, 0), cells.get(st.neg_strike, 0)
            row.update({"struct": st.structure, "pos": st.pos_strike, "pos_gex_m": round(pg / 1e6, 2),
                        "pos_rank": pos.index(pg) + 1 if pg in pos else None, "pos_share": round(pg / tot * 100, 1),
                        "pos_vs_max": round(pg / pos[0], 2) if pos else None,
                        "neg": st.neg_strike, "neg_gex_m": round(ng / 1e6, 2), "neg_share": round(abs(ng) / tot * 100, 1),
                        "dist": round(st.distance_pct * 100, 2)})
            if o:
                gap = (o / (s55.spot if s55 else s45.spot) - 1) * 100
                row["gap"] = round(gap, 2)
                if st.structure == "CLIFF":   # 天花板 pos 在上，深渊 neg 在下
                    row["result"] = "击穿天花板(反转)" if o > st.pos_strike else ("到达深渊" if o <= st.neg_strike else ("往下但没到" if o < s45.spot else "没动/往上"))
                else:
                    row["result"] = "跌破踏板(反转)" if o < st.pos_strike else ("到达加速区" if o >= st.neg_strike else ("往上但没到" if o > s45.spot else "没动/往下"))
        else:
            row["struct"] = "15:45 无结构(复算口径不同)"
        out.append(row)
        print(json.dumps(row, ensure_ascii=False))
json.dump(out, open("backtests/overnight_live_review/review.json", "w"), ensure_ascii=False, indent=1)
