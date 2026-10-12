"""
premarket_report.py —— 盘前研究工作流的中文 Markdown 报告与可审计 JSON 输出
（不含 API Key：只写业务响应、参数、内容时间与候选清单）
"""
from __future__ import annotations

import collections
import json
import os
from dataclasses import asdict, fields

from config import SystemConfig, et_now
from premarket_workflow import GROUP_LABEL, PHASE_CN, fmt_et, fmt_gex, fmt_usd

FLOW_CN = {"bullish": "bullish", "bearish": "bearish", "mixed": "mixed", "unknown": "unknown"}
PRICE_CN = {"ok": "可信", "degraded": "降级", "quote_unavailable": "quote_unavailable"}


def _cell(s) -> str:
    return ("—" if s in (None, "") else str(s)).replace("|", "\\|").replace("\n", " ")


def _node_txt(n: dict | None) -> str:
    if not n:
        return "—"
    d = n.get("distance_atr")
    return (f"{n['strike_usd']:g} @{n['expiration']}({n['dte']}D) {fmt_gex(n['net_dealer_gex_usd'])}"
            + (f" / {d:+.2f}ATR" if d is not None else ""))


def _price_txt(r) -> str:
    p = r.price
    if p.status == "quote_unavailable":
        return f"不可确认（quote_unavailable）；昨收 {fmt_usd(p.prev_close_usd)}（{p.prev_close_date or '?'}）"
    return f"{fmt_usd(p.price_usd)}（{PRICE_CN[p.status]}，{p.content_time_et}，{p.source}）"


def _flow_txt(r) -> str:
    f = r.flow
    parts = [f"**{FLOW_CN[f.flow_support]}**"]
    if f.direction_balance is not None:
        parts.append(f"balance={f.direction_balance:+.2f}，有效 ${f.effective_premium_usd:,.0f}"
                     + (f"，模糊 ${f.ambiguous_premium_usd:,.0f}" if f.ambiguous_premium_usd else ""))
    if f.package_share is not None:
        parts.append(f"组合占比 {f.package_share:.0%}")
    if f.background_7d:
        parts.append(f.background_7d)
    parts += f.evidence[:2]
    parts += f.limitations[:2]
    if f.session_date:
        parts.append(f"Flow 交易日 {f.session_date}")
    return "；".join(parts)


def _rank_txt(r, rank_date) -> str:
    e = r.entry
    src = "量榜" if e.source == "volume_top" else ("OI变化" if e.source == "oi_change" else "手动")
    oi = f"，OI {e.oi_change_pct:+.1%}" if e.oi_change_pct is not None else ""
    rv = f"，相对量 {e.rel_volume:.2f}x" if e.rel_volume is not None else ""
    return f"#{e.rank}（{src}，{rank_date}{oi}{rv}）"


def _table(rows, rank_date) -> list:
    head = ("| ticker | 分组 | 成交量排名 | 当前价格/时间 | 主要到期·上方正Wall | 下方正Wall | 附近负节点 | 相对Wall位置 "
            "| setup_type | Flow 证据 | 触发条件 | 失效条件 | 下一节点/空间 | status |")
    out = [head, "|" + "---|" * 14]
    for r in rows:
        exps = sorted({w["expiration"] for w in r.key_walls})[:2]
        negs = "；".join(_node_txt(n) for n in r.near_negatives[:2]) or "—"
        flags = [f for f in r.flags if f not in ("manual_distance_review",)]
        status = r.status + (f"（{','.join(flags)}）" if flags else "")
        out.append("| " + " | ".join(_cell(x) for x in (
            f"**{r.entry.ticker}**", GROUP_LABEL.get(r.entry.group, r.entry.group), _rank_txt(r, rank_date),
            _price_txt(r), f"到期 {','.join(exps) or '—'}；上 {_node_txt(r.upper_wall)}", _node_txt(r.lower_wall),
            negs, r.position_text, r.setup_type, _flow_txt(r), r.trigger, r.invalidation, r.space_text, status)) + " |")
    return out


