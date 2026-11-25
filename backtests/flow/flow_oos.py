"""B baseline 有/无大单对比，含 11/21 分析之后的样本外（11/23 起）。复用 flow_analysis.py 的函数；0 配额。
用法：python3 backtests/flow/flow_oos.py（在仓库根目录）"""
import os, random
_src = open(os.path.join(os.path.dirname(__file__), "flow_analysis.py"), encoding="utf-8").read().split("random.seed(1)")[0]
exec(_src)
random.seed(1)
for lab, s, e in [("全部 10/20 起", "2026-10-20", "2099"), ("10/27 起（现行规则）", "2026-10-27", "2099"),
                  ("样本外 11/23 起", "2026-11-23", "2099")]:
    b = load("", s, e)
    y = [r for r in b if r["flow_confirmed"] == "1"]; n = [r for r in b if r["flow_confirmed"] == "0"]
    none = [r for r in b if kind(r) == "无·窗口内没有大单"]; act = [r for r in b if kind(r) != "无·窗口内没有大单"]
    print(f"\n## B {lab}（{len({r['day'] for r in b})} 天）")
    print("全部      ", stats(b)); print("有大单    ", stats(y)); print("无大单    ", stats(n))
    if y and n: print("  平均ROI差 %+.1f pp，置换检验 p=%.2f" % boot(y, n))
    print("窗口完全没大单", stats(none)); print("有大单活动  ", stats(act))
    if none and act: print("  平均ROI差 %+.1f pp，置换检验 p=%.3f" % boot(none, act, 20000))
