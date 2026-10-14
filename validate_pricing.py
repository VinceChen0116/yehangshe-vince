"""validate_pricing.py —— 用实盘真实报价校验回测取价方法的误差（python3 validate_pricing.py）。
结果（10/14，15 笔 30 个报价点）：真实 IV 还原价 ± 真实价差 中位误差 3.0%；5 分钟真实成交均价 8.7%；30 分钟/1 小时 NBBO 原值 7.1%。"""
import csv, os, sys, statistics as st, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SystemConfig, et_datetime
from yhs_client import YHSClient
from main import HistoryClient
from config import parse_iso, to_et


def flow_bars(hc, core):
    """该合约当天每 5 分钟的真实成交汇总：[(桶结束时刻, 成交均价, 买价侧均价, 卖价侧均价)]"""
    payload = hc._cached_get("flowbars", core, f"/v1/flow/contracts/{core}/bars",
                             {"session_date": hc.date.isoformat(), "series": "contract", "limit": 100})
    out = []
    for b in payload.get("data") or []:
        side = {x["side"]: (x.get("contracts") or 0, x.get("premium_usd") or 0) for x in b.get("side_breakdown") or []}
        def avg(*names):
            n = sum(side.get(k, (0, 0))[0] for k in names)
            return sum(side.get(k, (0, 0))[1] for k in names) / n / 100 if n else None
        if b.get("avg_fill_usd"):
            end = to_et(parse_iso(b["bucket_end_at"]))
            out.append((et_datetime(hc.date, end.strftime("%H:%M")), float(b["avg_fill_usd"]),
                        avg("at_bid", "below_bid"), avg("at_ask", "above_ask")))
    return sorted(out)


def last_bar(hc, core, t):
    last = None
    for r in flow_bars(hc, core):
        if r[0] > t:
            break
        last = r
    return last if last and (t - last[0]).total_seconds() <= 1800 else None


cfg = SystemConfig.load(None); c = YHSClient(cfg)
day = dt.date(2026, 10, 14); hc = HistoryClient(c, cfg, day)
rows = list(csv.DictReader(open("live/trades_2026-10-14.csv", encoding="utf-8-sig")))
err = {k: [] for k in ["M0_IV+BS", "M1_成交均价±价差", "M2_买卖侧成交", "M3_NBBO原始"]}
pts = []
for r in rows:
    sym = r["contract_symbol"].split(":")[-1]
    for kind, ts, truth in (("ask", r["entry_time"], float(r["entry_ask"])), ("bid", r["exit_time"], float(r["exit_option_price"]))):
        t = et_datetime(day, ts[11:16])
        if truth <= 0: continue
        i = 1 if kind == "ask" else 0
        mid = hc.option_price(sym, t); hq = hc.hist_quote(sym, t)
        h = (hq[1]-hq[0])/(hq[1]+hq[0]) if hq and hq[0] > 0 else cfg.slippage_pct
        m0 = mid*(1+h) if (mid and kind=="ask") else (mid*(1-h) if mid else None)
        lb = last_bar(hc, sym, t)
        m1 = (lb[1] * (1 - h) if kind == "bid" else lb[1] * (1 + h)) if lb else None
        m2 = (lb[2] if kind == "bid" else lb[3]) if lb else None
        m3 = hq[i] if hq else None
        line = [r["ticker"], kind, ts[11:16], truth]
        for k, v in zip(err, (m0, m1, m2, m3)):
            if v: err[k].append(abs(v/truth-1)*100)
            line.append(round(v, 3) if v else None)
        print(*line)
print("方法  样本  中位误差%  平均误差%")
for k, v in err.items():
    print(k, len(v), round(st.median(v), 1) if v else "-", round(sum(v)/len(v), 1) if v else "-")
print("配额剩余", c.quota_remaining)
