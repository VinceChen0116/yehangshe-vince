"""成交方向（说明 4.3 节）。

- 有 aggressor_side（夜航社逐笔带 side）：用数据商给的方向，可信度 1.0。
  夜航社的定义：成交价相对成交时 NBBO 的位置——at_ask / above_ask = 主动买，at_bid / below_bid = 主动卖，其余（mid 等）未知。
- side 缺失时退回报价规则：pos = (P − B) / (A − B)，≥0.8 买、≤0.2 卖、其余未知。
  成交价来源三档：1 秒内到达 1.0（未知 0.7）；同一次订阅里的旧成交价 0.8（未知 0.6）；没有可用成交价 → 中间价、未知、0.5。
- 分钟桶（top_contracts）只给单合约的 net = ask 侧 − bid 侧权利金，不拆未知；
  用同一分钟同一侧（Call / Put）的"方向已知占比" k 估算：买 = (k·总 + net)/2，卖 = (k·总 − net)/2，未知 = (1−k)·总。
"""
from __future__ import annotations

from .source import BUY_SIDES, SELL_SIDES

BUY, SELL, UNKNOWN = "buy", "sell", "unknown"
POS_BUY, POS_SELL = 0.8, 0.2


def from_side(side):
    if side in BUY_SIDES:
        return BUY, 1.0
    if side in SELL_SIDES:
        return SELL, 1.0
    return None


def quote_rule(price, bid, ask, price_age_s=None, same_subscription=False):
    """返回 (方向, 可信度, 计权利金用的价格)。

    price_age_s: 成交价距这次成交的秒数（None = 从未收到成交价）
    same_subscription: 成交价与这次成交量来自同一次订阅
    """
    if bid is None or ask is None or ask < bid or bid < 0:
        return UNKNOWN, 0.5, None
    mid = 0.5 * (bid + ask)
    usable = price is not None and bid <= price <= ask
    if usable and price_age_s is not None and price_age_s <= 1.0:
        tier_known, tier_unknown = 1.0, 0.7
    elif usable and same_subscription:
        tier_known, tier_unknown = 0.8, 0.6
    else:
        return UNKNOWN, 0.5, mid
    if ask == bid:
        return UNKNOWN, tier_unknown, price
    pos = (price - bid) / (ask - bid)
    if pos >= POS_BUY:
        return BUY, tier_known, price
    if pos <= POS_SELL:
        return SELL, tier_known, price
    return UNKNOWN, tier_unknown, price


def classify_print(bp):
    """逐笔大单：优先用 side，缺失时用该笔自带的 NBBO 走报价规则（成交价与报价同一时刻 → 第 1 档）。"""
    s = from_side(bp.side)
    if s:
        return s
    d, c, _ = quote_rule(bp.price, bp.bid, bp.ask, price_age_s=0.0)
    return d, c


def split_bucket_row(row, known_ratio):
    """分钟桶单合约 → (买张数, 卖张数, 未知张数, 买权利金, 卖权利金)，均为估算。"""
    g = max(row.gross_premium, 0.0)
    if g <= 0 or row.contracts <= 0:
        return 0.0, 0.0, row.contracts, 0.0, 0.0
    k = max(known_ratio, abs(row.net_premium) / g)   # 已知部分至少要装得下 |net|
    k = min(k, 1.0)
    buy_p = max(0.0, (k * g + row.net_premium) / 2)
    sell_p = max(0.0, (k * g - row.net_premium) / 2)
    per = row.contracts / g
    buy_n, sell_n = buy_p * per, sell_p * per
    return buy_n, sell_n, max(0.0, row.contracts - buy_n - sell_n), buy_p, sell_p
