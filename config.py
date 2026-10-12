"""
config.py —— 系统配置数据类 (SystemConfig) 与美东时间工具
=========================================================
仅依赖 Python 3.10+ 标准库。

API Key 读取优先级：
  1. 命令行 --config 指定的 JSON 文件中的 "api_key"
  2. 环境变量 YHS_API_KEY
  （请勿把 Key 硬编码进源码或提交到 Git）

真实 API 对照（https://api.yehangshe.com/v1/openapi.json）：
  阶段1 成交量榜单   GET /v1/market/options-volume-rank      （固定 Top150，不接受 limit，无股价字段）
  阶段2 Heatmap      GET /v1/derived/heatmap/{ticker}/snapshot
  阶段3 Flow 大单    GET /v1/flow/trades?tickers=&min_premium_usd=
  阶段4 期权链       GET /v1/options/chain-snapshot/{ticker}?expiration=
  免费元数据         GET /v1/discover , /v1/health
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field, fields

log = logging.getLogger("yhs.config")

# ---------------------------------------------------------------------------
# 宽基指数黑名单（无论如何剔除）
# ---------------------------------------------------------------------------
INDEX_BLACKLIST = frozenset({
    "SPX", "SPXW", "NDX", "NDXP", "RUT", "RUTW", "VIX", "VIXW", "XSP", "DJX",
    "OEX", "XEO", "MRUT", "XND", "VXN", "RVX", "SKEW",
})

# 常见 ETF / ETN 名录（用于识别"非白名单 ETF"并剔除）。
# 榜单接口不返回证券类型，故采用本地名录；若接口行内带有 asset_type/is_etf 字段会优先使用。
KNOWN_ETFS = frozenset({
    # 宽基/风格
    "SPY", "QQQ", "IWM", "DIA", "VOO", "IVV", "VTI", "RSP", "MDY", "IJR", "IJH", "QQQM", "SPLG",
    "VUG", "VTV", "SCHD", "MTUM", "QUAL", "USMV",
    # 杠杆/反向
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SPXU", "UPRO", "SSO", "SDS", "QLD", "QID", "TNA", "TZA",
    "SOXL", "SOXS", "LABU", "LABD", "FAS", "FAZ", "TECL", "TECS", "UVXY", "SVXY", "VXX", "VIXY",
    "NVDL", "NVDX", "NVD", "TSLL", "TSLQ", "TSLZ", "CONL", "MSTU", "MSTX", "MSTZ", "BITX", "AMDL",
    "YINN", "YANG", "BOIL", "KOLD", "UCO", "SCO", "NUGT", "DUST", "JNUG", "JDST", "TMF", "TMV",
    "TBT", "UDOW", "SDOW", "FNGU", "FNGD", "BULZ", "GUSH", "DRIP", "ERX", "ERY", "SH", "PSQ",
    "SMCX", "PLTU", "GGLL", "AAPU", "METU", "AMZU", "MSFU",
    # 行业 / 主题
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "XBI", "XOP",
    "BLOK", "BOTZ", "FIW", "ITA", "KORU", "MAGS", "MJ", "MOAT", "URNM", "SPCX", "DRAM",
    "XME", "XRT", "XHB", "KRE", "KBE", "SMH", "SOXX", "IBB", "ITB", "IYR", "VNQ", "OIH", "GDX",
    "GDXJ", "SIL", "SILJ", "COPX", "URA", "LIT", "TAN", "ICLN", "JETS", "ARKK", "ARKG", "ARKW",
    "ARKF", "ARKQ", "IGV", "CIBR", "HACK", "KWEB", "FXI", "MCHI", "EWZ", "EWJ", "EWW", "EWT",
    "EWY", "EWG", "EWU", "INDA", "EEM", "EFA", "VWO", "IEMG", "VEA", "ASHR",
    # 债券 / 商品 / 加密 / 汇率
    "TLT", "IEF", "SHY", "HYG", "LQD", "JNK", "AGG", "BND", "TIP", "EMB", "BIL", "SGOV", "SHV",
    "GLD", "IAU", "SLV", "USO", "UNG", "DBA", "DBC", "PDBC", "CPER", "WEAT", "CORN",
    "IBIT", "FBTC", "GBTC", "BITO", "ETHA", "ETHE", "ARKB", "BITB", "UUP", "FXE", "FXY",
})


@dataclass
class SystemConfig:
    # ------------------------------------------------------------------ API
    api_key: str = ""
    base_url: str = "https://api.yehangshe.com"
    request_timeout_s: float = 20.0

    # ------------------------------------------------------------------ 配额与限流
    monthly_quota: int = 100_000          # 每月配额上限
    quota_reserve: int = 1_500            # X-Quota-Remaining 低于此值触发熔断，停止一切计费请求
    quota_soft_floor: int = 5_000         # 低于此值进入"节流模式"：标的池自动压缩
    safe_rpm: int = 50                    # 平台 60 RPM，安全上限 50
    min_request_interval_s: float = 1.2   # 串行请求最小间隔
    max_in_flight: int = 3                # 平台并发 5，系统上限 3
    rate_remaining_floor: int = 3         # X-RateLimit-Remaining ≤ 此值时等待窗口重置
    max_202_retries: int = 8              # 202 免费重试轮数（指数退避）
    max_503_retries: int = 3
    max_network_retries: int = 3
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0

    # ------------------------------------------------------------------ 阶段1 标的池
    universe_top_n: int = 100
    universe_fetch_time: str = "09:25"    # 每日拉取榜单的美东时刻（盘前；若当日榜单盘前为空可改为 "09:35"）
    etf_whitelist: tuple = ("SPY", "QQQ", "TLT", "SOXL", "TQQQ")
    min_spot_usd: float = 20.0            # 股价门槛（榜单无股价，在首次 Heatmap 拉取时用 spot_usd 惰性过滤，零额外配额）
    vol_oi_min: float = 0.65
    oi_growth_min: float = 0.05           # 小数比例，0.05 = 5%
    pcr_bull_max: float = 0.60
    pcr_bear_min: float = 1.60
    require_volume_anomaly: bool = True   # 必须满足 Vol/OI 或 OI 增幅
    require_pcr_extreme: bool = True      # 必须满足 PCR 极端倾斜
    pcr_direction_filter: bool = True     # 结构方向与 PCR 偏向相反时放弃信号
    max_universe_size: int = 60           # 预算守卫上限
    est_polls_per_ticker_day: int = 57    # 保守估计：5min×24 + 午间 15 次 + 5min×18 ≈ 57 次/标的/日
    flow_chain_budget_share: float = 0.15 # 每日预算中预留给 Flow/期权链的比例
    poll_offset_s: int = 25               # Heatmap 为 5 分钟固定采样，整点后延迟 25s 拉取以确保该段已归档

    # ------------------------------------------------------------------ 阶段2 Heatmap 轮询
    session_open: str = "09:30"
    session_close: str = "16:00"
    fast_windows: tuple = (("09:30", "11:30"), ("14:30", "16:00"))
    fast_poll_min: int = 5
    slow_poll_min: int = 12               # 11:30-14:30 午间降频
    poll_open_positions_fast: bool = True # 持仓标的始终按 5 分钟盯市（风控优先）
    # 到期选择：有 0DTE 用 0DTE，否则用最近的到期日（交易日口径，2026-10-12 用户规则）。
    #   设为整数 N 则只接受 ≤N 个交易日的到期（如 1 = "0DTE 否则 1DTE"，都没有则该标的不交易）
    expiry_max_trading_dte: int | None = None
    expiry_search_trading_days: int = 10  # 历史回放/存档向后探测最近到期日的最大交易日数（覆盖月度到期 + 节假日）
    node_distance_max_pct: float = 0.025  # 现货距正 GEX 节点 ≤ 2.5%
    fatigue_touch_band_pct: float = 0.004 # 距节点 ≤ 0.4% 视为一次"测试回踩"
    fatigue_reset_band_pct: float = 0.008 # 离开 ≥ 0.8% 后再回来才计新的一次
    fatigue_max_tests: int = 3            # 测试次数 > 3 视为节点疲劳
    max_snapshot_age_min: float = 15.0    # Heatmap 内容时间过旧则跳过本轮

    # ------------------------------------------------------------------ 阶段3 Flow 确认
    require_flow_confirm: bool = False    # Flow API 修复中，暂时默认关闭。False = 仅 Heatmap 模式：跳过阶段3，结构成立即开仓
    flow_min_premium_usd: float = 50_000.0
    flow_window_min: int = 15
    flow_recheck_min: int = 10            # Flow 未确认后，同一标的 10 分钟内不再重查（< 15 分钟窗口，不会漏单）
    flow_page_limit: int = 50
    flow_require_vol_ge_oi: bool = True
    flow_contract_lookup_fallback: bool = True   # 行内缺少 volume/OI 时调用 contracts/lookup 补查（每笔 1 次配额）
    flow_max_lookups_per_signal: int = 2

    # ------------------------------------------------------------------ 阶段4 模拟交易
    strike_mode: str = "target_node"      # "target_node"：买负 GEX 节点行权价（受 max_otm_pct 限制）；"atm"：买最近平值
    max_otm_pct: float = 0.005            # 行权价最多虚值 0.5%：负节点更远时，改买 Heatmap 挂牌行权价中虚值 ≤0.5% 且离节点最近的
    use_chain_greeks: bool = True         # 期权链若返回 delta/gamma 则使用，否则用默认值
    default_delta: float = 0.45
    default_gamma: float = 0.04
    fallback_iv: float = 0.45             # 期权链不可用时，用 BS 由现货换算权利金
    risk_free_rate: float = 0.04
    slippage_pct: float = 0.02            # 买入 +2%，卖出 -2%
    # 历史回放：用真实期权数据开仓与盯市（每个合约 2 次配额）——
    #   contract-intraday 真实成交价（仅最近 150 分钟）优先，其余时段用 contract-greeks-series 的真实 IV + BS
    use_real_option_pricing: bool = True
    greeks_interval: str = "1m"           # contract-greeks-series 粒度：1m 一次请求即全天 391 点，配额与 5m 相同
    intraday_mark_min: int = 1            # 历史回放持仓盯市间隔（分钟）；0 = 只在 5 分钟 Heatmap 快照时盯市
    min_entry_delta: float = 0.0          # 开仓合约 |delta| 低于此值不开仓（0 = 关闭；2026-10-12 用户决定关闭）；历史回放用真实 delta
    min_option_price: float = 0.05       # 期权价低于此值不开仓（防异常报价导致 ROI 失真）
    contracts_per_trade: int = 1
    contract_multiplier: int = 100

    stop_loss_pct: float = -35.0
    breakeven_trigger_pct: float = 35.0
    breakeven_stop_pct: float = 0.0
    trail_trigger_pct: float = 60.0
    trail_giveback_pct: float = 20.0      # 从峰值 ROI 回撤 20 个百分点（如峰值 +80% → +60% 止盈）
    hard_take_profit_pct: float = 150.0
    direction_hit_pct: float = 15.0       # 峰值浮盈 ≥ +15% 判定方向正确

    max_open_positions: int = 5
    max_trades_per_ticker_day: int = 2
    reentry_cooldown_min: int = 30
    no_new_entries_before: str = "09:45"  # 开盘前 15 分钟 Heatmap 噪音大，不开新仓
    no_new_entries_after: str = "15:30"
    force_close_time: str = "16:00"

    # ------------------------------------------------------------------ 盘前研究工作流（--premarket）
    # 以下均为"待验证研究参数"，不代表已回测的最优值
    pm_rank_top_n: int = 30               # 成交量排名前 N
    pm_oi_extra_n: int = 10               # 另选 OI 变化显著的标的数
    pm_oi_min_current: float = 50_000.0   # OI 基数过小的标的不参与 OI 变化排序（避免小基数放大）
    pm_pool_cap: int = 40                 # 与 Heatmap working_set 取交集后的上限
    pm_shortlist_min: int = 10
    pm_shortlist_max: int = 15
    pm_near_dte_max: int = 7              # 0–7 DTE 近端重点
    pm_mid_dte_max: int = 30              # 8–30 DTE 冲突检查；更远仅作背景
    pm_key_walls_n: int = 3               # 首版重点观察的正 Wall 数
    pm_atr_period: int = 14
    pm_atr_distance_mult: float = 1.5     # 重点节点距现价 ≤ 1.5 × daily_atr_14
    pm_eps_spread_mult: float = 2.0       # epsilon = max(2 × spread_usd, 0.05 × daily_atr_14)
    pm_eps_atr_mult: float = 0.05
    pm_quote_max_age_s: float = 60.0      # 报价超过 60 秒降级
    pm_spread_abnormal_pct: float = 0.003 # 价差 / 中间价 > 0.3% 视为异常
    pm_bar_max_age_min: float = 10.0      # 5 分钟 K 线作为降级价格源时允许的最大滞后
    pm_node_significance: float = 0.25    # |GEX| ≥ 近端最强正节点 × 该比例，视为显著节点
    pm_space_min_atr: float = 0.5         # 运动方向上下一正节点距离 < 0.5 ATR 视为空间过近
    pm_confirm_bars: int = 2              # 开盘确认：完成的 5 分钟 K 线数量
    pm_require_flow_for_confirm: bool = False  # Flow API 修复中，暂时默认关闭。True = Flow 不可用时最多到 candidate，不标 confirmed
    pm_flow_min_effective_usd: float = 250_000.0  # 有效方向样本金额低于此值 → flow_support=unknown
    pm_flow_balance_threshold: float = 0.15   # |direction_balance| ≥ 阈值才判 bullish/bearish
    pm_flow_package_share_max: float = 0.5    # 组合占比高于此值降级
    pm_flow_single_print_share_max: float = 0.5  # 单笔占比高于此值降级
    pm_flow_trades_limit: int = 100
    pm_relvol_min_history: int = 5        # 本地归档 ≥ 5 个交易日才计算相对成交量
    pm_report_dir: str = "reports"

    # ------------------------------------------------------------------ 输出
    trades_csv: str = "trades_history.csv"
    # 每日存档（archive.py）：服务端 Heatmap 历史只保留 3 个交易日
    archive_tickers: tuple = ("SPY", "QQQ", "TQQQ", "SOXL", "NVDA", "TSLA", "AAPL", "AMD", "META", "MSFT",
                              "AMZN", "GOOGL", "AVGO", "PLTR", "MSTR", "COIN", "NFLX")
    archive_max_dte: int = 7              # 存档未来 7 天内的全部到期日（另加下一个周五）
    data_dir: str = "yhs_data"
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "SystemConfig":
        cfg = cls()
        if not path:
            # 未指定 --config 时，自动读取项目目录下的 config.json（若存在）
            default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            if os.path.exists(default):
                path = default
        if path:
            with open(path, "r", encoding="utf-8") as f:
                overrides = json.load(f)
            known = {f.name for f in fields(cls)}
            for k, v in overrides.items():
                if k not in known:
                    log.warning("配置文件中存在未知字段 %s，已忽略", k)
                    continue
                if isinstance(getattr(cfg, k), tuple) and isinstance(v, list):
                    v = tuple(tuple(x) if isinstance(x, list) else x for x in v)
                setattr(cfg, k, v)
        if not cfg.api_key:
            cfg.api_key = os.environ.get("YHS_API_KEY", "").strip()
        return cfg

    def masked_key(self) -> str:
        k = self.api_key
        if not k:
            return "(未设置)"
        return k[:8] + "…" + k[-4:] if len(k) > 14 else "****"


# ===========================================================================
# 美东时间工具（不依赖 zoneinfo/tzdata，Windows 下也可直接运行）
# 美国夏令时：3 月第二个周日 02:00 起，至 11 月第一个周日 02:00 止
# ===========================================================================
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + dt.timedelta(days=offset + 7 * (n - 1))


def _et_offset_hours(utc_dt: dt.datetime) -> int:
    y = utc_dt.year
    dst_start = dt.datetime.combine(_nth_weekday(y, 3, 6, 2), dt.time(7, 0), dt.timezone.utc)   # 02:00 EST
    dst_end = dt.datetime.combine(_nth_weekday(y, 11, 6, 1), dt.time(6, 0), dt.timezone.utc)    # 02:00 EDT
    return -4 if dst_start <= utc_dt < dst_end else -5


def to_et(d: dt.datetime) -> dt.datetime:
    """任意带时区 datetime → 美东时间（带固定偏移 tzinfo）。naive 视为 UTC。"""
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    u = d.astimezone(dt.timezone.utc)
    return u.astimezone(dt.timezone(dt.timedelta(hours=_et_offset_hours(u))))


def et_now() -> dt.datetime:
    return to_et(dt.datetime.now(dt.timezone.utc))


def et_datetime(date: dt.date, hhmm: str) -> dt.datetime:
    """构造某交易日某美东时刻（自动判定 EDT/EST）。"""
    h, m = map(int, hhmm.split(":"))
    for off in (-4, -5):
        cand = dt.datetime.combine(date, dt.time(h, m), dt.timezone(dt.timedelta(hours=off)))
        if _et_offset_hours(cand.astimezone(dt.timezone.utc)) == off:
            return cand
    return dt.datetime.combine(date, dt.time(h, m), dt.timezone(dt.timedelta(hours=-5)))


def parse_iso(s) -> dt.datetime | None:
    """解析 ISO 时间串 / epoch 秒或微秒；失败返回 None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        v = float(s)
        if v > 1e14:      # 微秒
            v /= 1e6
        elif v > 1e11:    # 毫秒
            v /= 1e3
        return dt.datetime.fromtimestamp(v, dt.timezone.utc)
    try:
        t = str(s).strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(t)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def hhmm_to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def minutes_of_day(d: dt.datetime) -> int:
    return d.hour * 60 + d.minute


