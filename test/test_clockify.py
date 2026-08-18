#!/usr/bin/env python3
"""Offline tests for clockify.py's pure logic.

    python3 -m unittest discover -s test

No network and no API key: everything here works on synthetic entries, which is
exactly where the date arithmetic and the task grouping tend to go wrong.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clockify  # noqa: E402


def entry(start, end, description="", project_id="p1", project_name="Project"):
    return {
        "id": "e-" + start,
        "description": description,
        "timeInterval": {"start": start, "end": end},
        "projectId": project_id,
        "project": {"id": project_id, "name": project_name, "color": "#fff", "clientName": ""},
    }


class TimeHelpers(unittest.TestCase):
    def test_parse_stamp_accepts_zulu_and_fractions(self):
        self.assertEqual(
            clockify.parse_stamp("2026-08-17T11:21:10Z"),
            datetime(2026, 8, 17, 11, 21, 10, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(clockify.parse_stamp("2026-08-17T11:21:10.482Z"))
        self.assertIsNone(clockify.parse_stamp(""))
        self.assertIsNone(clockify.parse_stamp("not a date"))

    def test_week_start_lands_on_the_configured_first_day(self):
        # 2026-08-19 is a Wednesday.
        wednesday = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        monday_week = clockify.week_start(wednesday, "monday")
        sunday_week = clockify.week_start(wednesday, "sunday")
        self.assertEqual(monday_week.weekday(), 0)
        self.assertEqual(sunday_week.weekday(), 6)
        self.assertEqual((monday_week - sunday_week).days, 1)
        self.assertEqual((monday_week.hour, monday_week.minute), (0, 0))

    def test_week_start_on_the_first_day_is_that_midnight(self):
        monday = datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc)
        start = clockify.week_start(monday, "monday")
        self.assertEqual(start.date(), clockify.local_midnight(monday).date())

    def test_utc_stamps_are_comparable_as_strings(self):
        # Totals are computed by comparing stamps lexicographically, which only
        # holds while every stamp is fixed-width UTC.
        earlier = clockify.utc_stamp(datetime(2026, 8, 16, 21, 0, tzinfo=timezone.utc))
        later = clockify.utc_stamp(datetime(2026, 8, 17, 6, 0, tzinfo=timezone.utc))
        self.assertLess(earlier, later)
        self.assertEqual(earlier, "2026-08-16T21:00:00Z")


class EntryView(unittest.TestCase):
    def test_closed_entry_reports_its_duration(self):
        view = clockify.entry_view(
            entry("2026-08-17T09:00:00Z", "2026-08-17T10:30:00Z", "Writing docs"), {}
        )
        self.assertEqual(view["seconds"], 5400)
        self.assertFalse(view["running"])
        self.assertEqual(view["description"], "Writing docs")
        self.assertEqual(view["projectName"], "Project")

    def test_running_entry_has_no_duration_of_its_own(self):
        view = clockify.entry_view(entry("2026-08-17T09:00:00Z", None), {})
        self.assertTrue(view["running"])
        self.assertEqual(view["seconds"], 0)

    def test_project_name_falls_back_to_the_projects_list(self):
        raw = entry("2026-08-17T09:00:00Z", "2026-08-17T09:10:00Z")
        del raw["project"]
        view = clockify.entry_view(raw, {"p1": {"name": "From list", "color": "", "clientName": "ACME"}})
        self.assertEqual(view["projectName"], "From list")
        self.assertEqual(view["clientName"], "ACME")


class Payloads(unittest.TestCase):
    def test_base_payload_carries_every_key_the_panel_binds_to(self):
        payload = clockify.base_payload()
        for key in ("ok", "configured", "error", "note", "running", "todaySeconds",
                    "weekSeconds", "projects", "projectsLoaded", "fetchedAt",
                    "defaultProjectId", "weekStart", "userName", "workspaceName"):
            self.assertIn(key, payload)

    def test_unconfigured_keeps_preferences_and_stays_ok(self):
        payload = clockify.unconfigured({"weekStart": "sunday", "defaultProjectId": "p9"})
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["weekStart"], "sunday")
        self.assertEqual(payload["defaultProjectId"], "p9")

    def test_status_message_explains_the_common_refusals(self):
        self.assertIn("API key", clockify.status_message(401, ""))
        self.assertIn("rate limit", clockify.status_message(429, ""))
        self.assertEqual(clockify.status_message(404, '{"message": "not found"}'), "not found")
        self.assertIn("HTTP 500", clockify.status_message(500, ""))


if __name__ == "__main__":
    unittest.main()
