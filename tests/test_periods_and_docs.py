"""Rules a reader would otherwise catch by eye. Run from the repo root:
    python3 -m unittest discover tests

- A weekly series holds whole calendar weeks: it starts on a Monday and stops before this Monday, so neither
  end is a cut-short week that looks like a drop. A board's "N days" window stops before today.
- A unit cost never divides by a bare count: on a quiet account the count can be 0, and the warehouse's ANSI
  mode then fails the whole query instead of showing a dash.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import boards  # noqa: E402
import hunts  # noqa: E402
import names  # noqa: E402

WEEK0 = "date_trunc('WEEK', current_date())"


def weekly_items():
    items = {h["id"]: h["sql"] for h in hunts.HUNTS + hunts.MEASURES}
    items.update({f"{b['id']}/{p['id']}": p["sql"] for b in boards.BOARDS for p in b["panels"]})
    # The name lookups read eight weeks of job names, which is not a series, so they are left out.
    return {k: v.replace(names._BILLED_JOBS, "") for k, v in items.items() if "date_trunc('WEEK'" in v}


class WholeWeeks(unittest.TestCase):
    def test_no_weekly_series_counts_weeks_back_from_today(self):
        for item, sql in weekly_items().items():
            self.assertNotRegex(sql, r"current_date\(\)\s*-\s*INTERVAL\s+\d+\s+WEEKS", item)

    def test_every_weekly_window_stops_before_this_monday(self):
        for item, sql in weekly_items().items():
            if re.search(re.escape(WEEK0) + r"\s*-\s*INTERVAL\s+\d+\s+WEEKS", sql):
                self.assertRegex(sql, r"<\s*" + re.escape(WEEK0), item)


class FullDays(unittest.TestCase):
    def test_every_board_day_window_stops_before_today(self):
        for b in boards.BOARDS:
            for p in b["panels"]:
                # full_days' look-back finds today's ingestion day; it is a search, not a window
                sql = re.sub(r"latest AS \(.*?\n\),", "", p["sql"], flags=re.S)
                for m in re.finditer(r"([\w.]+)\s*>=\s*current_date\(\)\s*-\s*INTERVAL\s+\d+\s+DAYS", sql):
                    self.assertRegex(sql, re.escape(m.group(1)) + r"\s*<\s*current_date\(\)", f"{b['id']}/{p['id']}")


class ZeroDenominators(unittest.TestCase):
    def test_every_unit_cost_guards_its_denominator(self):
        for k in boards.ECON_KPIS:
            for expr in (k.value, k.coverage):
                for den in re.findall(r"/\s*([^,)]+)", expr):
                    if re.fullmatch(r"\s*\d+(\.\d+)?\s*", den):
                        continue
                    self.assertTrue(den.strip().startswith("nullif(") or "CASE WHEN" in expr,
                                    f"{k.metric}: / {den.strip()}")


if __name__ == "__main__":
    unittest.main()
