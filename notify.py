"""
notify.py —— Discord 实时通知（仅标准库）
=========================================
在 Discord 频道里建一个 Webhook（频道设置 → 整合 → Webhook → 新 Webhook → 复制 URL），
把 URL 填进 config.json 的 "discord_webhook_url"（不要提交到 git）。

实盘模式（--live）会推送：启动、当日标的池、开仓、平仓、收盘复盘、配额熔断与异常。
回测不推送。发送失败只记日志，绝不影响交易逻辑。

测试：python3 main.py --notify-test
"""
from __future__ import annotations

import copy
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("yhs.notify")

GREEN, RED, BLUE, GREY, ORANGE = 0x2ECC71, 0xE74C3C, 0x3498DB, 0x95A5A6, 0xE67E22
TAG_A, TAG_B = "🅰️【A·严格异动】", "🅱️【B】"
REASON_CN = {"STOP_LOSS": "硬止损", "BREAKEVEN_STOP": "保本止损", "TRAILING_STOP": "追踪止盈",
             "UNDERLYING_STOP": "标的破节点止损",
             "HARD_TAKE_PROFIT": "硬顶止盈", "EOD_FORCE_CLOSE": "收盘强平", "QUOTA_HALT": "配额熔断",
             "MANUAL_STOP": "手动停止", "REPLACED": "被更高分信号替换"}


class DiscordNotifier:
    def __init__(self, webhook_url: str, username: str = "夜航社 GEX 模拟盘", timeout_s: float = 10.0, tag: str = ""):
        self.url = (webhook_url or "").strip()
        self.username = username
        self.timeout_s = timeout_s
        self.tag = tag   # 账户标签，加在每条消息最前面（如 "🅰️【A·严格异动】"）

    def tagged(self, tag: str) -> "DiscordNotifier":
        n = copy.copy(self)
        n.tag = tag
        return n

    def _t(self, title: str) -> str:
        return f"{self.tag} {title}" if self.tag else title

    @property
    def enabled(self) -> bool:
        return self.url.startswith("https://")

    def send(self, content: str = "", embeds: list | None = None) -> bool:
        if not self.enabled:
            return False
        body = {"username": self.username, "content": content[:1900]}
        if embeds:
            body["embeds"] = embeds[:10]
        data = json.dumps(body).encode("utf-8")
        for attempt in range(3):
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "Content-Type": "application/json",
                # Discord 会拒绝 Python 默认 User-Agent（Cloudflare 1010）
                "User-Agent": "DiscordBot (https://github.com/VinceChen0116/yehangshe-vince, 1.0)"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s):
                    return True
            except urllib.error.HTTPError as e:
                if e.code == 429:   # Webhook 限频（约 30 条/分钟）：按 retry_after 等待后重试
                    try:
                        wait = float(json.loads(e.read().decode("utf-8")).get("retry_after", 2))
                    except (ValueError, AttributeError):
                        wait = 2.0
                    time.sleep(min(wait, 30) + 0.2)
                    continue
                log.warning("Discord 推送失败：HTTP %s %s", e.code, e.reason)
                return False
            except Exception as e:   # 网络问题不能影响交易
                log.warning("Discord 推送失败：%s", e)
                time.sleep(1 + attempt)
        return False

    # ------------------------------------------------------------------ 事件
    def text(self, msg: str) -> None:
        self.send(self._t(msg))

    def position_opened(self, p) -> None:
        bull = p.action == "BUY_CALL"
        self.send(embeds=[{
            "title": self._t(f"{'🟢' if bull else '🔴'} 开仓 {p.ticker} {'CALL' if bull else 'PUT'} {p.strike:g}"),
            "color": GREEN if bull else RED,
            "fields": [
                {"name": "合约", "value": f"`{p.contract_symbol}`", "inline": False},
                {"name": "结构", "value": "跳板（看涨）" if p.structure == "SPRINGBOARD" else "悬崖（看跌）", "inline": True},
                {"name": "到期", "value": p.expiration, "inline": True},
                {"name": "现货", "value": f"{p.entry_spot:.2f}", "inline": True},
                {"name": "成本（含滑点）", "value": f"${p.entry_option_price:.2f}", "inline": True},
                {"name": "目标加速位", "value": f"{p.target_strike:g}", "inline": True},
                {"name": "δ", "value": f"{p.delta:.2f}", "inline": True},
                {"name": "信号评分", "value": f"{getattr(p, 'signal_score', 0.0):.2f} / 1.00", "inline": True},
            ],
            "footer": {"text": f"{p.position_id} · 美东 {p.entry_time.strftime('%H:%M')} · 模拟盘"},
        }])

    def position_closed(self, p) -> None:
        win = p.realized_pnl > 0
        self.send(embeds=[{
            "title": self._t(f"{'✅' if win else '❌'} 平仓 {p.ticker} {p.strike:g}{'C' if p.right == 'C' else 'P'} "
                             f"{p.realized_roi_pct:+.1f}%（{REASON_CN.get(p.exit_reason, p.exit_reason)}）"),
            "color": GREEN if win else RED,
            "fields": [
                {"name": "期权", "value": f"${p.entry_option_price:.2f} → ${p.exit_option_price:.2f}", "inline": True},
                {"name": "现货", "value": f"{p.entry_spot:.2f} → {p.exit_spot:.2f}", "inline": True},
                {"name": "盈亏（每张）", "value": f"${p.realized_pnl:+,.2f}", "inline": True},
                {"name": "峰值 / 最大逆行", "value": f"{p.peak_roi_pct:+.1f}% / {p.max_adverse_roi_pct:+.1f}%", "inline": True},
                {"name": "持仓", "value": f"{p.entry_time.strftime('%H:%M')} → {p.exit_time.strftime('%H:%M')}", "inline": True},
            ],
            "footer": {"text": f"{p.position_id} · 模拟盘"},
        }])

    def daily_summary(self, day, stats: dict, positions: list) -> None:
        if not stats.get("trades"):
            self.send(self._t(f"📊 **{day} 收盘**：今日无交易。"))
            return
        lines = [f"`{p.ticker:<5} {p.action[4:]:<4} {p.realized_roi_pct:+7.1f}%  {REASON_CN.get(p.exit_reason, p.exit_reason)}`"
                 for p in positions if not p.is_open]
        pf = stats.get("profit_factor")
        self.send(embeds=[{
            "title": self._t(f"📊 {day} 收盘复盘：{stats['trades']} 笔，合计 ${stats['total_pnl_usd']:+,.2f}"),
            "color": GREEN if stats["total_pnl_usd"] >= 0 else RED,
            "description": "\n".join(lines)[:3500],
            "fields": [
                {"name": "胜率", "value": f"{stats['win_rate_pct']:.0f}%", "inline": True},
                {"name": "方向正确率", "value": f"{stats['directional_accuracy_pct']:.0f}%", "inline": True},
                {"name": "盈亏比", "value": "∞" if pf == float("inf") else f"{pf:.2f}", "inline": True},
            ],
            "footer": {"text": "每笔 1 张 · 模拟盘"},
        }])