def poll_interval_min(cfg: SystemConfig, now_et: dt.datetime) -> int:
    """自适应轮询节奏：早盘/尾盘 5 分钟，午间 12 分钟。"""
    m = minutes_of_day(now_et)
    for a, b in cfg.fast_windows:
        if hhmm_to_minutes(a) <= m < hhmm_to_minutes(b):
            return cfg.fast_poll_min
    return cfg.slow_poll_min


def setup_logging(level: str = "INFO", logfile: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        os.makedirs(os.path.dirname(logfile) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    # 兼容 Python 3.7：手动清理 root handlers，等价于 basicConfig(force=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


# =============================================================================
# NYSE 交易日历（规则生成，含提前收盘日）
# =============================================================================
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    return d + dt.timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def _observed(d: dt.date) -> dt.date:
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def nyse_holidays(year: int) -> dict:
    h = {}
    ny = dt.date(year, 1, 1)
    if ny.weekday() == 6:
        h[ny + dt.timedelta(days=1)] = "New Year's Day (observed)"
    elif ny.weekday() < 5:
        h[ny] = "New Year's Day"          # 周六元旦不在前一周五补休
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[_easter(year) - dt.timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(dt.date(year, 6, 19))] = "Juneteenth"
    h[_observed(dt.date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    h[_observed(dt.date(year, 12, 25))] = "Christmas Day"
    return h


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def next_trading_day(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def prev_trading_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def session_close_hhmm(d: dt.date) -> str:
    """13:00 提前收盘：独立日前一交易日（7/3）、感恩节次日、平安夜。"""
    thanksgiving = _nth_weekday(d.year, 11, 3, 4)
    early = {thanksgiving + dt.timedelta(days=1), dt.date(d.year, 12, 24), dt.date(d.year, 7, 3)}
    return "13:00" if d in early and is_trading_day(d) else "16:00"


def trading_dte(expiration: dt.date, today: dt.date) -> int:
    """交易日口径的 DTE：当天到期 = 0，下一个交易日到期 = 1（周五 → 周一也算 1）。"""
    if expiration < today:
        return -1
    n, d = 0, today
    while d < expiration:
        d = next_trading_day(d)
        n += 1
    return n


def expiry_candidates(day: dt.date, max_trading_dte: int | None, search: int = 10) -> list[str]:
    """历史回放/存档按顺序尝试的到期日：当天（0DTE）、下一个交易日（1DTE）……
    max_trading_dte 为 None 时向后最多探测 search 个交易日，取第一个有数据的（即最近到期）。"""
    n = search if max_trading_dte is None else min(max_trading_dte, search)
    out, d = [day], day
    for _ in range(n):
        d = next_trading_day(d)
        out.append(d)
    return [x.isoformat() for x in out]
