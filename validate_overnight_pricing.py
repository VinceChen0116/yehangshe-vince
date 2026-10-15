"""validate_overnight_pricing.py —— 隔夜规则回测（backtests/overnight_rules）每笔的开 / 平仓价 vs 真实成交均价（Flow 5 分钟成交）与历史 NBBO。
10/7–10/14 共 58 笔：开仓 15:55 中位绝对偏差 3.0%（偏差中位 −0.9%）；次日 09:35 平仓 8.0%（−1.3%），主要来自开盘 5 分钟内价格波动。
全部改用真实成交均价重算，合计 +$3,250（模型 +$3,544）。"""
import csv, os, sys, statistics as st, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SystemConfig, et_datetime, parse_iso, to_et
from yhs_client import YHSClient
from main import HistoryClient
cfg = SystemConfig.load(None); c = YHSClient(cfg)
def bars(hc, core):
    p = hc._cached_get("flowbars", core, f"/v1/flow/contracts/{core}/bars",
                       {"session_date": hc.date.isoformat(), "series": "contract", "limit": 100})
    out = {}
    for b in p.get("data") or []:
        end = to_et(parse_iso(b["bucket_end_at"])).strftime("%H:%M")
        n = sum(x.get("contracts") or 0 for x in b.get("side_breakdown") or [])
        if b.get("avg_fill_usd") and n: out[end] = (float(b["avg_fill_usd"]), n)
    return out
def vwap(bs, ends):
    v = [bs[e] for e in ends if e in bs]
    n = sum(x[1] for x in v)
    return (sum(x[0] * x[1] for x in v) / n, n) if n else (None, 0)
rows = []
for fn in sorted(os.listdir("backtests/overnight_rules")):
    if fn.startswith("trades_2"): rows += list(csv.DictReader(open("backtests/overnight_rules/" + fn, encoding="utf-8")))
E_in, E_out, out = [], [], []
for r in rows:
    P, S = [dt.date.fromisoformat(x) for x in r["night"].split("→")]
    core = r["contract"].split(":")[-1]
    hp, hs = HistoryClient(c, cfg, P), HistoryClient(c, cfg, S)
    m_in = hp.option_price(core, et_datetime(P, "15:55")); m_out = hs.option_price(core, et_datetime(S, r["exit_time"]))
    rin, nin = vwap(bars(hp, core), ["15:55", "16:00"])
    eh = r["exit_time"]; h, m = map(int, eh.split(":"))
    rout, nout = vwap(bars(hs, core), [eh, f"{h:02d}:{m+5:02d}"])
    qin = hp.hist_quote(core, et_datetime(P, "15:55")); qout = hs.hist_quote(core, et_datetime(S, eh))
    if m_in and rin: E_in.append((m_in / rin - 1) * 100)
    if m_out and rout: E_out.append((m_out / rout - 1) * 100)
    out.append((r["night"][5:10], r["ticker"], r["roi_pct"], m_in, rin, nin, qin, m_out, rout, nout, qout))
    print(r["night"][5:10], r["ticker"], "ROI", r["roi_pct"], "| 入 模型", m_in and round(m_in, 2), "真实成交", rin and round(rin, 2), f"({nin}张)", "NBBO", qin,
          "| 出 模型", m_out and round(m_out, 2), "真实成交", rout and round(rout, 2), f"({nout}张)", "NBBO", qout, flush=True)
f = lambda v: f"n={len(v)} 中位偏差 {st.median(v):+.1f}% 中位绝对 {st.median([abs(x) for x in v]):.1f}% 平均绝对 {sum(abs(x) for x in v)/len(v):.1f}%" if v else "无"
print("开仓 15:55 模型中间价 vs 真实成交均价:", f(E_in))
print("平仓 次日 模型中间价 vs 真实成交均价:", f(E_out))
print("配额", c.quota_remaining)
