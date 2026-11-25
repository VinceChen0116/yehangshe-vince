import datetime as dt
import gzip
import json
import os
import tempfile
import unittest

import flow_stream
from config import SystemConfig


class TestCompact(unittest.TestCase):
    def test_last_row_wins(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                rec = flow_stream.FlowRecorder(SystemConfig(), day=dt.date(2026, 11, 25))
                with open(rec.raw_path, "w", encoding="utf-8") as f:
                    for recv, agg, prem in [("t1", "a", 50000), ("t2", "b", 60000), ("t3", "a", 90000)]:
                        f.write(json.dumps({"recv": recv, "row": {"aggregate_id": agg, "premium_usd": prem,
                                                                  "occurred_at": recv}}) + "\n")
                out = rec.compact()
                lines = gzip.open(out, "rt").read().splitlines()
                meta, rows = json.loads(lines[0])["_meta"], [json.loads(x) for x in lines[1:]]
                self.assertEqual(meta["rows"], 2)
                a = next(r for r in rows if r["aggregate_id"] == "a")
                self.assertEqual(a["premium_usd"], 90000)
                self.assertEqual((a["first_recv"], a["last_recv"]), ("t1", "t3"))
            finally:
                os.chdir(cwd)

    def test_no_key_no_start(self):
        self.assertIsNone(flow_stream.start_background(SystemConfig()))


if __name__ == "__main__":
    unittest.main()
