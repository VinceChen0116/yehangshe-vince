"""
archive.py —— Heatmap 每日存档（收盘后运行）
==========================================
为什么要存档：/v1/derived/heatmap/{ticker}/history 只保留最近 3 个交易日，过期即无法回测；
而期权价格数据（contract-greeks-series）实测可回溯 9 个月以上，回测时现拉即可，无需存档。

存档内容（每个交易日）：
  * 每只标的：未来 7 天内的全部到期日 + 下一个周五的 Heatmap 全天 5 分钟快照
    → yhs_data/archive/heatmap/{日期}/{标的}/{到期日}.json
  * 当日成交量榜（仅存档当天，用于以后回放阶段1）→ yhs_data/archive/universe/{日期}.json
  * 完成标记 → yhs_data/archive/heatmap/{日期}/_manifest.json

幂等：已存在的文件不重复拉取。默认会顺带检查并补齐最近 3 个交易日中缺失的存档（错过一两天也能补回）。
配额：每只标的 1 次 snapshot（取到期日列表）+ 每个到期日约 4 页，17 只标的约 150–250 次/日。

用法：
  python3 archive.py                      # 存档最近一个已收盘交易日，并补齐近 3 个交易日的缺口
  python3 archive.py --date 2026-10-09    # 只存档指定日期（须在最近 3 个交易日内）
  python3 archive.py --tickers SPY,NVDA   # 覆盖默认标的
  python3 archive.py --dry-run            # 只打印计划，不请求
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

from config import (INDEX_BLACKLIST, KNOWN_ETFS, SystemConfig, et_datetime, et_now, expiry_candidates,
                    is_trading_day, next_trading_day, prev_trading_day, setup_logging)
from yhs_client import QuotaExhausted, YHSApiError, YHSClient

log = logging.getLogger("yhs.archive")

HEATMAP_HISTORY_PATH = "/v1/derived/heatmap/{ticker}/history"
HEATMAP_SNAPSHOT_PATH = "/v1/derived/heatmap/{ticker}/snapshot"
DISCOVER_PATH = "/v1/discover"
VOLUME_RANK_PATH = "/v1/market/options-volume-rank"
RETENTION_SESSIONS = 3      # 服务端 Heatmap 历史保留的交易日数


# =============================================================================
# 路径与读写（main.py 历史回放也通过这里读写存档）
# =============================================================================
def archive_root(cfg: SystemConfig) -> str:
    return os.path.join(cfg.data_dir, "archive")


def heatmap_path(cfg: SystemConfig, day: dt.date, ticker: str, expiration: str) -> str:
    return os.path.join(archive_root(cfg), "heatmap", day.isoformat(), ticker, f"{expiration}.json")


def manifest_path(cfg: SystemConfig, day: dt.date) -> str:
    return os.path.join(archive_root(cfg), "heatmap", day.isoformat(), "_manifest.json")


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)   # 原子替换，避免中断留下半个文件


def load_archived_heatmap(cfg: SystemConfig, day: dt.date, ticker: str, expiration: str) -> list[dict] | None:
    p = heatmap_path(cfg, day, ticker, expiration)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("rows") or []


def fetch_heatmap_rows(client, ticker: str, expiration: str, day: dt.date, max_pages: int = 10) -> list[dict]:
    """分页拉取某交易日某到期日的全天 Heatmap 快照（原始行）。"""
    rows, cursor, pages = [], None, 0
    while pages < max_pages:
        # from/to 只传日期时服务端返回空集，必须传带时区的完整时间
        params = {"expiration": expiration,
                  "from": et_datetime(day, "09:30").isoformat(),
                  "to": et_datetime(day, "16:00").isoformat(),
                  "limit": 24, "cursor": cursor}
        payload = client.get(HEATMAP_HISTORY_PATH.format(ticker=ticker), params)
        pages += 1
        data = payload.get("data") if isinstance(payload, dict) else None
        rows += data if isinstance(data, list) else []
        cursor = (payload.get("_meta") or {}).get("next_cursor") if isinstance(payload, dict) else None
        if not cursor:
            break
    rows.sort(key=lambda r: r.get("minute_at") or "")
    return rows


def get_heatmap_rows(client, cfg: SystemConfig, ticker: str, expiration: str, day: dt.date) -> tuple[list[dict], str]:
    """存档优先；没有则从 API 拉取并顺手存档。返回 (行, 来源)。"""
    rows = load_archived_heatmap(cfg, day, ticker, expiration)
    if rows is not None:
        return rows, "archive"
    rows = fetch_heatmap_rows(client, ticker, expiration, day)
    save_heatmap(cfg, day, ticker, expiration, rows)
    return rows, "api"


def save_heatmap(cfg: SystemConfig, day: dt.date, ticker: str, expiration: str, rows: list[dict]) -> None:
    _write_json(heatmap_path(cfg, day, ticker, expiration), {
        "ticker": ticker, "expiration": expiration, "session_date": day.isoformat(),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
    })


# =============================================================================
# 存档计划
# =============================================================================
def latest_closed_session(now_et: dt.datetime) -> dt.date:
    """最近一个已收盘的交易日（16:15 ET 之后才算当天已收盘，留出 Heatmap 归档时间）。"""
    d = now_et.date()
    if is_trading_day(d) and now_et >= et_datetime(d, "16:15"):
        return d
    return prev_trading_day(d)


def recent_sessions(last: dt.date, n: int) -> list[dt.date]:
    out = [last]
    while len(out) < n:
        out.append(prev_trading_day(out[-1]))
    return sorted(out)


def pick_expirations(listed: list[str], day: dt.date, today: dt.date, max_dte: int) -> list[str]:
    """未来 max_dte 天内的全部到期日 + 下一个周五。
    补存过去的交易日时，snapshot 列表里已不含当时已到期的合约，按规则补回（周五 0DTE、日到期标的的逐日到期）。"""
    exps = {dt.date.fromisoformat(e) for e in listed if e}
    window_end = day + dt.timedelta(days=max_dte)
    pick = {e for e in exps if day <= e <= window_end}
    daily = sum(1 for e in exps if today <= e <= today + dt.timedelta(days=7)) >= 4   # SPY/QQQ 这类每日到期
    d = day
    while d < today:   # 存档日到今天之间已到期、snapshot 不再列出的到期日
        if is_trading_day(d) and (daily or d.weekday() == 4):
            pick.add(d)
        d += dt.timedelta(days=1)
    this_fri = day + dt.timedelta(days=(4 - day.weekday()) % 7)
    pick.add(this_fri)
    later_fris = sorted(e for e in exps if e.weekday() == 4 and e > max(this_fri, window_end))
    pick.add(later_fris[0] if later_fris else this_fri + dt.timedelta(days=7))
    return sorted(e.isoformat() for e in pick)


# =============================================================================
# 执行
# =============================================================================
def build_heatmap_universe(client, cfg: SystemConfig, refresh: bool = False) -> list[str]:
    """回测标的池（规格阶段1的静态部分）：夜航社 Heatmap working_set 全部标的，
    剔除指数与白名单外 ETF，再用 Heatmap 快照的现价剔除 < min_spot_usd 的标的（每只 1 次配额，结果缓存）。"""
    path = os.path.join(archive_root(cfg), "universe", "heatmap_filtered.json")
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["tickers"]
    ws = ((client.get(DISCOVER_PATH).get("data") or {}).get("working_sets") or {}).get("dealer-heatmap") or {}
    all_t = sorted(ws.get("tickers") or [])
    whitelist = set(cfg.etf_whitelist)
    detail, keep = {}, []
    for t in all_t:
        if t in INDEX_BLACKLIST:
            detail[t] = "指数"
            continue
        if t in KNOWN_ETFS and t not in whitelist:
            detail[t] = "白名单外 ETF"
            continue
        try:
            spot = ((client.get(HEATMAP_SNAPSHOT_PATH.format(ticker=t)).get("data") or {}).get("spot_usd"))
        except QuotaExhausted:
            raise
        except YHSApiError as ex:
            detail[t] = f"快照失败：{ex.code}"
            continue
        if spot is None or float(spot) < cfg.min_spot_usd:
            detail[t] = f"股价 {spot} < {cfg.min_spot_usd}"
            continue
        detail[t] = float(spot)
        keep.append(t)
    _write_json(path, {"built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "source": "discover.working_sets.dealer-heatmap", "min_spot_usd": cfg.min_spot_usd,
                       "etf_whitelist": sorted(whitelist), "tickers": keep, "detail": detail})
    log.info("回测标的池：working_set %d 只 → 保留 %d 只（%s）", len(all_t), len(keep), path)
    return keep


def archive_day(client, cfg: SystemConfig, day: dt.date, tickers: list[str], today: dt.date,
                dry_run: bool = False, expiry_mode: str = "full") -> dict:
    """expiry_mode="full"：未来 archive_max_dte 天内全部到期日（每日收盘存档用）；
    "near"：只存 0DTE 与 1DTE（交易日口径），适合大批量补存；
    "nearest"：从 0DTE 起逐个交易日向后探测，存到第一个有数据的到期为止（回测"0DTE 否则最近到期"所需）。"""
    log.info("===== 存档 %s：%d 只标的（%s）=====", day, len(tickers), "仅 0DTE/1DTE" if expiry_mode == "near" else "全部近端到期")
    summary = {"session_date": day.isoformat(), "tickers": {}, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    for t in tickers:
        try:
            if expiry_mode == "nearest":
                info = {}
                for e in expiry_candidates(day, None, cfg.expiry_search_trading_days):
                    rows = load_archived_heatmap(cfg, day, t, e)
                    if rows is None and not dry_run:
                        rows = fetch_heatmap_rows(client, t, e, day)
                        save_heatmap(cfg, day, t, e, rows)
                    info[e] = len(rows) if rows is not None else "待拉取"
                    if rows:
                        break
                summary["tickers"][t] = info
                log.info("  %-5s %s", t, "  ".join(f"{e[5:]}:{v}" for e, v in info.items()))
                continue
            if expiry_mode == "near":
                exps = [day.isoformat(), next_trading_day(day).isoformat()]
            else:
                snap = client.get(HEATMAP_SNAPSHOT_PATH.format(ticker=t)) if not dry_run else {"data": {}}
                listed = ((snap.get("data") or {}).get("expirations") or []) if isinstance(snap, dict) else []
                exps = pick_expirations(listed, day, today, cfg.archive_max_dte)
            info = {}
            for e in exps:
                if os.path.exists(heatmap_path(cfg, day, t, e)):
                    info[e] = "已存在"
                    continue
                if dry_run:
                    info[e] = "待拉取"
                    continue
                rows = fetch_heatmap_rows(client, t, e, day)
                save_heatmap(cfg, day, t, e, rows)
                info[e] = len(rows)
            summary["tickers"][t] = info
            log.info("  %-5s %s", t, "  ".join(f"{e[5:]}:{v}" for e, v in info.items()))
        except QuotaExhausted:
            raise
        except YHSApiError as ex:
            summary["tickers"][t] = {"error": str(ex)}
            log.warning("  %s 存档失败：%s", t, ex)

    if not dry_run and day == latest_closed_session(et_now()) and day == today:
        up = os.path.join(archive_root(cfg), "universe", f"{day.isoformat()}.json")
        if not os.path.exists(up):
            try:
                _write_json(up, client.get(VOLUME_RANK_PATH))
                log.info("  成交量榜已存档")
            except YHSApiError as ex:
                log.warning("  成交量榜存档失败：%s", ex)

    failed = [t for t, v in summary["tickers"].items() if "error" in v]
    summary["complete"] = not failed
    summary["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    # 与已有 manifest 合并：分批存档不同标的时，不能把之前已完成的标的从 manifest 里抹掉
    mp = manifest_path(cfg, day)
    if not dry_run and os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            old = json.load(f)
        merged = dict(old.get("tickers") or {})
        for t, v in summary["tickers"].items():
            merged[t] = {**merged.get(t, {}), **v} if isinstance(merged.get(t), dict) and "error" not in v else v
        summary["tickers"] = merged
        summary["started_at"] = old.get("started_at", summary["started_at"])
        summary["complete"] = not [t for t, v in merged.items() if "error" in v]
    if not dry_run:
        _write_json(manifest_path(cfg, day), summary)
    log.info("存档 %s %s%s", day, "完成" if not failed else "部分失败：", "" if not failed else ",".join(failed))
    return summary


def is_archived(cfg: SystemConfig, day: dt.date, tickers: list[str]) -> bool:
    p = manifest_path(cfg, day)
    if not os.path.exists(p):
        return False
    with open(p, encoding="utf-8") as f:
        m = json.load(f)
    return bool(m.get("complete")) and set(tickers) <= set(m.get("tickers", {}))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Heatmap 每日存档（服务端仅保留 3 个交易日）")
    ap.add_argument("--date", help="只存档指定交易日 YYYY-MM-DD")
    ap.add_argument("--tickers", help="逗号分隔，默认 config.archive_tickers")
    ap.add_argument("--config", help="JSON 配置文件")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--universe", choices=["config", "heatmap"], default="config",
                    help="config：config.archive_tickers；heatmap：夜航社 Heatmap 全部标的按规格阶段1筛选（指数/白名单外ETF/<$20 剔除）")
    ap.add_argument("--expiries", choices=["full", "near", "nearest"], default="full",
                    help="full：未来 7 天内全部到期日；near：只存 0DTE 与 1DTE；nearest：0DTE 否则最近一个有数据的到期")
    ap.add_argument("--days", type=int, default=RETENTION_SESSIONS, help="补齐最近 N 个交易日（≤3，服务端只保留 3 天）")
    ap.add_argument("--log-file", default=None)
    args = ap.parse_args(argv)

    cfg = SystemConfig.load(args.config)
    base = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isabs(cfg.data_dir):
        cfg.data_dir = os.path.join(base, cfg.data_dir)   # 定时任务的工作目录不固定，统一落到项目目录
    setup_logging(cfg.log_level, args.log_file or os.path.join(base, "logs", "archive.log"))
    client = YHSClient(cfg)
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif args.universe == "heatmap":
        tickers = build_heatmap_universe(client, cfg)
    else:
        tickers = list(cfg.archive_tickers)

    now = et_now()
    today = now.date()
    last = latest_closed_session(now)
    if args.date:
        days = [dt.date.fromisoformat(args.date)]
        if not is_trading_day(days[0]):
            log.error("%s 不是交易日", days[0])
            return 2
    else:
        days = recent_sessions(last, min(args.days, RETENTION_SESSIONS))
        if args.expiries == "full":   # near/nearest 按文件逐个判断是否已存（已存在的读本地，0 配额），不看 manifest
            days = [d for d in days if not is_archived(cfg, d, tickers)]
        if not days:
            log.info("最近 %d 个交易日（截至 %s）均已存档，无需操作", RETENTION_SESSIONS, last)
            return 0

    ok = True
    try:
        for d in days:
            ok &= archive_day(client, cfg, d, tickers, today, args.dry_run, args.expiries)["complete"]
    except QuotaExhausted as e:
        log.critical("配额熔断，停止存档：%s", e)
        return 3
    log.info("本次存档结束。%s", client.quota_summary())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