def build_markdown(wf) -> str:
    c, ri = wf.clock, wf.rank_info
    rank_date = ri.get("session_date")
    L = []
    L.append(f"# 盘前期权活跃榜 → Heatmap 关键节点 → Flow 确认（目标交易日 {c.target_day}）")
    L.append("")
    L.append("> 研究观察名单，非交易建议。所有 setup 均为待验证模式，不代表固定胜率；"
             "`confirmed` 仅表示研究条件满足，不代表收益保证；direction_balance 仅为成交方向代理，不是上涨概率。")
    L.append("")
    # ---- 运行信息
    L.append("## 一、运行时间与数据日期")
    L.append("")
    L.append(f"- 本次运行：{fmt_et(c.now_et)}，市场阶段：**{PHASE_CN[c.phase]}**"
             + (f"（{c.holiday_note}）" if c.holiday_note else ""))
    L.append(f"- 当前（目标）交易日：**{c.target_day}**；上一完整交易日：**{c.prev_full_day}**（NYSE 日历）")
    rank_status_cn = {"previous_full_session": "上一完整交易日最终榜", "archive_previous_full_session": "上一交易日本地归档",
                      "intraday_partial": "当日盘中未完成榜", "reference_only": "reference_only（旧榜研究池，非今日最新榜）"}
    L.append(f"- 成交量榜：session_date={rank_date}，as_of={ri.get('as_of')}，状态={rank_status_cn.get(ri.get('status'), ri.get('status'))}"
             + (f"，data_freshness={ri.get('data_freshness_seconds')}s" if ri.get('data_freshness_seconds') else ""))
    if ri.get("notice"):
        L.append(f"  - 服务端 stale notice：{ri['notice']}")
    L.append(f"- OI 日期：随榜单 session_date={rank_date}（total_open_interest current vs previous）")
    pstat = collections.Counter(r.price.status for r in wf.results)
    L.append(f"- 价格：可信 {pstat.get('ok', 0)} 只，降级 {pstat.get('degraded', 0)} 只，"
             f"quote_unavailable {pstat.get('quote_unavailable', 0)} 只"
             + ("（未使用 Heatmap 内部 spot 冒充现价）" if pstat.get("quote_unavailable") else ""))
    labels = collections.Counter(r.structure_label for r in wf.results if r.structure_label)
    times = sorted({r.structure_time_et for r in wf.results if r.structure_time_et})
    L.append(f"- Heatmap 结构：{dict(labels)}；结构时间 {times[0] if times else '—'}"
             + (f" … {times[-1]}" if len(times) > 1 else ""))
    flow_state = "；".join(f"{k}: {v}" for k, v in wf.disabled.items() if k.startswith("flow.")) or "可用"
    L.append(f"- Flow：交易日 {c.prev_full_day}（昨日资金背景，single_leg_only=true）；能力状态：{flow_state}")
    L.append("")
    # ---- 缺口
    gaps = list(wf.coverage_gaps)
    if ri.get("not_in_working_set"):
        gaps.append(f"不在 Heatmap working_set（已剔除）：{', '.join(ri['not_in_working_set'])}")
    by_reason = {}
    for k, v in wf.disabled.items():
        by_reason.setdefault(v, []).append(k)
    for v, ks in by_reason.items():
        gaps.append(f"停用 {', '.join(ks)}：{v}")
    if pstat.get("quote_unavailable"):
        gaps.append("部分/全部标的无可信当前价：只输出昨日结构与条件触发价，位置以昨收为参考")
    unknown = [r.entry.ticker for r in wf.shortlist if r.flow.flow_support == "unknown"]
    if unknown:
        gaps.append(f"Flow 支持未知（{len(unknown)}/{len(wf.shortlist)} 只入围）：{', '.join(unknown)}；"
                    "这些标的方向判断只有结构证据，最多到 candidate")
    L.append("## 二、影响结论的 stale / 覆盖缺口")
    L.append("")
    L += [f"- {g}" for g in gaps] or ["- 无"]
    L.append("")
    # ---- 候选池
    groups = collections.defaultdict(list)
    for e in wf.pool:
        groups[e.group].append(f"{e.ticker}#{e.rank}" + ("*" if e.source == "oi_change" else ""))
    L.append(f"## 三、候选池（{len(wf.pool)} 只，入围 {len(wf.shortlist)} 只）")
    L.append("")
    L.append("排名为绝对成交量，不等同相对异常；`*` = OI 变化显著补入。")
    L.append("")
    for g in ("stock", "index_etf", "other_etf", "leveraged_inverse_etf"):
        if groups.get(g):
            L.append(f"- {GROUP_LABEL[g]}：{', '.join(groups[g])}")
    L.append("")
    # ---- 三张表
    short = wf.shortlist
    tables = (("long_watch：多头条件候选", "long_watch"), ("short_watch：空头条件候选", "short_watch"),
              ("range_or_uncertain：区间 / 冲突 / 数据不足 / 等待", "range_or_uncertain"))
    for i, (title, key) in enumerate(tables, 1):
        rows = sorted([r for r in short if r.watch == key], key=lambda r: -r.score)
        L.append(f"## 四-{i}、{title}（{len(rows)}）")
        L.append("")
        L += _table(rows, rank_date) if rows else ["（无）"]
        L.append("")
    rest = [r for r in wf.results if r not in short]
    if rest:
        L.append("<details><summary>未入围标的（结构摘要）</summary>")
        L.append("")
        for r in sorted(rest, key=lambda r: -r.score):
            L.append(f"- {r.entry.ticker}：{r.setup_type}；{r.position_text}；最近正节点 {_node_txt(r.nearest_positive)}")
        L.append("")
        L.append("</details>")
        L.append("")
    # ---- 优先观察
    L.append("## 五、优先观察（最多 5 个）")
    L.append("")
    pri = [r for r in sorted(short, key=lambda r: -r.score)
           if r.watch in ("long_watch", "short_watch") and r.status in ("candidate", "confirmed", "waiting")][:5]
    if not pri:
        L.append("证据不足，本次不给出优先名单。")
    for n, r in enumerate(pri, 1):
        why = [r.setup_type]
        if r.crossing:
            why.append({"up:premarket": "盘前新近上穿", "down:premarket": "盘前新近下穿",
                        "up:yesterday": "昨日上穿", "down:yesterday": "昨日下穿"}.get(r.crossing, r.crossing))
        if r.anchor and r.anchor.get("distance_atr") is not None:
            why.append(f"锚点距 {abs(r.anchor['distance_atr']):.2f} ATR")
        why.append(r.space_text)
        why.append(f"Flow {r.flow.flow_support}")
        L.append(f"{n}. **{r.entry.ticker}**（{r.status}，score {r.score}）— " + "；".join(why))
    L.append("")
    L.append("排序依据：发生跨越 > 负节点穿越 > 区间；锚点越近越优先；运动方向空间过近、同价位正负冲突、杠杆 ETF 降权；可信现价加分。")
    L.append("")
    # ---- 开盘确认
    L.append("## 六、开盘确认检查点")
    L.append("")
    cps = wf.checkpoints()
    if any(r.confirm_log for r in short):
        for r in short:
            if r.confirm_log:
                L.append(f"- **{r.entry.ticker}**：" + "；".join(r.confirm_log))
    else:
        L.append(f"尚未到开盘确认时间。待执行（不自动创建定时任务）：{', '.join(x.strftime('%H:%M') for x in cps)} ET "
                 "各重新运行一次 `python main.py --premarket`，检查已完成 5 分钟窗口的最新 Heatmap、"
                 "flow.ticker_bucket(window=5m) 与同窗 K 线。")
        L.append("")
        L.append("- Heatmap 09:30 标签对应 09:30–09:35 采样段，需该段完成并归档后才使用；"
                 "bucket_start_micros 使用真实 UTC epoch 微秒。")
        L.append(f"- 首版确认（待回测参数）：{wf.cfg.pm_confirm_bars} 根完成 5 分钟 K 线保持在 Wall 容差带外，并有回测/反抽验证。")
    L.append("")
    # ---- 参数与配额
    L.append("## 七、研究参数（待验证）与调用统计")
    L.append("")
    L.append(f"- 节点距离 ≤ {wf.cfg.pm_atr_distance_mult}×daily_atr_14；epsilon = max({wf.cfg.pm_eps_spread_mult}×spread_usd, "
             f"{wf.cfg.pm_eps_atr_mult}×daily_atr_14)（无可信报价时只用 ATR 项）；报价 >{wf.cfg.pm_quote_max_age_s:.0f}s 或价差 >"
             f"{wf.cfg.pm_spread_abnormal_pct:.1%} 降级")
    L.append(f"- 显著节点：|GEX| ≥ {wf.cfg.pm_node_significance:.0%} × 近端最强正节点；Top-node 缺席 ≠ 0，不推断 air pocket / 精确 Gamma Flip")
    L.append(f"- 本次请求：计费 {wf.client.billed_calls} 次，免费 {wf.client.free_calls} 次；按端点 {dict(wf.client.billed_by_endpoint)}")
    L.append(f"- 审计目录：`{wf.audit_dir}`")
    L.append("")
    qe = wf.quota_end if wf.quota_end is not None else wf.client.quota_remaining
    L.append(f"**quota.monthly_remaining = {int(qe) if qe is not None else '未知'}**"
             + (f"（启动时 {int(wf.quota_start)}）" if wf.quota_start is not None else ""))
    L.append("")
    return "\n".join(L)