def _money(v) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else '-'}${abs(v):,.0f}"


def experiment_summary(n: DiscordNotifier, s: dict, base: dict | None = None) -> None:
    """回测实验结果（experiment.py 跑完后调用）。base 为对照实验的 summary，用于并排对比。"""
    o = s["overall"]
    rows = [("笔数", "trades", str), ("合计", "total_pnl", _money), ("去掉最大一笔", "pnl_ex_best", _money),
            ("胜率", "win_rate_pct", lambda v: f"{v}%"), ("方向正确", "direction_pct", lambda v: f"{v}%"),
            ("盈亏比", "profit_factor", str), ("硬止损笔数", "hard_stops", str),
            ("标的破位止损", "underlying_stops", str)]
    def val(src, k, f):
        v = src.get(k)
        return "—" if v is None else f(v)
    if base:
        b = base["overall"]
        w = max(len(s["name"]), len(base["name"]), 8)
        table = [f"{'':<8}{base['name']:>{w}}  {s['name']:>{w}}"]
        table += [f"{label:<8}{val(b, k, f):>{w}}  {val(o, k, f):>{w}}" for label, k, f in rows]
    else:
        table = [f"{label:<8}{val(o, k, f):>12}" for label, k, f in rows]
    sess = s.get("breakdown", {}).get("by_session", {})
    sess_line = "  ".join(f"{k} {_money(v.get('total_pnl'))}（{v.get('trades', 0)} 笔）" for k, v in sess.items())
    ov = {k: v for k, v in s.get("overrides", {}).items()}
    n.send(embeds=[{
        "title": f"🧪 回测完成：{s['name']}" + (f"  vs  {base['name']}" if base else ""),
        "color": GREEN if (o.get("total_pnl") or 0) >= 0 else RED,
        "description": "```\n" + "\n".join(table) + "\n```",
        "fields": [
            {"name": "改动", "value": f"`{ov}`" if ov else "无（当前默认规则）", "inline": False},
            {"name": "日期", "value": f"{s['dates'][0]} ~ {s['dates'][-1]}（{len(s['dates'])} 天），标的 {len(s['tickers'])} 只",
             "inline": False},
            {"name": "按开仓时段", "value": sess_line or "—", "inline": False},
        ],
        "footer": {"text": "历史回放 · 真实期权价格 · 每笔 1 张"},
    }])


def from_config(cfg) -> DiscordNotifier | None:
    n = DiscordNotifier(getattr(cfg, "discord_webhook_url", ""))
    return n if n.enabled else None
