"""
nightwatch_run.py —— 云端并行运行别人分享的「SPX 尾盘工作台 v0.2.18」（third_party/spx_tail_observer，代码原样不改）
================================================================================================
2026-11-25 用户定：原样在云端和我们的 tail_live.py 并行跑，读它的本地快照推 Discord，收盘报告两套对比。

做的事（只用标准库）：
  1. 非交易日 / 半日市：不启动。
  2. 15:30 ET（收盘前半小时，用户 11-26 定；可用 --start 改）用独立 venv 启动它：`python -m observer.app --source nightwatch --port 8767`，
     key 通过环境变量 NIGHTWATCH_API_KEY 传入（取自 YHS_API_KEY，不打印、不落盘）。它自己限本进程 ≤3000 units。
  3. 每 5 秒读 http://127.0.0.1:8767/api/snapshot（只读本机，不调夜航社）：
       方向（CALL / PUT / NEUTRAL）连续 2 次一致且与上次推送不同 → 推 Discord「🌙【分享版工作台】」；疑似 pin 出现时推一次。
       日志：【NW倾向】每次变化；【NW健康】每 5 分钟（units、报价覆盖、成交缺口、阻挡原因）。
  4. 16:02 ET 结束它（SIGINT），推收盘总结，退出。
用法：python3 nightwatch_run.py --log-file logs/nightwatch_<日期>.log [--no-discord]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request

from config import SystemConfig, et_datetime, et_now, is_trading_day, session_close_hhmm, setup_logging, to_et

log = logging.getLogger("nightwatch_run")
HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "third_party", "spx_tail_observer")
VENV_PY = os.environ.get("NIGHTWATCH_PYTHON", "/home/user/.nw_venv/bin/python")
PORT = 8767
START, STOP = "15:30", "16:02"   # 2026-11-26 用户定：收盘前半小时（15:30 ET）启动
POLL_S = 5
TAG = "🌙【分享版工作台】"
LABEL = {"CALL": "偏多 CALL", "PUT": "偏空 PUT", "NEUTRAL": "中性", "STAND_BY": "待命"}


def fetch(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=4) as r:
        return json.loads(r.read().decode("utf-8"))


def describe(pb):
    s = pb.get("scores") or {}
    parts = [f"CALL {s.get('CALL', 0):g} / PUT {s.get('PUT', 0):g}"]
    if pb.get("conflict"):
        parts.append("证据分歧")
    reg = (pb.get("regime") or {}).get("label")
    if reg:
        parts.append(f"形态：{reg}")
    ref = pb.get("reference")
    if isinstance(ref, dict) and ref.get("strike") is not None:
        parts.append(f"近价参考 {ref.get('strike'):g}{ref.get('right', '')}")
    return " · ".join(parts)


def evidence_lines(pb):
    out = []
    for e in pb.get("evidence") or []:
        if isinstance(e, dict):
            name = e.get("name") or e.get("leg") or "?"
            side = e.get("side") or e.get("vote") or "-"
            score = e.get("score", e.get("points", ""))
            note = e.get("note") or e.get("reason") or ""
            out.append(f"· {name}：{side} {score} {note}".strip())
    return out[:10]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--no-discord", action="store_true")
    ap.add_argument("--start", default=START, help="启动时刻 HH:MM ET（默认 15:30；已过则立即启动）")
    a = ap.parse_args(argv)
    setup_logging("INFO", a.log_file)
    cfg = SystemConfig.load()
    from notify import from_config
    n = None if a.no_discord else from_config(cfg)
    if n is not None:
        n.username = "分享版尾盘工作台"

    def push(title, desc, color=0x9B59B6):
        if n:
            n.send(embeds=[{"title": f"{TAG}{title} · 仅观察 · 非建议", "description": desc[:3500], "color": color}])

    today = et_now().date()
    if not is_trading_day(today) or session_close_hhmm(today) != "16:00":
        log.info("今天休市或半日市，不启动")
        return 0
    start = et_datetime(today, a.start)
    stop = et_datetime(today, STOP)
    while et_now() < start:
        time.sleep(5)
    if not cfg.api_key:
        log.error("没有 YHS_API_KEY")
        return 1
    env = dict(os.environ, NIGHTWATCH_API_KEY=cfg.api_key, OBSERVER_PORT=str(PORT), PYTHONUNBUFFERED="1")
    out = open(os.path.join(HERE, "logs", f"nightwatch_app_{today}.out"), "a", encoding="utf-8")
    proc = subprocess.Popen([VENV_PY, "-m", "observer.app", "--source", "nightwatch", "--port", str(PORT)],
                            cwd=APP_DIR, env=env, stdout=out, stderr=subprocess.STDOUT)
    log.info("已启动分享版工作台 pid=%s 端口 %d", proc.pid, PORT)
    pending, pushed, last_key, last_health, pin_pushed = (None, 0), None, None, None, False
    last_pb, units = {}, None
    try:
        while et_now() < stop:
            if proc.poll() is not None:
                log.error("分享版工作台意外退出，返回码 %s", proc.returncode)
                push("意外退出", f"进程返回码 {proc.returncode}，详见 logs/nightwatch_app_{today}.out", 0xE74C3C)
                return 1
            try:
                s = fetch("/api/snapshot")
            except Exception as e:   # 启动中或暂时没响应
                log.debug("快照未就绪：%s", e)
                time.sleep(POLL_S)
                continue
            pb = s.get("playbook") or {}
            last_pb = pb
            units = (s.get("api") or {}).get("units", units)
            d = pb.get("direction")
            key = (d, (pb.get("scores") or {}).get("CALL"), (pb.get("scores") or {}).get("PUT"), pb.get("conflict"),
                   (pb.get("regime") or {}).get("label"), (pb.get("pin") or {}).get("state"))
            if key != last_key:
                last_key = key
                log.info("【NW倾向】%s %s | 阻挡：%s", LABEL.get(d, d), describe(pb),
                         "；".join(map(str, pb.get("blockers") or [])) or "无")
            now_et = et_now()
            m = now_et.replace(second=0, microsecond=0)
            if now_et.minute % 5 == 0 and last_health != m:
                last_health = m
                api = s.get("api") or {}
                log.info("【NW健康】units %s 报价覆盖 %s 成交缺口 %s 成交年龄 %ss 连接 %s 数据就绪 %s",
                         api.get("units"), s.get("quote_coverage"), api.get("flow_gap"), api.get("flow_age_seconds"),
                         s.get("connected"), s.get("healthy"))
            # 推送：方向连续 2 次一致、且与上次推送不同；待命不推
            if d in ("CALL", "PUT", "NEUTRAL") and pb.get("in_window"):
                pv, cnt = pending
                cnt = cnt + 1 if pv == d else 1
                pending = (d, cnt)
                if cnt >= 2 and d != pushed:
                    pushed = d
                    log.info("【NW推送】%s %s", LABEL.get(d), describe(pb))
                    body = describe(pb)
                    ev = evidence_lines(pb)
                    if ev:
                        body += "\n" + "\n".join(ev)
                    push(LABEL.get(d), f"{body}\n{now_et:%H:%M:%S} ET",
                         {"CALL": 0x2ECC71, "PUT": 0xE74C3C}.get(d, 0x95A5A6))
            if (pb.get("pin") or {}).get("state") == "ACTIVE" and not pin_pushed:
                pin_pushed = True
                pin = pb.get("pin") or {}
                log.info("【NW推送】疑似 pin %s", pin.get("strike"))
                push("疑似 pin 盘", f"中心 {pin.get('strike')}，区间 {pin.get('low')}–{pin.get('high')}", 0x5DADE2)
            time.sleep(POLL_S)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        out.close()
    log.info("收盘，已停止分享版工作台；本进程 units %s；最后方向 %s", units, last_pb.get("direction"))
    push("收盘", f"最后方向：{LABEL.get(last_pb.get('direction'), last_pb.get('direction'))}（{describe(last_pb)}）；"
                 f"本进程用 {units} units", 0x7F8C8D)
    return 0


if __name__ == "__main__":
    sys.exit(main())
