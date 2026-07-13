from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from code_reviewer.config import _report_work_week_end, default_report_output_dir


class ReportWorkCalendarTests(unittest.TestCase):
    def _calendar(self, holidays: list[str], workdays: list[str]):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "calendar.json"
        path.write_text(json.dumps({"holidays": holidays, "workdays": workdays}), encoding="utf-8")
        return temp, patch.dict(os.environ, {"CHINA_WORK_CALENDAR_FILE": str(path)}, clear=False)

    def test_normal_week_ends_on_friday(self) -> None:
        temp, environment = self._calendar([], [])
        with temp, environment:
            self.assertEqual(_report_work_week_end(date(2026, 7, 13)), date(2026, 7, 17))

    def test_adjusted_saturday_is_the_work_week_end(self) -> None:
        temp, environment = self._calendar([], ["2026-07-18"])
        with temp, environment:
            self.assertEqual(_report_work_week_end(date(2026, 7, 13)), date(2026, 7, 18))

    def test_adjusted_sunday_is_the_work_week_end(self) -> None:
        temp, environment = self._calendar([], ["2026-07-19"])
        with temp, environment:
            self.assertEqual(_report_work_week_end(date(2026, 7, 13)), date(2026, 7, 19))

    def test_friday_holiday_moves_week_end_to_thursday(self) -> None:
        temp, environment = self._calendar(["2026-07-17"], [])
        with temp, environment:
            self.assertEqual(_report_work_week_end(date(2026, 7, 13)), date(2026, 7, 16))

    def test_whole_holiday_week_uses_previous_actual_workday(self) -> None:
        holidays = [f"2026-02-{day:02d}" for day in range(16, 23)]
        temp, environment = self._calendar(holidays, ["2026-02-14"])
        with temp, environment:
            self.assertEqual(_report_work_week_end(date(2026, 2, 18)), date(2026, 2, 14))

    def test_default_directory_uses_calendar_week_end(self) -> None:
        temp, environment = self._calendar([], ["2026-07-18"])
        with temp, environment, tempfile.TemporaryDirectory() as output:
            with patch.dict(os.environ, {"REPORT_OUTPUT_BASE_DIR": output}, clear=False):
                path = default_report_output_dir(date(2026, 7, 13))
        self.assertEqual(path.name, "e-channel-sprint20260718")

    def test_bundled_2026_national_day_adjustment_ends_on_saturday(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CHINA_WORK_CALENDAR_FILE", None)
            self.assertEqual(_report_work_week_end(date(2026, 10, 5)), date(2026, 10, 10))


if __name__ == "__main__":
    unittest.main()
