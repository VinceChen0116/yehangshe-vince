"""
rules_registry.py —— baseline 日内规则登记表（用于样本内 / 样本外划分）
======================================================================
每条规则记"决定日"（美东，定规则时已经看过的最后一个交易日）和"生效日"（实盘第一天）。
* 样本内 = 决定日及以前的交易日：规则是看着这些天定的，在这些天上回放必然好看，不能当证据。
* 样本外 = 决定日之后的交易日：只有这里的结果才算检验。
新加 / 修改日内规则时，在这里追加一行（同一次提交）。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    key: str
    name: str
    decided: dt.date      # 定规则时看过的最后一个交易日
    effective: dt.date    # 实盘生效日
    kind: str = "signal"  # signal（影响开不开仓）/ exit / sizing / execution


D = dt.date
RULES = [
    Rule("bl_core", "baseline 核心规则（bl_v8，含 10/20 用户补充：超大正节点目标、路径 1.5 倍、墙外 3 格等）", D(2026, 10, 19), D(2026, 10, 20)),
    Rule("bl_sizing", "按金额开仓 ~$650，≥2 张 +80% 卖一半", D(2026, 10, 20), D(2026, 10, 21), "sizing"),
    Rule("sq_B", "逼空 B：Call 墙 ≥ 0.5 × 现价下方 0.5% 内最大节点", D(2026, 10, 20), D(2026, 10, 21)),
    Rule("sq_C", "逼空 C：现价下方 0.5% 内负节点 ≥ 0.5 × 路径负节点合计 → 不开", D(2026, 10, 20), D(2026, 10, 21)),
    Rule("sq_fuel", "逼空燃料：路径负节点 ≥ 2 × 正节点", D(2026, 10, 20), D(2026, 10, 21)),
    Rule("goog_cooldown", "GOOGL / GOOG 共享冷却", D(2026, 10, 20), D(2026, 10, 21)),
    Rule("pin_1pct", "钉住扩展：路径上或现价 ±1% 内任何负节点 ≥ 3 × 墙 → 不开", D(2026, 10, 21), D(2026, 10, 22)),
    Rule("sq_trend", "逼空走势：15 分钟新高且净涨 ≥ 0.3%", D(2026, 10, 21), D(2026, 10, 22)),
    Rule("spread_8", "买卖价差上限 25% → 8%", D(2026, 10, 26), D(2026, 10, 27), "execution"),
]


def last_decided(rules=RULES) -> dt.date:
    return max(r.decided for r in rules)


def active_on(day: dt.date, rules=RULES) -> list[Rule]:
    """实盘在 day 这天用的规则（已生效的）。"""
    return [r for r in rules if r.effective <= day]
