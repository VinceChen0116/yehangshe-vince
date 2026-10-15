"""
detector.py —— 阶段1 标的池过滤 / 阶段2 跳板·悬崖结构识别 / 阶段3 Flow 大单确认
==============================================================================
核心入口：SignalDetector.evaluate_full_signal(...)

字段兼容说明：
  夜航社 OpenAPI 对 Heatmap 给出了严格 schema（spot_usd / expirations / cells[strike_usd, expiration,
  net_dealer_gex_usd]），本模块按其精确解析。成交量榜单与 Flow 成交行的 schema 未在 OpenAPI 中公开，
  因此采用"多候选字段名"容错解析；若解析不到关键字段会打印该行的字段名，便于你据实调整。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from config import INDEX_BLACKLIST, KNOWN_ETFS, SystemConfig, parse_iso, to_et, trading_dte
from yhs_client import YHSApiError

log = logging.getLogger("yhs.detector")

VOLUME_RANK_PATH = "/v1/market/options-volume-rank"
HEATMAP_PATH = "/v1/derived/heatmap/{ticker}/snapshot"
FLOW_TRADES_PATH = "/v1/flow/trades"
CONTRACT_LOOKUP_PATH = "/v1/flow/contracts/lookup"


# =============================================================================
# 通用容错取值
# =============================================================================
def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick(d: Any, *paths: str, default=None):
    """按多个候选路径（支持 a.b.c）取第一个非 None 值。"""
    if not isinstance(d, dict):
        return default
    for p in paths:
        cur: Any = d
        ok = True
        for part in p.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def pick_num(d: Any, *paths: str) -> float | None:
    return _num(pick(d, *paths))


def extract_rows(payload: Any) -> list[dict]:
    """从 {data: [...]} 或 {data: {rows/items/...: [...]}} 中提取行列表。"""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for k in ("rows", "items", "results", "trades", "tickers", "ranking", "rank", "list",
                  "contracts", "data", "chain", "options"):
            v = data.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        # 兜底：取第一个元素为 dict 的 list
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


# =============================================================================
# 阶段 1：标的池过滤
# =============================================================================
@dataclass
class UniverseEntry:
    ticker: str
    rank: int
    total_volume: float
    call_volume: float
    put_volume: float
    oi_current: float | None
    oi_change_pct: float | None
    vol_oi_ratio: float | None
    pcr: float | None
    bias: str                 # BULL / BEAR / NEUTRAL
    score: float
    reasons: list[str] = field(default_factory=list)
    is_etf: bool = False
    spot_checked: bool = False
    spot_usd: float | None = None


def _oi_group(row: dict, group: str) -> tuple[float | None, float | None, float | None]:
    """返回 (current, previous, change_pct)。group ∈ total/call/put。"""
    candidates = [
        f"open_interest.{group}", f"oi.{group}", f"{group}_oi", f"{group}_open_interest",
        f"oi_{group}", f"open_interest_{group}",
    ]
    if group == "total":
        candidates += ["open_interest", "oi", "total_open_interest"]
    for c in candidates:
        g = pick(row, c)
        if isinstance(g, dict):
            cur = pick_num(g, "current", "value", "oi")
            prev = pick_num(g, "previous", "prev", "prior")
            chg = pick_num(g, "change_pct", "pct_change", "change_percent")
            if chg is None:
                ch = pick_num(g, "change")
                if ch is not None and prev:
                    chg = ch / prev
            if cur is not None or chg is not None:
                return cur, prev, chg
        elif _num(g) is not None:
            cur = _num(g)
            pre = "" if group == "total" else f"{group}_"
            prev = pick_num(row, f"{pre}oi_previous", f"{pre}open_interest_previous", f"{c}_previous")
            chg = pick_num(row, f"{pre}oi_change_pct", f"{pre}open_interest_change_pct", f"{c}_change_pct")
            return cur, prev, chg
    return None, None, None


class UniverseFilter:
    """阶段1：每日仅拉取一次成交量榜单（1 次配额），结果落盘缓存，重启不重复扣费。"""

    def __init__(self, cfg: SystemConfig):
        self.cfg = cfg

    def cache_path(self, day: dt.date) -> str:
        c = self.cfg
        suffix = "" if (c.require_volume_anomaly and c.require_pcr_extreme) else \
            f"_va{int(c.require_volume_anomaly)}_pcr{int(c.require_pcr_extreme)}"
        return os.path.join(c.data_dir, f"universe_{day.isoformat()}{suffix}.json")

    def build(self, client, day: dt.date, force: bool = False) -> list[UniverseEntry]:
        path = self.cache_path(day)
        if not force and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                entries = [UniverseEntry(**e) for e in raw["entries"]]
                log.info("【阶段1】命中当日标的池缓存 %s（%d 只），不消耗配额", path, len(entries))
                return entries
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as e:
                log.warning("标的池缓存损坏（%s），重新拉取", e)
        log.info("【阶段1】拉取当日期权成交量 Top150 榜单（计费 1 次）…")
        payload = client.get(VOLUME_RANK_PATH)
        rows = extract_rows(payload)
        if not rows:
            raise RuntimeError(f"成交量榜单解析为空，响应顶层字段：{list(payload.keys()) if isinstance(payload, dict) else type(payload)}")
        entries = self.filter_rows(rows)
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": day.isoformat(), "entries": [asdict(e) for e in entries]}, f,
                      ensure_ascii=False, indent=1)
        return entries

    def filter_rows(self, rows: list[dict]) -> list[UniverseEntry]:
        cfg = self.cfg
        parsed = []
        for i, r in enumerate(rows):
            t = str(pick(r, "ticker", "symbol", "underlying", "root", default="")).upper().strip()
            if not t:
                continue
            tv = pick_num(r, "total_volume", "volume", "options_volume", "volume.total")
            cv = pick_num(r, "call_volume", "volume.call", "calls_volume")
            pv = pick_num(r, "put_volume", "volume.put", "puts_volume")
            if tv is None and cv is not None and pv is not None:
                tv = cv + pv
            parsed.append((t, r, tv or 0.0, cv or 0.0, pv or 0.0, int(pick_num(r, "rank") or i + 1)))
        parsed.sort(key=lambda x: -x[2])
        top = parsed[: cfg.universe_top_n]
        log.info("【阶段1】榜单共 %d 行，截取成交量前 %d 名", len(parsed), len(top))

        stats = {"index": 0, "etf": 0, "no_anomaly": 0, "no_pcr": 0}
        whitelist = set(cfg.etf_whitelist)
        out: list[UniverseEntry] = []
        warned = False
        for t, r, tv, cv, pv, rank in top:
            if t in INDEX_BLACKLIST or t.startswith("^") or t.startswith("$"):
                stats["index"] += 1
                continue
            asset_type = str(pick(r, "asset_type", "type", "security_type", "instrument_type", default="")).lower()
            is_etf = bool(pick(r, "is_etf", default=False)) or asset_type in ("etf", "etn", "fund") or t in KNOWN_ETFS
            if asset_type == "index":
                stats["index"] += 1
                continue
            if is_etf and t not in whitelist:
                stats["etf"] += 1
                continue

            oi_cur, _oi_prev, oi_chg = _oi_group(r, "total")
            if oi_cur is None and not warned:
                log.warning("榜单行未解析到 OI 字段，行字段名：%s（Vol/OI 将仅依赖 OI 增幅）", sorted(r.keys()))
                warned = True
            vol_oi = (tv / oi_cur) if oi_cur else None
            pcr = (pv / cv) if cv else None

            reasons, score = [], 0.0
            vol_ok = False
            if vol_oi is not None and vol_oi >= cfg.vol_oi_min:
                vol_ok = True
                reasons.append(f"Vol/OI={vol_oi:.2f}")
                score += min(vol_oi, 3.0)
            if oi_chg is not None and oi_chg >= cfg.oi_growth_min:
                vol_ok = True
                reasons.append(f"OI增幅={oi_chg:.1%}")
                score += min(oi_chg * 10, 3.0)
            bias = "NEUTRAL"
            if pcr is not None and pcr <= cfg.pcr_bull_max:
                bias = "BULL"
                reasons.append(f"PCR={pcr:.2f}偏多")
                score += (cfg.pcr_bull_max - pcr) * 3 + 1
            elif pcr is not None and pcr >= cfg.pcr_bear_min:
                bias = "BEAR"
                reasons.append(f"PCR={pcr:.2f}偏空")
                score += min((pcr - cfg.pcr_bear_min), 2.0) + 1

            if cfg.require_volume_anomaly and not vol_ok:
                stats["no_anomaly"] += 1
                continue
            if cfg.require_pcr_extreme and bias == "NEUTRAL":
                stats["no_pcr"] += 1
                continue
            out.append(UniverseEntry(t, rank, tv, cv, pv, oi_cur, oi_chg, vol_oi, pcr, bias,
                                     round(score, 3), reasons, is_etf))

        out.sort(key=lambda e: -e.score)
        log.info("【阶段1】剔除：指数 %d、非白名单 ETF %d、量能不足 %d、PCR 未极端 %d → 入池 %d 只"
                 "（股价 ≥ $%.0f 门槛将在首次 Heatmap 拉取时用 spot_usd 校验，零额外配额）",
                 stats["index"], stats["etf"], stats["no_anomaly"], stats["no_pcr"], len(out), cfg.min_spot_usd)
        for e in out:
            log.info("   入池 %-6s 偏向=%-7s 评分=%.2f  %s", e.ticker, e.bias, e.score, "，".join(e.reasons))
        return out


def is_strict(e: UniverseEntry, cfg: SystemConfig) -> bool:
    """是否满足需求文档阶段 1 的严格初筛（A 方案）：量能异动 且 PCR 极端。"""
    anomaly = (e.vol_oi_ratio is not None and e.vol_oi_ratio >= cfg.vol_oi_min) or \
              (e.oi_change_pct is not None and e.oi_change_pct >= cfg.oi_growth_min)
    return anomaly and e.bias != "NEUTRAL"


# =============================================================================
# 阶段 2：Heatmap 解析与结构识别
# =============================================================================
@dataclass
class HeatmapSnapshot:
    ticker: str
    generated_at: dt.datetime | None
    session_date: dt.date | None
    spot: float
    expiration: str
    king_call_strike: float      # 最大正 GEX 节点
    king_call_gex: float
    king_put_strike: float       # 最大负 GEX 节点
    king_put_gex: float
    n_cells: int
    strikes: tuple = ()          # 该到期日 Heatmap 中出现的全部挂牌行权价（升序）
    total_abs_gex: float = 0.0   # 该到期日全部节点 |GEX| 之和（算节点占比用）
    cells: tuple = ()            # ((行权价, 净 GEX), ...) 升序，局部节点识别用


def parse_heatmap(payload: dict, cfg: SystemConfig, ref_date: dt.date) -> HeatmapSnapshot | None:
    d = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(d, dict):
        return None
    spot = _num(d.get("spot_usd"))
    if spot is None or spot <= 0:
        return None
    cells = [c for c in d.get("cells") or [] if isinstance(c, dict)]
    exps = sorted({str(c.get("expiration")) for c in cells if c.get("expiration")} | set(d.get("expirations") or []))
    sess = None
    try:
        sess = dt.date.fromisoformat(str(d.get("session_date_et"))) if d.get("session_date_et") else None
    except ValueError:
        pass
    base = sess or ref_date

    def dte(e: str) -> int:
        try:
            return trading_dte(dt.date.fromisoformat(e), base)
        except ValueError:
            return 10_000

    # 有 0DTE 用 0DTE，否则用最近的到期（expiry_max_trading_dte 可限制最远 DTE，超出则不给信号）
    cap = cfg.expiry_max_trading_dte
    valid = [e for e in exps if 0 <= dte(e) and (cap is None or dte(e) <= cap)
             and any(c.get("expiration") == e for c in cells)]
    if not valid:
        return None
    exp = min(valid, key=dte)
    ec = [c for c in cells if c.get("expiration") == exp and _num(c.get("net_dealer_gex_usd")) is not None]
    pos = [c for c in ec if _num(c["net_dealer_gex_usd"]) > 0]
    neg = [c for c in ec if _num(c["net_dealer_gex_usd"]) < 0]
    if not pos or not neg:
        return None
    kc = max(pos, key=lambda c: _num(c["net_dealer_gex_usd"]))
    kp = min(neg, key=lambda c: _num(c["net_dealer_gex_usd"]))
    gen = parse_iso(d.get("generated_at"))
    return HeatmapSnapshot(
        ticker=str(d.get("ticker", "")).upper(),
        generated_at=to_et(gen) if gen else None,
        session_date=sess,
        spot=spot,
        expiration=exp,
        king_call_strike=float(kc["strike_usd"]),
        king_call_gex=_num(kc["net_dealer_gex_usd"]),
        king_put_strike=float(kp["strike_usd"]),
        king_put_gex=_num(kp["net_dealer_gex_usd"]),
        n_cells=len(ec),
        strikes=tuple(sorted({float(c["strike_usd"]) for c in ec if c.get("strike_usd") is not None})),
        total_abs_gex=sum(abs(_num(c["net_dealer_gex_usd"])) for c in ec),
        cells=tuple(sorted((float(c["strike_usd"]), _num(c["net_dealer_gex_usd"])) for c in ec
                           if c.get("strike_usd") is not None)),
    )


@dataclass
class StructureSignal:
    structure: str        # SPRINGBOARD / CLIFF
    action: str           # BUY_CALL / BUY_PUT
    right: str            # C / P
    pos_strike: float
    neg_strike: float
    spot: float
    distance_pct: float   # 现货距正节点距离（小数）
    pos_gex: float | None = None   # 局部节点模式下正 / 负节点自身的 GEX（评分用）；None = 用全局最大节点
    neg_gex: float | None = None


def detect_local_structure(s: HeatmapSnapshot, cfg: SystemConfig) -> StructureSignal | None:
    """局部节点：现货 ±node_distance_max_pct 内最大的显著正 GEX 为地板（下方）/ 天花板（上方），
    地板到上方下一个显著正节点之间最强的负 GEX 为加速区（跳板）；天花板到下方上一个显著正节点之间最强的负 GEX 为深渊（悬崖）。
    上下都成立时取离现货更近的正节点。"""
    spot, w = s.spot, cfg.node_distance_max_pct
    cells = list(s.cells)
    if not cells or not spot:
        return None
    sig_min = cfg.local_min_share * (s.total_abs_gex or 0.0)
    walls = [(k, g) for k, g in cells if g > 0 and g >= sig_min]
    out = []
    floor = max(((k, g) for k, g in walls if spot * (1 - w) <= k <= spot), key=lambda x: x[1], default=None)
    if floor:
        nxt = min((k for k, _ in walls if k > spot), default=float("inf"))
        negs = [(k, g) for k, g in cells if spot < k < nxt and g < 0]
        if negs:
            nk, ng = min(negs, key=lambda x: x[1])
            out.append(StructureSignal("SPRINGBOARD", "BUY_CALL", "C", floor[0], nk, spot, (spot - floor[0]) / floor[0],
                                       floor[1], ng))
    ceil = max(((k, g) for k, g in walls if spot <= k <= spot * (1 + w)), key=lambda x: x[1], default=None)
    if ceil:
        prv = max((k for k, _ in walls if k < spot), default=float("-inf"))
        negs = [(k, g) for k, g in cells if prv < k < spot and g < 0]
        if negs:
            nk, ng = min(negs, key=lambda x: x[1])
            out.append(StructureSignal("CLIFF", "BUY_PUT", "P", ceil[0], nk, spot, (ceil[0] - spot) / ceil[0],
                                       ceil[1], ng))
    if not out:
        return None
    return min(out, key=lambda x: (x.distance_pct, -(x.pos_gex or 0)))


def detect_structure(s: HeatmapSnapshot, cfg: SystemConfig) -> StructureSignal | None:
    if getattr(cfg, "node_mode", "king") == "local":
        return detect_local_structure(s, cfg)
    pos, neg, spot = s.king_call_strike, s.king_put_strike, s.spot
    dist = abs(spot - pos) / pos
    if dist > cfg.node_distance_max_pct:
        return None
    # 跳板：正节点(地板) < 负节点(加速区)，现货站在地板之上
    if pos < neg and spot >= pos and spot < neg:
        return StructureSignal("SPRINGBOARD", "BUY_CALL", "C", pos, neg, spot, dist)
    # 悬崖：正节点(天花板) > 负节点(深渊)，现货压在天花板之下
    if pos > neg and spot <= pos and spot > neg:
        return StructureSignal("CLIFF", "BUY_PUT", "P", pos, neg, spot, dist)
    return None


class NodeFatigueTracker:
    """统计现货对同一正 GEX 节点的测试回踩次数。离开 ≥ reset 带宽后再次进入 touch 带宽记为新一次测试。"""

    def __init__(self, cfg: SystemConfig):
        self.cfg = cfg
        self._state: dict[str, dict] = {}

    def update(self, ticker: str, strike: float, spot: float) -> int:
        st = self._state.get(ticker)
        if st is None or abs(st["strike"] - strike) > 1e-9:
            st = {"strike": strike, "tests": 0, "away": True}
            self._state[ticker] = st
        dist = abs(spot - strike) / strike
        if dist <= self.cfg.fatigue_touch_band_pct:
            if st["away"]:
                st["tests"] += 1
                st["away"] = False
                log.debug("%s 第 %d 次测试正节点 %.2f（现货 %.2f）", ticker, st["tests"], strike, spot)
        elif dist >= self.cfg.fatigue_reset_band_pct:
            st["away"] = True
        return st["tests"]

    def tests(self, ticker: str) -> int:
        return self._state.get(ticker, {}).get("tests", 0)

    def reset(self) -> None:
        self._state.clear()


# =============================================================================
# 阶段 3：Flow 大单确认
# =============================================================================
@dataclass
class FlowConfirmation:
    ok: bool
    reason: str
    contract_symbol: str | None = None
    premium_usd: float | None = None
    occurred_at: str | None = None
    day_volume: float | None = None
    open_interest: float | None = None
    candidates: int = 0


def _row_right(r: dict) -> str:
    v = str(pick(r, "right", "option_type", "put_call", "type", "contract.right", default="")).upper()
    if v.startswith("C"):
        return "C"
    if v.startswith("P"):
        return "P"
    sym = str(pick(r, "contract_symbol", "symbol", "option_symbol", default=""))
    core = sym.split(":")[-1]
    if len(core) >= 15:
        ch = core[-9]
        if ch in "CP":
            return ch
    return ""


def _row_is_ask(r: dict) -> bool:
    v = str(pick(r, "side", "aggressor", "aggressor_side", "trade_side", "execution_side", default="")).lower()
    return v in ("ask", "a", "at_ask", "above_ask", "aa", "buy", "bought", "b_ask") or "ask" in v


class FlowConfirmer:
    def __init__(self, cfg: SystemConfig):
        self.cfg = cfg
        self._param_style_ok: bool | None = None  # sides/rights 服务端过滤是否被接受

    def _fetch(self, client, ticker: str, right: str, session_date: dt.date) -> list[dict]:
        base = {
            "tickers": ticker, "kind": "order", "min_premium_usd": int(self.cfg.flow_min_premium_usd),
            "sort_by": "occurredAt", "direction": "desc", "limit": self.cfg.flow_page_limit,
            "session_date": session_date.isoformat(),
        }
        if self._param_style_ok is not False:
            try:
                p = dict(base, sides="A,AA", rights=right)   # 接口的方向代码：A = at_ask，AA = above_ask
                rows = extract_rows(client.get(FLOW_TRADES_PATH, p))
                self._param_style_ok = True
                return rows
            except YHSApiError as e:
                if e.status != 400:
                    raise
                log.info("Flow 服务端 sides/rights 过滤参数不被接受（400，免费），改为客户端过滤")
                self._param_style_ok = False
        return extract_rows(client.get(FLOW_TRADES_PATH, base))

    def confirm(self, client, ticker: str, right: str, now_et: dt.datetime,
                window_min: int | None = None) -> FlowConfirmation:
        cfg = self.cfg
        rows = self._fetch(client, ticker, right, now_et.date())
        win_start = now_et - dt.timedelta(minutes=window_min or cfg.flow_window_min)
        cands = []
        for r in rows:
            ts = parse_iso(pick(r, "occurred_at", "occurredAt", "executed_at", "timestamp", "time", "ts"))
            if ts is None:
                continue
            ts = to_et(ts)
            if not (win_start <= ts <= now_et):
                continue
            prem = pick_num(r, "premium_usd", "premium", "total_premium_usd", "notional_usd")
            if prem is None:
                pc = pick_num(r, "premium_cents", "premiumCents")
                prem = pc / 100 if pc is not None else None
            if prem is None or prem < cfg.flow_min_premium_usd:
                continue
            if _row_right(r) != right or not _row_is_ask(r):
                continue
            # 可选：只认近期到期、行权价贴近现价的大单（远期/深度虚实值多为对冲或调仓，和日内方向无关）
            if cfg.flow_max_dte:
                dte = pick_num(r, "min_dte", "dte")
                if dte is None or dte > cfg.flow_max_dte:
                    continue
            if cfg.flow_max_moneyness_pct:
                mny = pick_num(r, "min_moneyness_pct", "moneyness_pct")
                if mny is None or abs(mny) > cfg.flow_max_moneyness_pct:
                    continue
            cands.append((ts, prem, r))
        if not cands:
            return FlowConfirmation(False, f"近 {cfg.flow_window_min} 分钟无 ≥${cfg.flow_min_premium_usd:,.0f} 的 Ask 侧{'Call' if right == 'C' else 'Put'}大单")

        cands.sort(key=lambda x: -x[1])
        lookups = 0
        last_reason = ""
        for ts, prem, r in cands:
            sym = pick(r, "contract_symbol", "symbol", "option_symbol")
            vol = pick_num(r, "day_volume", "contract_day_volume", "volume", "contract_volume", "session_volume")
            oi = pick_num(r, "open_interest", "oi", "contract_open_interest", "prev_open_interest")
            if cfg.flow_require_vol_ge_oi and (vol is None or oi is None) and sym \
                    and cfg.flow_contract_lookup_fallback and lookups < cfg.flow_max_lookups_per_signal:
                lookups += 1
                try:
                    lk = client.get(CONTRACT_LOOKUP_PATH, {"contract_symbol": sym,
                                                          "session_date": now_et.date().isoformat()})
                    lrow = extract_rows(lk)
                    src = lrow[0] if lrow else (lk.get("data") if isinstance(lk.get("data"), dict) else {})
                    vol = vol if vol is not None else pick_num(src, "volume", "day_volume", "activity.volume", "contracts")
                    oi = oi if oi is not None else pick_num(src, "open_interest", "oi", "activity.open_interest")
                except YHSApiError as e:
                    log.warning("合约补查失败 %s：%s", sym, e)
            fc = FlowConfirmation(True, "", sym, prem, ts.strftime("%H:%M:%S"), vol, oi, len(cands))
            if not cfg.flow_require_vol_ge_oi:
                fc.reason = "Ask 侧大单确认（未启用 Vol≥OI 检查）"
                return fc
            if vol is None or oi is None:
                last_reason = f"{sym} 无法获得当日成交量/OI，无法验证主动建仓"
                continue
            if vol >= oi:
                fc.reason = f"Ask 侧大单 ${prem:,.0f}，合约当日量 {vol:,.0f} ≥ OI {oi:,.0f}（日内主动建仓）"
                return fc
            last_reason = f"{sym} 当日量 {vol:,.0f} < OI {oi:,.0f}（疑似平仓）"
        return FlowConfirmation(False, last_reason or "大单未通过 Vol≥OI 检查", candidates=len(cands))


# =============================================================================
# 综合信号
# =============================================================================
@dataclass
class FullSignal:
    ticker: str
    time_et: dt.datetime
    snapshot: HeatmapSnapshot
    structure: StructureSignal
    flow: FlowConfirmation
    bias: str
    fatigue_tests: int
    score: float = 0.0
    score_parts: dict = field(default_factory=dict)


def score_signal(snap: HeatmapSnapshot, st: StructureSignal, tests: int, spots: list, cfg: SystemConfig) -> tuple:
    """信号质量分（0–1）与各分项。依据需求文档的跳板/悬崖逻辑，尚未经数据验证（见 backtests/signals）。
    spots：该标的当日此前的 5 分钟现货序列（含当前），用于估算 1 小时波动。"""
    total = snap.total_abs_gex or 0.0
    pg = st.pos_gex if st.pos_gex is not None else snap.king_call_gex
    ng = st.neg_gex if st.neg_gex is not None else snap.king_put_gex
    pos_share = pg / total if total else 0.0
    neg_share = abs(ng) / total if total else 0.0
    room = abs(st.neg_strike - snap.spot) / snap.spot
    rets = [math.log(b / a) for a, b in zip(spots, spots[1:]) if a > 0 and b > 0]
    vol_1h = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(12) if len(rets) >= 3 else None
    if vol_1h:
        rv = room / vol_1h   # 目标距离 / 1 小时波动；≈1 最好，<0.3 没肉，>2.5 到不了
        reach = max(0.0, 1 - abs(math.log(max(rv, 1e-6))) / math.log(4)) if rv > 0 else 0.0
    else:
        reach = 0.5          # 开盘初期波动样本不足，给中性分
    parts = {
        "fuel": min(neg_share / 0.4, 1.0),
        "floor": min(pos_share / 0.4, 1.0),
        "proximity": max(0.0, 1 - st.distance_pct / cfg.node_distance_max_pct),
        "distance": min(1.0, st.distance_pct / cfg.node_distance_max_pct),   # proximity 的反向：离正节点越远分越高
        "reach": reach,
        "fresh": {0: 1.0, 1: 0.5}.get(tests, 0.0),
    }
    w = dict(cfg.score_weights)
    score = sum(w.get(k, 0.0) * v for k, v in parts.items()) / (sum(w.values()) or 1.0)
    return round(score, 4), {k: round(v, 3) for k, v in parts.items()}


class SignalDetector:
    def __init__(self, cfg: SystemConfig, universe: Iterable[UniverseEntry] = ()):
        self.cfg = cfg
        self.universe: dict[str, UniverseEntry] = {e.ticker: e for e in universe}
        self.fatigue = NodeFatigueTracker(cfg)
        self.flow = FlowConfirmer(cfg)
        self.rejected_spot: set[str] = set()
        self.last_structure: dict[str, str] = {}
        self._last_note: dict[str, str] = {}
        self._flow_next_ok: dict[str, dt.datetime] = {}
        self.flow_disabled = False   # Flow 返回 401/403 权限错误后置 True
        self._flow_rejected: dict[str, tuple] = {}   # flow_mode="lookback"：已被否决的结构，同一结构不再等后来的大单
        self.spot_hist: dict[str, list] = {}   # 标的 → 当日 5 分钟现货序列（打分用）
        self.stats = {"snapshots": 0, "structures": 0, "fatigue_reject": 0, "pcr_reject": 0,
                      "flow_calls": 0, "flow_throttled": 0, "flow_reject": 0, "signals": 0, "stale_skip": 0}

    def _scored(self, sig: FullSignal) -> FullSignal:
        spots = [s for _, s in self.spot_hist.get(sig.ticker, (None, []))[1]]
        sig.score, sig.score_parts = score_signal(sig.snapshot, sig.structure, sig.fatigue_tests, spots, self.cfg)
        return sig

    def _note(self, ticker: str, key: str, msg: str, *args) -> None:
        """同一标的同类拒绝原因只在首次出现时以 INFO 打印，之后降为 DEBUG，避免刷屏。"""
        if self._last_note.get(ticker) != key:
            self._last_note[ticker] = key
            log.info(msg, *args)
        else:
            log.debug(msg, *args)

    def set_universe(self, universe: Iterable[UniverseEntry]) -> None:
        self.universe = {e.ticker: e for e in universe}
        self._flow_next_ok.clear()
        self._last_note.clear()
        self.last_structure.clear()
        self.fatigue.reset()
        self.rejected_spot.clear()

    def observe(self, ticker: str, payload: dict, now_et: dt.datetime) -> HeatmapSnapshot | None:
        """解析快照 + 股价门槛 + 疲劳度更新（不触发任何额外请求）。"""
        snap = parse_heatmap(payload, self.cfg, now_et.date())
        if snap is None:
            log.debug("%s Heatmap 无可用近周节点，跳过", ticker)
            return None
        self.stats["snapshots"] += 1
        if snap.generated_at and (now_et - snap.generated_at).total_seconds() > self.cfg.max_snapshot_age_min * 60:
            self.stats["stale_skip"] += 1
            log.warning("%s Heatmap 内容时间 %s 过旧，跳过本轮信号判断", ticker, snap.generated_at.strftime("%H:%M"))
            return snap
        entry = self.universe.get(ticker)
        if entry and not entry.spot_checked:
            entry.spot_checked, entry.spot_usd = True, snap.spot
            if snap.spot < self.cfg.min_spot_usd:
                self.rejected_spot.add(ticker)
                log.info("【阶段1·股价门槛】%s 现价 $%.2f < $%.2f，移出标的池", ticker, snap.spot, self.cfg.min_spot_usd)
        self.fatigue.update(ticker, snap.king_call_strike, snap.spot)
        day, hist = self.spot_hist.get(ticker, (None, []))
        if day != now_et.date():
            hist = []
        if not hist or hist[-1][0] != now_et.replace(second=0, microsecond=0):
            hist.append((now_et.replace(second=0, microsecond=0), snap.spot))
        self.spot_hist[ticker] = (now_et.date(), hist[-48:])
        return snap

    def evaluate_full_signal(self, client, ticker: str, payload: dict, now_et: dt.datetime,
                             allow_flow_check: bool = True) -> tuple[FullSignal | None, HeatmapSnapshot | None]:
        """阶段2 + 阶段3 综合判断。返回 (信号或 None, 快照)。仅在结构成立时才调用 Flow（按需计费）。"""
        snap = self.observe(ticker, payload, now_et)
        if snap is None or ticker in self.rejected_spot:
            return None, snap
        if snap.generated_at and (now_et - snap.generated_at).total_seconds() > self.cfg.max_snapshot_age_min * 60:
            return None, snap
        st = detect_structure(snap, self.cfg)
        if st is None:
            self.last_structure.pop(ticker, None)
            self._last_note.pop(ticker, None)
            return None, snap
        self.stats["structures"] += 1
        if self.last_structure.get(ticker) != st.structure:
            log.info("【阶段2】%s 识别到%s结构：现货 %.2f，正节点 %.2f（距 %.2f%%），负节点 %.2f，到期 %s",
                     ticker, "跳板(看涨)" if st.structure == "SPRINGBOARD" else "悬崖(看跌)",
                     snap.spot, st.pos_strike, st.distance_pct * 100, st.neg_strike, snap.expiration)
        self.last_structure[ticker] = st.structure

        tests = self.fatigue.tests(ticker)
        if tests > self.cfg.fatigue_max_tests:
            self.stats["fatigue_reject"] += 1
            self._note(ticker, f"fatigue{st.pos_strike}", "【阶段2·疲劳】%s 正节点 %.2f 已被测试 %d 次 > %d，对冲垫片耗尽，放弃信号",
                     ticker, st.pos_strike, tests, self.cfg.fatigue_max_tests)
            return None, snap
        entry = self.universe.get(ticker)
        bias = entry.bias if entry else "NEUTRAL"
        if self.cfg.pcr_direction_filter and ((bias == "BEAR" and st.right == "C") or (bias == "BULL" and st.right == "P")):
            self.stats["pcr_reject"] += 1
            self._note(ticker, "pcr", "【阶段2·方向】%s 结构 %s 与 PCR 偏向 %s 相反，放弃", ticker, st.action, bias)
            return None, snap
        if not allow_flow_check:
            return None, snap
        if not self.cfg.require_flow_confirm:
            self.stats["signals"] += 1
            self._last_note.pop(ticker, None)
            fc = FlowConfirmation(True, "仅 Heatmap 模式（跳过阶段3 Flow 确认）")
            log.info("【阶段3·跳过】%s %s：仅 Heatmap 模式，结构成立直接开仓", ticker, st.action)
            sig = self._scored(FullSignal(ticker, now_et, snap, st, fc, bias, tests))
            if self.cfg.flow_mode == "bonus" and self.cfg.flow_bonus and not self.flow_disabled:
                # Flow 只作排序加分：不拦信号，近 flow_window_min 分钟内有符合规则的大单就给分数加 flow_bonus
                try:
                    bfc = self.flow.confirm(client, ticker, st.right, now_et)
                    self.stats["flow_calls"] += 1
                    if bfc.ok:
                        sig.flow, sig.score = bfc, sig.score + self.cfg.flow_bonus
                        sig.score_parts["flow_bonus"] = self.cfg.flow_bonus
                        log.info("【阶段3·加分】%s Flow 大单 +%.2f：%s", ticker, self.cfg.flow_bonus, bfc.reason)
                except YHSApiError as e:
                    if e.status in (401, 403):
                        self.flow_disabled = True
                    log.warning("【阶段3·加分】%s Flow 查询失败：%s", ticker, e)
            return sig, snap
        if self.flow_disabled:
            return None, snap
        key = (st.structure, st.pos_strike, st.neg_strike)
        if self.cfg.flow_mode == "lookback" and self._flow_rejected.get(ticker) == key:
            return None, snap   # 同一结构首次出现时没有先行大单 → 不等后来的大单
        nxt = self._flow_next_ok.get(ticker)
        if nxt and now_et < nxt:
            self.stats["flow_throttled"] += 1
            return None, snap

        self.stats["flow_calls"] += 1
        try:
            fc = self.flow.confirm(client, ticker, st.right, now_et,
                                   self.cfg.flow_lookback_min if self.cfg.flow_mode == "lookback" else None)
        except YHSApiError as e:
            if e.status in (401, 403):
                # 权限类错误重试无意义：本会话内停用 Flow，避免每轮重复失败
                self.flow_disabled = True
                log.error("【阶段3】Flow 无访问权限（%s），本会话停止请求 Flow。阶段3 无法确认，将不会开仓；"
                          "如需仅按 Heatmap 结构开仓，请在 config.json 设置 \"require_flow_confirm\": false", e)
            else:
                log.warning("【阶段3】%s Flow 查询失败：%s", ticker, e)
            return None, snap
        if not fc.ok:
            self.stats["flow_reject"] += 1
            if self.cfg.flow_mode == "lookback":
                self._flow_rejected[ticker] = key
            self._flow_next_ok[ticker] = now_et + dt.timedelta(minutes=self.cfg.flow_recheck_min)
            self._note(ticker, "flow:" + fc.reason, "【阶段3】%s Flow 未确认：%s", ticker, fc.reason)
            return None, snap
        self.stats["signals"] += 1
        self._last_note.pop(ticker, None)
        log.info("【阶段3】✅ %s Flow 确认：%s（%s @ %s）", ticker, fc.reason, fc.contract_symbol, fc.occurred_at)
        return self._scored(FullSignal(ticker, now_et, snap, st, fc, bias, tests)), snap
