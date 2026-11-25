from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


class Calendar:
    """XNYS holiday/early-close calendar; missing dependency blocks live decisions."""
    def __init__(self, demo=False):
        self.demo = demo
        self.cached = {}
        self.cal = None
        try:
            import exchange_calendars as xc
            self.cal = xc.get_calendar("XNYS")
        except (ImportError, ValueError):
            pass

    def session(self, now):
        local = now.astimezone(NY)
        day = local.date().isoformat()
        if day in self.cached:
            return self.cached[day]
        result = {"date": day, "open": None, "close": None,
                  "previous": None, "verified": False, "note": "交易日历未就绪"}
        if self.cal is not None:
            try:
                if not self.cal.is_session(day):
                    result.update(verified=True, note="非交易日")
                else:
                    result.update(
                        open=self.cal.session_open(day).to_pydatetime(),
                        close=self.cal.session_close(day).to_pydatetime(),
                        previous=str(self.cal.previous_session(day).date()),
                        verified=True, note="XNYS 日历（含半日市）",
                    )
            except Exception:
                result["note"] = "超出本地日历覆盖；方向判断暂停"
        if self.demo and not result["close"]:
            prev = local.date() - timedelta(days=1)
            while prev.weekday() >= 5:
                prev -= timedelta(days=1)
            result.update(open=local.replace(hour=9, minute=30, second=0, microsecond=0),
                          close=local.replace(hour=16, minute=0, second=0, microsecond=0),
                          previous=prev.isoformat(), verified=True, note="演示专用时钟")
        self.cached[day] = result
        return result


def utcnow():
    return datetime.now(timezone.utc)