def _params(cfg: SystemConfig) -> dict:
    return {f.name: getattr(cfg, f.name) for f in fields(cfg) if f.name.startswith("pm_")}


def write_outputs(wf) -> str:
    os.makedirs(wf.audit_dir, exist_ok=True)
    os.makedirs(wf.cfg.pm_report_dir, exist_ok=True)
    md = build_markdown(wf)
    report = os.path.join(wf.cfg.pm_report_dir, f"premarket_{wf.run_id}.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write(md)
    audit = {
        "run_id": wf.run_id, "written_at_et": fmt_et(et_now()),
        "clock": {"now_et": fmt_et(wf.clock.now_et), "phase": wf.clock.phase,
                  "target_day": wf.clock.target_day.isoformat(), "prev_full_day": wf.clock.prev_full_day.isoformat()},
        "params": _params(wf.cfg), "rank_info": wf.rank_info, "disabled": wf.disabled,
        "discover": {"working_set": sorted(wf.working_set), "available_commands": sorted(wf.available_commands)},
        "coverage_gaps": wf.coverage_gaps, "quota_start": wf.quota_start, "quota_end": wf.quota_end,
        "pool": [asdict(e) for e in wf.pool],
        "shortlist": [r.entry.ticker for r in wf.shortlist],
        "results": [asdict(r) for r in wf.results],
        "calls": wf.audit,
        "report": report,
    }
    with open(os.path.join(wf.audit_dir, "audit.json"), "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=1, default=str)
    return report
