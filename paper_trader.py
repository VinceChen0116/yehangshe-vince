"""
paper_trader.py —— 阶段4：模拟交易、Delta-Gamma 凸性盯市、两阶追踪出场状态机、复盘统计与 CSV 导出
================================================================================================
配额纪律：
  * 开仓瞬间仅调用 1 次期权链快照（/v1/options/chain-snapshot/{ticker}?expiration=）获取合约代码与成本；
    期权链不可用时以 Black-Scholes 由现货换算（0 配额）。
  * 持仓期间绝不轮询期权链，盯市完全由 Heatmap 的 spot_usd 驱动：
        ΔP ≈ ΔS·δ + 0.5·γ·ΔS²       （Put 时 δ 取负号）
"""
from __future__ import annotations

import csv
import datetime as dt
import itertools
import logging
import math
import os
from dataclasses import dataclass, field

from config import SystemConfig, entry_blocked_window, hhmm_to_minutes, minutes_of_day
from detector import FullSignal, SignalDetector, extract_rows, pick, pick_num
from yhs_client import QuotaExhausted, YHSApiError

log = logging.getLogger("yhs.trader")

CHAIN_PATH = "/v1/options/chain-snapshot/{ticker}"

CSV_FIELDS = ["position_id", "ticker", "action", "contract_symbol", "strike", "entry_time", "entry_spot",
              "entry_option_price", "exit_time", "exit_spot", "exit_option_price", "realized_pnl",
              "realized_roi_pct", "peak_roi_pct", "max_adverse_roi_pct", "direction_correct", "exit_reason",
              "signal_score", "price_source", "entry_bid", "entry_ask", "entry_spread_pct"]


# =============================================================================
# Black-Scholes（仅作期权链不可用时的兜底换算）
# =============================================================================
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price_greeks(S: float, K: float, T: float, r: float, sigma: float, is_call: bool):
    """返回 (价格, |delta|, gamma)。"""
    T = max(T, 1.0 / 365 / 24)
    sig_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sig_t
    d2 = d1 - sig_t
    if is_call:
        price = S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
        delta = _ncdf(d1)
    else:
        price = K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
        delta = _ncdf(d1) - 1.0
    gamma = _npdf(d1) / (S * sig_t)
    return max(price, 0.01), abs(delta), gamma


def occ_symbol(ticker: str, expiration: str, right: str, strike: float) -> str:
    d = dt.date.fromisoformat(expiration)
    return f"O:{ticker.upper()}{d.strftime('%y%m%d')}{right}{int(round(strike * 1000)):08d}"


# =============================================================================
# 仓位
# =============================================================================
@dataclass
class Position:
    position_id: str
    ticker: str
    action: str
    right: str
    structure: str
    contract_symbol: str
    strike: float
    expiration: str
    entry_time: dt.datetime
    entry_spot: float
    entry_mid: float            # 开仓时期权中间价
    entry_option_price: float   # 含 2% 滑点的实际成本
    delta: float                # |delta|
    gamma: float
    price_source: str
    target_strike: float
    contracts: int
    last_spot: float = 0.0
    last_mid: float = 0.0
    roi_pct: float = 0.0
    peak_roi_pct: float = -100.0
    max_adverse_roi_pct: float = 0.0
    stop_pct: float = -35.0
    stage: str = "INITIAL"      # INITIAL / BREAKEVEN / TRAILING
    exit_time: dt.datetime | None = None
    exit_spot: float | None = None
    exit_option_price: float | None = None
    exit_reason: str = ""
    realized_pnl: float = 0.0
    realized_roi_pct: float = 0.0
    marks: int = 0
    signal_score: float = 0.0
    base_strike: float = 0.0    # 结构的正 GEX 节点（看涨=地板，看跌=天花板），underlying 止损用
    entry_bid: float = 0.0      # 实盘报价（price_source="quote"）：开仓时的 bid/ask
    entry_ask: float = 0.0
    last_bid: float = 0.0       # 最近一次报价的 bid，作为平仓/盯市的可卖价
    slip: float = -1.0          # 回放：按当时真实买卖价差算出的单边滑点（-1 = 用 cfg.slippage_pct）
    best_spot: float = 0.0      # trail_mode="underlying"：开仓后对我们最有利的现货价
    und_trail_active: bool = False

    @property
    def is_open(self) -> bool:
        return self.exit_time is None


