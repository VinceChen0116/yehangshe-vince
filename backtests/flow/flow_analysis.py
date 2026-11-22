import csv,glob,collections,random,statistics as st,re
U=650
def load(suf, start="2026-10-01", end="2099"):
    out=[]
    for f in sorted(glob.glob(f"live/trades_????-??-??{suf}.csv")):
        d=f.split("trades_")[1][:10]
        if not (start<=d<=end): continue
        for r in csv.DictReader(open(f,encoding="utf-8-sig")):
            if r.get("flow_confirmed") in ("0","1"):
                r["day"]=d; r["roi"]=float(r["realized_roi_pct"]); out.append(r)
    return out
def kind(r):
    n=r.get("flow_note","")
    if r["flow_confirmed"]=="1": return "有大单"
    if "疑似平仓" in n: return "无·有单但疑似平仓"
    if "无 ≥" in n or "无≥" in n: return "无·窗口内没有大单"
    return "无·其他("+n[:12]+")"
def stats(rs):
    if not rs: return "0"
    p=[r["roi"]/100*U for r in rs]; g=sum(x for x in p if x>0); l=-sum(x for x in p if x<0)
    return f"{len(rs)} 笔 | 胜率 {100*sum(x>0 for x in p)/len(p):.0f}% | 平均ROI {st.mean(r['roi'] for r in rs):+.1f}% | 中位 {st.median(r['roi'] for r in rs):+.1f}% | 合计 ${sum(p):+,.0f} | 盈亏比 {g/l if l else float('inf'):.2f}"
def boot(a,b,n=5000):
    a=[r["roi"] for r in a]; b=[r["roi"] for r in b]; diff=st.mean(a)-st.mean(b); c=0
    pool=a+b
    for _ in range(n):
        random.shuffle(pool); x=pool[:len(a)]; y=pool[len(a):]
        if abs(st.mean(x)-st.mean(y))>=abs(diff): c+=1
    return diff, c/n
random.seed(1)
for name,suf,s,e in [("B baseline 10/20–11/20","","2026-10-20","2099"),("B 旧规则 10/15–10/19","","2026-10-01","2026-10-19"),("L 旧规则 11/16–11/20","_L","2026-11-16","2099")]:
    rs=load(suf,s,e); y=[r for r in rs if r["flow_confirmed"]=="1"]; n=[r for r in rs if r["flow_confirmed"]=="0"]
    d,p=boot(y,n)
    print(f"\n## {name}（{len({r['day'] for r in rs})} 天）")
    print("全部     ", stats(rs)); print("有大单   ", stats(y)); print("无大单   ", stats(n))
    print(f"平均ROI差 {d:+.1f} 个百分点，置换检验 p={p:.2f}")
    for k,g in sorted(collections.Counter(kind(r) for r in rs).items()):
        print("  ",k, stats([r for r in rs if kind(r)==k]))
    if suf=="" and s>="2026-10-20":
        for stc in sorted({r.get("bl_structure","") for r in rs}):
            sub=[r for r in rs if r.get("bl_structure","")==stc]
            print(" ",stc); print("    有",stats([r for r in sub if r["flow_confirmed"]=="1"])); print("    无",stats([r for r in sub if r["flow_confirmed"]=="0"]))
        # premium buckets
        for lo,hi in [(0,1),(1,1e5),(1e5,3e5),(3e5,1e12)]:
            sub=[r for r in y if lo<=float(r.get("flow_premium_usd") or 0)<hi]
            if sub: print(f"  大单金额 {lo:,.0f}–{hi:,.0f}:", stats(sub))
        # by half of period
        mid="2026-11-04"
        for lab,f in [("前半 ≤11/03",lambda r:r["day"]<mid),("后半 ≥11/04",lambda r:r["day"]>=mid)]:
            sub=[r for r in rs if f(r)]
            print(" ",lab,"有",stats([r for r in sub if r['flow_confirmed']=='1'])); print("   ",lab,"无",stats([r for r in sub if r['flow_confirmed']=='0']))
print("\n## 合并：窗口内完全没有大单 vs 有同向 Ask 大单（含疑似平仓）")
allr=load("","2026-10-01")+load("_L","2026-11-16")
none=[r for r in allr if kind(r)=="无·窗口内没有大单"]; act=[r for r in allr if kind(r)!="无·窗口内没有大单"]
print("没有大单 ",stats(none)); print("有大单活动",stats(act)); d,p=boot(none,act,20000); print(f"差 {d:+.1f} pp, p={p:.3f}")
b=load("","2026-10-20"); none=[r for r in b if kind(r)=="无·窗口内没有大单"]; act=[r for r in b if kind(r)!="无·窗口内没有大单"]
d,p=boot(none,act,20000); print(f"仅 B baseline：差 {d:+.1f} pp, p={p:.3f}")
for stc in ["CLIFF_STRUCTURE","SHORT_GAMMA_VACUUM_SQUEEZE","SPRINGBOARD_STRUCTURE"]:
    s=[r for r in b if r.get("bl_structure")==stc]
    print(" ",stc,"没有:",stats([r for r in s if kind(r)=="无·窗口内没有大单"]),"\n      有活动:",stats([r for r in s if kind(r)!="无·窗口内没有大单"]))
print("按天：没有大单的单分布", collections.Counter(r["day"] for r in b if kind(r)=="无·窗口内没有大单"))
# top losers excluded robustness
none=sorted([r["roi"] for r in b if kind(r)=="无·窗口内没有大单"]); print("没有大单 去掉最差 2 笔平均", st.mean(none[2:]))
# exit reasons
print(collections.Counter(r["exit_reason"] for r in b if kind(r)=="无·窗口内没有大单").most_common(6))
print(collections.Counter(r["exit_reason"] for r in b if kind(r)!="无·窗口内没有大单").most_common(6))
print("\n## 10/27 起（现行规则）")
b=load("","2026-10-27")
for lab,f in [("没有大单",lambda r:kind(r)=="无·窗口内没有大单"),("有活动",lambda r:kind(r)!="无·窗口内没有大单")]:
    print(lab,stats([r for r in b if f(r)]))
    print("   逼空",stats([r for r in b if f(r) and r.get("bl_structure")=="SHORT_GAMMA_VACUUM_SQUEEZE"]))
sq=[r for r in load("","2026-10-20") if r.get("bl_structure")=="SHORT_GAMMA_VACUUM_SQUEEZE" and kind(r)=="无·窗口内没有大单"]
for r in sq: print("  ",r["day"],r["entry_time"][11:16],r["ticker"],r["roi"],r["exit_reason"])
