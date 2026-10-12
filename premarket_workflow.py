"""
premarket_workflow.py —— 盘前期权活跃榜 → Heatmap 关键节点 → Flow 确认 选股研究工作流
================================================================================
输出今日 long_watch / short_watch / range_or_uncertain 三张观察表（中文 Markdown 报告 + 可审计 JSON）。
这是研究工具：所有 setup 都是"待验证模式"，不下单、不宣称胜率。

MCP 命令与 REST 端点对照（本模块通过 REST 实现；search_datasets 的作用由免费 openapi.json 参数核查替代）：
  discover_datasets                → GET /v1/discover（免费）
  market.options_volume_rank       → GET /v1/market/options-volume-rank
  dealer_heatmap_snapshot          → GET /v1/derived/heatmap/{ticker}/snapshot
  stocks.quote / stocks.ohlc       → GET /v1/stocks/quote/{ticker} , /v1/stocks/ohlc/{ticker}?candle_size=
  flow.ticker_overview             → GET /v1/flow/tickers/{ticker}/overview
  flow.trades                      → GET /v1/flow/trades
  flow.contract_history            → GET /v1/flow/contracts/{contract_symbol}/history
  flow.ticker_bucket               → GET /v1/flow/tickers/{ticker}/bucket

时间口径：
  * 报价时间   —— stocks.quote 的 ts / 5 分钟 K 线 bar_end（盘外报价常为上一时段的旧 NBBO，不自动视为现价）
  * 结构时间   —— Heatmap generated_at 为 5 分钟采样段起点，该段在 +5 分钟后才完整
  * Flow 交易日 —— 显式 session_date = 上一完整交易日
  * OI 日期    —— 随成交量榜 session_date
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from config import (INDEX_BLACKLIST, KNOWN_ETFS, SystemConfig, et_datetime, et_now, is_trading_day,  # noqa: F401
                    next_trading_day, nyse_holidays, parse_iso, prev_trading_day, session_close_hhmm, to_et,
                    _nth_weekday)
from detector import HEATMAP_PATH, extract_rows, pick, pick_num, _row_right
from yhs_client import QuotaExhausted, YHSApiError

log = logging.getLogger("yhs.premarket")

RANK_PATH = "/v1/market/options-volume-rank"
QUOTE_PATH = "/v1/stocks/quote/{ticker}"
OHLC_PATH = "/v1/stocks/ohlc/{ticker}"
FLOW_OVERVIEW_PATH = "/v1/flow/tickers/{ticker}/overview"
FLOW_TRADES_PATH = "/v1/flow/trades"
FLOW_CONTRACT_HISTORY_PATH = "/v1/flow/contracts/{contract_symbol}/history"
FLOW_TICKER_BUCKET_PATH = "/v1/flow/tickers/{ticker}/bucket"

# 命令 → (operationId, 必须存在的参数)，用于免费 openapi.json 核查
COMMAND_SPECS = {
    "market.options_volume_rank": ("market.options_volume_rank", ()),
    "dealer_heatmap_snapshot": ("derived.dealer_heatmap_snapshot", ("ticker",)),
    "stocks.quote": ("stocks.quote", ("ticker",)),
    "stocks.ohlc": ("stocks.ohlc", ("ticker", "candle_size")),
    "flow.ticker_overview": ("flow.ticker_overview", ("ticker", "session_date", "window", "single_leg_only")),
    "flow.trades": ("flow.trades", ("tickers", "session_date", "kind", "sort_by", "limit")),
    "flow.contract_history": ("flow.contract_history", ("contract_symbol", "end_session_date", "limit")),
    "flow.ticker_bucket": ("flow.ticker_bucket", ("ticker", "session_date", "window", "bucket_start_micros", "section")),
}
# discover.capabilities_detail 中的命令名
DISCOVER_COMMAND = {
    "dealer_heatmap_snapshot": "derived.dealer_heatmap_snapshot",
}

INDEX_ETFS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VOO", "IVV", "VTI", "RSP", "MDY", "IJR", "IJH", "QQQM", "SPLG",
})
LEVERAGED_INVERSE_ETFS = frozenset({
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SPXU", "UPRO", "SSO", "SDS", "QLD", "QID", "TNA", "TZA",
    "SOXL", "SOXS", "LABU", "LABD", "FAS", "FAZ", "TECL", "TECS", "UVXY", "SVXY", "VXX", "VIXY",
    "NVDL", "NVDX", "NVD", "TSLL", "TSLQ", "TSLZ", "CONL", "MSTU", "MSTX", "MSTZ", "BITX", "AMDL",
    "YINN", "YANG", "BOIL", "KOLD", "UCO", "SCO", "NUGT", "DUST", "JNUG", "JDST", "TMF", "TMV",
    "TBT", "UDOW", "SDOW", "FNGU", "FNGD", "BULZ", "GUSH", "DRIP", "ERX", "ERY", "SH", "PSQ",
    "SMCX", "PLTU", "GGLL", "AAPU", "METU", "AMZU", "MSFU", "KORU",
})

GROUP_LABEL = {"stock": "普通股票", "index_etf": "指数ETF", "leveraged_inverse_etf": "杠杆/反向ETF",
               "other_etf": "其他ETF"}
LONG_SETUPS = ("positive_wall_reclaim_long", "negative_node_up_acceleration")
SHORT_SETUPS = ("positive_wall_loss_short", "negative_node_down_acceleration")


def monthly_opex(year: int, month: int) -> dt.date:
    """月度到期日：第三个周五（遇假日提前一个交易日）。"""
    d = _nth_weekday(year, month, 4, 3)
    return d if is_trading_day(d) else prev_trading_day(d)


@dataclass
class MarketClock:
    now_et: dt.datetime
    phase: str                 # closed / overnight / premarket / regular / postmarket
    target_day: dt.date        # 本次研究面向的"当前交易日"
    prev_full_day: dt.date     # 上一完整交易日
    holiday_note: str = ""


def market_clock(now_et: dt.datetime) -> MarketClock:
    today = now_et.date()
    m = now_et.hour * 60 + now_et.minute
    note = nyse_holidays(today.year).get(today, "")
    if not is_trading_day(today):
        phase = "closed"
        target = next_trading_day(today)
    else:
        close_h, close_m = map(int, session_close_hhmm(today).split(":"))
        close_min = close_h * 60 + close_m
        if m < 4 * 60:
            phase = "overnight"
        elif m < 9 * 60 + 30:
            phase = "premarket"
        elif m < close_min:
            phase = "regular"
        elif m < 20 * 60:
            phase = "postmarket"
        else:
            phase = "overnight"
        target = next_trading_day(today) if m >= close_min else today
    return MarketClock(now_et, phase, target, prev_trading_day(target), note)


PHASE_CN = {"closed": "休市（非交易日）", "overnight": "夜盘/盘前之前", "premarket": "盘前",
            "regular": "常规交易时段", "postmarket": "盘后"}


# =============================================================================
# 数据结构
# =============================================================================
@dataclass
class PoolEntry:
    ticker: str
    rank: int
    total_volume: float
    call_volume: float
    put_volume: float
    oi_current: float | None
    oi_change: float | None
    oi_change_pct: float | None     # 小数（单位已用 change/previous 核实）
    group: str
    source: str                     # volume_top / oi_change
    rel_volume: float | None = None
    notes: list = field(default_factory=list)


@dataclass
class PriceInfo:
    status: str = "quote_unavailable"   # ok / degraded / quote_unavailable
    price_usd: float | None = None
    source: str = ""
    content_time_et: str | None = None
    session: str | None = None
    bid_usd: float | None = None
    ask_usd: float | None = None
    spread_usd: float | None = None
    spread_pct: float | None = None
    age_s: float | None = None
    prev_close_usd: float | None = None
    prev_close_date: str | None = None
    prev2_close_usd: float | None = None
    prev2_close_date: str | None = None
    daily_atr_14: float | None = None
    notes: list = field(default_factory=list)


@dataclass
class Node:
    expiration: str
    strike_usd: float
    net_dealer_gex_usd: float
    dte: int

    @property
    def bucket(self) -> str:
        return "near" if self.dte <= 7 else ("mid" if self.dte <= 30 else "far")


@dataclass
class FlowView:
    flow_support: str = "unknown"      # bullish / bearish / mixed / unknown
    session_date: str | None = None
    direction_balance: float | None = None
    bull_proxy_usd: float | None = None
    bear_proxy_usd: float | None = None
    effective_premium_usd: float | None = None
    ambiguous_premium_usd: float | None = None
    package_share: float | None = None
    single_print_share: float | None = None
    proxy_source: str = ""
    background_7d: str | None = None
    evidence: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    top_contract: str | None = None


@dataclass
class TickerResult:
    entry: PoolEntry
    price: PriceInfo
    structure_time_et: str | None = None
    structure_session_date: str | None = None
    structure_label: str = ""
    heatmap_spot_internal: float | None = None
    nodes: list = field(default_factory=list)
    ref_price: float | None = None
    ref_label: str = ""
    epsilon_usd: float | None = None
    key_walls: list = field(default_factory=list)
    nearest_positive: dict | None = None
    anchor: dict | None = None
    anchor_sides: dict = field(default_factory=dict)
    crossing: str = ""
    upper_wall: dict | None = None
    lower_wall: dict | None = None
    near_negatives: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    setup_type: str = "no_setup"
    watch: str = "range_or_uncertain"
    position_text: str = ""
    trigger: str = ""
    invalidation: str = ""
    space_text: str = ""
    flow: FlowView = field(default_factory=FlowView)
    status: str = "waiting"
    score: float = 0.0
    flags: list = field(default_factory=list)
    confirm_log: list = field(default_factory=list)


# =============================================================================
# 小工具
# =============================================================================
def _num(v) -> float | None:
    return pick_num({"v": v}, "v")


def fmt_usd(v, nd=2) -> str:
    return "—" if v is None else f"{v:,.{nd}f}"


def fmt_gex(v) -> str:
    if v is None:
        return "—"
    return f"{v / 1e6:+,.1f}M" if abs(v) < 1e9 else f"{v / 1e9:+,.2f}B"


def fmt_et(d: dt.datetime | None) -> str | None:
    return d.strftime("%Y-%m-%d %H:%M:%S ET") if d else None


def side_of(price: float | None, strike: float, eps: float | None) -> str | None:
    if price is None:
        return None
    e = eps or 0.0
    if price > strike + e:
        return "above"
    if price < strike - e:
        return "below"
    return "in_band"


SIDE_CN = {"above": "上方", "below": "下方", "in_band": "容差带内", None: "不可确认"}


def utc_micros(d: dt.datetime) -> int:
    """真实 UTC epoch 微秒（d 必须带时区）。"""
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return int((d.astimezone(dt.timezone.utc) - epoch).total_seconds()) * 1_000_000


def flatten(x: Any, prefix: str = "") -> dict:
    out = {}
    if isinstance(x, dict):
        for k, v in x.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif not isinstance(x, list):
        out[prefix] = x
    return out


def find_dict_lists(x: Any, key_any=("contract_symbol", "symbol", "option_symbol")) -> list:
    """递归找出元素含合约字段的 list[dict]。"""
    found = []
    if isinstance(x, dict):
        for v in x.values():
            found += find_dict_lists(v, key_any)
    elif isinstance(x, list) and x and isinstance(x[0], dict):
        if any(k in x[0] for k in key_any):
            found.append(x)
        for v in x:
            found += find_dict_lists(v, key_any)
    return found


def pct_to_fraction(v, key: str, notes: list) -> float | None:
    """字段名含 _pct 时不默认已乘 100：>1 视为百分数，否则视为小数；推断结果写入 notes。"""
    v = _num(v)
    if v is None:
        return None
    if v > 1.0:
        notes.append(f"{key}={v} 推断为百分数（÷100）")
        return v / 100.0
    notes.append(f"{key}={v} 推断为小数比例（单位未在文档中声明，请人工复核）")
    return v


def _premium_of(r: dict) -> float | None:
    p = pick_num(r, "premium_usd", "premium", "total_premium_usd", "notional_usd")
    if p is None:
        pc = pick_num(r, "premium_cents", "premiumCents")
        p = pc / 100 if pc is not None else None
    return p


def _side_of_row(r: dict) -> str:
    v = str(pick(r, "side", "aggressor", "aggressor_side", "trade_side", "execution_side", default="")).lower()
    if "ask" in v or v in ("a", "aa", "buy", "bought"):
        return "ask"
    if "bid" in v or v in ("b", "bb", "sell", "sold"):
        return "bid"
    return "ambiguous"


def _is_package_row(r: dict) -> bool:
    kind = str(pick(r, "kind", default="")).lower()
    strat = str(pick(r, "strategy", "strategy_type", default="")).lower()
    legs = pick_num(r, "legs", "leg_count", "num_legs")
    return (kind == "package" or bool(pick(r, "is_package", "is_multi_leg", default=False))
            or (legs is not None and legs > 1)
            or (strat not in ("", "none", "single", "single_leg", "singleleg")))


def _occ_parts(sym: str):
    """O:NVDA261016C00195000 → (exp date, right, strike)。"""
    core = (sym or "").split(":")[-1]
    if len(core) < 15:
        return None
    try:
        return (dt.datetime.strptime(core[-15:-9], "%y%m%d").date(), core[-9], int(core[-8:]) / 1000)
    except ValueError:
        return None


# =============================================================================
# 主工作流
# =============================================================================
class PremarketWorkflow:
    def __init__(self, cfg: SystemConfig, client, now_et: dt.datetime | None = None,
                 tickers_override: list | None = None, use_flow: bool = True):
        self.cfg = cfg
        self.client = client
        self.clock = market_clock(now_et or et_now())
        self.tickers_override = tickers_override
        self.use_flow = use_flow
        stamp = self.clock.now_et.strftime("%Y%m%d_%H%M")
        self.run_id = f"{self.clock.target_day.isoformat()}_{stamp}"
        self.audit_dir = os.path.join(cfg.data_dir, "premarket", self.run_id)
        self.audit: list = []
        self.disabled: dict = {}          # capability → 原因（403 等）
        self.coverage_gaps: list = []
        self.quota_start = None
        self.quota_end = None
        self.working_set: set = set()
        self.available_commands: set = set()
        self.rank_info: dict = {}
        self.pool: list = []
        self.results: list = []
        self.shortlist: list = []
        self._seq = 0

    # ------------------------------------------------------------------ 调用封装
    def call(self, command: str, path: str, params: dict | None = None, tag: str = "") -> dict | None:
        if command in self.disabled:
            return None
        self._seq += 1
        rec = {"seq": self._seq, "command": command, "path": path, "params": params or {},
               "called_at_et": fmt_et(et_now())}
        try:
            payload = self.client.get(path, params)
        except QuotaExhausted:
            raise
        except YHSApiError as e:
            rec.update(status=e.status, error_code=e.code, error=e.message)
            self.audit.append(rec)
            if e.status in (401, 402, 403):
                # Flow 权限是账户级的：一个 flow.* 命令 403，整组 Flow 能力都停用
                family = [c for c in COMMAND_SPECS if c.startswith("flow.")] if command.startswith("flow.") else [command]
                for c in family:
                    self.disabled.setdefault(c, f"HTTP {e.status} {e.code}")
                log.warning("%s 无权限（%s %s），本次运行停止调用：%s", command, e.status, e.code, ", ".join(family))
            else:
                log.warning("%s %s 失败：%s", command, tag, e)
            return None
        meta = payload.get("_meta", {}) if isinstance(payload, dict) else {}
        rec.update(status=200, request_id=meta.get("request_id"),
                   data_freshness_seconds=meta.get("data_freshness_seconds"),
                   notice=meta.get("notice"), next_cursor=meta.get("next_cursor"))
        fname = f"{self._seq:04d}_{command.replace('.', '_')}{('_' + tag) if tag else ''}.json"
        os.makedirs(os.path.join(self.audit_dir, "responses"), exist_ok=True)
        with open(os.path.join(self.audit_dir, "responses", fname), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        rec["saved_file"] = fname
        self.audit.append(rec)
        return payload

    # ------------------------------------------------------------------ 一、启动与时间检查
    def stage_start(self) -> None:
        c = self.clock
        log.info("【启动】美东 %s，阶段=%s，目标交易日=%s，上一完整交易日=%s%s",
                 c.now_et.strftime("%Y-%m-%d %H:%M"), PHASE_CN[c.phase], c.target_day, c.prev_full_day,
                 f"（今日 {c.holiday_note}）" if c.holiday_note else "")
        d = self.client.discover()
        data = d.get("data", d) if isinstance(d, dict) else {}
        self.quota_start = pick_num(data, "quota.monthly_remaining")
        ws = pick(data, "working_sets.dealer-heatmap.tickers", default=[]) or []
        self.working_set = {str(t).upper() for t in ws}
        for cap in pick(data, "capabilities_detail", default=[]) or []:
            if isinstance(cap, dict) and cap.get("available"):
                self.available_commands.add(cap.get("command"))
        log.info("【启动】quota.monthly_remaining=%s，Heatmap working_set %d 只", self.quota_start, len(self.working_set))
        if not self.working_set:
            self.coverage_gaps.append("discover 未返回 dealer-heatmap working_set，无法取交集，改为逐只请求并以 404 判定")
        for cmd in COMMAND_SPECS:
            name = DISCOVER_COMMAND.get(cmd, cmd)
            if self.available_commands and name not in self.available_commands:
                self.disabled[cmd] = "discover 显示该能力不可用"
        if not self.use_flow:
            for cmd in COMMAND_SPECS:
                if cmd.startswith("flow."):
                    self.disabled[cmd] = "运行参数 --no-flow"
        self._verify_commands()

    def _verify_commands(self) -> None:
        """search_datasets 的 REST 替代：读取免费 openapi.json，核实命令与参数名。"""
        try:
            spec = self.client.get("/v1/openapi.json")
        except YHSApiError as e:
            log.info("openapi.json 读取失败（%s），跳过参数核查", e)
            return
        ops = {}
        for p, v in (spec.get("paths") or {}).items():
            for op in (v or {}).values():
                if isinstance(op, dict) and op.get("operationId"):
                    ops[op["operationId"]] = {x.get("name") for x in op.get("parameters", []) if isinstance(x, dict)}
        if not ops:
            self.coverage_gaps.append("openapi.json 未解析到 operationId，跳过参数核查")
            return
        for cmd, (op_id, params) in COMMAND_SPECS.items():
            if op_id not in ops:
                self.coverage_gaps.append(f"openapi 未找到 {op_id}")
                continue
            missing = [x for x in params if x not in ops[op_id]]
            if missing:
                self.coverage_gaps.append(f"{op_id} 缺少参数 {missing}，按服务端实际为准")

    # ------------------------------------------------------------------ 二、成交量榜 → 候选池
    def _rank_archive_path(self, session_date: str) -> str:
        return os.path.join(self.cfg.data_dir, "rank_archive", f"rank_{session_date}.json")

    def _load_rank_archive(self, session_date: str) -> dict | None:
        p = self._rank_archive_path(session_date)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def stage_rank(self) -> None:
        prev = self.clock.prev_full_day.isoformat()
        payload = self.call("market.options_volume_rank", RANK_PATH)
        data = (payload or {}).get("data") or {}
        meta = (payload or {}).get("_meta") or {}
        rows = extract_rows(payload) if payload else []
        live = {"session_date": data.get("session_date"), "as_of": data.get("as_of"), "rows": rows,
                "notice": meta.get("notice"), "data_freshness_seconds": meta.get("data_freshness_seconds")}
        if rows and live["session_date"]:
            os.makedirs(os.path.dirname(self._rank_archive_path(live["session_date"])), exist_ok=True)
            with open(self._rank_archive_path(live["session_date"]), "w", encoding="utf-8") as f:
                json.dump(dict(live, archived_at_et=fmt_et(et_now())), f, ensure_ascii=False)

        chosen, status = None, ""
        if rows and live["session_date"] == prev:
            chosen, status = live, "previous_full_session"
        elif rows and live["session_date"] and live["session_date"] > prev:
            arch = self._load_rank_archive(prev)
            if arch:
                chosen, status = arch, "archive_previous_full_session"
            else:
                chosen, status = live, "intraday_partial"
        else:
            arch = self._load_rank_archive(prev)
            if arch:
                chosen, status = arch, "archive_previous_full_session"
            elif rows:
                chosen, status = live, "reference_only"
            else:
                arch_dir = os.path.dirname(self._rank_archive_path(prev))
                older = sorted(f for f in os.listdir(arch_dir) if f.startswith("rank_")) if os.path.isdir(arch_dir) else []
                if older:
                    chosen, status = self._load_rank_archive(older[-1][5:-5]), "reference_only"
        if not chosen:
            raise RuntimeError("成交量榜为空且无本地归档，无法建立候选池（当日空榜不代表没有活跃标的）")
        if not rows:
            self.coverage_gaps.append("实时榜单为空（当日空榜不代表没有活跃标的），已改用本地归档")
        self.rank_info = {"status": status, "session_date": chosen.get("session_date"), "as_of": chosen.get("as_of"),
                          "notice": chosen.get("notice"), "data_freshness_seconds": chosen.get("data_freshness_seconds"),
                          "live_session_date": live["session_date"], "rows": len(chosen.get("rows") or [])}
        log.info("【榜单】session_date=%s as_of=%s 状态=%s 行数=%d%s", self.rank_info["session_date"],
                 self.rank_info["as_of"], status, self.rank_info["rows"],
                 "（含 stale notice）" if chosen.get("notice") else "")
        self.pool = self.build_pool(chosen.get("rows") or [], chosen.get("session_date") or prev)

    def _group(self, t: str) -> str:
        if t in INDEX_ETFS:
            return "index_etf"
        if t in LEVERAGED_INVERSE_ETFS:
            return "leveraged_inverse_etf"
        if t in KNOWN_ETFS:
            return "other_etf"
        return "stock"

    def _oi_unit_check(self, rows: list) -> str:
        """用 change / previous 核实 change_pct 单位。"""
        ratios = []
        for r in rows[:30]:
            g = r.get("total_open_interest") or {}
            ch, prev, pct = _num(g.get("change")), _num(g.get("previous")), _num(g.get("change_pct"))
            if ch and prev and pct:
                ratios.append(pct / (ch / prev))
        if not ratios:
            return "unknown"
        med = sorted(ratios)[len(ratios) // 2]
        return "percent" if 50 < med < 150 else ("fraction" if 0.5 < med < 1.5 else "unknown")

    def build_pool(self, rows: list, rank_session: str) -> list:
        cfg = self.cfg
        unit = self._oi_unit_check(rows)
        if unit != "fraction":
            self.coverage_gaps.append(f"OI change_pct 单位核查结果={unit}" + ("（已 ÷100）" if unit == "percent" else ""))
        parsed = []
        for i, r in enumerate(rows):
            t = str(pick(r, "ticker", "symbol", default="")).upper().strip()
            if not t or t in INDEX_BLACKLIST:
                continue
            g = r.get("total_open_interest") or {}
            pct = _num(g.get("change_pct"))
            if pct is not None and unit == "percent":
                pct /= 100.0
            parsed.append(PoolEntry(
                ticker=t, rank=int(pick_num(r, "rank") or i + 1),
                total_volume=pick_num(r, "total_volume") or 0.0, call_volume=pick_num(r, "call_volume") or 0.0,
                put_volume=pick_num(r, "put_volume") or 0.0, oi_current=_num(g.get("current")),
                oi_change=_num(g.get("change")), oi_change_pct=pct, group=self._group(t), source="volume_top"))
        parsed.sort(key=lambda e: e.rank)
        if self.tickers_override:
            by_t = {e.ticker: e for e in parsed}
            pool = [by_t.get(t) or PoolEntry(t, 0, 0, 0, 0, None, None, None, self._group(t), "manual")
                    for t in self.tickers_override]
        else:
            top = parsed[: cfg.pm_rank_top_n]
            top_set = {e.ticker for e in top}
            rest = [e for e in parsed if e.ticker not in top_set and e.oi_change_pct is not None
                    and (e.oi_current or 0) >= cfg.pm_oi_min_current]
            rest.sort(key=lambda e: -abs(e.oi_change_pct))
            extra = rest[: cfg.pm_oi_extra_n]
            for e in extra:
                e.source = "oi_change"
            pool = top + extra
        opex_note = self._opex_note(rank_session)
        out = []
        dropped = []
        for e in pool:
            if self.working_set and e.ticker not in self.working_set:
                dropped.append(e.ticker)
                continue
            if opex_note and e.oi_change is not None and e.oi_change < 0:
                e.notes.append(opex_note)
            e.rel_volume = self._rel_volume(e.ticker, e.total_volume, rank_session)
            out.append(e)
        out = out[: cfg.pm_pool_cap]
        log.info("【候选池】成交量前 %d + OI 变化 %d → 与 Heatmap working_set 交集后 %d 只（未覆盖：%s）",
                 cfg.pm_rank_top_n, sum(1 for e in pool if e.source == "oi_change"), len(out),
                 ",".join(dropped) or "无")
        self.rank_info["not_in_working_set"] = dropped
        return out

    def _opex_note(self, rank_session: str) -> str:
        try:
            d = dt.date.fromisoformat(rank_session)
        except ValueError:
            return ""
        ox = monthly_opex(d.year, d.month)
        if ox <= d <= next_trading_day(next_trading_day(ox)):
            return f"OPEX({ox}) 后 OI 下降多为到期了结，不能直接解释为看空"
        return ""

    def _rel_volume(self, ticker: str, vol: float, rank_session: str) -> float | None:
        d = os.path.join(self.cfg.data_dir, "rank_archive")
        if not os.path.isdir(d) or not vol:
            return None
        hist = []
        for fn in sorted(os.listdir(d)):
            if not fn.startswith("rank_") or fn == f"rank_{rank_session}.json":
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    rows = json.load(f).get("rows") or []
            except (OSError, json.JSONDecodeError):
                continue
            v = next((pick_num(r, "total_volume") for r in rows if str(r.get("ticker", "")).upper() == ticker), None)
            if v:
                hist.append(v)
        hist = hist[-20:]
        if len(hist) < self.cfg.pm_relvol_min_history:
            return None
        return vol / (sum(hist) / len(hist))

    # ------------------------------------------------------------------ 三、价格与 Heatmap
    def stage_structure(self) -> None:
        for i, e in enumerate(self.pool, 1):
            log.info("【结构】(%d/%d) %s", i, len(self.pool), e.ticker)
            res = TickerResult(entry=e, price=PriceInfo())
            hm = self.call("dealer_heatmap_snapshot", HEATMAP_PATH.format(ticker=e.ticker), tag=e.ticker)
            q = self.call("stocks.quote", QUOTE_PATH.format(ticker=e.ticker), tag=e.ticker)
            d1 = self.call("stocks.ohlc", OHLC_PATH.format(ticker=e.ticker), {"candle_size": "1d"}, tag=e.ticker + "_1d")
            m5 = self.call("stocks.ohlc", OHLC_PATH.format(ticker=e.ticker), {"candle_size": "5m"}, tag=e.ticker + "_5m")
            res.price = self.parse_price(q, d1, m5)
            res._bars5 = self.regular_bars_5m(m5)          # 开盘确认用（不进审计 JSON）
            res._premarket_bar = self.latest_premarket_bar(m5)
            self.parse_heatmap(res, hm)
            self.analyze(res)
            self.results.append(res)

    def parse_price(self, q: dict | None, d1: dict | None, m5: dict | None) -> PriceInfo:
        cfg, clock = self.cfg, self.clock
        p = PriceInfo()
        now = clock.now_et
        # 昨收 / 前收 / ATR —— 仅用常规时段日线
        bars = [b for b in (pick(d1 or {}, "data.bars", default=[]) or [])
                if isinstance(b, dict) and b.get("session") == "regular" and b.get("trading_date")]
        bars.sort(key=lambda b: b["trading_date"])
        bars = [b for b in bars if b["trading_date"] < clock.target_day.isoformat()]
        if bars:
            last = bars[-1]
            p.prev_close_usd, p.prev_close_date = _num(last.get("close_usd")), last["trading_date"]
            if last["trading_date"] != clock.prev_full_day.isoformat():
                p.notes.append(f"日线最新常规收盘为 {last['trading_date']}，不是上一完整交易日 {clock.prev_full_day}")
            if len(bars) >= 2:
                p.prev2_close_usd, p.prev2_close_date = _num(bars[-2].get("close_usd")), bars[-2]["trading_date"]
            n = cfg.pm_atr_period
            if len(bars) >= n + 1:
                trs = []
                for a, b in zip(bars[-n - 1:-1], bars[-n:]):
                    h, l, pc = _num(b.get("high_usd")), _num(b.get("low_usd")), _num(a.get("close_usd"))
                    if None in (h, l, pc):
                        continue
                    trs.append(max(h - l, abs(h - pc), abs(l - pc)))
                if len(trs) == n:
                    p.daily_atr_14 = sum(trs) / n
        if p.daily_atr_14 is None:
            p.notes.append("ATR 不可得（未填零），距离需人工复核")
        # 当前价：可信 NBBO 优先，其次本时段内的 5 分钟成交 K 线
        qd = (q or {}).get("data") or {}
        qts = parse_iso(qd.get("ts"))
        bid, ask = _num(qd.get("bid_usd")), _num(qd.get("ask_usd"))
        if qts is not None:
            p.bid_usd, p.ask_usd = bid, ask
            age = (now - to_et(qts)).total_seconds()
            if bid and ask and ask >= bid > 0:
                p.spread_usd = ask - bid
                p.spread_pct = p.spread_usd / ((ask + bid) / 2)
            if bid and ask and ask >= bid > 0 and 0 <= age <= cfg.pm_quote_max_age_s and clock.phase != "closed":
                p.status, p.price_usd, p.source = "ok", (ask + bid) / 2, "stocks.quote 中间价"
                p.content_time_et, p.age_s, p.session = fmt_et(to_et(qts)), age, clock.phase
                if p.spread_pct is not None and p.spread_pct > cfg.pm_spread_abnormal_pct:
                    p.status = "degraded"
                    p.notes.append(f"价差异常 {p.spread_pct:.2%}")
                return p
            p.notes.append(f"stocks.quote 内容时间 {fmt_et(to_et(qts))}（距今 {age / 60:.0f} 分钟），"
                           "盘外沿用的旧报价，不视为当前价")
        else:
            p.notes.append("stocks.quote 无有效内容时间")
        if clock.phase in ("premarket", "regular", "postmarket"):
            last_bar = None
            for b in pick(m5 or {}, "data.bars", default=[]) or []:
                end = parse_iso(b.get("bar_end"))
                if end is None or to_et(end) > now or to_et(end).date() != now.date():
                    continue
                if b.get("session") != clock.phase:
                    continue
                if last_bar is None or end > last_bar[0]:
                    last_bar = (end, b)
            if last_bar:
                end, b = last_bar
                age = (now - to_et(end)).total_seconds()
                if age <= cfg.pm_bar_max_age_min * 60:
                    p.status, p.price_usd, p.source = "degraded", _num(b.get("close_usd")), "stocks.ohlc 5m 收盘（非 NBBO）"
                    p.content_time_et, p.age_s, p.session = fmt_et(to_et(end)), age, b.get("session")
                    p.notes.append("无 60 秒内可信报价，使用最新完成 5 分钟 K 线")
                    return p
        p.status = "quote_unavailable"
        return p

    def regular_bars_5m(self, m5: dict | None) -> list:
        out = []
        for b in pick(m5 or {}, "data.bars", default=[]) or []:
            st, en = parse_iso(b.get("bar_start")), parse_iso(b.get("bar_end"))
            if st is None or en is None or b.get("session") != "regular":
                continue
            st, en = to_et(st), to_et(en)
            if st.date() != self.clock.target_day or en > self.clock.now_et:
                continue                                  # 只用目标交易日、已完成的 K 线
            out.append({"start": st, "end": en, "open": _num(b.get("open_usd")), "high": _num(b.get("high_usd")),
                        "low": _num(b.get("low_usd")), "close": _num(b.get("close_usd"))})
        out.sort(key=lambda x: x["start"])
        return out

    def latest_premarket_bar(self, m5: dict | None) -> dict | None:
        best = None
        for b in pick(m5 or {}, "data.bars", default=[]) or []:
            en = parse_iso(b.get("bar_end"))
            if en is None or b.get("session") != "premarket" or to_et(en).date() != self.clock.target_day:
                continue
            if best is None or en > best[0]:
                best = (en, b)
        return {"end": to_et(best[0]), "close": _num(best[1].get("close_usd"))} if best else None

    def parse_heatmap(self, res: TickerResult, hm: dict | None) -> None:
        d = (hm or {}).get("data") or {}
        if not d:
            res.flags.append("heatmap_unavailable")
            return
        gen = parse_iso(d.get("generated_at"))
        if gen:
            g = to_et(gen)
            res.structure_time_et = f"{g.strftime('%Y-%m-%d %H:%M')}–{(g + dt.timedelta(minutes=5)).strftime('%H:%M')} ET 采样段"
            if g + dt.timedelta(minutes=5) > self.clock.now_et:
                res.flags.append("heatmap_segment_incomplete")
        res.structure_session_date = d.get("session_date_et")
        sd = res.structure_session_date
        if sd == self.clock.target_day.isoformat():
            res.structure_label = "今日结构"
        elif sd == self.clock.prev_full_day.isoformat():
            res.structure_label = "昨日结构"
        else:
            res.structure_label = f"旧结构（{sd}，stale）"
            res.flags.append("heatmap_stale")
        res.heatmap_spot_internal = _num(d.get("spot_usd"))   # 仅记录，不作为当前价
        target = self.clock.target_day
        for c in d.get("cells") or []:
            try:
                exp = dt.date.fromisoformat(str(c.get("expiration")))
            except ValueError:
                continue
            if exp < target:
                continue                                    # 删除早于当前交易日的到期列
            g = _num(c.get("net_dealer_gex_usd"))
            k = _num(c.get("strike_usd"))
            if g is None or k is None:
                continue
            res.nodes.append(Node(exp.isoformat(), k, g, (exp - target).days))

    # ------------------------------------------------------------------ 四、Wall 与位置
    def analyze(self, res: TickerResult) -> None:
        cfg, p = self.cfg, res.price
        if p.status in ("ok", "degraded") and p.price_usd:
            res.ref_price, res.ref_label = p.price_usd, f"当前价（{p.source}）"
        elif p.prev_close_usd:
            res.ref_price, res.ref_label = p.prev_close_usd, f"昨收 {p.prev_close_date}（当前价不可确认）"
        if not res.nodes or res.ref_price is None:
            res.setup_type, res.status = "data_insufficient", "waiting"
            res.position_text = "Heatmap 或价格缺失"
            res.trigger = res.invalidation = "—"
            return
        atr = p.daily_atr_14
        eps_parts = []
        if p.status == "ok" and p.spread_usd is not None:
            eps_parts.append(cfg.pm_eps_spread_mult * p.spread_usd)
        if atr:
            eps_parts.append(cfg.pm_eps_atr_mult * atr)
        res.epsilon_usd = max(eps_parts) if eps_parts else None
        if res.epsilon_usd is None:
            res.flags.append("epsilon_unavailable")
        eps = res.epsilon_usd or 0.0
        ref = res.ref_price
        dist_lim = cfg.pm_atr_distance_mult * atr if atr else None

        near = [n for n in res.nodes if n.dte <= cfg.pm_near_dte_max]
        upto_mid = [n for n in res.nodes if n.dte <= cfg.pm_mid_dte_max]
        pos_mid = [n for n in upto_mid if n.net_dealer_gex_usd > 0]
        pos_near = [n for n in near if n.net_dealer_gex_usd > 0]
        max_pos = max([n.net_dealer_gex_usd for n in (pos_near or pos_mid)] or [0.0])
        sig = cfg.pm_node_significance * max_pos

        # 按到期取最强正节点 → 近端优先取前 N
        best_by_exp = {}
        for n in pos_mid:
            if n.expiration not in best_by_exp or n.net_dealer_gex_usd > best_by_exp[n.expiration].net_dealer_gex_usd:
                best_by_exp[n.expiration] = n
        walls = sorted(best_by_exp.values(), key=lambda n: (n.bucket != "near", -n.net_dealer_gex_usd))
        res.key_walls = [self._node_dict(n, ref, atr) for n in walls[: cfg.pm_key_walls_n]]
        nearest_pool = pos_near or pos_mid
        nearest = min(nearest_pool, key=lambda n: (abs(n.strike_usd - ref), -n.net_dealer_gex_usd)) if nearest_pool else None
        res.nearest_positive = self._node_dict(nearest, ref, atr) if nearest else None

        # 冲突：同一价位不同到期正负显著相反
        by_strike = {}
        for n in upto_mid:
            if abs(n.net_dealer_gex_usd) >= sig > 0:
                by_strike.setdefault(n.strike_usd, []).append(n)
        for k, ns in sorted(by_strike.items()):
            if any(n.net_dealer_gex_usd > 0 for n in ns) and any(n.net_dealer_gex_usd < 0 for n in ns):
                res.conflicts.append({"strike_usd": k, "nodes": [
                    {"expiration": n.expiration, "net_dealer_gex_usd": n.net_dealer_gex_usd} for n in ns]})

        # 上下方下一可见正节点（显著者），近端负节点
        sig_pos = [n for n in pos_mid if n.net_dealer_gex_usd >= sig]
        above = [n for n in sig_pos if n.strike_usd > ref + eps]
        below = [n for n in sig_pos if n.strike_usd < ref - eps]
        up = min(above, key=lambda n: n.strike_usd - ref) if above else None
        lo = min(below, key=lambda n: ref - n.strike_usd) if below else None
        res.upper_wall = self._node_dict(up, ref, atr) if up else None
        res.lower_wall = self._node_dict(lo, ref, atr) if lo else None
        negs = [n for n in near if n.net_dealer_gex_usd < 0 and abs(n.net_dealer_gex_usd) >= sig
                and (dist_lim is None or abs(n.strike_usd - ref) <= dist_lim)]
        negs.sort(key=lambda n: abs(n.strike_usd - ref))
        res.near_negatives = [self._node_dict(n, ref, atr) for n in negs[:3]]

        # 价格路径：前收 → 昨收 → 当前
        path = [("prev2", p.prev2_close_usd), ("prev", p.prev_close_usd)]
        if p.status in ("ok", "degraded"):
            path.append(("now", p.price_usd))

        def crossed(strike: float):
            sides = {k: side_of(v, strike, eps) for k, v in path}
            for (ka, _), (kb, _) in zip(path, path[1:]):
                a, b = sides[ka], sides[kb]
                if a == "below" and b == "above":
                    return "up", ("premarket" if kb == "now" else "yesterday"), sides
                if a == "above" and b == "below":
                    return "down", ("premarket" if kb == "now" else "yesterday"), sides
            return None, None, sides

        # 锚点：被跨越的最强关键正 Wall；否则距离限制内最近的关键 Wall
        # 锚点只取显著正节点（噪音级小节点仍在 nearest_positive 中展示，但不作为跨越依据）
        cands = {(n.strike_usd, n.expiration): n for n in walls[: cfg.pm_key_walls_n] if n.net_dealer_gex_usd >= sig}
        if nearest and nearest.net_dealer_gex_usd >= sig:
            cands[(nearest.strike_usd, nearest.expiration)] = nearest
        crossed_walls = []
        for n in cands.values():
            dirn, when, sides = crossed(n.strike_usd)
            if dirn and (dist_lim is None or abs(n.strike_usd - ref) <= dist_lim):
                crossed_walls.append((n, dirn, when, sides))
        anchor_node = None
        if crossed_walls:
            # 最近一次跨越优先（premarket > yesterday），同时间段内取 GEX 最大
            crossed_walls.sort(key=lambda x: (x[2] != "premarket", -x[0].net_dealer_gex_usd))
            anchor_node, dirn, when, sides = crossed_walls[0]
            res.crossing = f"{dirn}:{when}"
        else:
            in_range = [n for n in cands.values() if dist_lim is None or abs(n.strike_usd - ref) <= dist_lim]
            if in_range:
                anchor_node = min(in_range, key=lambda n: abs(n.strike_usd - ref))
                _, _, sides = crossed(anchor_node.strike_usd)
        if anchor_node:
            res.anchor = self._node_dict(anchor_node, ref, atr)
            res.anchor_sides = sides
            if any(abs(c["strike_usd"] - anchor_node.strike_usd) < 1e-9 for c in res.conflicts):
                res.flags.append("anchor_conflict")
        if dist_lim is None:
            res.flags.append("manual_distance_review")

        self.classify(res, crossed, negs, sig_pos)

    @staticmethod
    def _node_dict(n: Node, ref: float | None, atr: float | None) -> dict:
        d = {"expiration": n.expiration, "strike_usd": n.strike_usd, "net_dealer_gex_usd": n.net_dealer_gex_usd,
             "dte": n.dte, "bucket": n.bucket}
        if ref is not None:
            d["distance_usd"] = n.strike_usd - ref
            d["distance_atr"] = (n.strike_usd - ref) / atr if atr else None
        return d

    def classify(self, res: TickerResult, crossed, negs: list, sig_pos: list) -> None:
        cfg, p = self.cfg, res.price
        eps = res.epsilon_usd or 0.0
        atr = p.daily_atr_14
        ref = res.ref_price
        e_txt = f"±{eps:.2f}" if res.epsilon_usd else "（容差不可得，人工复核）"
        a = res.anchor
        if a:
            s = res.anchor_sides
            res.position_text = (f"锚点 {a['strike_usd']:g}（{a['expiration']}，{fmt_gex(a['net_dealer_gex_usd'])}）："
                                 f"前收{SIDE_CN[s.get('prev2')]} → 昨收{SIDE_CN[s.get('prev')]}"
                                 + (f" → 现价{SIDE_CN[s.get('now')]}" if "now" in s else " → 现价不可确认"))
        else:
            res.position_text = "距离限制内无关键正 Wall"

        def space(direction: str) -> tuple[str, bool]:
            w = res.upper_wall if direction == "up" else res.lower_wall
            if not w:
                return (f"{'上' if direction == 'up' else '下'}方无可见显著正节点（Top-node 未返回 ≠ 无阻力）", True)
            d_atr = w.get("distance_atr")
            ok = d_atr is None or abs(d_atr) >= cfg.pm_space_min_atr
            dist = f"{abs(w['distance_usd']):.2f}" + (f"，{abs(d_atr):.2f} ATR" if d_atr is not None else "")
            return (f"{'上' if direction == 'up' else '下'}方下一正节点 {w['strike_usd']:g}"
                    f"（{w['expiration']}，{fmt_gex(w['net_dealer_gex_usd'])}，距 {dist}）", ok)

        n_bars = cfg.pm_confirm_bars
        if a and res.crossing:
            dirn, when = res.crossing.split(":")
            k = a["strike_usd"]
            when_cn = "盘前新近跨越" if when == "premarket" else "昨日常规时段跨越"
            if dirn == "up":
                res.setup_type = "positive_wall_reclaim_long"
                res.trigger = (f"{when_cn}；开盘后 {n_bars} 根完成 5 分钟 K 线收盘 > {k + eps:.2f}（{k:g}{e_txt}），"
                               f"回测低点进入容差带后收回并守住；Flow 同向")
                res.invalidation = f"5 分钟收盘重新跌回 {k - eps:.2f} 下方（Wall 容差带下方）"
                res.space_text, ok = space("up")
            else:
                res.setup_type = "positive_wall_loss_short"
                res.trigger = (f"{when_cn}；开盘后 {n_bars} 根完成 5 分钟 K 线收盘 < {k - eps:.2f}（{k:g}{e_txt}），"
                               f"反抽不过容差带；Flow 支持下行")
                res.invalidation = f"5 分钟收盘重新收复 {k + eps:.2f} 并站稳"
                res.space_text, ok = space("down")
            if not ok:
                res.flags.append("space_tight")      # A/B 不因空间出表，但降权
        else:
            neg_hit = None
            for n in negs:
                dirn, when, _ = crossed(n.strike_usd)
                if dirn:
                    neg_hit = (n, dirn, when)
                    break
            if neg_hit:
                n, dirn, when = neg_hit
                k = n.strike_usd
                when_cn = "盘前" if when == "premarket" else "昨日"
                res.crossing = f"{dirn}:{when}"
                res.anchor = self._node_dict(n, ref, atr)
                res.position_text = (f"{when_cn}{'向上' if dirn == 'up' else '向下'}穿越近端负节点 {k:g}"
                                     f"（{n.expiration}，{fmt_gex(n.net_dealer_gex_usd)}）")
                txt, ok = space(dirn)
                res.space_text = txt
                if dirn == "up":
                    res.setup_type = "negative_node_up_acceleration"
                    res.trigger = (f"价格保持在 {k + eps:.2f} 上方并延续（{n_bars} 根 5 分钟收盘），Flow 同向，"
                                   f"上方无过近正节点")
                    res.invalidation = (f"收盘回到 {k - eps:.2f} 下方（回原区间）"
                                        + (f"，或在 {res.upper_wall['strike_usd']:g} 附近被吸收回落" if res.upper_wall else ""))
                else:
                    res.setup_type = "negative_node_down_acceleration"
                    res.trigger = (f"价格保持在 {k - eps:.2f} 下方并延续（{n_bars} 根 5 分钟收盘），反抽失败，Flow 支持")
                    res.invalidation = (f"收盘重新收复 {k + eps:.2f}"
                                        + (f"，或在 {res.lower_wall['strike_usd']:g} 附近遇正节点承接" if res.lower_wall else ""))
                if not ok:
                    res.flags.append("space_limited")
            elif res.upper_wall and res.lower_wall and self._within(res.upper_wall, atr) and self._within(res.lower_wall, atr):
                u, l = res.upper_wall["strike_usd"], res.lower_wall["strike_usd"]
                res.setup_type = "positive_node_bracket"
                res.position_text = f"夹在正节点 {l:g}（下）与 {u:g}（上）之间"
                res.trigger = (f"等待区间突破：{n_bars} 根 5 分钟收盘 > {u + eps:.2f} 转 reclaim_long 观察；"
                               f"< {l - eps:.2f} 转 loss_short 观察")
                res.invalidation = "区间内震荡期间不构成趋势信号"
                res.space_text = f"区间宽度 {u - l:.2f}" + (f"（{(u - l) / atr:.2f} ATR）" if atr else "")
            else:
                res.setup_type = "no_setup"
                if a:
                    k = a["strike_usd"]
                    res.trigger = f"等待跨越锚点 {k:g}：收盘 > {k + eps:.2f} 或 < {k - eps:.2f} 后重新评估"
                else:
                    res.trigger = "等待价格接近关键正 Wall"
                res.invalidation = "—"
                up_txt, _ = space("up")
                lo_txt, _ = space("down")
                res.space_text = f"{up_txt}；{lo_txt}"

        # 分表与状态
        if res.setup_type in LONG_SETUPS:
            res.watch = "long_watch"
        elif res.setup_type in SHORT_SETUPS:
            res.watch = "short_watch"
        else:
            res.watch = "range_or_uncertain"
        for f in ("anchor_conflict", "space_limited", "heatmap_stale"):
            if f in res.flags and res.watch != "range_or_uncertain":
                res.watch = "range_or_uncertain"
        if self.rank_info.get("status") == "reference_only":
            res.status = "reference_only"
        elif res.watch != "range_or_uncertain" and p.status in ("ok", "degraded"):
            res.status = "candidate"
        else:
            res.status = "waiting"
        res.score = self._score(res)

    def _within(self, w: dict, atr: float | None) -> bool:
        d = w.get("distance_atr")
        return True if d is None else abs(d) <= self.cfg.pm_atr_distance_mult

    def _score(self, res: TickerResult) -> float:
        s = 0.0
        st = res.setup_type
        if st in ("positive_wall_reclaim_long", "positive_wall_loss_short"):
            s += 3.0
        elif st in ("negative_node_up_acceleration", "negative_node_down_acceleration"):
            s += 2.0
        elif st == "positive_node_bracket":
            s += 0.5
        if res.crossing.endswith("premarket"):
            s += 0.5
        if res.anchor and res.anchor.get("distance_atr") is not None:
            s += max(0.0, (self.cfg.pm_atr_distance_mult - abs(res.anchor["distance_atr"])) / self.cfg.pm_atr_distance_mult)
        if "space_limited" in res.flags or "space_tight" in res.flags:
            s -= 1.0
        if "anchor_conflict" in res.flags or res.conflicts:
            s -= 0.5
        if res.price.status == "ok":
            s += 0.5
        if res.entry.group == "leveraged_inverse_etf":
            s -= 0.5
        if res.setup_type in ("data_insufficient",):
            s -= 5.0
        return round(s, 3)

    def stage_shortlist(self) -> None:
        cfg = self.cfg
        ranked = sorted(self.results, key=lambda r: -r.score)
        keep = [r for r in ranked if r.setup_type not in ("no_setup", "data_insufficient")][: cfg.pm_shortlist_max]
        if len(keep) < cfg.pm_shortlist_min:
            extra = [r for r in ranked if r not in keep and r.setup_type != "data_insufficient"]
            keep += extra[: cfg.pm_shortlist_min - len(keep)]
        self.shortlist = keep
        log.info("【入围】%d 只：%s", len(keep), ", ".join(f"{r.entry.ticker}({r.setup_type})" for r in keep))

    # ------------------------------------------------------------------ 五、Flow 复核
    def stage_flow(self) -> None:
        sd = self.clock.prev_full_day.isoformat()
        for r in self.shortlist:
            t = r.entry.ticker
            fv = FlowView(session_date=sd)
            ov1 = self.call("flow.ticker_overview", FLOW_OVERVIEW_PATH.format(ticker=t),
                            {"session_date": sd, "window": "1d", "single_leg_only": "true"}, tag=t + "_1d")
            if "flow.ticker_overview" in self.disabled and ov1 is None:
                fv.limitations.append(f"flow.ticker_overview 不可用：{self.disabled['flow.ticker_overview']}")
                r.flow = fv
                continue
            ov7 = self.call("flow.ticker_overview", FLOW_OVERVIEW_PATH.format(ticker=t),
                            {"session_date": sd, "window": "7d", "single_leg_only": "true"}, tag=t + "_7d")
            trades = self.call("flow.trades", FLOW_TRADES_PATH,
                               {"tickers": t, "session_date": sd, "kind": "order", "sort_by": "premiumCents",
                                "direction": "desc", "limit": self.cfg.pm_flow_trades_limit}, tag=t)
            self.evaluate_flow(fv, ov1, ov7, trades, r.ref_price)
            if fv.top_contract and "flow.contract_history" not in self.disabled:
                ch = self.call("flow.contract_history",
                               FLOW_CONTRACT_HISTORY_PATH.format(contract_symbol=fv.top_contract),
                               {"end_session_date": sd, "limit": 10}, tag=t)
                hist = extract_rows(ch) if ch else []
                if hist:
                    fv.evidence.append(f"关键合约 {fv.top_contract} 近 {len(hist)} 个交易日 Flow 历史已保存（审计文件）")
            r.flow = fv

    def evaluate_flow(self, fv: FlowView, ov1: dict | None, ov7: dict | None, trades: dict | None,
                      ref_price: float | None) -> None:
        cfg = self.cfg
        pct_notes = []
        bull = bear = amb = None
        if ov1:
            flat = flatten(ov1.get("data") or {})
            m = self._proxy_from_flat(flat)
            if m:
                bull, bear, amb = m
                fv.proxy_source = "flow.ticker_overview(1d, single_leg_only=true)"
            for k, v in flat.items():
                kl = k.lower()
                if "package" in kl and any(x in kl for x in ("pct", "share", "ratio")):
                    fv.package_share = pct_to_fraction(v, k, pct_notes)
                    break
            lists = find_dict_lists(ov1.get("data") or {})
            if lists and lists[0]:
                fv.top_contract = pick(lists[0][0], "contract_symbol", "symbol", "option_symbol")
        rows = extract_rows(trades) if trades else []
        meta = (trades or {}).get("_meta") or {}
        if rows:
            prem_rows = [(r, _premium_of(r) or 0.0) for r in rows]
            total = sum(p for _, p in prem_rows)
            if total > 0:
                fv.single_print_share = max(p for _, p in prem_rows) / total
            if meta.get("next_cursor") or meta.get("truncated"):
                fv.limitations.append(f"flow.trades 只读第一页（{len(rows)} 笔，按权利金降序），为样本")
            if bull is None:
                b = s = a = 0.0
                pkg = 0.0
                for r, p in prem_rows:
                    if _is_package_row(r):
                        pkg += p
                        continue
                    right, side = _row_right(r), _side_of_row(r)
                    if side == "ambiguous" or right not in ("C", "P"):
                        a += p
                    elif (right, side) in (("C", "ask"), ("P", "bid")):
                        b += p
                    else:
                        s += p
                bull, bear, amb = b, s, a
                fv.proxy_source = "flow.trades 第一页样本（已去除识别出的组合）"
                if fv.package_share is None and total > 0:
                    fv.package_share = pkg / total
            for r, p in prem_rows[:3]:
                sym = pick(r, "contract_symbol", "symbol", "option_symbol", default="?")
                fv.evidence.append(f"{sym} {_side_of_row(r)} ${p:,.0f}"
                                   f"{'（组合）' if _is_package_row(r) else ''}")
                parts = _occ_parts(str(sym))
                if parts and ref_price and total > 0 and p / total > 0.3:
                    exp, right, k = parts
                    deep_itm = (right == "C" and k < 0.8 * ref_price) or (right == "P" and k > 1.2 * ref_price)
                    if deep_itm and (exp - self.clock.target_day).days > 60:
                        fv.limitations.append(f"{sym} 为深度 ITM 远期大额单（占样本 {p / total:.0%}），降级")
            if not fv.top_contract:
                fv.top_contract = pick(rows[0], "contract_symbol", "symbol", "option_symbol")
        fv.limitations += pct_notes
        if bull is not None and bear is not None:
            fv.bull_proxy_usd, fv.bear_proxy_usd, fv.ambiguous_premium_usd = bull, bear, amb
            fv.effective_premium_usd = bull + bear
            fv.direction_balance = (bull - bear) / (bull + bear) if (bull + bear) > 0 else None
        fv.flow_support = self._support(fv)
        if ov7:
            m7 = self._proxy_from_flat(flatten(ov7.get("data") or {}))
            if m7 and (m7[0] + m7[1]) > 0:
                db7 = (m7[0] - m7[1]) / (m7[0] + m7[1])
                fv.background_7d = f"7d direction_balance={db7:+.2f}（含 1d，非独立确认）"
        if not ov1 and not rows:
            fv.limitations.append("无 Flow 数据返回")

    @staticmethod
    def _proxy_from_flat(flat: dict):
        """从扁平字段中找 call/put × ask/bid 权利金。找不全则返回 None。"""
        def find(right_tokens, side):
            best = None
            for k, v in flat.items():
                kl = k.lower()
                if _num(v) is None or "prem" not in kl or any(x in kl for x in ("pct", "share", "ratio", "count")):
                    continue
                if side not in kl or not any(t in kl for t in right_tokens):
                    continue
                if best is None or len(k) < len(best[0]):
                    best = (k, _num(v))
            return best[1] if best else None
        ca, cb = find(("call",), "ask"), find(("call",), "bid")
        pa, pb = find(("put",), "ask"), find(("put",), "bid")
        if None in (ca, cb, pa, pb):
            return None
        amb = 0.0
        for k, v in flat.items():
            kl = k.lower()
            if "prem" in kl and any(x in kl for x in ("mid", "neutral", "unknown", "ambiguous")) and _num(v) is not None:
                amb += _num(v)
        return ca + pb, pa + cb, amb

    def _support(self, fv: FlowView) -> str:
        cfg = self.cfg
        if fv.direction_balance is None or (fv.effective_premium_usd or 0) < cfg.pm_flow_min_effective_usd:
            if fv.direction_balance is not None:
                fv.limitations.append(f"有效样本 ${fv.effective_premium_usd:,.0f} < ${cfg.pm_flow_min_effective_usd:,.0f}")
            return "unknown"
        if abs(fv.direction_balance) < cfg.pm_flow_balance_threshold:
            return "mixed"
        downgraded = []
        if fv.package_share is not None and fv.package_share > cfg.pm_flow_package_share_max:
            downgraded.append(f"组合占比 {fv.package_share:.0%}")
        if fv.single_print_share is not None and fv.single_print_share > cfg.pm_flow_single_print_share_max:
            downgraded.append(f"单笔占比 {fv.single_print_share:.0%}")
        if any("深度 ITM" in x for x in fv.limitations):
            downgraded.append("深度 ITM 远期大额单")
        if downgraded:
            fv.limitations.append("方向降级为 mixed：" + "、".join(downgraded))
            return "mixed"
        return "bullish" if fv.direction_balance > 0 else "bearish"

    # ------------------------------------------------------------------ 七、开盘确认
    def checkpoints(self) -> list:
        return [et_datetime(self.clock.target_day, h) for h in ("09:35", "09:40", "09:45")]

    def stage_open_confirm(self) -> None:
        clock = self.clock
        if clock.phase != "regular" or clock.now_et.date() != clock.target_day:
            return
        due = [c for c in self.checkpoints() if c <= clock.now_et]
        if not due:
            return
        sd = clock.target_day.isoformat()
        for r in self.shortlist:
            if r.setup_type in ("no_setup", "data_insufficient") or not r.anchor:
                continue
            bars = getattr(r, "_bars5", [])
            for c in due:
                start = c - dt.timedelta(minutes=5)
                if "flow.ticker_bucket" in self.disabled:
                    break
                self.call("flow.ticker_bucket", FLOW_TICKER_BUCKET_PATH.format(ticker=r.entry.ticker),
                          {"session_date": sd, "window": "5m", "bucket_start_micros": utc_micros(start),
                           "section": "summary"}, tag=f"{r.entry.ticker}_{start.strftime('%H%M')}")
                r.confirm_log.append(f"{start.strftime('%H:%M')}–{c.strftime('%H:%M')} 窗口 flow.ticker_bucket 已保存")
            self.evaluate_open(r, bars)

    def evaluate_open(self, r: TickerResult, bars: list) -> None:
        cfg = self.cfg
        k = r.anchor["strike_usd"]
        eps = r.epsilon_usd or 0.0
        st = r.setup_type
        if not bars:
            r.confirm_log.append("目标交易日尚无完成的常规时段 5 分钟 K 线")
            return
        closes = [b["close"] for b in bars if b["close"] is not None]
        r.confirm_log.append("已完成 5m 收盘：" + ", ".join(f"{b['start'].strftime('%H:%M')}={b['close']:.2f}" for b in bars[:6]))
        # E. 盘前跨越、开盘回到原侧
        if r.crossing.endswith("premarket") and len(closes) >= cfg.pm_confirm_bars:
            orig = r.anchor_sides.get("prev")
            first = [side_of(c, k, eps) for c in closes[: cfg.pm_confirm_bars]]
            if orig in ("above", "below") and all(s == orig for s in first):
                r.confirm_log.append(f"盘前跨越 {k:g} 后开盘回到原侧（{SIDE_CN[orig]}）")
                r.setup_type, r.watch, r.status = "failed_breakout", "range_or_uncertain", "invalidated"
                r.trigger = "原方向候选取消；反向需另等独立确认"
                return
        if st in ("positive_node_bracket",):
            return
        want = "above" if st in LONG_SETUPS else "below"
        against = "below" if want == "above" else "above"
        if any(side_of(c, k, eps) == against for c in closes):
            r.status = "invalidated"
            r.confirm_log.append(f"已有 5 分钟收盘回到 {k:g} 容差带{SIDE_CN[against]}，失效")
            return
        run = 0
        for c in closes:
            run = run + 1 if side_of(c, k, eps) == want else 0
        held = run >= cfg.pm_confirm_bars
        if want == "above":
            retest = any(b["low"] is not None and b["low"] <= k + eps and side_of(b["close"], k, eps) == "above" for b in bars)
        else:
            retest = any(b["high"] is not None and b["high"] >= k - eps and side_of(b["close"], k, eps) == "below" for b in bars)
        flow_ok = (r.flow.flow_support == ("bullish" if want == "above" else "bearish"))
        if held and retest and (flow_ok or not cfg.pm_require_flow_for_confirm):
            r.status = "confirmed"
            r.confirm_log.append("价格条件满足（连续收盘在容差带外 + 回测守住）且 Flow 同向 → confirmed（研究模式，不代表收益）")
        elif held and retest:
            r.status = "candidate"
            r.confirm_log.append(f"价格条件满足，但 flow_support={r.flow.flow_support}，未达到 confirmed")
        else:
            r.status = "waiting" if r.status != "reference_only" else r.status
            r.confirm_log.append(f"连续 {run} 根在容差带外（需 {cfg.pm_confirm_bars}），回测验证={'是' if retest else '否'}")

    # ------------------------------------------------------------------ 运行
    def run(self) -> str:
        self.stage_start()
        self.stage_rank()
        self.stage_structure()
        self.stage_shortlist()
        self.stage_flow()
        self.stage_open_confirm()
        try:
            d = self.client.discover()
            self.quota_end = pick_num(d.get("data", d) if isinstance(d, dict) else {}, "quota.monthly_remaining")
        except YHSApiError as e:
            log.warning("结束时 discover 失败：%s", e)
        from premarket_report import write_outputs
        return write_outputs(self)


class AuditReplayClient:
    """用某次运行的审计目录离线重放（0 配额），用于调整研究参数后对比结果。"""

    def __init__(self, audit_dir: str):
        with open(os.path.join(audit_dir, "audit.json"), "r", encoding="utf-8") as f:
            self.audit = json.load(f)
        self.dir = audit_dir
        self.map = {}
        for rec in self.audit.get("calls", []):
            self.map[(rec["path"], json.dumps(rec.get("params") or {}, sort_keys=True))] = rec
        self.billed_calls = self.free_calls = 0
        self.billed_by_endpoint: dict = {}
        self.quota_remaining = self.audit.get("quota_end")

    def recorded_now(self) -> dt.datetime | None:
        s = (self.audit.get("clock") or {}).get("now_et", "")
        try:
            return et_datetime(dt.date.fromisoformat(s[:10]), s[11:16])
        except ValueError:
            return None

    def discover(self) -> dict:
        d = self.audit.get("discover") or {}
        return {"data": {"quota": {"monthly_remaining": self.audit.get("quota_end")},
                         "working_sets": {"dealer-heatmap": {"tickers": d.get("working_set", [])}},
                         "capabilities_detail": [{"command": c, "available": True} for c in d.get("available_commands", [])]}}

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "/v1/openapi.json":
            raise YHSApiError(0, "REPLAY", "离线重放不核查 openapi", path)
        rec = self.map.get((path, json.dumps(params or {}, sort_keys=True)))
        if rec is None:
            raise YHSApiError(404, "NOT_IN_AUDIT", "审计目录中没有该请求的响应", path)
        if rec.get("status") != 200:
            raise YHSApiError(rec.get("status") or 0, rec.get("error_code") or "", rec.get("error") or "", path)
        with open(os.path.join(self.dir, "responses", rec["saved_file"]), "r", encoding="utf-8") as f:
            return json.load(f)


def run_premarket(cfg: SystemConfig, client, now_et: dt.datetime | None = None,
                  tickers: list | None = None, use_flow: bool = True) -> str:
    wf = PremarketWorkflow(cfg, client, now_et, tickers, use_flow)
    if isinstance(client, AuditReplayClient):
        wf.run_id += "_replay"
        wf.audit_dir += "_replay"
        wf.coverage_gaps.append(f"离线重放：数据来自审计目录 {client.dir}，非实时")
    return wf.run()
