"""
tail_live.py —— 尾盘彩票精简版（SPX 0DTE 尾盘观察台，2026-11-17 起）
=================================================================
只读观察：不下单、不开模拟仓、不落盘行情。美东 14:58–16:00 拉夜航社数据，网页显示 Feed / Strike Leader / GEX / Playbook，
Playbook 倾向（CALL / PUT / 中性 / 待命）变化时推 Discord。所有读数都是"仅观察，非建议"。
规则与局限见 tail_lottery/README.md。

用法：
  python3 tail_live.py                         # 常驻；交易日 14:58 自动开始，16:01 推收盘总结
  python3 tail_live.py --port 8765 --log-file logs/tail_live.log
  python3 tail_live.py --no-discord            # 只看网页，不推送
  python3 tail_live.py --demo                  # 假行情源 + 模拟时钟（15:25 起 10 倍速），0 配额，看页面效果
密钥：环境变量 YHS_API_KEY / DISCORD_WEBHOOK_URL（或 config.json），不会打印。
"""
from __future__ import annotations

import argparse
import logging
import threading

from config import SystemConfig, setup_logging

log = logging.getLogger("tail")


def main(argv=None):
    ap = argparse.ArgumentParser(description="尾盘彩票精简版：SPX 0DTE 尾盘观察台（只读）")
    ap.add_argument("--port", type=int, default=8765, help="网页端口（默认 8765，只监听 127.0.0.1）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", default=None)
    ap.add_argument("--log-file", default=None)
    ap.add_argument("--no-discord", action="store_true")
    ap.add_argument("--demo", action="store_true", help="假行情源演示（0 配额）")
    a = ap.parse_args(argv)
    setup_logging("INFO", a.log_file)

    from tail_lottery.engine import Engine, run_forever
    from tail_lottery.web import serve

    if a.demo:
        from tail_lottery.demo import demo_engine
        engine = demo_engine()
    else:
        from notify import from_config
        from tail_lottery.source import YHSSource
        from yhs_client import YHSClient
        cfg = SystemConfig.load(a.config)
        if not cfg.api_key:
            raise SystemExit("未配置 YHS_API_KEY")
        n = None if a.no_discord else from_config(cfg)
        if n is not None:
            n.username = "尾盘彩票观察台"
        log.info("Discord 推送：%s", "开" if n else "关")
        engine = Engine(YHSSource(YHSClient(cfg)), notifier=n)
    serve(engine, a.host, a.port)
    stop = threading.Event()
    try:
        run_forever(engine, stop)
    except KeyboardInterrupt:
        log.info("退出")


if __name__ == "__main__":
    main()
