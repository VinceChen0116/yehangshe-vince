"""Display-only intraday notes from assessments already made by the worker."""
from collections import defaultdict, deque
import re

from .engine import number

ORDER_CONTEXT_MAX_AGE = 15  # Seconds before the first trigger; never backfill with a later frame.
SIDES = {"CALL", "PUT"}


def readout(frame):
    book, gex = frame["playbook"], frame["gex"]
    raw, preview = book["direction"], book.get("price_preview")
    note = {"at": frame["time"], "stage": "WAIT", "side": None, "pattern": None,
            "pin_strike": None, "support": [], "missing": []}
    if not book["in_window"]:
        note.update(stage="OUTSIDE", missing=["不在尾盘方向观察窗口"])
    elif frame["connected"] and frame["healthy"] and raw in ("CALL", "PUT", "NEUTRAL"):
        pin = book.get("pin", {})
        if pin.get("state") == "ACTIVE" and (number(pin.get("strike")) or 0) > 0:
            note.update(stage="PIN", pin_strike=pin["strike"], missing=["区间观察，未确认单边方向"])
        else:
            votes = [{k: v[k] for k in ("name", "side", "points")} for v in book["evidence"]
                     if v["side"] in SIDES and v["points"] > 0]
            note["support"] = sorted(votes, key=lambda v: v["points"], reverse=True)
            if raw == "NEUTRAL":
                note.update(stage="NEUTRAL", missing=["多空分差未达方向门槛"])
            else:
                label = book.get("regime", {}).get("label")
                if label and book.get("volume", {}).get("state") == "EXPANDING":
                    label += " · 放量"
                note.update(stage="DIRECTION", side=raw, pattern=label)
    elif (preview and preview["side"] in SIDES and (preview["source"] == "massive" or
          (frame["connected"] and (preview["source"] == "parity" or gex["available"])))):
        watch = preview.get("tier") == "watch"
        missing = list(preview.get("unconfirmed", ["成交未确认"]))
        if watch:
            missing.append("价格样本尚未达到初判门槛")
        note.update(stage="WATCH" if watch else "PRICE", side=preview["side"],
                    pattern=preview["label"], missing=missing,
                    support=[{"name": "price_path", "side": preview["side"],
                              "label": preview["label"], "points": None}])
    else:
        note["missing"] = list(dict.fromkeys(book.get("blockers") or [book.get("reason") or "等待有效数据"]))
    return note


def nearest_level(frame):
    """Choose a wall or Top node relative to the validated GEX context price."""
    gex, now = frame["gex"], frame["time"]
    source, spot_at, spot = number(gex.get("source_at")), number(frame.get("spot_at")), number(frame.get("spot"))
    if not gex["available"] or not spot or not source or not spot_at or max(source, spot_at) > now:
        return None, None
    limit = frame["api"]["freshness_limit_seconds"]
    content_age = number(gex.get("content_age_seconds")) or 0
    expires = min(source + limit, spot_at + limit, now + limit - max(content_age, 0))
    if expires < now:
        return None, None
    levels = defaultdict(list)
    for value, label in [(gex.get("call_wall"), "Call Wall"), (gex.get("put_wall"), "Put Wall"),
                         *((node["strike"], "GEX Top 节点") for node in gex["top"])]:
        strike = number(value)
        if strike is not None and strike > 0:
            levels[strike].append(label)
    if not levels:
        return None, None
    strike = min(levels, key=lambda k: (abs(k - spot), k))
    return {"strike": strike, "roles": levels[strike], "spot": spot,
            "offset": round(spot - strike, 2), "source_at": source}, expires


class TailJournal:
    def __init__(self):
        self.recent = deque(maxlen=512)
        self.changes = deque(maxlen=2048)
        self.last_change = None

    def observe(self, frame):
        now = frame["time"]
        if not frame["market_open"] or (self.recent and self.recent[-1][0]["at"] >= now):
            return
        note = readout(frame)
        self.recent.append((note, *nearest_level(frame)))
        while self.recent and now - self.recent[0][0]["at"] > 60 + ORDER_CONTEXT_MAX_AGE:
            self.recent.popleft()  # The detector can receive up to a minute of late events.
        if note["stage"] == "OUTSIDE":
            return
        # Different blockers matter; counters/ages ticking upward do not.
        reasons = tuple("成交分页追补中" if s.startswith("成交分页追补中") else re.sub(r"\d+(?:\.\d+)?", "#", s)
                        for s in note["missing"])
        key = (note["stage"], note["side"], note["pattern"], note["pin_strike"], reasons)
        if key != self.last_change:
            self.changes.append(note)
            self.last_change = key

    def for_order(self, trigger, right):
        for note, level, expires in reversed(self.recent):
            lag = trigger - note["at"]
            if lag < 0:
                continue
            if lag > ORDER_CONTEXT_MAX_AGE:
                break
            # Copy the frozen assessment, keep only factors that support this order.
            side = "CALL" if right == "C" else "PUT"
            result = {**note, "support": [v for v in note["support"] if v["side"] == side],
                      "missing": list(note["missing"]), "level": level if level and trigger <= expires else None}
            if note["stage"] == "DIRECTION" and note["side"] != side:
                result["missing"].append("当时综合方向与本笔大单反向")
            if level and trigger > expires:
                result["missing"].append("到触发时这帧 GEX 关键位已过期")
            return result
        return None