class PaperTrader:
    def __init__(self, cfg: SystemConfig):
        self.cfg = cfg
        self.positions: list[Position] = []
        self._ids = itertools.count(1)
        self.chain_calls = 0
        # 可选的真实期权价格源：callable(contract_symbol, now_et) -> 价格 | None（历史回放接入 contract-intraday）
        self.price_provider = None
        self.delta_provider = None   # 可选：callable(contract_symbol, now_et) -> 真实 |delta| | None
        # 可选的实时报价源（仅实盘）：callable(contract_symbol) -> {"bid","ask","mid"} | None。
        # 设置后按 ask 开仓、按 bid 盯市与平仓，不再加滑点估算
        self.quote_provider = None
        # 回放可选：callable(contract_symbol, now_et) -> (bid, ask) | None，用当时真实 NBBO 定单边滑点、bid 为 0 不开仓
        self.spread_provider = None
        self.notifier = None         # 可选：notify.DiscordNotifier（仅实盘模式设置）

    # ------------------------------------------------------------------ 查询
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.is_open]

    def open_for(self, ticker: str) -> Position | None:
        for p in self.positions:
            if p.ticker == ticker and p.is_open:
                return p
        return None

    def trades_today(self, ticker: str, day: dt.date) -> list[Position]:
        return [p for p in self.positions if p.ticker == ticker and p.entry_time.date() == day]

    # ------------------------------------------------------------------ 开仓
    def _choose_strike(self, sig: FullSignal) -> tuple[float, bool]:
        """返回 (行权价, 是否为挂牌行权价)。target_node：目标负 GEX 节点，但虚值不超过 max_otm_pct；
        优先在 Heatmap 当前到期的挂牌行权价中选离节点最近的，避免造出不存在的合约。"""
        spot = sig.snapshot.spot
        if self.cfg.strike_mode == "atm":
            # 平值：Heatmap 挂牌行权价中离现价最近的那个（避免取整出不存在的合约，如 192.5 → 192）
            if sig.snapshot.strikes:
                return min(sig.snapshot.strikes, key=lambda k: (abs(k - spot), k)), True
            return spot, False
        if self.cfg.strike_mode == "itm":
            # 实值：挂牌行权价中离 现价×(1∓itm_depth_pct) 最近、且确实在价内的那个（看涨低于现价，看跌高于现价）
            is_call = sig.structure.right == "C"
            want = spot * (1 - self.cfg.itm_depth_pct) if is_call else spot * (1 + self.cfg.itm_depth_pct)
            ok = [k for k in sig.snapshot.strikes if (k < spot if is_call else k > spot)]
            if ok:
                return min(ok, key=lambda k: abs(k - want)), True
            return want, False
        node, is_call = sig.structure.neg_strike, sig.structure.right == "C"
        limit = spot * (1 + self.cfg.max_otm_pct) if is_call else spot * (1 - self.cfg.max_otm_pct)
        ok = [k for k in sig.snapshot.strikes
              if (k <= limit + 1e-6 if is_call else k >= limit - 1e-6)]
        if ok:
            return min(ok, key=lambda k: (abs(k - node), abs(k - spot))), True
        return (min(node, limit) if is_call else max(node, limit)), False

    def _price_from_chain(self, client, sig: FullSignal, want_strike: float):
        """单次期权链调用。返回 (symbol, strike, mid, |delta|, gamma) 或 None。"""
        try:
            self.chain_calls += 1
            payload = client.get(CHAIN_PATH.format(ticker=sig.ticker),
                                 {"expiration": sig.snapshot.expiration, "limit": 500})
        except QuotaExhausted:
            raise
        except YHSApiError as e:
            if e.code == "HISTORY_NO_CHAIN":
                self.chain_calls -= 1
                log.debug("历史回放不调用实时期权链（0 配额）")
            else:
                log.warning("期权链获取失败（%s），改用 BS 现货换算", e)
            return None
        rows = extract_rows(payload)
        spot = sig.snapshot.spot
        best = None
        for r in rows:
            right = str(pick(r, "right", "option_type", "type", "put_call", "contract.right", default="")).upper()[:1]
            sym = pick(r, "contract_symbol", "symbol", "option_symbol", "ticker", "contract.symbol")
            if not right and isinstance(sym, str) and len(sym.split(":")[-1]) >= 15:
                right = sym.split(":")[-1][-9]
            if right != sig.structure.right:
                continue
            k = pick_num(r, "strike_usd", "strike", "strike_price", "contract.strike")
            if k is None:
                continue
            if self.cfg.strike_mode != "atm" and abs(k - spot) / spot > self.cfg.max_otm_pct + 1e-9 and \
                    ((right == "C" and k > spot) or (right == "P" and k < spot)):
                continue   # 超出虚值上限
            if best is None or abs(k - want_strike) < abs(best[0] - want_strike):
                best = (k, r, sym)
        if best is None:
            log.warning("期权链中未找到 %s %s 合约（%d 行），改用 BS 换算", sig.ticker, sig.structure.right, len(rows))
            return None
        k, r, sym = best
        bid = pick_num(r, "bid", "bid_usd", "nbbo.bid", "quote.bid")
        ask = pick_num(r, "ask", "ask_usd", "nbbo.ask", "quote.ask")
        mid = pick_num(r, "mid", "mid_usd", "mark", "nbbo.mid")
        if mid is None and bid is not None and ask is not None and ask > 0:
            mid = (bid + ask) / 2
        if mid is None:
            mid = pick_num(r, "last", "last_price", "close", "price")
        if mid is None or mid <= 0:
            log.warning("期权链合约 %s 无有效报价，改用 BS 换算", sym)
            return None
        delta = pick_num(r, "delta", "greeks.delta")
        gamma = pick_num(r, "gamma", "greeks.gamma")
        if not isinstance(sym, str) or not sym:
            sym = occ_symbol(sig.ticker, sig.snapshot.expiration, sig.structure.right, k)
        elif not sym.startswith("O:"):
            sym = "O:" + sym
        return sym, k, mid, (abs(delta) if delta is not None else None), gamma

    def open_position(self, client, sig: FullSignal, now_et: dt.datetime) -> Position | None:
        cfg = self.cfg
        want, listed = self._choose_strike(sig)
        spot = sig.snapshot.spot
        exp = sig.snapshot.expiration
        bid = ask = 0.0
        quote = self._price_from_chain(client, sig, want) if client is not None else None
        if quote:
            sym, strike, mid, delta, gamma = quote
            src = "chain"
        else:
            if listed:
                # 来自 Heatmap 的挂牌行权价，不能再取整（如 192.5 取整成 192 会生成不存在的合约）
                strike = want
            else:
                strike = _round_strike(want, spot)
                if cfg.strike_mode != "atm":   # 取整后超出虚值上限时，向现货方向退一档
                    step = _strike_step(spot)
                    if sig.structure.right == "C" and strike > spot * (1 + cfg.max_otm_pct) + 1e-6:
                        strike = round(strike - step, 2)
                    elif sig.structure.right == "P" and strike < spot * (1 - cfg.max_otm_pct) - 1e-6:
                        strike = round(strike + step, 2)
            sym = occ_symbol(sig.ticker, exp, sig.structure.right, strike)
            T = max((dt.date.fromisoformat(exp) - now_et.date()).days, 0) / 365 + \
                max(0, hhmm_to_minutes("16:00") - minutes_of_day(now_et)) / (365 * 24 * 60)
            mid, delta, gamma = bs_price_greeks(spot, strike, T, cfg.risk_free_rate, cfg.fallback_iv,
                                                sig.structure.right == "C")
            src = "bs_fallback"
            real = None
            if self.price_provider:
                real = self.price_provider(sym, now_et)
                if real is None:   # 此刻无报价（如开盘首个 5 分钟）：按 5 分钟内下一个可用报价成交
                    real = self.price_provider(sym, now_et + dt.timedelta(minutes=5))
            if real is not None and self.spread_provider:
                hq = self.spread_provider(sym, now_et)
                if hq and hq[0] <= 0:
                    log.info("【阶段4·放弃】%s %s 当时 bid 为 0（没有买盘），不开仓", sig.ticker, sym)
                    return None
                if hq and hq[1] > 0:
                    bid, ask = hq
            if real is not None:
                mid, src = real, "real_data"   # 真实成交价或真实 IV 定价；γ 仍取 BS 值（仅作兜底）
                real_delta = self.delta_provider(sym, now_et) if self.delta_provider else None
                if real_delta is not None:
                    delta = real_delta
        if self.quote_provider:
            q = self.quote_provider(sym)
            if q and (q.get("ask") or 0) > 0 and (q.get("bid") or 0) <= 0:
                log.warning("【阶段4·放弃】%s %s bid 为 0（没有买盘，买了卖不掉），不开仓", sig.ticker, sym)
                return None
            if q and (q.get("ask") or 0) > 0:
                bid, ask = q["bid"], q["ask"]
                mid, src = q.get("mid") or (bid + ask) / 2, "quote"
            elif cfg.require_live_quote:
                log.warning("【阶段4·放弃】%s %s 无实时报价，不用估算价开仓", sig.ticker, sym)
                return None
            else:
                log.info("%s 无实时报价，沿用 %s 定价", sym, src)
        if not cfg.use_chain_greeks or delta is None:
            delta = cfg.default_delta
        if not cfg.use_chain_greeks or gamma is None:
            gamma = cfg.default_gamma
        if cfg.min_entry_delta and delta is not None and delta < cfg.min_entry_delta:
            log.info("【阶段4·放弃】%s %s |δ|=%.3f < %.2f，过于虚值（彩票合约），不开仓",
                     sig.ticker, sym, delta, cfg.min_entry_delta)
            return None
        if mid < cfg.min_option_price:
            log.warning("【阶段4·放弃】%s %s 期权价 %.3f < $%.2f，报价不可信或流动性不足，不开仓",
                        sig.ticker, sym, mid, cfg.min_option_price)
            return None
        slip = (ask - bid) / (ask + bid) if (src == "real_data" and ask > 0 and bid > 0) else cfg.slippage_pct
        cost = ask if src == "quote" else mid * (1 + slip)
        p = Position(
            position_id=f"P{now_et.strftime('%Y%m%d')}-{next(self._ids):04d}",
            ticker=sig.ticker, action=sig.structure.action, right=sig.structure.right,
            structure=sig.structure.structure, contract_symbol=sym, strike=strike, expiration=exp,
            entry_time=now_et, entry_spot=spot, entry_mid=mid, entry_option_price=round(cost, 4),
            delta=delta, gamma=gamma, price_source=src, target_strike=sig.structure.neg_strike,
            contracts=cfg.contracts_per_trade, last_spot=spot, last_mid=mid,
            stop_pct=cfg.stop_loss_pct,
        )
        p.signal_score = getattr(sig, "score", 0.0)
        p.base_strike = sig.structure.pos_strike
        p.entry_bid, p.entry_ask, p.last_bid = bid, ask, bid
        if src == "real_data":
            p.slip = slip
        if cfg.stop_mode == "underlying":
            p.stop_pct = cfg.disaster_stop_pct
        p.roi_pct = self._roi(p, mid)
        p.peak_roi_pct = p.roi_pct
        p.max_adverse_roi_pct = min(0.0, p.roi_pct)
        self.positions.append(p)
        cost_note = f"按 ask 成交（bid {bid:.3f} / ask {ask:.3f}）" if src == "quote" else f"含滑点 {cfg.slippage_pct * 100:.0f}%"
        log.info("【阶段4·开仓】%s %s %s | 行权价 %.2f 到期 %s | 现货 %.2f | 中间价 %.3f → 成本 %.3f（%s）"
                 " | δ=%.3f γ=%.4f | 定价来源=%s | 目标加速位 %.2f",
                 p.position_id, p.ticker, p.action, strike, exp, spot, mid, cost, cost_note,
                 delta, gamma, src, p.target_strike)
        if self.notifier:
            self.notifier.position_opened(p)
        return p

    # ------------------------------------------------------------------ 盯市与出场
    def _exit_value(self, p: Position, mid: float) -> float:
        """可卖价：实时报价持仓一律用真实 bid（bid 为 0 就按 0 计，不再用中间价估算），其余用中间价减滑点。"""
        if p.price_source == "quote":
            return p.last_bid
        return mid * (1 - (p.slip if p.slip >= 0 else self.cfg.slippage_pct))

    def _roi(self, p: Position, mid: float) -> float:
        return (self._exit_value(p, mid) / p.entry_option_price - 1.0) * 100.0

    def current_mid(self, p: Position, spot: float, now_et: dt.datetime) -> float:
        """真实价格源优先；开仓用的是真实价但此刻无成交时沿用上一次价格，否则用 Delta-Gamma 估算。"""
        if p.price_source == "quote" and self.quote_provider:
            q = self.quote_provider(p.contract_symbol)
            if q and (q.get("ask") or 0) > 0:
                p.last_bid = q.get("bid") or 0.0
                return q.get("mid") or (p.last_bid + q["ask"]) / 2
            return p.last_mid   # 本轮无报价：沿用上次价格
        if p.price_source == "real_data" and self.price_provider:
            real = self.price_provider(p.contract_symbol, now_et)
            return real if real is not None else p.last_mid
        return self.estimate_mid(p, spot)

    def estimate_mid(self, p: Position, spot: float) -> float:
        ds = spot - p.entry_spot
        sign = 1.0 if p.right == "C" else -1.0
        dp = sign * p.delta * ds + 0.5 * p.gamma * ds * ds
        return max(0.01, p.entry_mid + dp)

    def mark(self, p: Position, spot: float, now_et: dt.datetime) -> Position | None:
        """盯市一次；若触发出场则平仓并返回该仓位。"""
        if not p.is_open:
            return None
        cfg = self.cfg
        mid = self.current_mid(p, spot, now_et)
        roi = self._roi(p, mid)
        p.last_spot, p.last_mid, p.roi_pct, p.marks = spot, mid, roi, p.marks + 1
        p.peak_roi_pct = max(p.peak_roi_pct, roi)
        p.max_adverse_roi_pct = min(p.max_adverse_roi_pct, roi)

        if cfg.trail_mode == "underlying" and spot:
            bull = p.right == "C"
            p.best_spot = spot if not p.best_spot else (max(p.best_spot, spot) if bull else min(p.best_spot, spot))
            if not p.und_trail_active and p.peak_roi_pct >= cfg.breakeven_trigger_pct:
                p.und_trail_active = True
                log.info("   ⤴ %s 峰值浮盈 %.1f%% ≥ %.0f%%，改为按标的追踪（最有利价 %.2f，回撤 %.1f%% 平仓）",
                         p.ticker, p.peak_roi_pct, cfg.breakeven_trigger_pct, p.best_spot, cfg.underlying_trail_pct * 100)
            if roi >= cfg.hard_take_profit_pct:
                return self.close(p, spot, now_et, "HARD_TAKE_PROFIT", mid)
            if p.und_trail_active:
                t = cfg.underlying_trail_pct
                if (spot < p.best_spot * (1 - t)) if bull else (spot > p.best_spot * (1 + t)):
                    return self.close(p, spot, now_et, "UNDERLYING_TRAIL", mid)
            elif cfg.stop_mode == "underlying" and p.base_strike:
                b = cfg.underlying_stop_buffer_pct
                if (spot < p.base_strike * (1 - b)) if bull else (spot > p.base_strike * (1 + b)):
                    return self.close(p, spot, now_et, "UNDERLYING_STOP", mid)
            if roi <= p.stop_pct:
                return self.close(p, spot, now_et, "STOP_LOSS", mid)
            return None

        # 状态机：止损线只升不降
        if p.peak_roi_pct >= cfg.trail_trigger_pct:
            new_stop = max(p.stop_pct, p.peak_roi_pct - cfg.trail_giveback_pct, cfg.breakeven_stop_pct)
            if p.stage != "TRAILING":
                log.info("   ⤴ %s 峰值浮盈 %.1f%% ≥ %.0f%%，激活追踪止盈（回撤 %.0f 个百分点）",
                         p.ticker, p.peak_roi_pct, cfg.trail_trigger_pct, cfg.trail_giveback_pct)
            p.stage, p.stop_pct = "TRAILING", new_stop
        elif p.peak_roi_pct >= cfg.breakeven_trigger_pct and p.stage == "INITIAL":
            p.stage, p.stop_pct = "BREAKEVEN", max(p.stop_pct, cfg.breakeven_stop_pct)
            log.info("   ⤴ %s 峰值浮盈 %.1f%% ≥ %.0f%%，止损上移至保本 %.0f%%",
                     p.ticker, p.peak_roi_pct, cfg.breakeven_trigger_pct, cfg.breakeven_stop_pct)

        log.debug("   盯市 %s 现货 %.2f 估值 %.3f ROI %.1f%% 峰值 %.1f%% 止损线 %.1f%%",
                  p.ticker, spot, mid, roi, p.peak_roi_pct, p.stop_pct)

        if roi >= cfg.hard_take_profit_pct:
            return self.close(p, spot, now_et, "HARD_TAKE_PROFIT", mid)
        if cfg.stop_mode == "underlying" and p.stage == "INITIAL" and p.base_strike and spot:
            b = cfg.underlying_stop_buffer_pct
            broken = spot < p.base_strike * (1 - b) if p.right == "C" else spot > p.base_strike * (1 + b)
            if broken:
                return self.close(p, spot, now_et, "UNDERLYING_STOP", mid)
        if roi <= p.stop_pct:
            reason = {"INITIAL": "STOP_LOSS", "BREAKEVEN": "BREAKEVEN_STOP", "TRAILING": "TRAILING_STOP"}[p.stage]
            return self.close(p, spot, now_et, reason, mid)
        return None

    def close(self, p: Position, spot: float, now_et: dt.datetime, reason: str, mid: float | None = None) -> Position:
        cfg = self.cfg
        if mid is None:
            mid = self.current_mid(p, spot, now_et)
        exit_px = self._exit_value(p, mid)
        p.exit_time, p.exit_spot, p.exit_option_price, p.exit_reason = now_et, spot, round(exit_px, 4), reason
        p.realized_roi_pct = (exit_px / p.entry_option_price - 1) * 100
        p.realized_pnl = (exit_px - p.entry_option_price) * cfg.contract_multiplier * p.contracts
        p.peak_roi_pct = max(p.peak_roi_pct, p.realized_roi_pct)
        p.max_adverse_roi_pct = min(p.max_adverse_roi_pct, p.realized_roi_pct)
        log.info("【阶段4·平仓】%s %s %s | 原因 %s | 现货 %.2f→%.2f | 期权 %.3f→%.3f | ROI %+.1f%% | 峰值 %+.1f%%"
                 " | 最大逆行 %+.1f%% | 盈亏 $%+.2f",
                 p.position_id, p.ticker, p.action, _REASON_CN.get(reason, reason), p.entry_spot, spot,
                 p.entry_option_price, exit_px, p.realized_roi_pct, p.peak_roi_pct, p.max_adverse_roi_pct,
                 p.realized_pnl)
        if self.notifier:
            self.notifier.position_closed(p)
        return p

    def force_close_all(self, now_et: dt.datetime, spots: dict[str, float], reason: str = "EOD_FORCE_CLOSE"):
        for p in self.open_positions():
            self.close(p, spots.get(p.ticker, p.last_spot), now_et, reason)

    # ------------------------------------------------------------------ 统计与导出
    def stats(self) -> dict:
        closed = [p for p in self.positions if not p.is_open]
        n = len(closed)
        if n == 0:
            return {"trades": 0}
        hit = [p for p in closed if p.peak_roi_pct >= self.cfg.direction_hit_pct]
        wins = [p for p in closed if p.realized_pnl > 0]
        by_reason: dict[str, int] = {}
        for p in closed:
            by_reason[p.exit_reason] = by_reason.get(p.exit_reason, 0) + 1
        gross_win = sum(p.realized_pnl for p in wins)
        gross_loss = -sum(p.realized_pnl for p in closed if p.realized_pnl < 0)
        return {
            "trades": n,
            "directional_accuracy_pct": 100 * len(hit) / n,
            "win_rate_pct": 100 * len(wins) / n,
            "avg_peak_mfe_pct": sum(p.peak_roi_pct for p in closed) / n,
            "avg_realized_roi_pct": sum(p.realized_roi_pct for p in closed) / n,
            "avg_mae_pct": sum(p.max_adverse_roi_pct for p in closed) / n,
            "total_pnl_usd": sum(p.realized_pnl for p in closed),
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "exit_reasons": by_reason,
            "chain_calls": self.chain_calls,
        }

    def print_report(self) -> None:
        s = self.stats()
        log.info("=" * 78)
        if s["trades"] == 0:
            log.info("复盘统计：本次运行无已平仓交易")
            log.info("=" * 78)
            return
        log.info("复盘统计：共 %d 笔", s["trades"])
        log.info("  方向正确率（峰值浮盈 ≥ +%.0f%%）: %.1f%%", self.cfg.direction_hit_pct, s["directional_accuracy_pct"])
        log.info("  胜率（实现盈亏 > 0）           : %.1f%%", s["win_rate_pct"])
        log.info("  平均峰值收益率 Avg Peak MFE    : %+.1f%%", s["avg_peak_mfe_pct"])
        log.info("  平均实现收益率                 : %+.1f%%", s["avg_realized_roi_pct"])
        log.info("  平均最大逆行 MAE               : %+.1f%%", s["avg_mae_pct"])
        log.info("  合计盈亏                       : $%+.2f（每笔 %d 张）", s["total_pnl_usd"], self.cfg.contracts_per_trade)
        pf = s["profit_factor"]
        log.info("  盈亏比 Profit Factor           : %s", "∞" if pf == float("inf") else f"{pf:.2f}")
        log.info("  出场原因分布                   : %s",
                 "，".join(f"{_REASON_CN.get(k, k)}×{v}" for k, v in s["exit_reasons"].items()))
        log.info("-" * 78)
        log.info("%-15s %-6s %-9s %-24s %8s %8s %8s  %s", "仓位", "标的", "方向", "合约", "实现%", "峰值%", "逆行%", "出场")
        for p in self.positions:
            if p.is_open:
                continue
            log.info("%-15s %-6s %-9s %-24s %+8.1f %+8.1f %+8.1f  %s %s→%s", p.position_id, p.ticker, p.action,
                     p.contract_symbol, p.realized_roi_pct, p.peak_roi_pct, p.max_adverse_roi_pct,
                     _REASON_CN.get(p.exit_reason, p.exit_reason),
                     p.entry_time.strftime("%H:%M"), p.exit_time.strftime("%H:%M"))
        log.info("=" * 78)

    def export_trades_csv(self, path: str | None = None, append: bool = False) -> str:
        path = path or self.cfg.trades_csv
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        mode = "a" if (append and exists) else "w"
        with open(path, mode, newline="", encoding="utf-8-sig" if mode == "w" else "utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if mode == "w":
                w.writeheader()
            for p in self.positions:
                if p.is_open:
                    continue
                w.writerow({
                    "position_id": p.position_id, "ticker": p.ticker, "action": p.action,
                    "contract_symbol": p.contract_symbol, "strike": f"{p.strike:.2f}",
                    "entry_time": p.entry_time.isoformat(timespec="minutes"),
                    "entry_spot": f"{p.entry_spot:.2f}", "entry_option_price": f"{p.entry_option_price:.4f}",
                    "exit_time": p.exit_time.isoformat(timespec="minutes"),
                    "exit_spot": f"{p.exit_spot:.2f}", "exit_option_price": f"{p.exit_option_price:.4f}",
                    "realized_pnl": f"{p.realized_pnl:.2f}", "realized_roi_pct": f"{p.realized_roi_pct:.2f}",
                    "peak_roi_pct": f"{p.peak_roi_pct:.2f}", "max_adverse_roi_pct": f"{p.max_adverse_roi_pct:.2f}",
                    "direction_correct": p.peak_roi_pct >= self.cfg.direction_hit_pct,
                    "exit_reason": p.exit_reason, "signal_score": f"{p.signal_score:.3f}",
                    "price_source": p.price_source,
                    "entry_bid": f"{p.entry_bid:.4f}" if p.entry_ask else "",
                    "entry_ask": f"{p.entry_ask:.4f}" if p.entry_ask else "",
                    "entry_spread_pct": f"{100 * (p.entry_ask - p.entry_bid) / ((p.entry_ask + p.entry_bid) / 2):.2f}"
                    if p.entry_ask and p.entry_bid > 0 else "",
                })
        log.info("交易明细已导出：%s", os.path.abspath(path))
        return path


_REASON_CN = {
    "STOP_LOSS": "硬止损", "BREAKEVEN_STOP": "保本止损", "TRAILING_STOP": "追踪止盈", "UNDERLYING_STOP": "标的破节点止损",
    "HARD_TAKE_PROFIT": "硬顶止盈+150%", "EOD_FORCE_CLOSE": "收盘强平", "QUOTA_HALT": "配额熔断平仓",
    "REPLACED": "被更高分信号替换", "UNDERLYING_TRAIL": "标的追踪止盈",
}


def _strike_step(spot: float) -> float:
    return 0.5 if spot < 50 else (1.0 if spot < 200 else (2.5 if spot < 500 else 5.0))


def _round_strike(k: float, spot: float) -> float:
    step = _strike_step(spot)
    return round(round(k / step) * step, 2)


def spot_from_payload(payload: dict) -> float | None:
    d = payload.get("data") if isinstance(payload, dict) else None
    try:
        v = float(d.get("spot_usd")) if isinstance(d, dict) and d.get("spot_usd") is not None else None
    except (TypeError, ValueError):
        v = None
    return v if v and v > 0 else None


# =============================================================================
# 会话引擎：把 Heatmap 快照串起 阶段2→3→4（实盘、模拟回放、历史回放共用）
# =============================================================================
class SessionEngine:
    def __init__(self, cfg: SystemConfig, client, detector: SignalDetector, trader: PaperTrader):
        self.cfg, self.client, self.detector, self.trader = cfg, client, detector, trader
        self.last_spot: dict[str, float] = {}
        self.last_exit_time: dict[str, dt.datetime] = {}
        self.pending: dict = {}   # 本轮待排序的信号：标的 → FullSignal（signal_ranking="score" 时使用）

    def can_enter(self, ticker: str, now_et: dt.datetime) -> tuple[bool, str]:
        cfg = self.cfg
        if minutes_of_day(now_et) < hhmm_to_minutes(cfg.no_new_entries_before):
            return False, f"{cfg.no_new_entries_before} 前不开新仓（开盘噪音期）"
        if minutes_of_day(now_et) >= hhmm_to_minutes(cfg.no_new_entries_after):
            return False, f"{cfg.no_new_entries_after} 后不再开新仓"
        blocked = entry_blocked_window(cfg, now_et)
        if blocked:
            return False, f"{blocked[0]}–{blocked[1]} 不开新仓"
        if len(self.trader.open_positions()) >= cfg.max_open_positions:
            return False, "持仓数已达上限"
        if cfg.max_trades_per_day and \
                sum(1 for p in self.trader.positions if p.entry_time.date() == now_et.date()) >= cfg.max_trades_per_day:
            return False, f"当日开仓已达 {cfg.max_trades_per_day} 笔上限"
        if cfg.max_trades_before and minutes_of_day(now_et) < hhmm_to_minutes(cfg.max_trades_before[0]) and \
                sum(1 for p in self.trader.positions if p.entry_time.date() == now_et.date()) >= cfg.max_trades_before[1]:
            return False, f"{cfg.max_trades_before[0]} 前已开 {cfg.max_trades_before[1]} 笔（名额留给下午）"
        if len(self.trader.trades_today(ticker, now_et.date())) >= cfg.max_trades_per_ticker_day:
            return False, "该标的当日开仓次数已达上限"
        le = self.last_exit_time.get(ticker)
        if le and (now_et - le).total_seconds() < cfg.reentry_cooldown_min * 60:
            return False, "平仓冷却期中"
        return True, ""

    def on_snapshot(self, ticker: str, payload: dict, now_et: dt.datetime) -> None:
        spot = spot_from_payload(payload)
        if spot is not None:
            self.last_spot[ticker] = spot
        pos = self.trader.open_for(ticker)
        if pos is not None:
            self.detector.observe(ticker, payload, now_et)
            if spot is not None:
                closed = self.trader.mark(pos, spot, now_et)
                if closed:
                    self.last_exit_time[ticker] = now_et
            return
        ok, why = self.can_enter(ticker, now_et)
        sig, _snap = self.detector.evaluate_full_signal(self.client, ticker, payload, now_et, allow_flow_check=ok)
        if not ok and _snap is not None and ticker in self.detector.last_structure:
            log.debug("%s 结构成立但不开仓：%s", ticker, why)
        if sig is None:
            return
        if self.cfg.signal_ranking == "score":
            self.pending[ticker] = sig   # 先收集，本轮结束后 flush() 统一排序开仓
        elif sig.score >= self.cfg.min_signal_score:
            self.trader.open_position(self.client, sig, now_et)

    def flush(self, now_et: dt.datetime) -> None:
        """一轮快照处理完后调用：按信号分数从高到低开仓，直到持仓/当日笔数上限。first_come 模式下无操作。"""
        if not self.pending:
            return
        ranked = sorted(self.pending.values(), key=lambda s: -s.score)
        self.pending = {}
        opened, skipped = [], []
        for sig in ranked:
            if sig.score < self.cfg.min_signal_score:
                skipped.append(f"{sig.ticker}({sig.score:.2f}<门槛)")
                continue
            ok, why = self.can_enter(sig.ticker, now_et)
            if ok and self.cfg.max_same_direction and sum(
                    1 for p in self.trader.open_positions() if p.right == sig.structure.right) >= self.cfg.max_same_direction:
                ok, why = False, f"同方向持仓已达 {self.cfg.max_same_direction} 笔"
            if not ok and why == "持仓数已达上限" and self._replace_weakest(sig, now_et):
                ok, why = self.can_enter(sig.ticker, now_et)
            if not ok:
                skipped.append(f"{sig.ticker}({sig.score:.2f},{why})")
                continue
            if self.trader.open_position(self.client, sig, now_et) is not None:
                opened.append(f"{sig.ticker}({sig.score:.2f})")
        log.info("【排序】%s 本轮信号 %d 个 → 开仓 %s%s", now_et.strftime("%H:%M"), len(ranked),
                 "、".join(opened) or "无", ("；未开：" + "、".join(skipped[:8]) + ("…" if len(skipped) > 8 else "")) if skipped else "")

    def _replace_weakest(self, sig, now_et: dt.datetime) -> bool:
        """持仓已满时：若新信号分数比某个未盈利持仓的开仓分数高出 rank_replace_margin，平掉分数最低的那个腾位。"""
        cfg = self.cfg
        if cfg.rank_replace_margin <= 0:
            return False
        weak = [p for p in self.trader.open_positions()
                if p.roi_pct <= cfg.rank_replace_max_roi and p.signal_score + cfg.rank_replace_margin <= sig.score]
        if not weak:
            return False
        p = min(weak, key=lambda x: x.signal_score)
        log.info("【替换】%s(%.2f, ROI %+.1f%%) → %s(%.2f)", p.ticker, p.signal_score, p.roi_pct, sig.ticker, sig.score)
        self.trader.close(p, self.last_spot.get(p.ticker, p.last_spot), now_et, "REPLACED")
        self.last_exit_time[p.ticker] = now_et
        return True

    def end_of_day(self, now_et: dt.datetime) -> None:
        if self.trader.open_positions():
            log.info("【收盘】%s ET 强制清平全部未结头寸（%d 笔）", now_et.strftime("%H:%M"),
                     len(self.trader.open_positions()))
        self.trader.force_close_all(now_et, self.last_spot)
