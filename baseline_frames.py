"""baseline_frames.py —— 从本地存档拼出与实盘 snapshot 相同结构的多到期快照（回放 / 测试用，0 配额）。"""
from __future__ import annotations

import datetime as dt
import os

from archive import heatmap_path, load_archived_heatmap, rows_to_frames
from tape_archive import tape_heatmap_path
from tape_filter_study import load_rows


def archived_expirations(cfg, day: dt.date, ticker: str) -> list:
    exps = set()
    for base in (os.path.dirname(heatmap_path(cfg, day, ticker, "x")), os.path.dirname(tape_heatmap_path(cfg, day, ticker, "x"))):
        if os.path.isdir(base):
            exps |= {f[:-5] for f in os.listdir(base) if f.endswith(".json") and not f.startswith("_")}
    return sorted(e for e in exps if e >= day.isoformat())


def merged_frames(cfg, day: dt.date, ticker: str) -> dict:
    """{快照标记时间(ET): payload}；payload 与 /v1/derived/heatmap/{t}/snapshot 相同：data.cells 含所有已存档到期。"""
    out: dict = {}
    for exp in archived_expirations(cfg, day, ticker):
        rows = load_archived_heatmap(cfg, day, ticker, exp) or load_rows(cfg, day, ticker, exp)
        if not rows:
            continue
        for t, fr in rows_to_frames(ticker, day, exp, rows).items():
            d = fr.get("data") or {}
            m = out.setdefault(t, {"data": {"ticker": ticker, "generated_at": d.get("generated_at"),
                                             "session_date_et": day.isoformat(), "spot_usd": d.get("spot_usd"),
                                             "expirations": [], "cells": []}})
            md = m["data"]
            if d.get("spot_usd") and not md.get("spot_usd"):
                md["spot_usd"] = d.get("spot_usd")
            md["expirations"].append(exp)
            for c in d.get("cells") or []:
                c = dict(c)
                c.setdefault("expiration", exp)
                md["cells"].append(c)
    return dict(sorted(out.items()))
